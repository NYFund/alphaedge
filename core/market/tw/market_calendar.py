import datetime

from core.api.tw.stock_price_api import StockPriceAPI
from core.market.calendar import BaseTradingCalendar

"""
台股交易日曆：開盤日判定與營業日平移

- Features:
    1. 判定某日是否開盤
    2. 以交易日為單位前後平移 N 日
    3. 取某日之前最後一個交易日
- 使用場景:
    **判準是「`price` 表那天有沒有資料」而不是官方開休市日曆**：台股有補行
    交易日（補班的週六照常開市），用「非週末」近似會整天判錯；而官方日曆
    只替已入庫的年度作答。

    **與 `FuturesCalendar` 共用 `BaseTradingCalendar`**：純建構子收交易日清單，
    查詢都在清單上做；要從資料庫取清單時走 `from_api()`。呼叫端手上已有清單
    （回測預建的區間）就直接建構，不必每次查詢都回頭問資料庫。

    放在 `core/market/` 而不是回測底下：交易日判定回測與實盤都要用，
    擺進引擎會讓實盤反向相依回測。
"""


class MarketCalendar(BaseTradingCalendar):
    """台股交易日曆：開盤日判定與營業日平移（運算在 `BaseTradingCalendar`，這裡只決定清單從哪來）"""

    @classmethod
    def from_api(
        cls,
        api: StockPriceAPI,
        start_date: datetime.date,
        end_date: datetime.date,
        **kwargs,
    ) -> "MarketCalendar":
        """
        - Description:
            由 `price` 表建立日曆（區間含頭含尾）

            **判準是資料而非規則**：當日有日 K 就是開盤日，臨時休市、颱風假與
            補行交易日因此自動涵蓋，那些是公告出來的事實，推不出來。
        - Parameters:
            - api: StockPriceAPI
                行情 API
            - start_date / end_date: datetime.date
                涵蓋區間
        - Return:
            - MarketCalendar
        """

        return cls(api.get_trading_days(start_date, end_date))
