import datetime
from typing import Any, Dict, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import (
    API_LOG_FILE_LEVEL,
    API_LOGS_DIR_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.utils.log_manager import LogManager

"""Stock margin trading API: query margin table through StockMarginDAO（融資融券餘額，單位：張）"""


class StockMarginAPI(BaseDataAPI):
    """Stock margin trading API"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.dao: Optional[StockMarginDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
        self.dao = StockMarginDAO(conn=self.conn)
        LogManager.setup_logger(
            "stock_margin_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的信用交易資料"""

        return self.dao.get_by_date(date)

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票日期範圍內的信用交易資料"""

        return self.dao.get_range(start_date, end_date)

    def get_stock_margin(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的信用交易資料"""

        return self.dao.get_by_stock(stock_id, start_date, end_date)

    def get_short_balance(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的融券餘額與券資比（券源檢核用）"""

        return self.dao.get_short_balance(date)

    def get_short_balance_map(self, date: datetime.date) -> Dict[str, int]:
        """
        - Description:
            取得單日全市場的融券今日餘額對照表（張），供回測的券源檢核使用

            **`margin` 表不存在時回傳空 dict 而非拋錯**：該表的歷史回補是獨立作業，
            尚未執行時回測仍應能跑，由呼叫端（`FillModel`）以 warning 表明
            「查無資料，本次跳過檢核」，而不是靜默地把所有標的都當成借不到券。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - Dict[str, int]
                `{stock_id: 融券今日餘額（張）}`；查無資料時為空 dict
        """

        if not self.dao.table_exists():
            return {}

        df: pd.DataFrame = self.get_short_balance(date)
        balance_map: Dict[str, Any] = self.build_column_map(df, "融券今日餘額")

        result: Dict[str, int] = {}
        for stock_id, balance in balance_map.items():
            try:
                result[stock_id] = int(balance)
            except (TypeError, ValueError):
                continue

        return result

    def get_stock_short_balance(
        self,
        stock_id: str,
        date: datetime.date,
    ) -> Optional[int]:
        """
        - Description:
            取得指定個股在指定日期的融券今日餘額（單位：張），供回測開倉前的券源檢核使用

        - Parameters:
            - stock_id: str
                股票代號
            - date: datetime.date
                查詢日期

        - Return:
            - Optional[int]
                融券今日餘額（張）；查無資料時回傳 None（呼叫端須自行決定是否跳過檢核）
        """

        df: pd.DataFrame = self.dao.get_stock_short_balance(stock_id, date)

        if df.empty:
            return None
        return int(df.iloc[0, 0])
