import datetime
from pathlib import Path
from typing import Optional, Set, Type

from core.api.base import BaseDataAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.market_holiday_dao import MarketHolidayDAO

"""
Market Holiday API：查詢 `tw_stock.db` 中 TWSE 官方公告的開休市日期，以及人工登記的臨時休市

**三種答案，`None` 不可當成 True**：

| 回傳 | 意義 |
|------|------|
| `True` | 該年度已公告，這天是平日、不在休市列表上，也沒有登記臨時休市 |
| `False` | 該年度已公告且這天是週末或官方列為休市（含「市場無交易，僅辦理結算交割作業」）；或這天登記了臨時休市 |
| `None` | 該年度尚未入庫、也沒有登記臨時休市——**不知道**，不是「不是假日」 |

「年度沒入庫」與「這天不是假日」在表裡長得一樣（都查不到列），
所以一律先問 `get_covered_years()` 再下結論。臨時休市是確定的事實，
登記了就答休市，不受年度是否入庫影響。
"""


class MarketHolidayAPI(BaseDataAPI):
    """市場開休市日期 API（官方公告的事前行事曆）"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = MarketHolidayDAO
    LOG_FILE_NAME: str = "market_holiday_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢

    def get_covered_years(self) -> Set[int]:
        """已入庫（官方已公告）的年度；表不存在時為空集合"""

        return self.dao.get_covered_years()

    def get_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """
        - Description:
            區間內的休市日（不含週末本身）：官方表上列出者，加上人工登記的臨時休市

            **官方休市只回答已涵蓋的年度**：區間跨到未入庫的年度時，那一段只會有
            登記過的臨時休市，呼叫端需要區分時請搭配 `get_covered_years()`。
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
            該日是否為休市日：官方列出或登記了臨時休市（週末不另外判斷）
        - Parameters:
            - date: datetime.date
                查詢日
        - Return:
            - Optional[bool]
                列為休市或登記臨時休市為 True、未列為 False；該年度未入庫且未登記為 None
        """

        if date in self.get_closures(date, date):
            return True
        if date.year not in self.get_covered_years():
            return None
        return False

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        """
        - Description:
            該日是否為交易日

            年度未入庫回 None——**週末也一樣**：本來源只替自己涵蓋的年度作答，
            週末另有 `WeekendCalendarSource` 負責，不必在這裡越界回答。
            已涵蓋的年度內，週末與列為休市的日子為 False、其餘平日為 True；
            登記了臨時休市的日子不論年度是否入庫都是 False。
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
