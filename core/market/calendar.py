import bisect
import datetime
from typing import Any, Iterable, List, Optional, Set

"""
BaseTradingCalendar: 交易日曆的共用骨架（開盤日判定、交易日平移與計數）

**判準是資料不是規則**：各市場的日曆都以「行情表那天有沒有資料」建清單，臨時休市、
颱風假與補行交易日因此自動涵蓋——那些是公告出來的事實，推不出來。
基底只做清單上的運算；清單從哪張表來（`from_api()`）、市場專屬的規則
（期貨的結算日與時段）留在各市場的子類。

放在 `core/market/` 而不是回測底下：交易日判定回測與實盤都要用，
擺進引擎會讓實盤反向相依回測。
"""


class BaseTradingCalendar:
    """交易日曆的共用骨架：已排序的交易日清單 ＋ O(1) 查詢的集合"""

    # 向資料庫要「前一個交易日」時，最多回推幾個曆日。
    #
    # **上界不是效能考量，是防卡死**：沒有上界的話，起始日落在資料庫最早一筆
    # 之前時會一路往回找也不會停，而且不會有任何錯誤訊息——回測看起來就是「卡住了」。
    #
    # **90 天而不是 30 天**：上界要按「行情表可能缺多久」抓，不是按連假長度
    # （史上最長是 2023 年春節的 12 天）；表裡真的會有缺口，上界抓 30 天的話，
    # 一段一個月的缺漏就會讓整場回測以 `LookupError` 中止。
    MAX_LOOKBACK_DAYS: int = 90

    def __init__(self, trading_days: Optional[Iterable[datetime.date]] = None) -> None:
        # 已排序的交易日清單 ＋ 供 O(1) 查詢的集合
        self.trading_days: List[datetime.date] = sorted(set(trading_days or []))
        self.trading_day_set: Set[datetime.date] = set(self.trading_days)

    @classmethod
    def from_api(
        cls, api: Any, start_date: datetime.date, end_date: datetime.date, **kwargs
    ) -> "BaseTradingCalendar":
        """
        - Description:
            由行情表建立日曆（區間含頭含尾）；各市場以自己的行情 API 實作

            留在子類是因為行情 API 的型別與查詢參數各市場不同（期貨多一個商品），
            基底只定義簽名的前三個位置參數，`previous_trading_day_from_api()` 靠它們就夠。
        - Parameters:
            - api: Any
                行情 API
            - start_date / end_date: datetime.date
                涵蓋區間
        - Return:
            - BaseTradingCalendar
        """

        raise NotImplementedError(f"{cls.__name__} 未實作 from_api()")

    @classmethod
    def previous_trading_day_from_api(
        cls, api: Any, date: datetime.date
    ) -> datetime.date:
        """
        - Description:
            向資料庫取指定日期的前一個交易日（不含當日）

            給**手上沒有涵蓋該日的清單**的呼叫端用：策略的清單依回測區間預建，
            實盤查的是今天，早已超出區間。一次查 `MAX_LOOKBACK_DAYS` 個曆日的交易日，
            而不是逐日往回試。
        - Parameters:
            - api: Any
                行情 API（型別依市場）
            - date: datetime.date
                基準日
        - Return:
            - datetime.date
                前一個交易日
        - Raise:
            - LookupError
                回推上界內都找不到交易日（多半是起始日早於資料涵蓋範圍）
        """

        calendar: BaseTradingCalendar = cls.from_api(
            api,
            date - datetime.timedelta(days=cls.MAX_LOOKBACK_DAYS),
            date - datetime.timedelta(days=1),
        )
        previous: Optional[datetime.date] = calendar.get_previous_trading_day(date)
        if previous is None:
            raise LookupError(
                f"自 {date} 往前 {cls.MAX_LOOKBACK_DAYS} 個曆日內找不到交易日；"
                f"多半是回測起始日早於資料涵蓋範圍，或行情表缺了一整段"
            )
        return previous

    # === 交易日 ===
    def is_trading_day(self, date: datetime.date) -> bool:
        """該日是否有行情（清單涵蓋範圍內才有意義）"""

        return date in self.trading_day_set

    def get_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """區間內的交易日（含頭含尾）"""

        left: int = bisect.bisect_left(self.trading_days, start_date)
        right: int = bisect.bisect_right(self.trading_days, end_date)
        return self.trading_days[left:right]

    def count_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> int:
        """
        區間內的交易日數

        **持倉天數要用這個而不是曆日相減**：跨春節的部位用曆日算會多出 10 天，
        用在「最長持有天數」這類風控上會提早出場。
        """

        return len(self.get_trading_days(start_date, end_date))

    def shift_trading_days(
        self, date: datetime.date, offset: int
    ) -> Optional[datetime.date]:
        """
        - Description:
            以**交易日**為單位平移日期；`offset` 為負代表往前推

            「前 N 個營業日」的規則（融券最後回補日、停券起始日、結算日順延）都必須
            以實際開盤日計算，用曆日相減會在連假整段位移。
        - Parameters:
            - date: datetime.date
                基準日；不在清單內時以「不早於它的第一個交易日」為基準
            - offset: int
                平移的交易日數，負值往前
        - Return:
            - Optional[datetime.date]
                平移後的交易日；超出清單範圍時為 None（代表日曆涵蓋的區間不足以推算，
                不是「沒有那天」）
        """

        index: int = bisect.bisect_left(self.trading_days, date) + offset

        if index < 0 or index >= len(self.trading_days):
            return None
        return self.trading_days[index]

    def get_next_trading_day(self, date: datetime.date) -> Optional[datetime.date]:
        """下一個交易日（不含當日）；清單內沒有更晚的交易日時為 None"""

        index: int = bisect.bisect_right(self.trading_days, date)
        return self.trading_days[index] if index < len(self.trading_days) else None

    def get_previous_trading_day(self, date: datetime.date) -> Optional[datetime.date]:
        """前一個交易日（不含當日）；清單內沒有更早的交易日時為 None"""

        index: int = bisect.bisect_left(self.trading_days, date) - 1
        return self.trading_days[index] if index >= 0 else None
