import datetime
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from core.market.tw.futures_calendar import FuturesCalendar
from core.utils import FuturesRollRule

"""
FuturesRollPlanner: 決定「某個交易日應該持有哪一個到期月」

**換月是政策不是資料**，故獨立成一層讓兩邊共用同一份實作：

- **建連續合約時**：決定每一天的序列取自哪個契約。
- **回測時**：決定策略何時把部位轉到次月。

兩處若各寫一套，回測拿到的連續合約與策略實際轉倉的時點會對不上，
而且不會有任何錯誤——只會讓績效差一截卻找不到原因。

---

**週契約預設不納入**：`YYYYMMWn` 與月契約是不同的商品（到期規則不同、
流動性差一個數量級），把它們混進「近月」的判斷會讓連續合約每週都換一次月。
要做週契約的連續序列請另建一條，不要混用。
"""


class FuturesRollPlanner:
    """依換月規則決定每個交易日的「當家契約」"""

    # 月契約代碼：`YYYYMM`；帶 `W` 尾碼者為週契約
    MONTHLY_EXPIRY_PATTERN: re.Pattern = re.compile(r"^\d{6}$")

    # `DAYS_BEFORE_EXPIRY` 的預設值：最後交易日前 1 個交易日換月。
    # **不用 0**：0 等同 `LAST_TRADING_DAY`，兩個規則就沒有差別了
    DEFAULT_DAYS_BEFORE_EXPIRY: int = 1

    def __init__(
        self,
        calendar: FuturesCalendar,
        rule: FuturesRollRule = FuturesRollRule.LAST_TRADING_DAY,
        days_before_expiry: int = DEFAULT_DAYS_BEFORE_EXPIRY,
        include_weekly: bool = False,
    ) -> None:
        self.calendar: FuturesCalendar = calendar
        self.rule: FuturesRollRule = rule
        self.days_before_expiry: int = days_before_expiry
        self.include_weekly: bool = include_weekly

    def filter_expiries(self, expiries: List[str]) -> List[str]:
        """留下要納入判斷的到期月並排序（`YYYYMM` 的字典序即時間序）"""

        if self.include_weekly:
            return sorted(expiries)

        return sorted(
            expiry
            for expiry in expiries
            if self.MONTHLY_EXPIRY_PATTERN.match(str(expiry))
        )

    def resolve_active_expiry(
        self,
        date: datetime.date,
        expiries: List[str],
        open_interest: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        - Description:
            決定該交易日的當家契約

            三種規則的差別只在「什麼時候放棄近月」：

            | 規則 | 放棄近月的時點 |
            |------|----------------|
            | `LAST_TRADING_DAY` | 最後交易日**收盤後**（當天仍是近月） |
            | `DAYS_BEFORE_EXPIRY` | 最後交易日前 `days_before_expiry` 個交易日 |
            | `OPEN_INTEREST` | 次月的未沖銷契約量**超過**近月當天 |

            **已到期的契約一律不會被選中**：`get_last_trading_date()` 早於查詢日
            的契約直接跳過——資料表偶爾會在結算日之後仍有殘留列。
        - Parameters:
            - date: datetime.date
                交易日
            - expiries: List[str]
                當日掛牌中的到期月
            - open_interest: Optional[Dict[str, Any]]
                `{expiry: 未沖銷契約量}`；`OPEN_INTEREST` 規則必填
        - Return:
            - Optional[str]
                當家契約的到期月；當日沒有可用契約時為 None
        """

        candidates: List[str] = [
            expiry
            for expiry in self.filter_expiries(expiries)
            if self.check_tradable(date, expiry)
        ]
        if not candidates:
            return None

        if self.rule == FuturesRollRule.OPEN_INTEREST:
            return self.resolve_by_open_interest(candidates, open_interest)

        if self.rule == FuturesRollRule.DAYS_BEFORE_EXPIRY:
            return self.resolve_by_days_before_expiry(date, candidates)

        return candidates[0]

    def check_tradable(self, date: datetime.date, expiry: str) -> bool:
        """該契約在當日是否仍未到期（最後交易日當天仍算）"""

        last_trading_date: Optional[datetime.date] = (
            self.calendar.get_last_trading_date(expiry)
        )
        # 算不出最後交易日（代碼異常、或日曆涵蓋不到）時不排除：
        # 寧可用到一個可能已到期的契約，也不要整天沒有序列
        return last_trading_date is None or last_trading_date >= date

    def resolve_by_days_before_expiry(
        self, date: datetime.date, candidates: List[str]
    ) -> str:
        """
        提前 N 個交易日換月

        **用交易日不是曆日**：春節前後「還有 5 天」用曆日算會差一整週，
        換月時點跟著漂掉（見 `FuturesCalendar.get_trading_days_to_expiry()`）。
        """

        for expiry in candidates:
            remaining: Optional[int] = self.calendar.get_trading_days_to_expiry(
                date, expiry
            )
            # 算不出剩餘天數時保守採用該契約（同 `check_tradable()` 的取捨）
            if remaining is None or remaining > self.days_before_expiry:
                return expiry

        # 全部都進入換月區間時取最遠的一個，不可回頭選最近月
        return candidates[-1]

    def resolve_by_open_interest(
        self, candidates: List[str], open_interest: Optional[Dict[str, Any]]
    ) -> str:
        """
        - Description:
            未沖銷契約量交叉換月：次月的未沖銷量超過近月時就換

            **只比較近月與次月**，不是取全場最大：更遠月的未沖銷量在特定時期
            （例如季月）本來就可能較高，取最大會讓序列在遠月與近月之間反覆跳。

            未沖銷量缺漏（夜盤為 NULL）時退回近月——**不可當成 0**，
            那會讓近月的未沖銷量被判定為輸給次月而誤觸換月。
        - Parameters:
            - candidates: List[str]
                當日仍可交易的到期月（已排序）
            - open_interest: Optional[Dict[str, Any]]
                `{expiry: 未沖銷契約量}`
        - Return:
            - str
                當家契約
        """

        near: str = candidates[0]
        if open_interest is None or len(candidates) < 2:
            return near

        next_month: str = candidates[1]
        near_oi: Optional[float] = self.to_open_interest(open_interest.get(near))
        next_oi: Optional[float] = self.to_open_interest(open_interest.get(next_month))

        if near_oi is None or next_oi is None:
            return near

        return next_month if next_oi > near_oi else near

    @staticmethod
    def to_open_interest(value: Any) -> Optional[float]:
        """未沖銷契約量轉 float；NULL／NaN／無法轉換者一律為 None（不是 0）"""

        if value is None:
            return None
        try:
            number: float = float(value)
        except (TypeError, ValueError):
            return None
        return None if number != number else number  # NaN

    def build_roll_schedule(
        self,
        dates: List[datetime.date],
        expiries_by_date: Dict[datetime.date, List[str]],
        open_interest_by_date: Optional[Dict[datetime.date, Dict[str, Any]]] = None,
    ) -> Dict[datetime.date, str]:
        """
        - Description:
            一次算出整段期間每天的當家契約

            **換月只往前不回頭**：規則算出的契約若比昨天的還早到期（例如
            未沖銷量在交叉後又反轉），一律沿用昨天那個。真實轉倉是實際的
            買賣行為，不可能「換回去」，序列若來回跳會憑空生出價差。
        - Parameters:
            - dates: List[datetime.date]
                已排序的交易日
            - expiries_by_date: Dict[datetime.date, List[str]]
                每天掛牌中的到期月
            - open_interest_by_date: Optional[Dict[datetime.date, Dict[str, Any]]]
                每天的 `{expiry: 未沖銷契約量}`；`OPEN_INTEREST` 規則必填
        - Return:
            - Dict[datetime.date, str]
                `{交易日: 當家契約}`；當天無可用契約者不列入
        """

        schedule: Dict[datetime.date, str] = {}
        previous: Optional[str] = None

        for date in dates:
            expiries: List[str] = expiries_by_date.get(date, [])
            if not expiries:
                continue

            open_interest: Optional[Dict[str, Any]] = (
                open_interest_by_date.get(date) if open_interest_by_date else None
            )
            active: Optional[str] = self.resolve_active_expiry(
                date, expiries, open_interest
            )
            if active is None:
                continue

            if previous is not None and active < previous:
                # 規則想換回更近的月份：沿用昨天那個（見 docstring）
                active = previous if previous in expiries else active

            schedule[date] = active
            previous = active

        if not schedule:
            logger.warning("[Futures Roll] 整段期間都找不到可用契約，換月表為空")

        return schedule


@dataclass
class FuturesRollConfig:
    """
    回測的換月設定：**策略挑合約與轉倉部位用同一組規則**

    兩處不一致的後果很具體：訊號在次月產生、部位卻還留在近月，
    或是反過來——而且不會有任何錯誤訊息，只會讓績效莫名其妙地差一截。

    `calendar` 由 `TwFuturesDataFeed.setup()` 注入（與保證金 API 同一種接法）：
    策略、結算模型與 DataFeed 共用同一個設定物件，注入一次三邊都看得到。

    **`enabled=False` 的語意是「不自動轉倉」**，不是「不換月」：策略照樣可以
    自己決定要交易哪個契約，只是留倉的部位不會被結算模型轉到次月——
    那些部位最後會走 `TwFuturesSettlementModel` 的到期權宜出場。
    """

    rule: FuturesRollRule = FuturesRollRule.LAST_TRADING_DAY
    days_before_expiry: int = 1
    enabled: bool = True  # 是否在換月時自動把未平倉部位轉到次月
    calendar: Optional[FuturesCalendar] = None  # 由 DataFeed 注入

    def build_planner(self) -> Optional[FuturesRollPlanner]:
        """建立換月規劃器；尚未注入日曆時回傳 None（呼叫端據此跳過換月）"""

        if self.calendar is None:
            return None

        return FuturesRollPlanner(
            self.calendar,
            rule=self.rule,
            days_before_expiry=self.days_before_expiry,
        )
