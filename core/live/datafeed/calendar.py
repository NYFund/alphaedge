import datetime
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Sequence

from loguru import logger

from core.api.tw.market_holiday_api import MarketHolidayAPI

"""
交易日判定：盤前沒有今天的資料，既有的兩種判斷都用不上

`MarketCalendar` 的兩種判斷（查 `price` 表、查當日 tick）都是**事後**的——
「爬得到資料 ⇒ 有開市」。盤前那個訊號根本還不存在，而實盤的每一天都是從盤前開始。

本模組把判斷抽成可疊加的來源，規則只有兩條：

1. **至少要有一個來源給出明確答案**，否則拒絕啟動。「判斷不出來就當成有開市」
   會讓程式在休市日照常跑完整套流程——送單全被退、對帳全是差異、
   然後推播一整天的告警。
2. **來源之間衝突就拒絕啟動**，不投票也不取多數。衝突代表其中一個來源的語意
   與我們以為的不同，那時繼續跑只是在賭。

**主來源是官方開休市日曆**（`OfficialHolidayCalendarSource`，TWSE 公告落地成
`market_holiday` 表，`python -m tasks.update_db --target market_holiday`）。它只替
已入庫的年度作答；年度未入庫時回 `None`，交由其他來源——平日就只剩券商合約檔。

⚠️ **官方日曆與券商合約檔可能衝突**：休市日若券商因系統作業更新了合約檔，
合約檔會說「開市」、官方日曆說「休市」，依第 2 條規則拒絕啟動。這是刻意的：
不偏袒任何一方，衝突時由人判斷。
"""


class TradingCalendarUnavailableError(RuntimeError):
    """無法判定是否為交易日（沒有來源給出答案，或來源互相衝突）"""


class TradingCalendarSource(ABC):
    """交易日來源；判斷不出來時回 `None`，**不要猜**"""

    name: str = "unknown"

    @abstractmethod
    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """
        - Description:
            這一天是不是交易日
        - Parameters:
            - date: datetime.date
                待判定的日期
        - Return:
            - Optional[bool]
                True／False 為明確答案；**`None` 代表本來源判斷不出來**
        """
        pass


class WeekendCalendarSource(TradingCalendarSource):
    """
    週末必定休市

    **只回答得了一半**：週六日回 False，平日回 `None`——平日可能是國定假日，
    而這個來源看不出來。回 True 的話，所有國定假日都會被當成交易日。
    """

    name: str = "weekend"

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """週末回 False，平日回 None"""

        return False if date.weekday() >= 5 else None


class OfficialHolidayCalendarSource(TradingCalendarSource):
    """
    以 TWSE 公告的市場開休市日期判定（主來源）

    唯一事前就答得出「明天休不休市」的來源。**只替已入庫的年度作答**：年度未入庫時
    回 `None`，不把「表上查不到」當成「不是假日」——明年的公告通常 12 月才出來，
    那之前問明年的日期只能說不知道。
    """

    name: str = "official_holiday"

    def __init__(self, holiday_api: MarketHolidayAPI) -> None:
        """
        - Description:
            建立來源
        - Parameters:
            - holiday_api: MarketHolidayAPI
                開休市日期 API（連 `tw_stock.db`，唯讀即可）
        """

        self._api: MarketHolidayAPI = holiday_api

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """已涵蓋年度內：休市（含週末）回 False、其餘回 True；年度未入庫回 None"""

        return self._api.is_trading_day(date)


class BrokerContractCalendarSource(TradingCalendarSource):
    """
    以券商合約檔的更新日期判定

    合約檔每個交易日更新一次，所以「更新日 ＝ 今天」是有開市的**佐證**。

    ⚠️ **這是副作用不是契約**：休市日也可能因為券商的系統作業而更新，
    而且它只答得出「今天」——問明天或上週都回 `None`。故它只當交叉驗證；
    主來源是 `OfficialHolidayCalendarSource`，官方日曆的年度未入庫時才由它單獨作答。
    """

    name: str = "broker_contract"

    def __init__(self, contract_update_date_provider: Any) -> None:
        """
        - Description:
            建立來源
        - Parameters:
            - contract_update_date_provider: Any
                無參數的 callable，回傳合約檔的更新日期（取不到時回 None）
        """

        self._provider: Any = contract_update_date_provider

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """只答得出「今天」；其餘一律 None"""

        update_date: Optional[datetime.date] = self._provider()
        if update_date is None:
            return None
        return True if update_date == date else None


class PriceTableCalendarSource(TradingCalendarSource):
    """
    以 `price` 表有無資料判定

    **只對過去有效**：今天的資料要到收盤後才會進來，所以問今天一律 `None`。
    它的用途是檢查「資料有沒有補到前一個交易日」。
    """

    name: str = "price_table"

    def __init__(self, has_data: Any) -> None:
        """
        - Description:
            建立來源
        - Parameters:
            - has_data: Any
                `(date) -> bool`，該日 `price` 表有無資料
        """

        self._has_data: Any = has_data

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """有資料 ⇒ 有開市；沒資料時**不下結論**（可能只是還沒爬）"""

        return True if self._has_data(date) else None


def resolve_trading_day(
    date: datetime.date, sources: Sequence[TradingCalendarSource]
) -> bool:
    """
    - Description:
        綜合所有來源判定是否為交易日
    - Parameters:
        - date: datetime.date
            待判定的日期
        - sources: Sequence[TradingCalendarSource]
            交易日來源
    - Return:
        - bool
            是否為交易日
    - Raise:
        - TradingCalendarUnavailableError
            沒有來源給出答案，或來源互相衝突
    """

    answers: List[tuple] = [
        (source.name, source.is_trading_day(date)) for source in sources
    ]
    definite: List[tuple] = [
        (name, value) for name, value in answers if value is not None
    ]

    if not definite:
        raise TradingCalendarUnavailableError(
            f"沒有任何來源判定得出 {date} 是否為交易日（已詢問 "
            f"{[name for name, _ in answers]}）。**不預設為開市**——"
            "休市日照常跑完整套流程會送單被退、對帳全是差異，然後推播一整天的告警。"
            "請執行 `python -m tasks.update_db --target market_holiday` 補上官方"
            "開休市日曆，或確認券商合約檔取得正常"
        )

    values: set = {value for _, value in definite}
    if len(values) > 1:
        raise TradingCalendarUnavailableError(
            f"交易日來源對 {date} 的判定互相衝突：{definite}。"
            "不投票也不取多數——衝突代表其中一個來源的語意與我們以為的不同"
        )

    result: bool = definite[0][1]
    logger.debug(f"{date} 交易日判定：{result}（依據 {definite}）")
    return result
