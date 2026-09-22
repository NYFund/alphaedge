import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import (
    TW_STOCK_DB_PATH,
)
from core.dao.base import BaseDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO

"""Stock margin trading API: query margin table through StockMarginDAO（融資融券餘額，單位：張）"""


class StockMarginAPI(BaseDataAPI):
    """Stock margin trading API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = StockMarginDAO
    LOG_FILE_NAME: str = "stock_margin_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢

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
