import datetime
from typing import Any, Dict, Optional

from core.models.base.execution import BrokerPositionSnapshot, OrderTicket
from core.models.base.order import BaseOrder
from core.utils import (
    LiveOrderStatus,
    PositionType,
    StockOrderCond,
    StockOrderLot,
)

"""台股的委託單與部位快照：補上委託條件（現股／融資／融券／借券）與下單單位"""


class StockOrderTicket(OrderTicket):
    """
    台股委託單

    補的兩個欄位記的是**實際送給券商的值**，不是策略的意圖：
    `order_cond` 由送單前的轉換依持倉方向 ＋ 放空管道 ＋ 是否當沖推導，
    策略從頭到尾看不到它。把推導結果留在委託上，出事時才回答得了
    「那張單到底是用現股還是融券送出去的」——而這正是成本對不上時的第一個問題。
    """

    def __init__(
        self,
        client_order_id: str = "",
        strategy_name: str = "",
        order: Optional[BaseOrder] = None,
        status: LiveOrderStatus = LiveOrderStatus.PENDING_SUBMIT,
        broker_order_id: Optional[str] = None,
        broker_seqno: Optional[str] = None,
        filled_volume: int = 0,
        avg_fill_price: float = 0.0,
        created_at: Optional[datetime.datetime] = None,
        updated_at: Optional[datetime.datetime] = None,
        reject_reason: Optional[str] = None,
        dry_run: bool = False,
        order_cond: Optional[StockOrderCond] = None,
        order_lot: StockOrderLot = StockOrderLot.Common,
        day_trade_short: bool = False,
    ) -> None:
        super().__init__(
            client_order_id=client_order_id,
            strategy_name=strategy_name,
            order=order,
            status=status,
            broker_order_id=broker_order_id,
            broker_seqno=broker_seqno,
            filled_volume=filled_volume,
            avg_fill_price=avg_fill_price,
            created_at=created_at,
            updated_at=updated_at,
            reject_reason=reject_reason,
            dry_run=dry_run,
        )

        # Stock Execution Info（送出時的實際值）
        self.order_cond: Optional[StockOrderCond] = order_cond  # 委託條件
        self.order_lot: StockOrderLot = order_lot  # 下單單位（整股／零股）

        # 現股當沖的「先賣」旗標。它與 `order_cond=Cash` 併用，**兩者缺一不可**：
        # 少了它，先賣的現股單會被當成賣出持股而退單（因為根本沒有庫存）
        self.day_trade_short: bool = day_trade_short


class StockPositionSnapshot(BrokerPositionSnapshot):
    """
    台股部位快照

    比骨架多一個 `order_cond`：**對帳要連融資券別一起比**。
    同一檔股票的現股多單與融券空單在券商端是兩筆不同的部位，
    只比對「代號 ＋ 方向 ＋ 數量」的話，兩者互換時數字會剛好對得上。
    """

    def __init__(
        self,
        symbol: str = "",
        direction: PositionType = PositionType.LONG,
        volume: int = 0,
        avg_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
        order_cond: Optional[StockOrderCond] = None,
    ) -> None:
        super().__init__(
            symbol=symbol,
            direction=direction,
            volume=volume,
            avg_price=avg_price,
            unrealized_pnl=unrealized_pnl,
            raw=raw,
        )

        self.order_cond: Optional[StockOrderCond] = order_cond
