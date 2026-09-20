import datetime
from typing import Optional

from core.models.base.order import BaseOrder
from core.utils import (
    Action,
    ExecutionTiming,
    OrderType,
    PositionType,
    ShortMethod,
    StockOrderLot,
    StockPriceType,
)

"""StockOrder: structure for stock orders in backtesting (direction, quantity, price)"""


class StockOrder(BaseOrder):
    """個股買賣的訂單"""

    def __init__(
        self,
        stock_id: str = "",  # 股票代號
        date: datetime.datetime = None,  # 交易日期（Tick會是Timestamp）
        action: Action = Action.BUY,  # 訂單動作（Buy / Sell）
        position_type: PositionType = PositionType.LONG,  # 持倉方向（Long / Short）
        price: float = 0.0,  # 交易價位
        volume: int = 0,  # 交易張數（Unit: Lot）
        short_method: Optional[ShortMethod] = None,  # 放空管道（由引擎補值）
        is_day_trade: bool = False,  # 是否為現股當沖（由引擎補值）
        order_type: OrderType = OrderType.ROD,  # 委託效期
        timing: Optional[ExecutionTiming] = None,  # 實盤執行時點；回測忽略
        client_order_id: Optional[str] = None,  # 本地委託識別碼；由 OMS 填入
        price_type: Optional[StockPriceType] = None,  # 價格類型；None 由前處理決定
        order_lot: StockOrderLot = StockOrderLot.Common,  # 下單單位（整股／零股）
    ) -> None:
        super().__init__(
            symbol=stock_id,
            date=date,
            action=action,
            position_type=position_type,
            price=price,
            volume=volume,
            order_type=order_type,
            timing=timing,
            client_order_id=client_order_id,
        )

        # Short Info（策略不需自行填寫，由 Backtester._enrich_orders 依策略設定補值）
        self.short_method: Optional[ShortMethod] = short_method
        self.is_day_trade: bool = is_day_trade

        # === 實盤執行欄位（回測完全不讀）===
        #
        # 價格類型放子類別而不放 `BaseOrder`：期貨多一個 `MKP`（範圍市價），
        # 放在骨架就得用 `str` 混著裝，於是股票訂單也長出一個它送不出去的值。
        #
        # `None` 表示「由 `order_preprocess` 依策略意圖決定」。需要市價語意時直接
        # 送 `MKT`，不再一律換算成可成交限價
        self.price_type: Optional[StockPriceType] = price_type

        # 下單單位。`Common` 的 `volume` 是張，`IntradayOdd` 是股——
        # 單位換算錯了不會報錯，只會下成 1000 倍或 1/1000 的量
        self.order_lot: StockOrderLot = order_lot

        # **沒有 `order_cond` 欄位**：委託條件（現股／融資／融券／借券）由
        # 送單前的轉換依 `position_type` ＋ `short_method` ＋ `is_day_trade` 推導，
        # 不開放策略填寫。策略能填的話，就會出現「回測走融券成本、實盤送現股」
        # 這種兩邊各自成立、合起來錯的組合

    @property
    def stock_id(self) -> str:
        """symbol 的台股別名：既有策略與報表沿用 stock_id 取值，不需改寫"""

        return self.symbol
