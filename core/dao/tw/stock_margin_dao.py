import datetime
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import MARGIN_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股信用交易（`margin` 表，融資融券餘額）的資料存取"""


class StockMarginDAO(BaseDAO):
    """`margin` 表的建表、寫入與查詢（數量單位：張；券資比單位：%）"""

    TABLE_NAME: str = MARGIN_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
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
