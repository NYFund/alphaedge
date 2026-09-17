from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import MONTHLY_REVENUE_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股月營收（`monthly_revenue` 表）的資料存取"""


class MonthlyRevenueDAO(BaseDAO):
    """
    - Description:
        月營收表的建表、寫入與查詢

        **欄位清單由呼叫端傳入**：月營收的欄位定義存在清洗器產出的 JSON
        （`*_cleaned_columns.json`），讀檔屬於 pipeline 的事；DAO 只負責把欄名
        對應成 SQL 型別。
    """

    TABLE_NAME: str = MONTHLY_REVENUE_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("year", "month", "stock_id", "公司名稱")
    TEXT_NOT_NULL_COLUMNS: Tuple[str, ...] = ("stock_id", "公司名稱")
    INT_NOT_NULL_COLUMNS: Tuple[str, ...] = ("year", "month")

    # === 建表 ===
    def ensure_table(self, columns: List[str]) -> None:
        """
        - Description:
            確保資料表存在；可重複呼叫
        - Parameters:
            - columns: List[str]
                建表時的欄位清單（表已存在時不使用）
        """

        if not self.table_exists():
            self.create_table(columns)

    def create_table(self, columns: List[str]) -> None:
        """
        - Description:
            依欄位清單建立月營收表並 commit：主鍵欄為 NOT NULL，其餘數值欄一律 REAL
        - Parameters:
            - columns: List[str]
                欄位清單（清洗後的欄名）
        """

        col_defs: List[str] = []
        for col in columns:
            col_name: str = f'"{col}"'

            if col in self.TEXT_NOT_NULL_COLUMNS:
                col_defs.append(f"{col_name} TEXT NOT NULL")
            elif col in self.INT_NOT_NULL_COLUMNS:
                col_defs.append(f"{col_name} INT NOT NULL")
            else:
                col_defs.append(f"{col_name} REAL")

        primary_key: str = ", ".join(f'"{col}"' for col in self.PRIMARY_KEY_COLUMNS)
        col_defs.append(f"PRIMARY KEY ({primary_key})")

        col_defs_sql: str = ",\n            ".join(col_defs)
        create_table_query: str = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
            {col_defs_sql}
        )
        """
        self.conn.execute(create_table_query)
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
            logger.info(create_table_query)
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
    def get_by_year_month(self, year: int, month: int) -> pd.DataFrame:
        """取得指定年月的全市場月營收"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE year = ?
            AND month = ?
            """,
            (year, month),
        )

    def get_range(
        self,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
    ) -> pd.DataFrame:
        """
        - Description:
            取得 `start_year/start_month` 到 `end_year/end_month`（兩端皆含）的月營收

            **以 `year * 100 + month` 比較，不可年、月各自 `BETWEEN`**：後者在跨年時
            查錯——2023-11～2024-02 會變成「月份介於 11 與 2 之間」，一筆都查不到；
            2023-02～2024-11 則會漏掉兩年的 1 月與 12 月。
        - Parameters:
            - start_year / start_month: int
                起始年月（含）
            - end_year / end_month: int
                結束年月（含）
        - Return:
            - pd.DataFrame
                區間內的月營收；起點晚於終點時為空表
        """

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE year * 100 + month BETWEEN ? AND ?
            """,
            (start_year * 100 + start_month, end_year * 100 + end_month),
        )

    def get_latest_year_month(self) -> Optional[Tuple[int, int]]:
        """
        - Description:
            表內最新的（年, 月）；表不存在或為空時為 None

            不吞 `sqlite3.Error`：吞掉會讓 updater 把「欄位打錯、DB 損毀」當成
            「表是空的」，從預設起點靜默重跑整段回補。
        - Return:
            - Optional[Tuple[int, int]]
                最新的（year, month）
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"""
            SELECT year, month FROM {self.TABLE_NAME}
            ORDER BY CAST(year AS INTEGER) DESC, CAST(month AS INTEGER) DESC
            LIMIT 1
            """
        )
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    def get_existing_year_months(self) -> Set[Tuple[int, int]]:
        """表內已有的 (year, month)；表不存在時為空集合（初次更新的正常狀態）"""

        if not self.table_exists():
            return set()

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT DISTINCT year, month FROM {self.TABLE_NAME}"
        ).fetchall()
        return {(int(year), int(month)) for year, month in rows}
