import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import pandas as pd
from loguru import logger

from core.config import (
    BALANCE_SHEET_TABLE_NAME,
    CASH_FLOW_TABLE_NAME,
    COMPREHENSIVE_INCOME_TABLE_NAME,
    EQUITY_CHANGE_TABLE_NAME,
    TW_STOCK_DB_PATH,
)
from core.dao.base import BaseDAO

"""
台股財報四表（資產負債表、綜合損益表、現金流量表、權益變動表）的資料存取

四張表共用同一組查詢，**表名由建構子傳入，一律走白名單**：表名不能參數化、
只能拼進 SQL，拼字串的地方就是注入的入口。四張表是封閉集合，白名單既擋得住，
也讓「傳錯表名」在當下就報錯，而不是回一張空表。
"""


class FinancialStatementDAO(BaseDAO):
    """
    - Description:
        單一財報表的建表、寫入與查詢

        **欄位清單由呼叫端傳入**：欄位定義存在清洗器產出的 `*_cleaned_columns.json`，
        讀檔屬於 pipeline 的事；DAO 只負責把欄名對應成 SQL 型別。
    """

    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # 允許存取的資料表；表名不可參數化，只能以白名單擋下非預期的值
    ALLOWED_TABLES: Tuple[str, ...] = (
        BALANCE_SHEET_TABLE_NAME,
        COMPREHENSIVE_INCOME_TABLE_NAME,
        CASH_FLOW_TABLE_NAME,
        EQUITY_CHANGE_TABLE_NAME,
    )

    # 各報表的主鍵。其他三張是「一家公司一列」，權益變動表攤平成長表後
    # 一家公司一季有數十列，必須把攤平出來的兩個維度一起納入主鍵才唯一；
    # 且來源端點（逐檔查詢）不回傳公司名稱，故該表不含 `公司名稱`
    PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
        BALANCE_SHEET_TABLE_NAME: ("year", "season", "stock_id", "公司名稱"),
        COMPREHENSIVE_INCOME_TABLE_NAME: ("year", "season", "stock_id", "公司名稱"),
        CASH_FLOW_TABLE_NAME: ("year", "season", "stock_id", "公司名稱"),
        EQUITY_CHANGE_TABLE_NAME: (
            "year",
            "season",
            "stock_id",
            "權益項目",
            "變動原因",
        ),
    }

    TEXT_NOT_NULL_COLUMNS: Tuple[str, ...] = (
        "date",
        "stock_id",
        "公司名稱",
        "權益項目",
        "變動原因",
    )
    INT_NOT_NULL_COLUMNS: Tuple[str, ...] = ("year", "season")

    def __init__(
        self,
        table_name: str,
        conn: Optional[sqlite3.Connection] = None,
        db_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        - Description:
            建立指定財報表的 DAO
        - Parameters:
            - table_name: str
                財報表名稱；必須在 `ALLOWED_TABLES` 內
            - conn: Optional[sqlite3.Connection]
                共用連線；指定時本 DAO 不擁有它
            - db_path: Optional[Union[str, Path]]
                自行開連線時的資料庫路徑；None 取 `DEFAULT_DB_PATH`
        - Raise:
            - ValueError
                表名不在白名單內（在開連線之前就擋下）
        """

        self.TABLE_NAME: str = self.check_table_name(table_name)
        super().__init__(conn=conn, db_path=db_path)

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
                原樣回傳，供直接拼進 SQL
        - Raise:
            - ValueError
                表名不在 `ALLOWED_TABLES` 內
        """

        if table_name not in cls.ALLOWED_TABLES:
            raise ValueError(
                f"不支援的財報資料表：{table_name!r}；"
                f"可用的有 {sorted(cls.ALLOWED_TABLES)}"
            )
        return table_name

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
            依欄位清單建表並 commit：鍵欄為 NOT NULL，其餘數值欄一律 REAL
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

        pk_sql: str = ", ".join(
            f'"{col}"' for col in self.PRIMARY_KEYS[self.TABLE_NAME]
        )
        col_defs.append(f"PRIMARY KEY ({pk_sql})")

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
    def get_by_year_season(self, year: int, season: int) -> pd.DataFrame:
        """取得指定年季的全市場財報"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE year = ? AND season = ?
            """,
            (year, season),
        )

    def get_range(
        self,
        start_year: int,
        start_season: int,
        end_year: int,
        end_season: int,
    ) -> pd.DataFrame:
        """
        - Description:
            取得 `start_year Q start_season` 到 `end_year Q end_season`（兩端皆含）的財報

            **以 `year * 10 + season` 比較，不可年、季各自 `BETWEEN`**：後者在跨年時
            查錯——2023Q4～2024Q1 會變成「季別介於 4 與 1 之間」，一筆都查不到。
        - Parameters:
            - start_year / start_season: int
                起始年季（含）
            - end_year / end_season: int
                結束年季（含）
        - Return:
            - pd.DataFrame
                區間內的財報；起點晚於終點時為空表
        """

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE year * 10 + season BETWEEN ? AND ?
            """,
            (start_year * 10 + start_season, end_year * 10 + end_season),
        )

    def get_latest_year_season(self) -> Optional[Tuple[int, int]]:
        """
        - Description:
            表內最新的（年, 季）；表不存在或為空時為 None，查詢錯誤往外拋
        - Return:
            - Optional[Tuple[int, int]]
                最新的（year, season）
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"""
            SELECT year, season FROM {self.TABLE_NAME}
            ORDER BY CAST(year AS INTEGER) DESC, CAST(season AS INTEGER) DESC
            LIMIT 1
            """
        )
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    def get_existing_year_seasons(self) -> Set[Tuple[int, int]]:
        """表內已有的 (year, season)；表不存在時為空集合（初次更新的正常狀態）"""

        if not self.table_exists():
            return set()

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT DISTINCT year, season FROM {self.TABLE_NAME}"
        ).fetchall()
        return {(int(year), int(season)) for year, season in rows}

    def get_stock_ids(self, year: int, season: int) -> Set[str]:
        """
        - Description:
            取得指定年季已入庫的 stock_id；表不存在時為空集合

            **查詢錯誤往外拋**：逐檔 resume 以這個集合為「已完成」，吞掉錯誤回空集合
            會讓整季被當成「一檔都還沒爬」，重打兩千多次請求。
        - Parameters:
            - year / season: int
                年季
        - Return:
            - Set[str]
                已入庫的股票代號
        """

        if not self.table_exists():
            return set()

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT stock_id FROM {self.TABLE_NAME}
            WHERE year = ? AND season = ?
            """,
            (year, season),
        )
        return set(df["stock_id"].astype(str))
