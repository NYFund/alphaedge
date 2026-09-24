from typing import List, Optional

from core.models.base.position import BasePosition
from core.models.base.record import BaseTradeRecord
from core.utils import PositionType

"""BaseAccount: 市場與商品皆無關的帳戶骨架（部位查詢一律以 symbol 為鍵）"""


class BaseAccount:
    """
    庫存及餘額資訊的共用骨架

    FIFO 取倉、方向篩選與績效彙總都與市場與商品皆無關，故全部留在此；
    保證金佔用與空頭曝險屬台股信用交易，由 StockAccount 補上。
    """

    def __init__(self, init_capital: float = 0.0) -> None:
        # Initial Setup
        self.init_capital: float = init_capital  # 初始本金

        # Account Balances
        self.balance: float = init_capital  # 餘額

        # Account Performance
        self.realized_pnl: float = 0.0  # 總已實現損益（profit and loss）
        self.roi: float = 0.0  # 帳戶已實現總報酬率

        # Transaction Costs
        self.total_commission: float = 0.0  # 總手續費
        self.total_tax: float = 0.0  # 總交易稅
        self.total_transaction_cost: float = 0  # 總交易成本

        # 滑價吃掉的價差總額（統計用）。
        #
        # ⚠️ **刻意不併進 `total_transaction_cost`**：手續費與稅是真的從餘額扣掉的
        # 一筆錢，滑價則是**內含在成交價裡**——成交價已經比委託價差了，損益早就
        # 反映了它。加進交易成本等於重複計算一次，帳會對不起來。
        # 它回答的是「這組滑價假設總共讓我少賺多少」，見報表的 `Slippage Cost`。
        self.total_slippage_cost: float = 0.0

        # Trade ID
        self.trade_id_counter: int = 0  # 交易編號（每筆交易唯一編號）

        # Positions & Trading History
        self.positions: List[BasePosition] = []  # 持有未平倉的庫存
        self.trade_records: List[BaseTradeRecord] = []  # 歷史交易紀錄

    def generate_trade_id(self) -> int:
        """生成下一筆交易編號"""

        self.trade_id_counter += 1
        return self.trade_id_counter

    def get_position_count(self) -> int:
        """
        取得庫存**商品檔數**（同一檔加碼多次只算一檔）

        `max_holdings` 的語意一律是「檔數」。**不可改回部位筆數**：允許加碼的
        策略（例如 `MomentumStrategy1`）同一檔加碼兩次就會佔掉兩個名額，
        實際持有檔數比設定少。
        """

        return len(
            {position.symbol for position in self.positions if not position.is_closed}
        )

    def get_first_open_position(self, symbol: str) -> Optional[BasePosition]:
        """根據商品代號取得庫存中該商品最早開倉的部位（FIFO）"""

        for position in self.positions:
            if position.symbol == symbol and not position.is_closed:
                return position
        return None

    def remove_closed_positions(self) -> None:
        """移除已平倉的部位"""

        self.positions = [
            position for position in self.positions if not position.is_closed
        ]

    def get_positions(
        self,
        symbol: Optional[str] = None,
        position_type: Optional[PositionType] = None,
    ) -> List[BasePosition]:
        """取得庫存中符合條件的未平倉部位；參數為 None 表示不限制該條件"""

        return [
            position
            for position in self.positions
            if not position.is_closed
            and (symbol is None or position.symbol == symbol)
            and (position_type is None or position.position_type == position_type)
        ]

    def check_has_position(
        self,
        symbol: str,
        position_type: Optional[PositionType] = None,
    ) -> bool:
        """
        - Description:
            檢查指定的商品是否有**未平倉**部位

            **`is_closed` 一定要濾掉**：`positions` 是只增不減的
            清單，平倉只是把 `is_closed` 設為 True。少了這個條件，一檔賣掉之後
            仍會被當成「還在庫存」——雙向持倉檢查會永久拒絕該標的的反向開倉，
            而 `Backtester` 用它挑出「有部位的報價」時也會一直帶著已平倉的標的。
            同檔案的 `get_positions()` 也是同一套濾法，兩者不一致本身就是徵兆。
        - Parameters:
            - symbol: str
                商品代碼
            - position_type: Optional[PositionType]
                方向；None 表示不分方向
        - Return:
            - bool
                有未平倉部位為 True
        """

        return any(
            not position.is_closed
            and position.symbol == symbol
            and (position_type is None or position.position_type == position_type)
            for position in self.positions
        )

    def update_realized_pnl(self) -> None:
        """更新已實現損益"""

        self.realized_pnl = sum(
            record.realized_pnl for record in self.trade_records if record.is_closed
        )

    def update_roi(self) -> None:
        """更新已實現 ROI (Return On Investment)"""

        self.roi = round(self.realized_pnl / self.init_capital * 100, 2)

    def update_transaction_cost(self) -> None:
        """更新交易成本；持有期間的計提費用由各市場的子類自行加總"""

        self.total_commission = sum(record.commission for record in self.trade_records)
        self.total_tax = sum(record.tax for record in self.trade_records)

        self.total_transaction_cost = self.total_commission + self.total_tax

    def update_account_status(self) -> None:
        """更新帳戶資訊"""

        self.update_realized_pnl()
        self.update_roi()
        self.update_transaction_cost()
