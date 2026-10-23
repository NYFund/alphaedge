import datetime

from core.models.base.position import BasePosition
from core.utils import PositionType

"""FuturesPosition: 台期貨未平倉部位（含保證金與逐日盯市的已結算損益）"""


class FuturesPosition(BasePosition):
    """
    期貨未平倉部位

    **與股票最大的差異是「逐日盯市」**：期貨每個交易日都會以結算價結清當日損益，
    現金當天就進出保證金專戶，`price` 隨之重設為結算價。因此本部位的
    `price` **不是開倉價**，而是「最近一次結算價」；真正的開倉價保存在
    `entry_price`，兩者在第一次結算之前相同。

    這也意味著平倉時的 `price − entry_price` 只是**最後一段**的損益，
    先前各段已經記在 `settled_pnl`。整筆交易的損益 =
    `settled_pnl` ＋ 最後一段，見 `FuturesPositionManager.close_single_position()`。
    """

    def __init__(
        self,
        id: int = 0,
        product: str = "",
        expiry: str = "",
        is_closed: bool = False,
        position_type: PositionType = PositionType.LONG,
        date: datetime.date = None,
        price: float = 0.0,
        volume: int = 0,  # Unit: Contract（口）
        commission: float = 0.0,
        tax: float = 0.0,
        transaction_cost: float = 0.0,
        unrealized_pnl: float = 0.0,
        unrealized_roi: float = 0.0,
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
            date=date,
            price=price,
            volume=volume,
            commission=commission,
            tax=tax,
            transaction_cost=transaction_cost,
            unrealized_pnl=unrealized_pnl,
            unrealized_roi=unrealized_roi,
            margin=margin,
            holding_days=holding_days,
        )

        # Contract Info
        self.product: str = product  # 商品代碼（Ex: TX）
        self.expiry: str = expiry  # 到期月份（Ex: 202601）
        self.multiplier: int = multiplier  # 契約乘數（元／點）

        # Margin & Settlement
        self.settled_pnl: float = settled_pnl  # 逐日盯市已結算的累計損益

    @property
    def contract_id(self) -> str:
        """symbol 的期貨別名：`{product}{expiry}`"""

        return self.symbol
