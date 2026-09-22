import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import MARGIN_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO, create_symbol_date_index

"""台股信用交易（`margin` 表，融資融券餘額）的資料存取"""


class StockMarginDAO(BaseDAO):
    """`margin` 表的建表、寫入與查詢（數量單位：張；券資比單位：%）"""

    TABLE_NAME: str = MARGIN_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表與 `(stock_id, date)` 索引存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

        create_symbol_date_index(self.conn, self.TABLE_NAME)

    def create_table(self) -> None:
        """建立 `margin` 表並 commit"""

        # 券資比 = 融券今日餘額 / 融資今日餘額
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "融資買進" INT NOT NULL,
                "融資賣出" INT NOT NULL,
                "融資現金償還" INT NOT NULL,
                "融資前日餘額" INT NOT NULL,
                "融資今日餘額" INT NOT NULL,
                "融資限額" INT NOT NULL,
                "融券買進" INT NOT NULL,
                "融券賣出" INT NOT NULL,
                "融券現券償還" INT NOT NULL,
                "融券前日餘額" INT NOT NULL,
                "融券今日餘額" INT NOT NULL,
                "融券限額" INT NOT NULL,
                "資券互抵" INT NOT NULL,
                "券資比" REAL NOT NULL,
                "註記" TEXT,
                PRIMARY KEY ("date", "stock_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
    def get_by_date(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的信用交易資料"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE date = ?
            """,
            (date,),
        )

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票日期範圍內的信用交易資料；`start_date > end_date` 時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )

    def get_by_stock(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股在區間內的信用交易資料；`start_date > end_date` 時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE stock_id = ?
            AND date BETWEEN ? AND ?
            """,
            (stock_id, start_date, end_date),
        )

    def get_short_balance(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的融券餘額與券資比（券源檢核用）"""

        return self.query_df(
            f"""
            SELECT date, stock_id, 證券名稱, 融券今日餘額, 融券限額, 券資比, 註記
            FROM {self.TABLE_NAME}
            WHERE date = ?
            """,
            (date,),
        )

    def get_stock_short_balance(
        self, stock_id: str, date: datetime.date
    ) -> pd.DataFrame:
        """取得指定個股在指定日期的融券今日餘額（單欄；查無資料時為空表）"""

        return self.query_df(
            f"""
            SELECT 融券今日餘額 FROM {self.TABLE_NAME}
            WHERE stock_id = ?
            AND date = ?
            """,
            (stock_id, date),
        )

    def get_latest_date(self) -> Optional[Any]:
        """表內最新的日期（`YYYY-MM-DD` 字串）；表不存在或為空時為 None"""

        return self._get_latest_value("date")
