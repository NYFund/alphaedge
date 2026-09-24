import datetime
from typing import Optional

from core.models.base.record import BaseTradeRecord
from core.utils import PositionType, ShortMethod

"""StockTradeRecord: single stock trade event in backtesting (transaction and performance details)"""


class StockTradeRecord(BaseTradeRecord):
    """
    單筆股票交易紀錄

    欄位語意：
    - buy_* / sell_* 以「操作動作」對應：SHORT 的 sell_* 是放空開倉、buy_* 是回補
    - entry_* / exit_* 為方向中立欄位：entry 一律是開倉、exit 一律是平倉
    - ⚠ 報表與統計的時間軸一律使用 exit_date，**不要用 sell_date**
      （SHORT 的 sell_date 是開倉日，用它會把損益畫在開倉那天）
    """

    def __init__(
        self,
        id: int = 0,
        stock_id: str = "",
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
        short_method: Optional[ShortMethod] = None,
        borrow_fee: float = 0.0,
        interest: float = 0.0,
        dividend_compensation: float = 0.0,
        margin: float = 0.0,
        holding_days: int = 0,
        roi_on_capital: float = 0.0,
    ) -> None:
        super().__init__(
            id=id,
            symbol=stock_id,
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
        )

        self.roi_on_capital: float = roi_on_capital  # 資金效率（僅供報表參考）

        # Short Info（LONG 一律維持預設值）
        # 台股的 transaction_cost 口徑 = 交易手續費 + 交易稅 + 借券費 + 股利補償 − 利息
        self.short_method: Optional[ShortMethod] = short_method  # 放空管道
        self.borrow_fee: float = borrow_fee  # 借券費（融券手續費或 SBL 累計費用）
        self.interest: float = interest  # 融券保證金利息收入
        # 持有期間跨越除息日時補償給出借方的現金股利
        self.dividend_compensation: float = dividend_compensation
        self.margin: float = margin  # 開倉時繳交的保證金
        self.holding_days: int = holding_days  # 持有曆日數

    @property
    def stock_id(self) -> str:
        """symbol 的台股別名：既有策略與報表沿用 stock_id 取值，不需改寫"""

        return self.symbol
