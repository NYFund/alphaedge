from pathlib import Path
from typing import Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO

"""Monthly Revenue Report Data API: query monthly revenue table through MonthlyRevenueDAO"""


class MonthlyRevenueReportAPI(BaseDataAPI):
    """Monthly Revenue Report Data API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = MonthlyRevenueDAO
    LOG_FILE_NAME: str = "monthly_revenue_report_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢

    def get(
        self,
        year: int,
        month: int,
    ) -> pd.DataFrame:
        """取得指定年度與月份的月營收報表"""

        return self.dao.get_by_year_month(year, month)

    def get_range(
        self,
        start_year: int,
        end_year: int,
        start_month: int,
        end_month: int,
    ) -> pd.DataFrame:
        """
        - Description:
            取得 `start_year/start_month` 到 `end_year/end_month`（兩端皆含）的月營收報表

            參數順序維持既有公開介面（先年後月）。**舊版年、月各自 `BETWEEN`**，
            跨年區間（例如 2023-11～2024-02）一筆都查不到，現已改為連續年月區間。
        - Parameters:
            - start_year / end_year: int
                起始與結束年度
            - start_month / end_month: int
                起始年度的起始月份、結束年度的結束月份
        - Return:
            - pd.DataFrame
                區間內的月營收；起點晚於終點時為空表
        """

        return self.dao.get_range(start_year, start_month, end_year, end_month)
