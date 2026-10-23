import datetime

from core.models.base.record import BaseTradeRecord
from core.utils import PositionType

"""FuturesTradeRecord: 單筆期貨交易紀錄（含逐日盯市的已結算損益）"""


class FuturesTradeRecord(BaseTradeRecord):
    """
    單筆期貨交易紀錄

    `realized_pnl` 是**整筆交易的總損益**，涵蓋逐日盯市各段：
    `settled_pnl`（平倉前各交易日結算的累計）＋ 最後一段（結算價 → 平倉價）− 交易成本。
    直接用 `exit_price − entry_price` 去驗證會少掉前面各段。

    欄位語意其餘部分與 `BaseTradeRecord` 相同（buy_* / sell_* 對應操作動作，
    entry_* / exit_* 為方向中立欄位，報表時間軸一律用 exit_date）。
    `roi_on_capital` 與 `roi` 相同：期貨報酬率的分母本來就是保證金。
    """

    def __init__(
        self,
        id: int = 0,
        product: str = "",
        expiry: str = "",
        is_closed: bool = False,
        position_type: PositionType = PositionType.LONG,
        buy_date: datetime.date = None,
        buy_price: float = 0.0,
        buy_volume: int = 0,
        sell_date: datetime.date = None,
        sell_price: float = 0.0,
        sell_volume: int = 0,
        commission: float = 0.0,
        tax: float = 0.0,
        transaction_cost: float = 0.0,
        realized_pnl: float = 0.0,
        roi: float = 0.0,
        multiplier: int = 0,
        margin: float = 0.0,
        settled_pnl: float = 0.0,
        holding_days: int = 0,
    ) -> None:
        super().__init__(
            id=id,
            symbol=f"{product}{expiry}",
            is_closed=is_closed,
            position_type=position_type,
            buy_date=buy_date,
            buy_price=buy_price,
            buy_volume=buy_volume,
            sell_date=sell_date,
            sell_price=sell_price,
            sell_volume=sell_volume,
            commission=commission,
            tax=tax,
            transaction_cost=transaction_cost,
            realized_pnl=realized_pnl,
            roi=roi,
            margin=margin,
            holding_days=holding_days,
        )

        # Contract Info
        self.product: str = product  # 商品代碼（Ex: TX）
        self.expiry: str = expiry  # 到期月份（Ex: 202601）
        self.multiplier: int = multiplier  # 契約乘數（元／點）

        # Margin & Settlement
        self.settled_pnl: float = settled_pnl  # 平倉前逐日盯市已結算的累計損益

    @property
    def contract_id(self) -> str:
        """symbol 的期貨別名：`{product}{expiry}`"""

        return self.symbol
