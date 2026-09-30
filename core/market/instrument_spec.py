from abc import ABC, abstractmethod
from typing import Optional, Tuple

from core.utils import (
    Action,
)

"""
InstrumentSpec: 商品規格的抽象基底（報價單位換算、跳動點、漲跌停規則、滑價調價）

**屬於市場規則而非回測模擬**：部位帳務、實盤與回測都要用同一份跳動點與漲跌停規則，
故放在 `core/market/`；各市場的實作在 `core/market/<市場>/instrument_spec.py`。
"""


# 基點換算：1 bps = 0.01% = 萬分之一
BPS_PER_UNIT: float = 10_000.0


class InstrumentSpec(ABC):
    """
    商品規格：報價單位換算、跳動點、漲跌停規則

    市場差異最集中的地方，被成交價驗證與每日權益快照兩處使用。
    對應 Lean 的 SymbolProperties。
    """

    def apply_slippage(
        self,
        price: float,
        action: Action,
        bps: float,
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            對參考價套用滑價，回傳含滑價的成交價

            **方向寫死、不由呼叫端決定符號**：買進往上、賣出往下，
            兩者都是對下單者不利的方向。滑價的意義是「你拿不到理想價」，
            若允許呼叫端傳負值，就會出現「滑價讓績效變好」這種無意義的設定。

            調整後會對齊該商品的跳動點，避免算出不可能成交的價格；
            對齊方向同樣取**對下單者不利**的一側（買進進位、賣出捨去）。
        - Parameters:
            - price: float
                參考價（策略給的委託價）
            - action: Action
                訂單動作；買進加價、賣出減價
            - bps: float
                滑價基點（1 bps = 0.01%）；`0` 時原價回傳，不做任何對齊
            - product: Optional[str]
                商品代碼，只有跳動點逐商品不同的市場（期貨）會用到
        - Return:
            - float
                含滑價的成交價
        """

        if not bps or price <= 0:
            return price

        ratio: float = bps / BPS_PER_UNIT

        if action == Action.BUY:
            return self.round_to_tick(price * (1 + ratio), "up", product)
        return self.round_to_tick(price * (1 - ratio), "down", product)

    @abstractmethod
    def to_units(self, volume: int) -> int:
        """
        - Description:
            下單數量 → 計價單位（台股：張 → 股 ×1000；期貨：口 → 契約乘數）
        - Parameters:
            - volume: int
                下單數量（台股為張、期貨為口）
        - Return:
            - int
                計價單位數量
        """

        pass

    @abstractmethod
    def round_to_tick(
        self,
        price: float,
        direction: str = "nearest",
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            將價格對齊該商品的跳動點，避免算出不可能成交的價格
        - Parameters:
            - price: float
                原始價格
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
            - product: Optional[str]
                商品代碼；只有跳動點逐商品不同的市場（期貨）需要，台股用不到
        - Return:
            - float
                對齊檔位後的價格
        """

        pass

    @abstractmethod
    def get_price_limits(
        self, prev_close: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        - Description:
            依前一交易日收盤價推算漲跌停區間
        - Parameters:
            - prev_close: float
                前一交易日收盤價
        - Return:
            - Tuple[Optional[float], Optional[float]]
                (跌停價, 漲停價)；無漲跌停制度時回傳 (None, None)
        """

        pass
