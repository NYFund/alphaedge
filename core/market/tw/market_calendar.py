import bisect
import datetime
from typing import Iterable, List, Optional, Set

from core.api.tw.stock_price_api import StockPriceAPI

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

    **建構形狀與 `FuturesCalendar` 相同**：純建構子收交易日清單，查詢都在清單上做；
    要從資料庫取清單時走 `from_api()`。呼叫端手上已有清單（回測預建的區間）就直接
    建構，不必每次查詢都回頭問資料庫。

    放在 `core/market/` 而不是回測底下：交易日判定回測與實盤都要用，
    擺進引擎會讓實盤反向相依回測。
"""


class MarketCalendar:
    """台股交易日曆：開盤日判定與營業日平移"""

    # 向資料庫要「前一個交易日」時，最多回推幾個曆日。
    #
    # **上界不是效能考量，是防卡死**：沒有上界的話，起始日落在資料庫最早一筆
    # 之前時會一路往回找也不會停，而且不會有任何錯誤訊息——回測看起來就是「卡住了」。
    #
    # **90 天而不是 30 天**：上界要按「`price` 表可能缺多久」抓，不是按連假長度
    # （史上最長是 2023 年春節的 12 天）；表裡真的會有缺口，上界抓 30 天的話，
    # 一段一個月的缺漏就會讓整場回測以 `LookupError` 中止。
    MAX_LOOKBACK_DAYS: int = 90

    def __init__(self, trading_days: Optional[Iterable[datetime.date]] = None) -> None:
        # 已排序的交易日清單 ＋ 供 O(1) 查詢的集合
        self.trading_days: List[datetime.date] = sorted(set(trading_days or []))
        self.trading_day_set: Set[datetime.date] = set(self.trading_days)

    @classmethod
    def from_api(
        cls,
        api: StockPriceAPI,
        start_date: datetime.date,
        end_date: datetime.date,
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

    @classmethod
    def previous_trading_day_from_api(
        cls, api: StockPriceAPI, date: datetime.date
    ) -> datetime.date:
        """
        - Description:
            向資料庫取指定日期的前一個交易日（不含當日）

            給**手上沒有涵蓋該日的清單**的呼叫端用：策略的清單依回測區間預建，
            實盤查的是今天，早已超出區間。一次查 `MAX_LOOKBACK_DAYS` 個曆日的交易日，
            而不是逐日往回試。
        - Parameters:
            - api: StockPriceAPI
                行情 API
            - date: datetime.date
                基準日
        - Return:
            - datetime.date
                前一個交易日
        - Raise:
            - LookupError
                回推上界內都找不到交易日（多半是起始日早於資料涵蓋範圍）
        """

        calendar: MarketCalendar = cls.from_api(
            api,
            date - datetime.timedelta(days=cls.MAX_LOOKBACK_DAYS),
            date - datetime.timedelta(days=1),
        )
        previous: Optional[datetime.date] = calendar.get_previous_trading_day(date)
        if previous is None:
            raise LookupError(
                f"自 {date} 往前 {cls.MAX_LOOKBACK_DAYS} 個曆日內找不到交易日；"
                f"多半是回測起始日早於資料涵蓋範圍，或 price 表缺了一整段"
            )
        return previous

    def is_trading_day(self, date: datetime.date) -> bool:
        """該日是否為開盤日（清單涵蓋範圍內才有意義）"""

        return date in self.trading_day_set

    def shift_trading_days(
        self, date: datetime.date, offset: int
    ) -> Optional[datetime.date]:
        """
        - Description:
            以**營業日**為單位平移日期；`offset` 為負代表往前推

            台股多數「前 N 個營業日」的規則（融券最後回補日、停券起始日）都必須
            以實際開盤日計算，用曆日相減會在連假整段位移。
        - Parameters:
            - date: datetime.date
                基準日；不在清單內時以「不早於它的第一個交易日」為基準
            - offset: int
                平移的營業日數，負值往前
        - Return:
            - Optional[datetime.date]
                平移後的交易日；超出清單範圍時為 None（代表交易日資料不足以推算）
        """

        index: int = bisect.bisect_left(self.trading_days, date) + offset

        if index < 0 or index >= len(self.trading_days):
            return None
        return self.trading_days[index]

    def get_previous_trading_day(self, date: datetime.date) -> Optional[datetime.date]:
        """前一個交易日（不含當日）；清單內沒有更早的交易日時為 None"""

        index: int = bisect.bisect_left(self.trading_days, date) - 1
        return self.trading_days[index] if index >= 0 else None
