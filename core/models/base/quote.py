import datetime
from typing import Optional, Union

from core.utils import Scale

"""BaseQuote: 市場與商品皆無關的報價骨架（識別欄位一律為 symbol）"""


class LiveDataUnavailableError(AttributeError):
    """
    實盤在這個時點拿不到這個欄位

    **繼承 `AttributeError` 而不是 `ValueError`**：它就是「這個屬性此刻不存在」，
    而且 `getattr(quote, "close", 預設值)` 這種寫法會因此安靜地拿到預設值——
    那正是我們要讓它現形的情況之一，繼承對的基底才不會讓既有的防禦性程式碼
    把錯誤吞掉還自以為正常。
    """


class BaseQuote:
    """
    報價資訊的共用骨架

    識別欄位命名為 symbol 而非 stock_id：引擎骨架不應該知道自己在跑哪個市場，
    台股的 stock_id 由 StockQuote 以 property 別名維持相容。
    """

    def __init__(
        self,
        symbol: str = "",
        scale: Scale = None,
        date: datetime.datetime = None,
        cur_price: float = 0.0,
        volume: int = 0,  # Unit: Lot
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        adj_close: Optional[float] = None,
    ) -> None:
        # Basic Info
        self.symbol: str = symbol  # 商品代號（台股為股票代號、期貨為契約代號）
        self.scale: Scale = scale  # Quote scale (DAY or TICK or ALL)
        self.date: Union[datetime.date, datetime.datetime] = date  # Current date

        # Current Price & Volume
        self.cur_price: float = cur_price  # Current price
        self.volume: int = volume  # order's volume (Unit: Lot)

        # OHLC Info（一律為原始成交價：成交、成本、漲跌停與檔位判定都用這一組）
        self.open: float = open  # Open price
        self.high: float = high  # High price
        self.low: float = low  # Low price
        self.close: float = close  # Close price

        # 還原收盤價（後復權）；None 代表未啟用還原，取值時退回 close
        self.adj_close: Optional[float] = adj_close

    @property
    def signal_close(self) -> float:
        """
        - Description:
            訊號計算專用的收盤價：啟用還原時為還原價，否則退回原始收盤價

            **策略算漲跌幅／均線／動能一律用這個 property**，不要直接用 `close`——
            除權息造成的跳空會被當成真實漲跌。
            反過來，成交價、手續費、證交稅、漲跌停與檔位判定**一律用 `close`**，
            因為稅費是對實際成交金額課徵的。

            未啟用還原時本 property 等於 `close`，既有策略行為完全不變。
        - Return:
            - float
                訊號用收盤價
        """

        return self.close if self.adj_close is None else self.adj_close
