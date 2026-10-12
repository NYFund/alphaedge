from abc import ABC, abstractmethod
from typing import List, Optional

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

    各市場都有的四項：手續費、稅、已實現損益、報酬率。**損益與報酬率的公式只有這裡一份**：
    已實現損益 ＝（出場價 − 進場價）× 計價單位 × 數量 × 方向 − 成本，
    報酬率 ＝ 損益 ÷ 資金基礎。市場差異只在「計價單位是什麼」（台股一張 1,000 股、
    期貨為契約乘數）與「資金基礎是什麼」（台股為開倉價金或實際佔用的資金、期貨為保證金），
    這兩樣由部位管理層傳入。兩個市場原本各寫一份公式、期貨的部位管理層又再寫一份報酬率，
    改了乘數或方向的處理時另外兩份不會跟著改，也不會有測試失敗。

    放空專屬的借券費、保證金與利息給預設 `0` 實作——期貨的保證金是交易所公告的每口金額，
    由期貨的部位管理層查表，不走成本模型。對應 Lean 的 FeeModel。
    """

    @abstractmethod
    def commission(
        self, price: float, volume: int, product: Optional[str] = None
    ) -> int:
        """手續費；`product` 給有逐商品費率的市場（台股不看它）"""

        pass

    @abstractmethod
    def tax(self, price: float, volume: int, **kwargs) -> int:
        """
        交易稅（台股為證交稅、期貨為期交稅）

        **課稅依據兩邊不同，參數由各市場自定**：台股看買賣方向與是否當沖，
        期貨看契約乘數且買賣都課；硬湊成同一組參數只會讓其中一邊多出沒意義的欄位。
        """

        pass

    def realized_pnl(
        self,
        position_type: PositionType,
        entry_price: float,
        exit_price: float,
        volume: int,
        unit_size: int,
        cost: float = 0.0,
    ) -> float:
        """
        - Description:
            已實現損益 ＝（出場價 − 進場價）× 計價單位 × 數量 × 方向 − 成本，
            round 到小數點後 2 位

            **`round` 只在這裡做一次**：呼叫端拿到的就是記進帳上的數字，
            算報酬率時直接拿它當分子，不可再對中間值 round 一次——
            位置一變，歷史回歸會差到分位。
        - Parameters:
            - position_type: PositionType
                部位方向；SHORT 的價差方向與 LONG 相反
            - entry_price: float
                開倉價（LONG 為買進價、SHORT 為放空賣出價）
            - exit_price: float
                平倉價（LONG 為賣出價、SHORT 為回補買進價）
            - volume: int
                數量（台股為張、期貨為口）
            - unit_size: int
                計價單位：台股為每張股數、期貨為契約乘數（元／點）
            - cost: float
                要從損益扣掉的成本總額（台股為開倉 ＋ 平倉 ＋ 持有期間淨成本、
                期貨為交易成本）；只算價格損益時給 0
        - Return:
            - float
                已實現損益
        """

        units: int = unit_size * volume
        gross: float = (exit_price - entry_price) * units
        if position_type == PositionType.SHORT:
            gross = -gross
        return round(gross - cost, 2)

    def roi(self, pnl: float, capital_base: float) -> float:
        """
        - Description:
            報酬率（%）＝ 損益 ÷ 資金基礎 × 100，round 到小數點後 2 位

            資金基礎由部位管理層依市場決定：台股的名目報酬率用開倉價金 ＋ 開倉成本、
            資金效率用實際佔用的資金（放空為保證金 ＋ 開倉成本）；期貨一律用保證金，
            用契約價值當分母會把槓桿效果整個抹掉。
        - Parameters:
            - pnl: float
                已實現損益（`realized_pnl()` 的結果）
            - capital_base: float
                資金基礎；不是正數時回 0 而不是拋除零（例如保證金比率設成 0）
        - Return:
            - float
                報酬率（%）
        """

        if capital_base <= 0:
            return 0.0
        return round(pnl / capital_base * 100, 2)

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
