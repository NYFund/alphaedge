import datetime
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import PRICE_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO, create_symbol_date_index, to_sql_params

"""台股日 K（`price` 表）的資料存取"""


class StockPriceDAO(BaseDAO):
    """
    - Description:
        `price` 表的建表、寫入與查詢

        `price` 表同時是**台股交易日曆的來源**：當日有日 K 即為開盤日。
        回測（`MarketCalendar`）、ETL 的日期規劃與公司行動偵測都讀這一份，
        交易日的判準因此只有一個實作。
    """

    TABLE_NAME: str = PRICE_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # 主鍵含證券名稱：同一天同一檔若更名，來源會出現兩列，兩列都要留下
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id", "證券名稱")

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表與 `(stock_id, date)` 索引存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

        create_symbol_date_index(self.conn, self.TABLE_NAME)

    def create_table(self) -> None:
        """建立 `price` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "開盤價" REAL,
                "最高價" REAL,
                "最低價" REAL,
                "收盤價" REAL,
                "漲跌價差" REAL,
                "成交股數" INTEGER,
                "成交金額" INTEGER,
                "成交筆數" INTEGER,
                "最後揭示買價" REAL,
                "最後揭示買量" INTEGER,
                "最後揭示賣價" REAL,
                "最後揭示賣量" INTEGER,
                "本益比" REAL,
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
        """取得所有股票指定日期的日 K"""

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
        """取得所有股票指定日期範圍的日 K；`start_date > end_date` 時回傳空表"""

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
        """取得指定個股在區間內的日 K；`start_date > end_date` 時回傳空表"""

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

    def get_trading_days(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        - Description:
            取得區間內的交易日（已排序、去重）；當日有日 K 即為開盤日
        - Parameters:
            - start_date: datetime.date
                起始日（含）
            - end_date: datetime.date
                結束日（含）
        - Return:
            - List[datetime.date]
                區間內的交易日；無資料時回傳空 list
        """

        return self.get_distinct_dates(start_date, end_date)

    def get_close_prices(
        self, start_date: Optional[datetime.date] = None
    ) -> pd.DataFrame:
        """
        - Description:
            取得全市場逐日收盤價（`date`／`stock_id`／`收盤價` 三欄），供跳空偵測使用
        - Parameters:
            - start_date: Optional[datetime.date]
                只取這一天（含）之後；None 表示全期間
        - Return:
            - pd.DataFrame
                未排序的收盤價；表內無資料時為空表
        """

        if start_date is None:
            return self.query_df(
                f"SELECT date, stock_id, 收盤價 FROM {self.TABLE_NAME}"
            )

        return self.query_df(
            f"SELECT date, stock_id, 收盤價 FROM {self.TABLE_NAME} WHERE date >= ?",
            (start_date,),
        )

    def get_existing_stock_ids(self, stock_ids: List[str]) -> Set[str]:
        """
        - Description:
            傳入的代號中，表內有日 K 的那些（股期標的對照現股用）
        - Parameters:
            - stock_ids: List[str]
                要比對的股票代號
        - Return:
            - Set[str]
                表內找得到的代號；空清單時為空集合
        """

        if not stock_ids:
            return set()

        placeholders: str = ",".join("?" * len(stock_ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT stock_id FROM {self.TABLE_NAME} "
            f"WHERE stock_id IN ({placeholders})",
            tuple(stock_ids),
        )
        return {row[0] for row in rows}

    def get_high_close_by_stocks(
        self,
        stock_ids: List[str],
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """
        - Description:
            批次取得多檔股票在區間內的最高價與收盤價（研究用的價格面板）
        - Parameters:
            - stock_ids: List[str]
                股票代號；空清單回空表
            - start_date / end_date: datetime.date
                區間（含頭含尾）
        - Return:
            - pd.DataFrame
                欄位 `date`／`stock_id`／`最高價`／`收盤價`，依 `stock_id, date` 排序
        """

        if not stock_ids:
            return pd.DataFrame(columns=["date", "stock_id", "最高價", "收盤價"])

        placeholders: str = ",".join("?" * len(stock_ids))
        return self.query_df(
            f"""
            SELECT date, stock_id, 最高價, 收盤價
            FROM {self.TABLE_NAME}
            WHERE stock_id IN ({placeholders})
              AND date BETWEEN ? AND ?
            ORDER BY stock_id, date
            """,
            (*stock_ids, start_date, end_date),
        )

    # === 維護 ===
    def count_by_date(self, date: datetime.date) -> int:
        """指定日期的列數"""

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE date = ?", (date,)
        )
        return int(row[0]) if row else 0

    def delete_by_date(self, date: datetime.date) -> int:
        """
        - Description:
            刪除指定日期的全部列，回傳刪除列數；**不 commit**

            不可逆的操作一律由呼叫端確認後自行 `commit()`——DAO 不替它決定。
        - Parameters:
            - date: datetime.date
                要刪除的日期
        - Return:
            - int
                刪除的列數
        """

        cursor = self.conn.execute(
            f"DELETE FROM {self.TABLE_NAME} WHERE date = ?", to_sql_params(date)
        )
        return cursor.rowcount

    def get_latest_date(self) -> Optional[Any]:
        """表內最新的日期（`YYYY-MM-DD` 字串）；表不存在或為空時為 None"""

        return self._get_latest_value("date")
