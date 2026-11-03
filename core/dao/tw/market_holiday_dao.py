import datetime
import sqlite3
from pathlib import Path
from typing import Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    MARKET_ADHOC_CLOSURE_TABLE_NAME,
    MARKET_HOLIDAY_TABLE_NAME,
    TW_STOCK_DB_PATH,
)
from core.dao.base import BaseDAO, table_exists

"""
市場開休市日期的資料存取：`tw_stock.db` 的 `market_holiday`（官方公告）與
`market_adhoc_closure`（人工登記的臨時休市）兩張表
"""


class MarketHolidayDAO(BaseDAO):
    """
    - Description:
        市場開休市日期表的建表、寫入與查詢

        **「某年有沒有列」就是「該年度是否已涵蓋」**：官方表每年至少有元旦一列，
        所以 `year` 欄有值的年度才能回答「這天不是假日」；沒有列的年度只能回答
        「不知道」。查詢端靠 `get_covered_years()` 分辨這兩件事。

        表不存在（尚未跑過 ETL）時查詢回空集合；被鎖住或 schema 壞掉一律往外拋。

        **臨時休市另存一張表、查休市日時合併**：颱風等臨時休市不在官方公告上，
        而官方表每次更新都整年替換，補登在同一張表的列下一次更新就被洗掉。
        臨時休市表只由人工登記（`scripts/manual/manual_market_closure.py`），
        ETL 不建也不寫；不存在時（從沒登記過、或以唯讀連線開啟）視為沒有臨時休市。
    """

    TABLE_NAME: str = MARKET_HOLIDAY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    # 可缺：官方行事曆只替已入庫的年度作答，缺表＝沒有任何年度
    OPTIONAL_TABLE: bool = True
    # 一天一列，只有日期鍵
    KEY_COLUMNS: Tuple[str, ...] = ()

    # === 建表 ===
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

    def ensure_adhoc_table(self) -> None:
        """建立臨時休市表並 commit；可重複呼叫"""

        # 不帶 `year` 欄：本表不參與「年度是否已涵蓋」的判定——登記一天颱風假
        # 不代表官方已公告那一年的行事曆
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MARKET_ADHOC_CLOSURE_TABLE_NAME}(
                "date" TEXT NOT NULL,
                "reason" TEXT NOT NULL,
                "created_at" TEXT NOT NULL,
                PRIMARY KEY ("date")
            );
            """
        )
        self.conn.commit()

    def adhoc_table_exists(self) -> bool:
        """臨時休市表是否存在"""

        return table_exists(self.conn, MARKET_ADHOC_CLOSURE_TABLE_NAME)

    # === 寫入 ===
    def add_adhoc_closure(self, date: datetime.date, reason: str) -> bool:
        """
        - Description:
            登記一天臨時休市；不 commit。已登記過的日子不覆蓋原因
        - Parameters:
            - date: datetime.date
                休市日
            - reason: str
                原因（例如「颱風停止交易」）
        - Return:
            - bool
                有新寫入為 True；該日已登記過為 False
        """

        cursor: sqlite3.Cursor = self.conn.execute(
            f"""
            INSERT OR IGNORE INTO {MARKET_ADHOC_CLOSURE_TABLE_NAME}
                ("date", "reason", "created_at")
            VALUES (?, ?, ?)
            """,
            (
                date.isoformat(),
                reason,
                # 帶時差：主機時區不是台北，不帶的話事後看不出是哪個時區的幾點登記
                datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        return cursor.rowcount == 1

    def remove_adhoc_closure(self, date: datetime.date) -> bool:
        """撤銷一天臨時休市登記（登記錯日子時用）；不 commit。有刪到為 True"""

        if not self.adhoc_table_exists():
            return False
        cursor: sqlite3.Cursor = self.conn.execute(
            f'DELETE FROM {MARKET_ADHOC_CLOSURE_TABLE_NAME} WHERE "date" = ?',
            (date.isoformat(),),
        )
        return cursor.rowcount == 1

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
            區間內的休市日：官方公告的休市（不含「開始交易日」這類提醒列）
            加上人工登記的臨時休市
        - Parameters:
            - start_date: datetime.date
                起日（含）
            - end_date: datetime.date
                迄日（含）
        - Return:
            - Set[datetime.date]
                休市日；兩張表都不存在時為空集合
        """

        closures: Set[datetime.date] = self.get_adhoc_closures(start_date, end_date)
        if not self.table_exists():
            return closures

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT date FROM {self.TABLE_NAME}
            WHERE is_trading_day = 0 AND date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
        return closures | {
            datetime.date.fromisoformat(str(value)) for value in df["date"]
        }

    def get_adhoc_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """區間內人工登記的臨時休市日；表不存在時為空集合"""

        if not self.adhoc_table_exists():
            return set()

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT date FROM {MARKET_ADHOC_CLOSURE_TABLE_NAME}
            WHERE date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
        return {datetime.date.fromisoformat(str(value)) for value in df["date"]}

    def list_adhoc_closures(self) -> pd.DataFrame:
        """所有人工登記的臨時休市（日期、原因、登記時刻），依日期排序；表不存在時為空表"""

        if not self.adhoc_table_exists():
            return pd.DataFrame(columns=["date", "reason", "created_at"])

        return self.query_df(
            f"""
            SELECT date, reason, created_at FROM {MARKET_ADHOC_CLOSURE_TABLE_NAME}
            ORDER BY date
            """
        )
