import datetime
from typing import Any, Dict, Optional

from core.models.base.execution import (
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    OrderTicket,
)
from core.models.base.order import BaseOrder
from core.utils import FuturesOCType, LiveOrderStatus, PositionType

"""台期貨的委託單、部位快照與保證金帳務快照"""


class FuturesOrderTicket(OrderTicket):
    """
    期貨委託單

    `octype`（開平倉別）記的是實際送出的值。它由 `Action` 與當時的持倉推導，
    **不交給券商的 `Auto` 猜**——`Auto` 在同時有多空部位或換月時的行為不透明，
    而換月正是「平舊月＋開新月」兩張單同時在場上的時候。
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
        product: str = "",
        expiry: str = "",
        octype: Optional[FuturesOCType] = None,
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

        # Contract Info（拆開存而不只存合併後的 symbol：換月時要能比對月份）
        self.product: str = product  # 商品代碼（Ex: TX）
        self.expiry: str = expiry  # 到期月份（Ex: 202601）

        # Execution Info（送出時的實際值）
        self.octype: Optional[FuturesOCType] = octype


class FuturesPositionSnapshot(BrokerPositionSnapshot):
    """
    期貨部位快照

    比骨架多拆出 `product`／`expiry`：換月期間同一商品會同時有兩個月份的部位，
    只看 symbol 的話，「還沒平掉的舊月」與「已經開好的新月」會被當成同一件事。
    """

    def __init__(
        self,
        symbol: str = "",
        direction: PositionType = PositionType.LONG,
        volume: int = 0,
        avg_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
        product: str = "",
        expiry: str = "",
    ) -> None:
        super().__init__(
            symbol=symbol,
            direction=direction,
            volume=volume,
            avg_price=avg_price,
            unrealized_pnl=unrealized_pnl,
            raw=raw,
        )

        self.product: str = product
        self.expiry: str = expiry


class FuturesAccountSnapshot(BrokerAccountSnapshot):
    """
    期貨帳務快照

    期貨的「可用資金」語意和股票不同：能不能再開一口看的是**可用保證金**，
    不是帳戶餘額。故除了骨架的餘額與權益，另外記保證金三項。
    """

    def __init__(
        self,
        ts: Optional[datetime.datetime] = None,
        available_balance: float = 0.0,
        total_equity: float = 0.0,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
        initial_margin: float = 0.0,
        maintenance_margin: float = 0.0,
        available_margin: float = 0.0,
    ) -> None:
        super().__init__(
            ts=ts,
            available_balance=available_balance,
            total_equity=total_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            raw=raw,
        )

        self.initial_margin: float = initial_margin  # 原始保證金（已佔用）
        self.maintenance_margin: float = maintenance_margin  # 維持保證金
        self.available_margin: float = available_margin  # 可用保證金（送單前要檢查）
