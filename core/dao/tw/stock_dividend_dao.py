import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import DIVIDEND_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股除權除息計算結果（`dividend` 表）的資料存取"""


class StockDividendDAO(BaseDAO):
    """
    - Description:
        `dividend` 表的建表、寫入與查詢

        價格單位為元；還原係數 = 除權息參考價 / 除權息前收盤價（恆 < 1）；
        現金股利單位為元／股；配股率為每股配股數（純除息時為 0）。
    """

    TABLE_NAME: str = DIVIDEND_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
    def create_table(self) -> None:
        """建立 `dividend` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT,
                "除權息前收盤價" REAL NOT NULL,
                "除權息參考價" REAL NOT NULL,
                "權息值合計" REAL,
                "權息別" TEXT,
                "現金股利" REAL,
                "配股率" REAL,
                "漲停價" REAL,
                "跌停價" REAL,
                "開盤競價基準" REAL,
                "減除股利參考價" REAL,
                "還原係數" REAL NOT NULL,
                "資料來源" TEXT NOT NULL,
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
    def get_by_stock(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股在區間內的除權除息資料（依日期排序）；區間顛倒時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE stock_id = ?
            AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            (stock_id, start_date, end_date),
        )

    def get_ex_dividend_dates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """取得日期範圍內所有出現除權息的交易日（已排序、去重）"""

        return self.get_distinct_dates(start_date, end_date)

    def get_adjust_factors(self) -> pd.DataFrame:
        """取得全期間的單次還原係數（`date`／`stock_id`／`還原係數` 三欄，未排序）"""

        return self.query_df(f"SELECT date, stock_id, 還原係數 FROM {self.TABLE_NAME}")

    def get_event_keys(self) -> pd.DataFrame:
        """取得全期間的除權息事件鍵（`date`／`stock_id` 兩欄，未排序）"""

        return self.query_df(f"SELECT date, stock_id FROM {self.TABLE_NAME}")
