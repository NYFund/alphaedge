import datetime
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import STOCK_TRADING_DAILY_REPORT_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""當日券商分點統計（FinMind `taiwan_stock_trading_daily_report_secid_agg` 表）的資料存取"""


class BrokerTradingDAO(BaseDAO):
    """
    - Description:
        券商分點統計表的建表、寫入與查詢

        主鍵是 `(stock_id, date, securities_trader_id)` 三欄複合鍵的時間序列；
        寫入走底座的 `insert_or_ignore()`（**不用 `DataFrame.to_sql`**：pandas 的
        `to_sql` 寫完會自行 commit，批次更新傳的 `commit=False` 因此形同虛設）。
    """

    TABLE_NAME: str = STOCK_TRADING_DAILY_REPORT_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("stock_id", "date", "securities_trader_id")

    # 欄位順序與 crawler schema 註解一致
    COLUMN_ORDER: Tuple[str, ...] = (
        "securities_trader",
        "securities_trader_id",
        "stock_id",
        "date",
        "buy_volume",
        "sell_volume",
        "buy_price",
        "sell_price",
    )

    # === 建表 ===
    def ensure_table(self) -> None:
        """
        - Description:
            確保資料表與 metadata 查詢用索引存在；可重複呼叫

            索引每次都建（`IF NOT EXISTS`）：`(securities_trader_id, stock_id, date)` 是
            metadata 重建時 `GROUP BY` 用的，少了它每批更新都會卡在全表掃描。
        """

        if not self.table_exists():
            self.create_table()

        self.conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_broker_trading_secid_stock_date "
            f"ON {self.TABLE_NAME} (securities_trader_id, stock_id, date)"
        )
        self.conn.commit()

    def create_table(self) -> None:
        """建立券商分點統計表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "securities_trader" TEXT,
                "securities_trader_id" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "date" TEXT NOT NULL,
                "buy_volume" INTEGER,
                "sell_volume" INTEGER,
                "buy_price" REAL,
                "sell_price" REAL,
                PRIMARY KEY ("stock_id", "date", "securities_trader_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
    def get_by_stock_and_date(self, stock_id: str, date: datetime.date) -> pd.DataFrame:
        """取得指定股票在指定日期的券商分點日報"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE stock_id = ? AND date = ?
            """,
            (stock_id, date),
        )

    def get_by_stock_in_range(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定股票在日期區間內的券商分點日報；`start_date > end_date` 時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE stock_id = ? AND date BETWEEN ? AND ?
            """,
            (stock_id, start_date, end_date),
        )

    def get_by_trader_name_and_date(
        self, securities_trader: str, date: datetime.date
    ) -> pd.DataFrame:
        """依券商中文名稱與日期取得該券商當日所有股票的分點日報"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE securities_trader = ? AND date = ?
            """,
            (securities_trader, date),
        )

    def get_latest_rows(self, limit: int) -> pd.DataFrame:
        """最新的幾列（依日期由新到舊，人工抽查用）"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            ORDER BY date DESC, stock_id, securities_trader_id
            LIMIT ?
            """,
            (limit,),
        )

    def get_date_ranges_by_trader_stock(self) -> pd.DataFrame:
        """
        - Description:
            每個 `(securities_trader_id, stock_id)` 組合在表內的最早與最晚日期

            給券商分點的 resume metadata 重建用；走 `(securities_trader_id, stock_id, date)` 索引。
        - Return:
            - pd.DataFrame
                欄位 `securities_trader_id`／`stock_id`／`earliest_date`／`latest_date`
        """

        return self.query_df(
            f"""
            SELECT
                securities_trader_id,
                stock_id,
                MIN(date) AS earliest_date,
                MAX(date) AS latest_date
            FROM {self.TABLE_NAME}
            GROUP BY securities_trader_id, stock_id
            ORDER BY securities_trader_id, stock_id
            """
        )
