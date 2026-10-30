from typing import Any, Callable, List, Optional, Sequence, Tuple

from loguru import logger

from core.broker.base import BROKER_SESSION_DOWN, BaseBroker
from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.reconciler import Reconciler
from core.live.risk import event_categories
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingMode

"""
ConnectionSupervisor：券商連線的降級、重連與重連後的恢復

偵測跑在券商的執行緒上、只入列；本元件在主執行緒取出並決定要不要降級，
因為降級要寫紀錄庫。重連後的恢復流程（重新訂閱 → 接管委託 → 對帳）順序固定，
三件事都做完才可以恢復送單。
"""


class ConnectionSupervisor:
    """券商連線守門：套用降級事件、確認連線、重連後恢復"""

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        broker: BaseBroker,
        order_manager: Any,
        risk_manager: PreTradeRiskManager,
        reconciler: Reconciler,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        on_reconciled: Callable[[Any], None],
    ) -> None:
        """
        - Description:
            建立連線守門；相依一律由外部注入
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡（重新訂閱要用標的池）
            - broker: BaseBroker
                券商閘道
            - order_manager: Any
                委託管理（重連後接管委託）
            - risk_manager: PreTradeRiskManager
                事前風控（降級的唯一入口）
            - reconciler: Reconciler
                對帳器
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘
            - on_reconciled: Callable[[Any], None]
                重連後的對帳結果交給誰保存：退出碼由最後一次對帳結果決定，
                而那個結果屬於引擎本體，本元件不持有它
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.broker: BaseBroker = broker
        self.order_manager: Any = order_manager
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.reconciler: Reconciler = reconciler
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.on_reconciled: Callable[[Any], None] = on_reconciled

    def apply_broker_degrades(self) -> None:
        """
        - Description:
            取出券商閘道回報的降級事件，依類別決定要不要降級

            偵測跑在券商的執行緒上，只入列；這裡在主執行緒處理，因為降級要寫紀錄庫。

            - **連線中斷**：不降級，只寫 WARN 事件並推播。短暫斷線不該停掉之後的開倉——
              降級會落地並延續到之後的段落，要人工解除。盤中由心跳重連並走完恢復流程，
              斷線期間若有漏接，重連後的對帳會抓到並由對帳器降級。
            - **重連次數用完、時鐘偏差過大**：帳戶層降為 `REDUCE_ONLY` 並推 CRITICAL。
              前者是今天不會自己恢復的故障；後者讓段落時窗與報價過期判斷都失準。
              **不降 `HALTED`**（與行情中斷同一個理由）：部位還在場上，停損要送得出去。
        """

        drain: Optional[Callable[[], List[Tuple[str, str]]]] = getattr(
            self.broker, "drain_degrade_events", None
        )
        if drain is None:
            return

        for kind, reason in drain():
            if kind == BROKER_SESSION_DOWN:
                self.events.write(
                    category=event_categories.BROKER_SESSION_DOWN,
                    severity=NotifyLevel.WARN,
                    message=reason,
                )
                notify_safely(self.notifier, NotifyLevel.WARN, "券商連線中斷", reason)
                continue

            self.risk_manager.on_degrade_event(reason, TradingMode.REDUCE_ONLY)
            notify_safely(self.notifier, NotifyLevel.CRITICAL, "券商連線降級", reason)

    def ensure_connected(self) -> bool:
        """
        - Description:
            確認連線還在；斷了就重連並走完恢復流程

            **這是 `ShioajiSession.reconnect()` 唯一的呼叫端**。它連同退避與
            每日登入上限都寫好了，但正式路徑上一個呼叫點都沒有——
            斷線之後程式會一路跑到收盤，每一次送單都失敗。
        - Return:
            - bool
                連線是否可用；False 時呼叫端不可再送單
        """

        if self.broker.is_connected():
            return True

        logger.error("偵測到連線中斷，嘗試重連")
        if not self.broker.reconnect():
            # 次數用完時閘道會回報降級事件，當場套用，不等下一次心跳
            self.apply_broker_degrades()
            notify_safely(
                self.notifier,
                NotifyLevel.CRITICAL,
                "重連失敗",
                "已停止送單，請人工確認",
            )
            return False

        self.recover_after_reconnect()
        # 恢復流程含對帳：不一致時對帳器已經降級，走到這裡代表狀態確認過，不降級只留紀錄
        message: str = "券商重連成功，已重新訂閱、接管委託並對帳"
        logger.warning(message)
        self.events.write(
            category=event_categories.BROKER_RECONNECTED,
            severity=NotifyLevel.WARN,
            message=message,
        )
        notify_safely(self.notifier, NotifyLevel.WARN, "券商重連成功", message)
        return True

    def recover_after_reconnect(self) -> None:
        """
        - Description:
            重連成功之後的恢復流程

            **順序固定，而且三件事都做完才可以恢復送單**：
            1. 重新訂閱行情——重連換了一個 session，舊的訂閱一併失效；
               不訂閱的話盤中迴圈收不到任何報價，然後心跳會判成「行情中斷」，
               症狀看起來像券商的問題。
            2. `order_manager.recover()`——斷線期間送出的委託可能已經成交，
               回報卻在斷掉的那條連線上。不接管就會重複下單。
            3. 對帳——前兩步都做完才知道本地到底是什麼狀態。

            **只對帳、不以券商部位重建本地帳**：`rebuild_from_broker()` 是啟動用的，
            它把 lot 逐筆 `open_position()` 回去而不先清空，運行中呼叫一次部位就多一份，
            策略會照兩倍的部位送平倉單。斷線期間漏接的成交由對帳抓出來並降級，
            不在這裡自動補——自動補會把回報漏接這類問題蓋掉。

            **失敗不吞**：恢復沒做完就繼續送單，等於拿一份不知道對不對的部位
            去交易。例外往上拋，由呼叫端決定停或再試。
        """

        logger.warning("重連成功，開始恢復：重新訂閱 → 接管委託 → 對帳")

        symbols: List[str] = sorted(
            {symbol for context in self.contexts for symbol in context.symbols}
        )
        if symbols:
            self.broker.subscribe_quotes(symbols)

        self.order_manager.recover(self.clock.today())

        positions: List[Any] = self.broker.get_positions()
        self.on_reconciled(self.reconciler.check(positions))

        logger.info("恢復完成，可以繼續送單")
