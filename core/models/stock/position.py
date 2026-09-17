import datetime
from typing import Optional

from core.models.base.position import BasePosition
from core.utils import PositionType, ShortMethod

"""StockPosition: 個股未平倉部位（含信用交易的保證金與借券費）"""


class StockPosition(BasePosition):
    """
    庫存未平倉倉位資訊

    放空欄位的計算時點：
    - MARGIN 融券手續費：開倉時一次收取，記入 borrow_fee
    - MARGIN 融券利息：平倉時依 exit_date − entry_date 一次算出，不逐日累加
    - SBL 借券費：由 accrue_holding_cost() 逐日計提至 accrued_borrow_fee
    - 股利補償：由結算模型於除息日一次計入 dividend_compensation（放空）
      或 dividend_received（做多）
    """

    def __init__(
        self,
        id: int = 0,
        stock_id: str = "",
        is_closed: bool = False,
        position_type: PositionType = PositionType.LONG,
        date: datetime.date = None,
        price: float = 0.0,
        volume: int = 0,
        commission: float = 0.0,
        tax: float = 0.0,
        transaction_cost: float = 0.0,
        unrealized_pnl: float = 0.0,
        unrealized_roi: float = 0.0,
        short_method: Optional[ShortMethod] = None,
        is_day_trade: bool = False,
        margin: float = 0.0,
        short_proceeds: float = 0.0,
        borrow_fee: float = 0.0,
        accrued_borrow_fee: float = 0.0,
        dividend_compensation: float = 0.0,
        dividend_received: float = 0.0,
        holding_days: int = 0,
        last_accrual_date: Optional[datetime.date] = None,
    ) -> None:
        super().__init__(
            id=id,
            symbol=stock_id,
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
        )

        # Short Info（LONG 部位一律維持預設值，不受影響）
        self.short_method: Optional[ShortMethod] = short_method  # 放空管道
        self.is_day_trade: bool = is_day_trade  # 是否為現股當沖
        self.margin: float = margin  # 已繳保證金
        self.short_proceeds: float = (
            short_proceeds  # 融券賣出擔保價款（不計入可用餘額）
        )
        self.borrow_fee: float = borrow_fee  # 開倉時一次收取的融券手續費
        self.accrued_borrow_fee: float = accrued_borrow_fee  # SBL 逐日計提的借券費
        # 除息日補償給出借方的現金股利（放空專屬支出）
        self.dividend_compensation: float = dividend_compensation
        # 除息日收到的現金股利（做多專屬收入）。**除息當日即入帳**：
        # 盯市用的是未還原價，跳空已經反映在未實現損益裡，這筆現金正好補回那一段
        self.dividend_received: float = dividend_received
        self.holding_days: int = holding_days  # 已持有曆日數（＝當前日期 − 開倉日）
        # SBL 借券費上次計提到哪一天。**逐日計提必須看曆日而不是 bar**：
        # 週五開的空單到週一只過了 1 根 bar，卻是 3 個曆日的借券費。
        # 每根 bar 計 1/365 的話，一年只計到 252 天、年費低估約 31%
        self.last_accrual_date: Optional[datetime.date] = last_accrual_date
        # 連續無報價天數；停牌／下市的部位靠它才有出場依據
        self.no_quote_days: int = 0

    @property
    def stock_id(self) -> str:
        """symbol 的台股別名：既有策略與報表沿用 stock_id 取值，不需改寫"""

        return self.symbol
