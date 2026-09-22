import datetime
from typing import Optional, Set

from core.api.base import BaseDataAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.utils.log_manager import LogManager

"""
Market Holiday API: query the official TWSE holiday schedule in tw_stock.db

**三種答案，`None` 不可當成 True**：

| 回傳 | 意義 |
|------|------|
| `True` | 該年度已公告，這天是平日且不在休市列表上 |
| `False` | 該年度已公告，這天是週末或官方列為休市（含「市場無交易，僅辦理結算交割作業」） |
| `None` | 該年度尚未入庫——**不知道**，不是「不是假日」 |

「年度沒入庫」與「這天不是假日」在表裡長得一樣（都查不到列），
所以一律先問 `get_covered_years()` 再下結論。
"""


class MarketHolidayAPI(BaseDataAPI):
    """Market Holiday API"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None
        self.dao: Optional[MarketHolidayDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
        self.dao = MarketHolidayDAO(conn=self.conn)
        LogManager.setup_logger(
            "market_holiday_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get_covered_years(self) -> Set[int]:
        """已入庫（官方已公告）的年度；表不存在時為空集合"""

        return self.dao.get_covered_years()

    def get_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """
        - Description:
            區間內的官方休市日（不含週末本身，只含表上列出者）

            **只回答已涵蓋的年度**：區間跨到未入庫的年度時，那一段不會有任何休市日，
            呼叫端需要區分時請搭配 `get_covered_years()`。
        - Parameters:
            - start_date: datetime.date
                起日（含）
            - end_date: datetime.date
                迄日（含）
        - Return:
            - Set[datetime.date]
                休市日
        """

        return self.dao.get_closures(start_date, end_date)

    def is_closure(self, date: datetime.date) -> Optional[bool]:
        """
        - Description:
            該日是否為官方列出的休市日（週末不另外判斷）
        - Parameters:
            - date: datetime.date
                查詢日
        - Return:
            - Optional[bool]
                列為休市為 True、未列為 False；該年度未入庫為 None
        """

        if date.year not in self.get_covered_years():
            return None
        return date in self.get_closures(date, date)

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """
        - Description:
            該日是否為交易日

            年度未入庫回 None——**週末也一樣**：本來源只替自己涵蓋的年度作答，
            週末另有 `WeekendCalendarSource` 負責，不必在這裡越界回答。
            已涵蓋的年度內，週末與列為休市的日子為 False、其餘平日為 True。
        - Parameters:
            - date: datetime.date
                查詢日
        - Return:
            - Optional[bool]
                交易日為 True；休市為 False；判斷不出來為 None
        """

        closure: Optional[bool] = self.is_closure(date)
        if closure is None:
            return None
        return date.weekday() < 5 and not closure
