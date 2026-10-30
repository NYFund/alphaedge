import datetime
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.execution.base import mark_execution
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.risk.event_log import RiskEventLogger
from core.market.segment import SegmentWindow
from core.models import BaseOrder, PendingAction
from core.utils import ExecutionStyle, ExecutionTiming

"""
PendingActionsRunner：開盤段補平昨日的跨日待辦

待辦由盤後作業（`AfterCloseRunner`）與送單元件（出場單未送出時）寫下，
在**次日開盤段的第一件事**由本元件補平：數量截到目前還持有的量、扣掉本段落
已送出的補平量，送單走與一般委託同一條管線。寫入端與執行端本來就不對稱
（盤後寫、開盤執行），所以執行端獨立成一個元件，而不併進盤後作業。
"""

# 送單管線：`(候選委託, 時窗) → 沒有送出的委託`，由引擎的送單元件提供
DispatchFn = Callable[
    [List[Tuple[StrategyContext, BaseOrder]], Optional[SegmentWindow]],
    List[Tuple[StrategyContext, BaseOrder]],
]


class PendingActionsRunner:
    """執行到期的跨日待辦：截量、組補平單、送出、標記結果"""

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        dao: LiveTradeDAO,
        ledger: PositionAttributionLedger,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        dispatch: DispatchFn,
    ) -> None:
        """
        - Description:
            建立待辦執行者；相依一律由外部注入
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - dao: LiveTradeDAO
                實盤紀錄庫（讀待辦、標記結果、順延）
            - ledger: PositionAttributionLedger
                部位歸屬帳（截量要看目前持有量）
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘
            - dispatch: DispatchFn
                送單管線
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.dao: LiveTradeDAO = dao
        self.ledger: PositionAttributionLedger = ledger
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.dispatch: DispatchFn = dispatch

    def apply(self) -> int:
        """
        - Description:
            執行到期的跨日待辦（次日開盤段的第一件事）

            **數量截到目前還持有的量**：待辦寫下之後部位可能已經變了——遲到的成交、
            人工在券商端平倉、重建後部位已不在。照原數量送出的話，
            多出來的部分不是「多平一點」，是直接把部位做反。持有量為 0 就標 `DONE`
            並記一筆事件，不送單。

            **同一個部位的多筆待辦要扣掉前面已送出的量**：出場單連兩天沒成交時，
            盤後會對「補平單殘量」與「策略出場單殘量」各寫一筆待辦。開盤段 08:30 送單、
            09:00 才撮合，逐筆只看歸屬帳的話兩筆都會截到「持有 N」而送出 2N——
            本段落已送出、尚未撮合的補平量也是在途的出場，第二筆要先扣掉它。

            **送單走 `dispatch()`**：與一般委託同一條管線（資金保留、事前風控、
            送出後消化回報），不另外直接呼叫 OMS。

            成功送出才標 `DONE`；送不出去就**留在 `PENDING` 並把到期日滾到次日**，
            同時再推播一次——一張補不成的平倉單不會因為換了一天就變得不重要。
        - Return:
            - int
                實際送出的補平單數
        """

        today: datetime.date = self.clock.now().date()
        pending: List[PendingAction] = self._load_pending_actions(today)
        if not pending:
            return 0

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        submitted: int = 0
        # 本段落已送出的補平量，以（策略, 標的, 方向）累計
        in_flight: Dict[Tuple[str, str, str], int] = {}

        for action in pending:
            context: Optional[StrategyContext] = by_name.get(action.strategy_name)
            if context is None:
                logger.warning(
                    f"待辦 {action.action_id} 的策略未在本次啟動的清單中，本日略過"
                )
                continue

            key: Tuple[str, str, str] = (
                context.name,
                action.symbol,
                action.position_type or "",
            )
            volume: Optional[int] = self._pending_volume_to_cover(
                context, action, in_flight.get(key, 0)
            )
            if volume is None:
                continue

            order: Optional[BaseOrder] = self._build_cover_order(
                context, replace(action, volume=volume)
            )
            if order is None:
                continue
            # 補平是系統產生的出場單，一律要成交；只在開盤段執行
            mark_execution([order], ExecutionTiming.AT_OPEN, ExecutionStyle.MARKET)

            if self.dispatch([(context, order)], None):
                logger.error(f"補平單未送出，待辦保留：{action.action_id}")
                self.dao.postpone_pending_action(
                    action.action_id, today + datetime.timedelta(days=1)
                )
                notify_safely(
                    self.notifier,
                    NotifyLevel.CRITICAL,
                    "補平單送出失敗",
                    f"{action.symbol} 殘量 {volume} 仍未平掉",
                )
                continue

            self.dao.resolve_pending_action(
                action.action_id, self.dao.ACTION_DONE, self.clock.now()
            )
            in_flight[key] = in_flight.get(key, 0) + volume
            submitted += 1

        return submitted

    def _load_pending_actions(self, today: datetime.date) -> List[PendingAction]:
        """
        讀出到期待辦並轉成領域物件

        **一列轉不出來只丟那一列**：這是開盤段的第一件事，而每一筆待辦都是
        一個未平掉的隔夜部位。讓例外往上拋的話，一列壞資料會讓**所有**待辦
        都補不成，而那正是最需要它們動起來的時候。
        """

        actions: List[PendingAction] = []
        for row in self.dao.get_pending_actions(today):
            try:
                actions.append(PendingAction.from_row(row))
            except (KeyError, TypeError, ValueError) as exc:
                logger.error(
                    f"待辦 {row.get('action_id', '?')} 的欄位無法解讀（{exc}），"
                    "本筆略過，需人工處理"
                )
        return actions

    def _pending_volume_to_cover(
        self, context: StrategyContext, action: PendingAction, in_flight: int = 0
    ) -> Optional[int]:
        """
        - Description:
            待辦實際要補平的數量：待辦數量與「歸屬帳持有量 − 本段落已送出的補平量」取小

            持有量為 0 時把待辦標成 `DONE` 並寫事件；數量被截短時也寫事件——
            兩者都代表「寫待辦之後部位變了」或「前一筆待辦已經在平它」，
            事後要查得到是怎麼截的。待辦沒有部位方向時無從比對，留給人工。
        - Parameters:
            - context: StrategyContext
                待辦所屬的策略
            - action: PendingAction
                待辦內容
            - in_flight: int
                本段落已對同一個（標的, 方向）送出、尚未撮合的補平量
        - Return:
            - Optional[int]
                要送出的數量；None 表示本筆不送
        """

        action_id: str = action.action_id
        symbol: str = action.symbol
        wanted: int = action.volume
        direction: str = action.position_type or ""
        if not direction:
            logger.warning(f"待辦 {action_id} 沒有部位方向，無法比對持有量，需人工處理")
            return None

        held: int = self.ledger.get_strategy_positions(context.name).get(
            (symbol, direction), 0
        )
        available: int = max(held - in_flight, 0)
        volume: int = min(wanted, available)
        if volume == wanted:
            return volume

        message: str = (
            f"待辦 {action_id} 要補平 {symbol} {direction} {wanted}，"
            f"歸屬帳目前持有 {held}"
            + (f"、本段落已送出補平 {in_flight}" if in_flight else "")
            + "，"
            + ("已無可平部位，不送單" if volume <= 0 else f"改送 {volume}")
        )
        logger.warning(message)
        self.events.write(
            category="PENDING_ACTION_TRUNCATED",
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=symbol,
        )
        if volume <= 0:
            self.dao.resolve_pending_action(
                action_id, self.dao.ACTION_DONE, self.clock.now()
            )
            return None
        return volume

    def _build_cover_order(
        self, context: StrategyContext, action: PendingAction
    ) -> Optional[BaseOrder]:
        """
        - Description:
            由待辦組出補平單：策略有實作 `build_cover_order()` 就用策略的，否則由引擎組

            **引擎預設組**：補平是系統產生的出場單，價格類型由執行層依 `MARKET` 換算，
            訂單型別走依市場注入的 `context.build_filled_order`（與當沖回補同一個），
            引擎本體因此不必知道任何市場特性。只靠策略組的話，沒實作的策略
            出場殘量會一路留到人工處理——「出場必補」等於沒有執行端。

            決策價取**合約參考價**：開盤段還沒有成交，參考價是唯一的公告價格。
            取不到就不送，待辦留在 `PENDING`，並推播讓人知道。

            **傳給策略的是 `PendingAction` 而不是資料列**：這個參數會落到策略作者手上，
            傳 dict 等於把紀錄庫的 schema 變成策略層的公開契約——
            改一個欄位名就會無聲地弄壞每一支策略。
        - Parameters:
            - context: StrategyContext
                待辦所屬的策略
            - action: PendingAction
                待辦（數量已截到目前持有量，`position_type` 已確認有值）
        - Return:
            - Optional[BaseOrder]
                補平單；組不出來時為 None
        """

        builder: Optional[Callable[..., BaseOrder]] = getattr(
            context.strategy, "build_cover_order", None
        )
        if builder is not None:
            try:
                return builder(action)
            except Exception as exc:
                logger.opt(exception=True).error(f"組補平單失敗：{exc}")
                return None

        reason: Optional[str] = None
        reference: Optional[float] = None
        if context.build_filled_order is None or action.position_type is None:
            reason = "沒有可用的訂單建構器或部位方向"
        else:
            reference = context.data_feed.get_reference_price(action.symbol)
            if not reference:
                reason = "取不到合約參考價"

        if reason is not None or reference is None:
            message: str = (
                f"待辦 {action.action_id}（{action.symbol}）無法組出補平單：{reason}，"
                "待辦保留，需人工確認"
            )
            logger.error(message)
            notify_safely(
                self.notifier, NotifyLevel.CRITICAL, "補平單無法組出", message
            )
            return None

        return context.build_filled_order(
            action.symbol,
            self.clock.now(),
            action.action,
            action.position_type,
            float(reference),
            int(action.volume),
        )
