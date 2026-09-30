from abc import ABC, abstractmethod
from typing import List

from core.models import BaseOrder
from core.utils import (
    PositionType,
)

"""
成本模型的抽象基底：手續費／稅／借券費／保證金／損益口徑（對應 Lean 的 FeeModel）

**屬於市場規則而非回測模擬**：部位帳務、實盤估成本與回測都要用同一套費率與口徑，
故放在 `core/market/`；各市場的實作在 `core/market/<市場>/cost_model.py`。
成本設定（`CostConfig`／`FuturesCostConfig`／`ShortConstraint`）是參數容器，在 `core/models/cost_config.py`。
"""


class BaseCostModel(ABC):
    """
    成本模型的共用介面

    必要方法為每個市場都有的四項：手續費、稅、已實現損益、報酬率。
    放空專屬的借券費、保證金與利息給預設 `0` 實作——期貨的保證金語意與
    台股融券不同，屆時由期貨實作自行覆寫，不必為此再拆一層介面。
    對應 Lean 的 FeeModel。
    """

    @abstractmethod
    def commission(self, price: float, volume: int) -> int:
        """手續費"""

        pass

    @abstractmethod
    def tax(self, price: float, volume: int, **kwargs) -> int:
        """交易稅（台股為證交稅、期貨為期交稅）"""

        pass

    @abstractmethod
    def realized_pnl(self, **kwargs) -> float:
        """已實現損益"""

        pass

    @abstractmethod
    def roi(self, **kwargs) -> float:
        """名目報酬率（%）"""

        pass

    @abstractmethod
    def roi_on_capital(self, **kwargs) -> float:
        """資金效率報酬率（%）"""

        pass

    def borrow_fee(self, **kwargs) -> int:
        """借券費；無此制度的市場維持 0"""

        return 0

    def margin_required(self, **kwargs) -> int:
        """開倉所需保證金；無保證金制度的市場維持 0"""

        return 0

    def short_interest(self, **kwargs) -> int:
        """放空擔保品的利息收入；無此制度的市場維持 0"""

        return 0

    def enrich_orders(self, orders: List[BaseOrder]) -> List[BaseOrder]:
        """
        - Description:
            依成本設定補上市場專屬的訂單欄位，策略不需自行填寫

            預設為不補值；有信用交易制度的市場自行覆寫。
        - Parameters:
            - orders: List[BaseOrder]
                策略回傳的訂單
        - Return:
            - List[BaseOrder]
                補值後的訂單
        """

        return orders

    @staticmethod
    def is_short(order: BaseOrder) -> bool:
        """訂單是否為放空方向（補值與成本路徑一律看每一張 order 的方向）"""

        return order.position_type == PositionType.SHORT
