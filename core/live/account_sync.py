import datetime
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution.order_preprocess import resolve_close_action
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.managers.base.position_manager import BasePositionManager
from core.models import (
    BaseOrder,
    BrokerPositionSnapshot,
    ExecutionReport,
    StockOrder,
)
from core.utils import Action, PositionType

"""
帳戶同步：成交回報 → 本地帳戶與歸屬帳

**策略讀到的 `self.account` 要和回測長得一模一樣**，否則同一支策略在兩邊會看到
不同結構，「策略層不分家」就不成立了。故這裡不自己記帳，而是呼叫**該策略自己那份**
`PositionManager`——與回測用的是同一個類別、同一份成本模型。

兩條路徑刻意分成兩個函式，**不共用一個帶旗標的「同步」函式**：

| 函式 | 時機 | 以誰為準 |
|------|------|----------|
| `rebuild_from_broker()` | 啟動時 | 券商（本地還沒有任何狀態，只能以它為準） |
| `apply_fill()` | 運行中 | 回報（本地狀態由回報推導，差異本身就是訊號） |

共用一個旗標的話，那個旗標遲早會在錯的時機被打開，而「運行中以券商覆寫本地」
會把真正的 bug（例如回報漏接）蓋掉——蓋掉之後明天還會再發生一次。
"""


