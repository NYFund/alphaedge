import datetime
from typing import Optional

import pandas as pd

from core.models.base.quote import BaseQuote, LiveDataUnavailableError
from core.utils import Scale

"""Quote structures: tick-level and daily-level pricing in backtesting"""


class TickQuote:
    """Tick 報價資訊（即時報價）"""

    def __init__(
        self,
        stock_id: str = "",
        time: pd.Timestamp = None,
        close: float = 0.0,
        volume: int = 0,  # Unit: Lot
        bid_price: float = 0.0,
        bid_volume: int = 0,  # Unit: Lot
        ask_price: float = 0.0,
        ask_volume: int = 0,  # Unit: Lot
        tick_type: int = 0,
    ) -> None:
        # Basic Info
        self.stock_id: str = stock_id  # Stock ID
        self.time: pd.Timestamp = time  # Quote timestamp

        # Current Price & Volume
        self.close: float = close  # 成交價
        self.volume: int = volume  # 成交量（Unit: Lot）

        # Bid & Ask Price & Volume
        self.bid_price: float = bid_price  # 委買價
        self.bid_volume: int = bid_volume  # 委買量
        self.ask_price: float = ask_price  # 委賣價
        self.ask_volume: int = ask_volume  # 委賣量

        # Tick Info
        self.tick_type: int = tick_type  # 內外盤別{1: 外盤, 2: 內盤, 0: 無法判定}


class StockQuote(BaseQuote):
    """個股報價資訊"""

    def __init__(
        self,
        stock_id: str = "",
        scale: Scale = None,
        date: datetime.datetime = None,
        cur_price: float = 0.0,
        volume: int = 0,  # Unit: Lot
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        adj_close: Optional[float] = None,
        tick: TickQuote = None,
    ) -> None:
        super().__init__(
            symbol=stock_id,
            scale=scale,
            date=date,
            cur_price=cur_price,
            volume=volume,
            open=open,
            high=high,
            low=low,
            close=close,
            adj_close=adj_close,
        )

        # Tick Data
        self.tick_quote: TickQuote = tick  # tick quote data

    @property
    def stock_id(self) -> str:
        """symbol 的台股別名：既有策略與報表沿用 stock_id 取值，不需改寫"""

        return self.symbol


class PreOpenStockQuote(StockQuote):
    """
    盤前（開盤段）的個股報價：**只有參考價，沒有 OHLC**

    開盤前根本不存在當日 OHLC。**不可以把它們填成參考價**——
    以 `quote.signal_close / 昨收 - 1` 算漲幅的策略會永遠算出 0%，
    訊號默默不成立，而且不會有任何錯誤訊息。所以這裡讓它**讀取時就炸**：
    策略作者會在 dry-run 當場看到，然後被迫明確改寫成「以參考價為基準」，
    並在 docstring 說明與回測的差異。

    三個實作細節都是必要的，少一個就漏掉一條路徑：

    1. **property 要帶 setter。** `BaseQuote.__init__` 會直接做 `self.open = open`，
       沒有 setter 的話物件根本建不出來（`AttributeError`），連拋出「拿不到資料」
       的機會都沒有。setter 刻意什麼都不做——盤前沒有這些值可存。
    2. **`adj_close` 也要擋。** 它是 `signal_close` 的來源。
    3. **`signal_close` 要自己覆寫。** 它在 `adj_close` 非 None 時**完全不讀 `close`**，
       只擋 `close` 的話，盤前只要 `adj_close` 有值，訊號就照樣算得出來。
    """

    def __init__(
        self,
        stock_id: str = "",
        date: datetime.datetime = None,
        reference_price: float = 0.0,
        volume: int = 0,
        limit_up: Optional[float] = None,
        limit_down: Optional[float] = None,
    ) -> None:
        super().__init__(
            stock_id=stock_id,
            scale=Scale.DAY,
            date=date,
            cur_price=reference_price,
            volume=volume,
        )

        # 開盤競價基準價（交易所公告值）。**盤前唯一可用的價格**，
        # 需要「市價」語意時以它為基準換算可成交的限價
        self.reference_price: float = reference_price

        # 當日漲跌停（交易所公告值）；除權息日的基準與公式推算不同，故一律用公告值
        self.limit_up: Optional[float] = limit_up
        self.limit_down: Optional[float] = limit_down

    @staticmethod
    def _unavailable(field: str) -> LiveDataUnavailableError:
        """統一的錯誤訊息，直接告訴作者該改用什麼"""

        return LiveDataUnavailableError(
            f"開盤段沒有當日 {field}：盤前不存在 OHLC。"
            "需要基準價請用 `reference_price`，需要漲跌停請用 `limit_up`／`limit_down`；"
            "若策略的實盤語意本來就與回測不同，請在策略上開一個具名的可覆寫方法，"
            "不要在這裡取值"
        )

    @property
    def open(self) -> float:
        """盤前沒有開盤價"""

        raise self._unavailable("open")

    @open.setter
    def open(self, value: float) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    @property
    def high(self) -> float:
        """盤前沒有最高價"""

        raise self._unavailable("high")

    @high.setter
    def high(self, value: float) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    @property
    def low(self) -> float:
        """盤前沒有最低價"""

        raise self._unavailable("low")

    @low.setter
    def low(self, value: float) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    @property
    def close(self) -> float:
        """盤前沒有收盤價"""

        raise self._unavailable("close")

    @close.setter
    def close(self, value: float) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    @property
    def adj_close(self) -> Optional[float]:
        """盤前沒有還原收盤價"""

        raise self._unavailable("adj_close")

    @adj_close.setter
    def adj_close(self, value: Optional[float]) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    @property
    def signal_close(self) -> float:
        """
        盤前沒有訊號用收盤價

        **必須自己覆寫**：父類的版本在 `adj_close` 非 None 時完全不讀 `close`，
        只擋 `close` 會讓這條路徑漏掉。
        """

        raise self._unavailable("signal_close")
