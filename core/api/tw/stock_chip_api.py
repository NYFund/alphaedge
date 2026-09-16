import datetime
import sqlite3
from typing import Any, Dict, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import (
    API_LOG_FILE_LEVEL,
    API_LOGS_DIR_PATH,
    CHIP_TABLE_NAME,
    TW_STOCK_DB_PATH,
)
from core.config.schema import ChipColumn
from core.utils.log_manager import LogManager

"""Institutional investors chip API: query SQLite chip table"""


class StockChipAPI(BaseDataAPI):
    """Institutional investors chip API"""

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[sqlite3.Connection] = conn
        self.owns_conn: bool = conn is None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = sqlite3.connect(TW_STOCK_DB_PATH)
        LogManager.setup_logger(
            "stock_chip_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的三大法人籌碼"""

        query: str = f"""
        SELECT * FROM {CHIP_TABLE_NAME}
        WHERE date = ?
        """
        df: pd.DataFrame = pd.read_sql_query(
            query,
            self.conn,
            params=self.sql_params(
                date,
            ),
        )
        return df

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票日期範圍內的三大法人籌碼"""

        if start_date > end_date:
            return pd.DataFrame()

        query: str = f"""
        SELECT * FROM {CHIP_TABLE_NAME}
        WHERE date BETWEEN ? AND ?
        """
        df: pd.DataFrame = pd.read_sql_query(
            query,
            self.conn,
            params=self.sql_params(start_date, end_date),
        )
        return df

    def get_stock_chip(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的三大法人籌碼"""

        if start_date > end_date:
            return pd.DataFrame()

        query: str = f"""
        SELECT * FROM {CHIP_TABLE_NAME}
        WHERE stock_id = ?
        AND date BETWEEN ? AND ?
        """
        df: pd.DataFrame = pd.read_sql_query(
            query,
            self.conn,
            params=self.sql_params(stock_id, start_date, end_date),
        )
        return df

    # === 具名查詢：策略層一律走這一組，不要自行操作 DataFrame 欄位 ===
    def get_foreign_net_shares_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的外資買賣超股數對照表

            **值為買賣超，賣超是負數**；單位是「股」不是「張」，呼叫端要比較
            張數門檻時須自行乘 `Units.LOT`，不要直接拿張數與本值相比。

            取值細節見 `BaseDataAPI.build_column_map()`；值維持資料庫原樣，
            由呼叫端決定如何轉型與判斷。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - Dict[str, Any]
                {stock_id: 外資買賣超股數}
        """

        df: pd.DataFrame = self.get(date)
        return self.build_column_map(df, ChipColumn.FOREIGN_NET_SHARES.value)

    def get_trust_net_shares_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的投信買賣超股數對照表

            範圍僅限現有策略實際用到的欄位，不預先補齊整張表——沒有呼叫端的
            方法只會變成下一批死碼。

            取值細節見 `BaseDataAPI.build_column_map()`；值維持資料庫原樣，
            由呼叫端決定如何轉型與判斷。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - Dict[str, Any]
                {stock_id: 投信買賣超股數}
        """

        df: pd.DataFrame = self.get(date)
        return self.build_column_map(df, ChipColumn.TRUST_NET_SHARES.value)

    def get_net_chip(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票的三大法人淨買賣超"""

        if start_date > end_date:
            return pd.DataFrame()

        # **舊版寫的是 `self.get(start_date, end_date)`**，而 `get()` 只收一個
        # `date` ——這個公開方法一被呼叫就 `TypeError`。
        # 全專案沒有呼叫端，所以壞了三年也沒人發現，但 API 門面看起來是可用的
        df: pd.DataFrame = self.get_range(start_date, end_date)
        if df.empty:
            return df

        df = df.loc[
            :,
            (
                "date",
                "stock_id",
                "證券名稱",
                "外資買賣超股數",
                "投信買賣超股數",
                "自營商買賣超股數",
            ),
        ]
        return df

    def get_stock_net_chip(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的三大法人淨買賣超"""

        if start_date > end_date:
            return pd.DataFrame()

        df: pd.DataFrame = self.get_stock_chip(stock_id, start_date, end_date)
        df = df.loc[
            :,
            (
                "date",
                "stock_id",
                "證券名稱",
                "外資買賣超股數",
                "投信買賣超股數",
                "自營商買賣超股數",
            ),
        ]
        return df
