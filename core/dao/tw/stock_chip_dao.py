import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import CHIP_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO, create_symbol_date_index

"""台股三大法人盤後籌碼（`chip` 表）的資料存取"""


class StockChipDAO(BaseDAO):
    """`chip` 表的建表、寫入與查詢（數量單位：股）"""

    TABLE_NAME: str = CHIP_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id", "證券名稱")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表與 `(stock_id, date)` 索引存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

        create_symbol_date_index(self.conn, self.TABLE_NAME)

    def create_table(self) -> None:
        """建立 `chip` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "外資買進股數" INT NOT NULL,
                "外資賣出股數" INT NOT NULL,
                "外資買賣超股數" INT NOT NULL,
                "投信買進股數" INT NOT NULL,
                "投信賣出股數" INT NOT NULL,
                "投信買賣超股數" INT NOT NULL,
                "自營商買進股數(自行買賣)" INT,
                "自營商賣出股數(自行買賣)" INT,
                "自營商買賣超股數(自行買賣)" INT,
                "自營商買進股數(避險)" INT,
                "自營商賣出股數(避險)" INT,
                "自營商買賣超股數(避險)" INT,
                "自營商買進股數" INT,
                "自營商賣出股數" INT,
                "自營商買賣超股數" INT NOT NULL,
                "三大法人買賣超股數" INT NOT NULL,
                PRIMARY KEY ("date", "stock_id", "證券名稱")
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
        """取得所有股票指定日期的三大法人籌碼"""

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
        """取得所有股票日期範圍內的三大法人籌碼；`start_date > end_date` 時回傳空表"""

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
        """取得指定個股在區間內的三大法人籌碼；`start_date > end_date` 時回傳空表"""

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

    def get_latest_date(self) -> Optional[Any]:
        """表內最新的日期（`YYYY-MM-DD` 字串）；表不存在或為空時為 None"""

        return self._get_latest_value("date")
