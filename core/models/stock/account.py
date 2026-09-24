from typing import List, Optional

from core.models.base.account import BaseAccount
from core.utils import PositionType

from .position import StockPosition
from .record import StockTradeRecord

"""StockAccount: manages account-level state in backtesting (positions, balance, realized PnL, costs)"""


class StockAccount(BaseAccount):
    """
    庫存及餘額資訊

    相對於 BaseAccount 多的是台股信用交易專屬的部分：保證金佔用、空頭曝險，
    以及把借券費與融券利息納入的總交易成本口徑。
    """

    def __init__(self, init_capital: float = 0.0) -> None:
        super().__init__(init_capital)

        # Short Positions
        self.margin_used: float = 0.0  # 放空部位佔用的保證金總額

        # Positions & Trading History（型別窄化為台股專屬 model）
        self.positions: List[StockPosition] = []  # 持有未平倉的股票庫存
        self.trade_records: List[StockTradeRecord] = []  # 股票歷史交易紀錄

    # === stock_id 關鍵字相容層 ===
    # 引擎內部一律以 symbol 為鍵，但既有策略與測試沿用 stock_id，故保留具名別名。
    def get_first_open_position(self, stock_id: str) -> Optional[StockPosition]:
        """根據股票代號取得庫存中該股票最早開倉的部位（FIFO）"""

        return super().get_first_open_position(symbol=stock_id)

    def get_positions(
        self,
        stock_id: Optional[str] = None,
        position_type: Optional[PositionType] = None,
    ) -> List[StockPosition]:
        """取得庫存中符合條件的未平倉部位；參數為 None 表示不限制該條件"""

        return super().get_positions(symbol=stock_id, position_type=position_type)

    def check_has_position(
        self,
        stock_id: str,
        position_type: Optional[PositionType] = None,
    ) -> bool:
        """檢查指定的股票是否有未平倉部位；position_type 為 None 時不分方向"""

        return super().check_has_position(symbol=stock_id, position_type=position_type)

    def update_transaction_cost(self) -> None:
        """更新交易成本"""

        self.total_commission = sum(record.commission for record in self.trade_records)
        self.total_tax = sum(record.tax for record in self.trade_records)

        # 放空的借券費與股利補償為支出、融券利息為收入，一併計入總交易成本
        # （與 `StockTradeRecord.transaction_cost` 同一口徑，兩邊須一致）
        total_borrow_fee: float = sum(
            record.borrow_fee for record in self.trade_records
        )
        total_interest: float = sum(record.interest for record in self.trade_records)
        total_dividend_compensation: float = sum(
            record.dividend_compensation for record in self.trade_records
        )

        self.total_transaction_cost = (
            self.total_commission
            + self.total_tax
            + total_borrow_fee
            + total_dividend_compensation
            - total_interest
        )
