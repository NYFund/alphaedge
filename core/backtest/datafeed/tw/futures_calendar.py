import bisect
import datetime
import re
from typing import Iterable, List, Optional, Set, Tuple

from loguru import logger

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.utils import FuturesSession

"""
FuturesCalendar: 台期貨交易日曆（交易日、時段、結算日與最後交易日）

**不沿用股票 calendar**（`market_calendar.py`），三個原因每一個都會算錯：

1. **期貨有夜盤**，且夜盤跨日（15:00 → 次日 05:00）——股票沒有這個概念。
2. **期貨有結算日與最後交易日**：契約到期後就不再有報價，用股票那套「一直有
   報價」的假設會讓部位卡在已到期的契約上。
3. **兩者的休市日不完全相同**：期貨在部分日子（例如颱風假的處置）與現貨不一致，
   且期貨的最後交易日遇休市要順延，順延到哪一天必須看**期貨自己**的開盤日。

---

**交易日的判準是資料，規則只用來算日期**：本日曆的 `trading_days` 來自
`futures_price_daily`（實際有行情的日子），故臨時休市、颱風假、補行交易日
全部自動涵蓋——那些是公告出來的事實，推不出來。規則（第三個星期三）只用於
算「應該在哪一天到期」，再用交易日順延。

以 TX 2015-01 ~ 2026-08 的 **140 個已到期月契約**實測，本日曆算出的最後交易日
與行情表中該契約實際出現的最後一天 **140/140 完全相同**（見
`tests/test_futures_calendar.py` 的 `slow` 測試）。
"""


