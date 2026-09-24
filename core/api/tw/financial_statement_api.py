from pathlib import Path
from typing import Dict, Set

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.tw.financial_statement_dao import FinancialStatementDAO

"""
Financial Statement Data API: query financial statement tables through FinancialStatementDAO

**表名由呼叫端傳入**（四張報表共用同一組查詢），白名單由 `FinancialStatementDAO`
把關：傳錯表名在當下就報錯，而不是回一張空表。
"""


class FinancialStatementAPI(BaseDataAPI):
    """Financial Statement Data API"""

    # 允許查詢的資料表（與 DAO 的白名單同一份）
    ALLOWED_TABLES: Set[str] = set(FinancialStatementDAO.ALLOWED_TABLES)

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    LOG_FILE_NAME: str = "financial_statement_api.log"

    def setup(self) -> None:
        """建連線與主 DAO 由基底負責；此處只補本 API 多出來的 DAO"""

        super().setup()
        # 依報表類型延遲建立，故在此只備空 dict
        self.daos: Dict[str, FinancialStatementDAO] = {}

    @classmethod
    def check_table_name(cls, table_name: str) -> str:
        """
        - Description:
            確認表名在白名單內；不在就當場拋出
        - Parameters:
            - table_name: str
                呼叫端傳入的資料表名稱
        - Return:
            - str
                原樣回傳
        - Raise:
            - ValueError
                表名不在 `ALLOWED_TABLES` 內
        """

        return FinancialStatementDAO.check_table_name(table_name)

    def get_dao(self, table_name: str) -> FinancialStatementDAO:
        """取得指定財報表的 DAO（共用本 API 的連線，依表名快取）"""

        if table_name not in self.daos:
            self.daos[table_name] = FinancialStatementDAO(table_name, conn=self.conn)
        return self.daos[table_name]

    def get(
        self,
        table_name: str,
        year: int,
        season: int,
    ) -> pd.DataFrame:
        """取得指定年度跟季度的財報"""

        return self.get_dao(table_name).get_by_year_season(year, season)

    def get_range(
        self,
        table_name: str,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> pd.DataFrame:
        """
        - Description:
            取得 `start_year Q start_season` 到 `end_year Q end_season`（兩端皆含）的財報

            參數順序為「先年後季」。區間以**連續年季**比對，**不是年、季各自
            `BETWEEN`**——後者在跨年區間（例如 2023Q4～2024Q1）一筆都查不到。
        - Parameters:
            - table_name: str
                財報表名稱
            - start_year / end_year: int
                起始與結束年度
            - start_season / end_season: int
                起始年度的起始季、結束年度的結束季
        - Return:
            - pd.DataFrame
                區間內的財報；起點晚於終點時為空表
        """

        return self.get_dao(table_name).get_range(
            start_year, start_season, end_year, end_season
        )
