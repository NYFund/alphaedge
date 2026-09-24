import datetime
from typing import Optional

import pandas as pd

from core.models.base.quote import (
    BaseQuote,
    PreOpenQuoteMixin,
)
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


class PreOpenStockQuote(PreOpenQuoteMixin, StockQuote):
    """
    盤前（開盤段）的個股報價：**只有參考價與漲跌停，沒有 OHLC**

    開盤前不存在當日 OHLC，**不可以把它們填成參考價**——以
    `quote.signal_close / 昨收 - 1` 算漲幅的策略會永遠算出 0%，訊號默默不成立，
    而且不會有任何錯誤訊息。故 OHLC 相關欄位一律讀取即拋（`PreOpenQuoteMixin`），
    策略作者在 dry-run 當場就會看到，被迫明確改寫成「以參考價為基準」。

    **Mixin 必須排在 `StockQuote` 之前**，否則 MRO 會先找到父類那份 property。
    """

    UNAVAILABLE_HINT: str = (
        "需要基準價請用 `reference_price`，需要漲跌停請用 `limit_up`／`limit_down`；"
        "若策略的實盤語意本來就與回測不同，請在策略上開一個具名的可覆寫方法，"
        "不要在這裡取值"
    )

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