class AccountSynchronizer:
    """
    - Description:
        把成交回報套進各策略的本地帳戶與歸屬帳

        多策略下每支策略各有一份 `Account` 與 `PositionManager`；
        帳戶層的合計由它們加總得出。
    """

    def __init__(
        self,
        position_managers: Dict[str, BasePositionManager],
        ledger: PositionAttributionLedger,
        dao: LiveTradeDAO,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立同步器
        - Parameters:
            - position_managers: Dict[str, BasePositionManager]
                `{策略名: 該策略自己的 PositionManager}`
            - ledger: PositionAttributionLedger
                部位歸屬帳
            - dao: LiveTradeDAO
                實盤紀錄庫（用來由委託反查策略）
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.position_managers: Dict[str, BasePositionManager] = position_managers
        self.ledger: PositionAttributionLedger = ledger
        self.dao: LiveTradeDAO = dao
        self._now: Callable[[], datetime.datetime] = now_provider

    # === 運行中：以回報為準 ===
    def apply_fill(
        self, report: ExecutionReport, strategy_name: Optional[str] = None
    ) -> Optional[str]:
        """
        - Description:
            把一筆成交套進對應策略的帳戶與歸屬帳

            歸屬鏈：成交的 `broker_seqno` → `live_order` → `strategy_name`。
            查不到策略時**不猜**，記 warning 並跳過帳戶更新——那筆成交仍在
            `live_fill` 裡，由對帳抓出差異。猜一支策略會讓它的已實現損益直接錯掉。
        - Parameters:
            - report: ExecutionReport
                成交回報
            - strategy_name: Optional[str]
                已知的歸屬策略；None 時由委託反查
        - Return:
            - Optional[str]
                實際歸屬到的策略；無法歸屬時為 None
        """

        name: Optional[str] = strategy_name or self._resolve_strategy(report)
        if name is None or name not in self.position_managers:
            logger.warning(
                f"成交 {report.symbol} {report.volume} 無法歸屬到任何策略"
                f"（seqno={report.broker_seqno}），本地帳戶不更新；"
                "差異將由對帳抓出"
            )
            return None

        manager: BasePositionManager = self.position_managers[name]
        direction: PositionType = self._resolve_direction(name, report)
        order: BaseOrder = self._to_order(report, direction)

        if self._is_closing(order):
            manager.close_position(order)
            self.ledger.close_lots(name, report.symbol, report.volume, direction)
        else:
            manager.open_position(order)
            self.ledger.open_lot(name, report, direction)

        return name

    def _resolve_strategy(self, report: ExecutionReport) -> Optional[str]:
        """由委託序號反查策略；查不到回 None"""

        rows: List[Tuple[object, ...]] = self.dao.conn.execute(
            "SELECT strategy_name FROM live_order WHERE broker_seqno = ? LIMIT 1",
            (report.broker_seqno,),
        ).fetchall()
        return str(rows[0][0]) if rows else None

    def _resolve_direction(
        self, strategy_name: str, report: ExecutionReport
    ) -> PositionType:
        """
        推導這筆成交動到的是多單還是空單

        **先看歸屬帳裡有沒有同標的的反向部位**：回補空單與買進開多都是 BUY，
        只看買賣別會把回補記成新開一筆多單，於是帳上憑空多出一個部位、
        而空單永遠平不掉。
        """

        positions: Dict[Tuple[str, str], int] = self.ledger.get_strategy_positions(
            strategy_name
        )
        if report.action is Action.BUY:
            if positions.get((report.symbol, PositionType.SHORT.value), 0) > 0:
                return PositionType.SHORT
            return PositionType.LONG

        if positions.get((report.symbol, PositionType.LONG.value), 0) > 0:
            return PositionType.LONG
        return PositionType.SHORT

    @staticmethod
    def _to_order(report: ExecutionReport, direction: PositionType) -> BaseOrder:
        """
        把成交回報組回一張「已成交的訂單」餵給 `PositionManager`

        **成本在這裡只有估算值**：成交回報不帶手續費與稅，盤中先用 cost model 估，
        盤後再以券商的損益明細回填實際值並記錄差額——那個差額是校正成本設定的
        唯一依據。
        """

        return StockOrder(
            stock_id=report.symbol,
            date=report.ts,
            action=report.action,
            position_type=direction,
            price=report.price,
            volume=report.volume,
        )

    @staticmethod
    def _is_closing(order: BaseOrder) -> bool:
        """是否為平倉單；判定委派給共用的動作推導"""

        return order.action is resolve_close_action(order.position_type)

    # === 啟動時：以券商為準 ===
    def rebuild_from_broker(
        self,
        positions: List[BrokerPositionSnapshot],
        balances: Optional[Dict[str, float]] = None,
    ) -> Dict[str, int]:
        """
        - Description:
            啟動時重建本地帳戶

            **多策略的重建走 `live_position_lot`，不是把券商部位平均分配**：
            券商給的是合併部位，分不回策略。以 lot 表重建各策略帳後，
            再把券商多出來的部分收進 `__unattributed__`（只允許平倉）。

            **本函式只在啟動時呼叫。** 運行中的差異由 `Reconciler` 處理，
            而它**不自動修正本地部位**——自動修正會把回報漏接這類 bug 蓋掉。

            **重建期間停用餘額檢查。** `PositionManager.open_position()` 會檢查
            「餘額夠不夠買」——那在交易時是對的，在重建時是錯的：帳戶的餘額
            **已經**被這些部位佔住了，再檢查一次必然不足，部位會被靜默丟掉
            （只留一行 warning），然後對帳每天都報「本地兩份紀錄不一致」。
            重建不是交易，是還原狀態。
        - Parameters:
            - positions: List[BrokerPositionSnapshot]
                券商端部位
            - balances: Optional[Dict[str, float]]
                重建後各策略的餘額（由券商帳務與額度分配決定）；
                未提供時沿用帳戶原本的餘額
        - Return:
            - Dict[str, int]
                `{策略: 重建出的部位筆數}`，含 `__unattributed__`
        """

        self.ledger.adopt_broker_positions(positions)

        rebuilt: Dict[str, int] = {}
        for name in self.ledger.get_strategy_names():
            lots: List[Dict[str, object]] = self.dao.get_open_lots(strategy_name=name)
            rebuilt[name] = len(lots)

            manager: Optional[BasePositionManager] = self.position_managers.get(name)
            if manager is None:
                if name != UNATTRIBUTED_STRATEGY:
                    logger.warning(
                        f"歸屬帳裡有策略 {name} 的部位，但本次啟動沒有載入它；"
                        "那些部位將無人管理，請確認策略清單"
                    )
                continue

            self._restore_positions(manager, lots, (balances or {}).get(name))

        logger.info(f"由歸屬帳重建本地部位：{rebuilt}")
        return rebuilt

    @staticmethod
    def _restore_positions(
        manager: BasePositionManager,
        lots: List[Dict[str, object]],
        balance: Optional[float],
    ) -> None:
        """
        把 lot 還原成部位，**期間停用餘額檢查**

        作法是重建前把餘額設成無限大、重建後再設成真實值。繞過檢查而不是
        另寫一套記帳：部位的成本、手續費與稅都由 `PositionManager` 算，
        另寫一份必然與回測漂移。
        """

        original: float = manager.account.balance
        manager.account.balance = float("inf")
        try:
            for lot in lots:
                manager.open_position(AccountSynchronizer._lot_to_order(lot))
        finally:
            manager.account.balance = original if balance is None else balance

    @staticmethod
    def _lot_to_order(lot: Dict[str, object]) -> BaseOrder:
        """
        把一筆 lot 還原成開倉訂單

        **原始開倉日取自 lot 表**。推不出來時會落到啟動日，那會讓持有天數
        與當沖判定都算錯，故 lot 表本身就要記住它。
        """

        open_date: object = lot["open_date"]
        parsed: datetime.date = (
            open_date
            if isinstance(open_date, datetime.date)
            else datetime.date.fromisoformat(str(open_date))
        )
        direction: PositionType = PositionType(str(lot["direction"]))

        return StockOrder(
            stock_id=str(lot["symbol"]),
            date=parsed,
            action=Action.BUY if direction is PositionType.LONG else Action.SELL,
            position_type=direction,
            price=float(lot["open_price"]),
            volume=int(lot["volume"]),
        )
