import datetime
from pathlib import Path
from typing import Optional, Set

import pandas as pd
from loguru import logger

from core.config import MARKET_HOLIDAY_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""市場開休市日期（`tw_stock.db` 的 `market_holiday` 表）的資料存取"""


class MarketHolidayDAO(BaseDAO):
    """
    - Description:
        市場開休市日期表的建表、寫入與查詢

        **「某年有沒有列」就是「該年度是否已涵蓋」**：官方表每年至少有元旦一列，
        所以 `year` 欄有值的年度才能回答「這天不是假日」；沒有列的年度只能回答
        「不知道」。查詢端靠 `get_covered_years()` 分辨這兩件事。

        表不存在（尚未跑過 ETL）時查詢回空集合；被鎖住或 schema 壞掉一律往外拋。
    """

    TABLE_NAME: str = MARKET_HOLIDAY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

    def create_table(self) -> None:
        """建立開休市日期表並 commit"""

        # `is_trading_day` 為 0／1：表上多數列是休市（0），但「開始交易日」這類
        # 提醒列當天其實有開市（1），兩者都留下而不是只存休市——原文保留才查得回
        # 分類依據。`year` 單獨成欄是為了涵蓋判定與整年替換，不必每次解析日期字串
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "name" TEXT NOT NULL,
                "description" TEXT,
                "is_trading_day" INTEGER NOT NULL,
                "year" INTEGER NOT NULL,
                PRIMARY KEY ("date")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 寫入 ===
    def replace_year(self, year: int, df: pd.DataFrame) -> int:
        """
        - Description:
            以新資料整年替換該年度的列；不 commit

            **先刪後寫而不是 `INSERT OR REPLACE`**：站方若更正公告（刪掉一天或改日期），
            只覆蓋同鍵的寫法會把被刪掉的那天永遠留在表裡，而多一天休市不會有任何錯誤。
        - Parameters:
            - year: int
                西元年
            - df: pd.DataFrame
                該年度清洗後的資料（欄位與資料表一致）
        - Return:
            - int
                寫入的列數
        """

        self.conn.execute(f"DELETE FROM {self.TABLE_NAME} WHERE year = ?", (year,))
        inserted: int
        inserted, _ = self.insert_or_ignore(df)
        return inserted

    # === 查詢 ===
    def get_covered_years(self) -> Set[int]:
        """已入庫（官方已公告）的年度；表不存在時為空集合"""

        if not self.table_exists():
            return set()

        return {
            int(row[0])
            for row in self.conn.execute(
                f"SELECT DISTINCT year FROM {self.TABLE_NAME}"
            ).fetchall()
        }

    def get_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """
        - Description:
            區間內的休市日（不含「開始交易日」這類提醒列）
        - Parameters:
            - start_date: datetime.date
                起日（含）
            - end_date: datetime.date
                迄日（含）
        - Return:
            - Set[datetime.date]
                休市日；表不存在時為空集合
        """

        if not self.table_exists():
            return set()

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT date FROM {self.TABLE_NAME}
            WHERE is_trading_day = 0 AND date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
        return {datetime.date.fromisoformat(str(value)) for value in df["date"]}
