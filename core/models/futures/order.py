import datetime
from typing import Optional

from core.models.base.order import BaseOrder
from core.utils import (
    Action,
    ExecutionTiming,
    FuturesPriceType,
    OrderType,
    PositionType,
)

"""FuturesOrder: 台期貨訂單（數量單位為口）"""


class FuturesOrder(BaseOrder):
    """
    期貨買賣的訂單

    **`volume` 的單位是口**。期貨沒有「張」，也沒有股數換算——PnL 直接由
    價格變動 × 乘數 × 口數決定。
    """

    def __init__(
        self,
        product: str = "",
        expiry: str = "",
        date: datetime.datetime = None,
        action: Action = Action.BUY,
        position_type: PositionType = PositionType.LONG,
        price: float = 0.0,
        volume: int = 0,  # Unit: Contract（口）
        order_type: OrderType = OrderType.ROD,  # 委託效期
        timing: Optional[ExecutionTiming] = None,  # 實盤執行時點；回測忽略
        client_order_id: Optional[str] = None,  # 本地委託識別碼；由 OMS 填入
        price_type: Optional[FuturesPriceType] = None,  # 價格類型；None 由前處理決定
    ) -> None:
        super().__init__(
            symbol=f"{product}{expiry}",
            date=date,
            action=action,
            position_type=position_type,
            price=price,
            volume=volume,
            order_type=order_type,
            timing=timing,
            client_order_id=client_order_id,
        )

        # Contract Info
        self.product: str = product  # 商品代碼（Ex: TX）
        self.expiry: str = expiry  # 到期月份（Ex: 202601）

        # === 實盤執行欄位（回測完全不讀）===
        #
        # `None` 表示由送單前的處理決定。期貨的值域比股票多一個 `MKP`（範圍市價），
        # 故型別是 `FuturesPriceType` 而不是共用一個
        self.price_type: Optional[FuturesPriceType] = price_type

        # **沒有 `octype` 欄位**：開平倉別由 mapper 依 `Action` 與當前持倉推導。
        # 交給券商的 `Auto` 猜是不行的——它在同時有多空部位或換月時的行為不透明，
        # 而換月正是「平舊月＋開新月」兩張單同時在場上的時候

    @property
    def contract_id(self) -> str:
        """symbol 的期貨別名：`{product}{expiry}`"""

        return self.symbol
