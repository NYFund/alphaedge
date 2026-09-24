import datetime
from typing import Optional

from core.models.base.quote import (
    BaseQuote,
    PreOpenQuoteMixin,
)
from core.utils import FuturesSession, Scale

"""FuturesQuote: 台期貨單一契約的報價（識別為 product ＋ expiry 組成的契約代號）"""


class FuturesQuote(BaseQuote):
    """
    期貨契約報價資訊

    **一個 `FuturesQuote` 只代表一個契約**（單一商品的單一到期月的單一時段）。
    同一天同一商品有多個到期月在交易，那是多個 quote，不是一個 quote 的多個欄位——
    要哪一個由呼叫端決定（`FuturesPriceAPI`）。

    與 `StockQuote` 的兩個語意差異：
    - `volume` 的單位是**口**，不是張；不需要 `Units.LOT` 換算。
    - `settlement_price` 與 `open_interest` **夜盤為 None**（來源就沒有這兩項），
      不可視為 0。
    """

    def __init__(
        self,
        product: str = "",
        expiry: str = "",
        scale: Scale = None,
        date: datetime.datetime = None,
        cur_price: float = 0.0,
        volume: int = 0,  # Unit: Contract（口）
        open: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        session: FuturesSession = FuturesSession.DAY,
        settlement_price: Optional[float] = None,
        open_interest: Optional[int] = None,
        multiplier: int = 0,
    ) -> None:
        super().__init__(
            symbol=f"{product}{expiry}",
            scale=scale,
            date=date,
            cur_price=cur_price,
            volume=volume,
            open=open,
            high=high,
            low=low,
            close=close,
            # 期貨沒有除權息還原的概念，一律用原始價
            adj_close=None,
        )

        # Contract Info
        self.product: str = product  # 商品代碼（Ex: TX）
        self.expiry: str = expiry  # 到期月份（Ex: 202601；週契約帶 W 尾碼）
        self.session: FuturesSession = session  # 交易時段（日盤／夜盤）
        self.multiplier: int = multiplier  # 契約乘數（元／點）

        # Settlement Info（夜盤為 None）
        self.settlement_price: Optional[float] = settlement_price  # 結算價
        self.open_interest: Optional[int] = open_interest  # 未沖銷契約量

    @property
    def contract_id(self) -> str:
        """symbol 的期貨別名：`{product}{expiry}`（Ex: TX202601）"""

        return self.symbol


class PreOpenFuturesQuote(PreOpenQuoteMixin, FuturesQuote):
    """
    盤前（開盤段）的期貨報價：**只有參考價，沒有 OHLC**

    語意與行為和 `PreOpenStockQuote` 完全相同，理由也一樣——盤前不存在當日 OHLC，
    填成參考價會讓漲幅類訊號永遠算出 0%，而且不報錯。

    期貨另外要注意 `settlement_price` 與 `open_interest`：它們本來就可能是 `None`
    （夜盤沒有），所以**不覆寫**——維持 None 表示「沒有資料」，與回測一致。
    """

    def __init__(
        self,
        product: str = "",
        expiry: str = "",
        date: datetime.datetime = None,
        reference_price: float = 0.0,
        volume: int = 0,
        session: FuturesSession = FuturesSession.DAY,
        multiplier: int = 0,
        limit_up: Optional[float] = None,
        limit_down: Optional[float] = None,
    ) -> None:
        super().__init__(
            product=product,
            expiry=expiry,
            scale=Scale.DAY,
            date=date,
            cur_price=reference_price,
            volume=volume,
            session=session,
            multiplier=multiplier,
        )

        self.reference_price: float = reference_price
        self.limit_up: Optional[float] = limit_up
        self.limit_down: Optional[float] = limit_down