class FuturesCalendar:
    """台期貨交易日曆"""

    # === 交易時段（TAIFEX 現行制度）===
    DAY_SESSION_OPEN: datetime.time = datetime.time(8, 45)
    DAY_SESSION_CLOSE: datetime.time = datetime.time(13, 45)
    NIGHT_SESSION_OPEN: datetime.time = datetime.time(15, 0)
    NIGHT_SESSION_CLOSE: datetime.time = datetime.time(5, 0)  # 次一曆日

    # 夜盤上線日：**2017-05-15 之前沒有夜盤**，該日之前的夜盤查詢一律無資料
    NIGHT_SESSION_LAUNCH_DATE: datetime.date = datetime.date(2017, 5, 15)

    # === 結算日規則 ===
    SETTLEMENT_WEEKDAY: int = 2  # 星期三（`weekday()` 的 0 = 星期一）
    MONTHLY_SETTLEMENT_WEEK: int = 3  # 月契約為交割月份的「第三個」星期三
    # 順延時最多往後找幾個曆日：2023 年春節連休 12 天是史上最長的一次
    MAX_POSTPONE_DAYS: int = 21

    # 到期月代碼：`YYYYMM` 或 `YYYYMMWn`（週契約，n 為第幾週）
    EXPIRY_PATTERN: re.Pattern = re.compile(r"^(\d{4})(\d{2})(?:W(\d))?$")

    def __init__(self, trading_days: Optional[Iterable[datetime.date]] = None) -> None:
        # 已排序的交易日清單 ＋ 供 O(1) 查詢的集合
        self.trading_days: List[datetime.date] = sorted(set(trading_days or []))
        self.trading_day_set: Set[datetime.date] = set(self.trading_days)

    @classmethod
    def from_api(
        cls,
        api: FuturesPriceAPI,
        start_date: datetime.date,
        end_date: datetime.date,
        product: Optional[str] = None,
    ) -> "FuturesCalendar":
        """
        - Description:
            由行情表建立日曆

            **單一商品用 `product`**：不同商品的掛牌期間不同，用「任一商品有資料」
            會把該策略根本不交易的商品的交易日也算成開盤日。

            **多商品策略則取聯集**（`TwFuturesDataFeed.build_calendar()` 走這條）：
            那裡問的是「今天市場有沒有開」與「結算日要順延到哪天」，
            兩者都是**市場層級**的問題——只要有任一商品在交易，市場就是開的，
            而結算順延依的是交易所休市日、不是個別商品的暫停交易。
            舊版只看 `products[0]`，第一個商品停止交易的那些日子整場回測都被
            判定為休市，連還在交易的其他商品都跟著停擺。
        - Parameters:
            - api: FuturesPriceAPI
                行情 API
            - start_date / end_date: datetime.date
                涵蓋區間；建議比回測區間再往後多留一個月，換月推算會用到
            - product: Optional[str]
                商品代碼；None 表示任一商品有資料即算開盤日
        - Return:
            - FuturesCalendar
        """

        return cls(api.get_trading_days(start_date, end_date, product=product))

    # === 交易日 ===
    def is_trading_day(self, date: datetime.date) -> bool:
        """該日是否有行情（涵蓋臨時休市與補行交易日，見模組說明）"""

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
        以**交易日**為單位平移；`offset` 為負代表往前推

        基準日不在清單內時，以「不早於它的第一個交易日」為基準
        （與 `MarketCalendar.shift_trading_days()` 同一種語意）。
        超出清單範圍時回傳 None——那代表日曆涵蓋的區間不足以推算，不是「沒有那天」。
        """

        index: int = bisect.bisect_left(self.trading_days, date) + offset

        if index < 0 or index >= len(self.trading_days):
            return None
        return self.trading_days[index]

    def get_next_trading_day(self, date: datetime.date) -> Optional[datetime.date]:
        """下一個交易日（不含當日）"""

        index: int = bisect.bisect_right(self.trading_days, date)
        return self.trading_days[index] if index < len(self.trading_days) else None

    def get_previous_trading_day(self, date: datetime.date) -> Optional[datetime.date]:
        """前一個交易日（不含當日）"""

        index: int = bisect.bisect_left(self.trading_days, date) - 1
        return self.trading_days[index] if index >= 0 else None

    # === 結算日與最後交易日 ===
    @classmethod
    def get_nth_weekday(
        cls, year: int, month: int, weekday: int, nth: int
    ) -> datetime.date:
        """取某年某月的第 n 個指定星期幾（純日期運算，不看是否為交易日）"""

        first: datetime.date = datetime.date(year, month, 1)
        first_match: datetime.date = first + datetime.timedelta(
            days=(weekday - first.weekday()) % 7
        )
        return first_match + datetime.timedelta(days=7 * (nth - 1))

    def postpone_to_trading_day(self, date: datetime.date) -> datetime.date:
        """
        - Description:
            遇非交易日時**順延**至次一交易日（TAIFEX 的最後交易日規則）

            2023-01 契約是最好的例子：第三個星期三 2023-01-18 遇春節連休 12 天，
            最後交易日順延到 **2023-01-30**——這種長度推不出來，只能看實際開盤日。

            日曆沒有涵蓋到該日期（例如查詢未來的月份）時**原樣回傳**：
            那是「還不知道」，不是「當天不交易」，硬推只會給出假答案。
        - Parameters:
            - date: datetime.date
                規則算出的日期
        - Return:
            - datetime.date
                順延後的交易日
        """

        if not self.trading_days:
            return date

        if date > self.trading_days[-1] or date < self.trading_days[0]:
            return date

        adjusted: datetime.date = date
        for _ in range(self.MAX_POSTPONE_DAYS):
            if self.is_trading_day(adjusted):
                return adjusted
            adjusted += datetime.timedelta(days=1)

        logger.warning(
            f"[Futures Calendar] {date} 起連續 {self.MAX_POSTPONE_DAYS} 個曆日"
            f"皆非交易日，順延結果可能不正確"
        )
        return adjusted

    def get_settlement_date(self, year: int, month: int) -> datetime.date:
        """
        月契約的結算日（＝最後交易日）：交割月份的**第三個星期三**，遇休市順延

        週契約請走 `get_last_trading_date()`，它們的到期規則不同。
        """

        return self.postpone_to_trading_day(
            self.get_nth_weekday(
                year, month, self.SETTLEMENT_WEEKDAY, self.MONTHLY_SETTLEMENT_WEEK
            )
        )

    def get_last_trading_date(self, expiry: str) -> Optional[datetime.date]:
        """
        - Description:
            由到期月代碼算出最後交易日

            **月契約與週契約的規則不同**：月契約是第三個星期三；週契約
            （`YYYYMMWn`）是該月的第 n 個星期三——TAIFEX 不發行 W3，
            因為第三週就是月契約本身。兩者遇休市都順延。
        - Parameters:
            - expiry: str
                到期月代碼（`202601` 或 `202601W2`）
        - Return:
            - Optional[datetime.date]
                最後交易日；代碼格式無法解析時為 None
        """

        matched: Optional[re.Match] = self.EXPIRY_PATTERN.match(expiry)
        if matched is None:
            logger.warning(f"[Futures Calendar] 無法解析到期月代碼：{expiry}")
            return None

        year: int = int(matched.group(1))
        month: int = int(matched.group(2))
        week: Optional[str] = matched.group(3)

        if week is None:
            return self.get_settlement_date(year, month)

        return self.postpone_to_trading_day(
            self.get_nth_weekday(year, month, self.SETTLEMENT_WEEKDAY, int(week))
        )

    def is_settlement_date(self, date: datetime.date, expiry: str) -> bool:
        """該日是否為指定契約的最後交易日（＝結算日）"""

        return self.get_last_trading_date(expiry) == date

    def get_trading_days_to_expiry(
        self, date: datetime.date, expiry: str
    ) -> Optional[int]:
        """
        - Description:
            距離最後交易日還有幾個**交易日**（當日為 0，已過期為負）

            這是換月規則的輸入（「提前 N 日換月」）。用曆日算會在
            連假整段位移——距離 5 個曆日與 5 個交易日在春節前差了一週以上。
        - Parameters:
            - date: datetime.date
                當前交易日
            - expiry: str
                到期月代碼
        - Return:
            - Optional[int]
                交易日數；日曆涵蓋不到該區間時為 None
        """

        last_trading_date: Optional[datetime.date] = self.get_last_trading_date(expiry)
        if last_trading_date is None:
            return None

        if not self.trading_days or last_trading_date > self.trading_days[-1]:
            return None

        if last_trading_date >= date:
            return self.count_trading_days(date, last_trading_date) - 1

        return -(self.count_trading_days(last_trading_date, date) - 1)

    # === 交易時段 ===
    @classmethod
    def get_session_window(
        cls, date: datetime.date, session: FuturesSession = FuturesSession.DAY
    ) -> Tuple[datetime.datetime, datetime.datetime]:
        """
        - Description:
            取得該交易日某時段的起訖時間

            **夜盤跨日**：`date` 當天 15:00 開始，到**次一曆日** 05:00 結束。
            回傳的結束時間因此可能落在下一天，這是刻意的，不是計算錯誤。
        - Parameters:
            - date: datetime.date
                交易日；夜盤取的是**它開始的那一天**，與行情表的 `date` 欄不同
                （行情表的 `night` 列記的是該段夜盤所屬的次一交易日）
            - session: FuturesSession
                交易時段
        - Return:
            - Tuple[datetime.datetime, datetime.datetime]
                （開始時間, 結束時間）
        """

        if session == FuturesSession.DAY:
            return (
                datetime.datetime.combine(date, cls.DAY_SESSION_OPEN),
                datetime.datetime.combine(date, cls.DAY_SESSION_CLOSE),
            )

        return (
            datetime.datetime.combine(date, cls.NIGHT_SESSION_OPEN),
            datetime.datetime.combine(
                date + datetime.timedelta(days=1), cls.NIGHT_SESSION_CLOSE
            ),
        )

    @classmethod
    def resolve_session(cls, moment: datetime.datetime) -> Optional[FuturesSession]:
        """
        - Description:
            判斷某個時間點屬於哪個交易時段

            **凌晨 05:00 之前算前一個交易日的夜盤**——這是期貨與股票最容易搞錯的
            地方：凌晨 03:00 成交的那一筆，屬於前一天開始的那一段夜盤。
        - Parameters:
            - moment: datetime.datetime
                時間點
        - Return:
            - Optional[FuturesSession]
                所屬時段；非交易時間（13:45~15:00、05:00~08:45）為 None
        """

        clock: datetime.time = moment.time()

        if cls.DAY_SESSION_OPEN <= clock <= cls.DAY_SESSION_CLOSE:
            return FuturesSession.DAY

        if clock >= cls.NIGHT_SESSION_OPEN or clock <= cls.NIGHT_SESSION_CLOSE:
            return FuturesSession.NIGHT

        return None

    def has_night_session(self, date: datetime.date) -> bool:
        """
        該交易日是否有夜盤

        **2017-05-15 之前沒有夜盤**：更早的區間查夜盤一律無資料，那是制度而非
        資料缺漏，回測若把它當缺漏處理會一路找不到原因。
        """

        return self.is_trading_day(date) and date >= self.NIGHT_SESSION_LAUNCH_DATE
