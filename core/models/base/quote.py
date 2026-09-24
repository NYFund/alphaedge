import datetime
from typing import Optional, Tuple, Union

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

            未啟用還原時本 property 等於 `close`。
        - Return:
            - float
                訊號用收盤價
        """

        return self.close if self.adj_close is None else self.adj_close


class PreOpenQuoteMixin:
    """
    - Description:
        盤前報價：**OHLC 尚未產生，讀取一律拋出而不是回 0**

        開盤前根本不存在當日 OHLC。**不可以把它們填成參考價**——
        以 `quote.signal_close / 昨收 - 1` 算漲幅的策略會永遠算出 0%，
        訊號默默不成立，而且不會有任何錯誤訊息。所以這裡讓它**讀取時就炸**：
        策略作者會在 dry-run 當場看到，然後被迫明確改寫成「以參考價為基準」。

        〈三個必要的實作細節，少一個就漏掉一條路徑〉

        1. **property 要帶 setter。** `BaseQuote.__init__` 會直接做
           `self.open = open`，沒有 setter 的話物件根本建不出來
           （`AttributeError`），連拋出「拿不到資料」的機會都沒有。
           setter 刻意什麼都不做——盤前沒有這些值可存。
        2. **`adj_close` 也要擋。** 它是 `signal_close` 的來源。
        3. **`signal_close` 要自己覆寫。** 它在 `adj_close` 非 None 時
           **完全不讀 `close`**，只擋 `close` 的話，盤前只要 `adj_close` 有值，
           訊號就照樣算得出來。

        **三個細節收在這個 Mixin 而不是各市場各寫一份**：漏掉 `signal_close`
        那條不會報錯，只會讓盤前訊號默默算得出來，新市場繼承就全部到位。

        **Mixin 要排在具體報價類別之前**（`class X(PreOpenQuoteMixin, StockQuote)`），
        否則 MRO 會先找到父類那份 property，六道防線全部失效。
    """

    # 盤前取不到的欄位；`signal_close` 也在內，理由見上方第 3 點
    UNAVAILABLE_FIELDS: Tuple[str, ...] = (
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "signal_close",
    )

    # 子類可覆寫，補上該市場專屬的替代欄位說明
    UNAVAILABLE_HINT: str = (
        "需要基準價請用 `reference_price`，需要漲跌停請用 `limit_up`／`limit_down`"
    )

    @classmethod
    def unavailable(cls, field: str) -> LiveDataUnavailableError:
        """統一的錯誤訊息，直接告訴作者該改用什麼"""

        return LiveDataUnavailableError(
            f"開盤段沒有當日 {field}：盤前不存在 OHLC。{cls.UNAVAILABLE_HINT}"
        )


def _make_unavailable_property(field: str) -> property:
    """
    - Description:
        產生一組「讀取即拋、寫入不存」的 property

        **setter 不可省**：父類 `__init__` 會直接賦值，沒有 setter 物件就建不出來。
    - Parameters:
        - field: str
            欄位名，只用於錯誤訊息
    - Return:
        - property
            讀取時拋 `LiveDataUnavailableError` 的 property
    """

    def getter(self: "PreOpenQuoteMixin") -> float:
        raise type(self).unavailable(field)

    def setter(self: "PreOpenQuoteMixin", value: object) -> None:
        """僅供父類 `__init__` 賦值用；刻意不存"""

    getter.__doc__ = f"盤前沒有當日 {field}"
    return property(getter, setter)


for _field in PreOpenQuoteMixin.UNAVAILABLE_FIELDS:
    setattr(PreOpenQuoteMixin, _field, _make_unavailable_property(_field))
