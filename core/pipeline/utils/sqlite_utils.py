import datetime
import sqlite3
from typing import Any, Optional, Tuple

from loguru import logger

from core.dao.base import table_exists

"""
SQLite 共用操作：表存在檢查、最早／最新值查詢、刪表

**這裡不吞 `sqlite3.Error`**：舊版三個查詢函式一律 `except sqlite3.Error` 後
回 `None`／預設值，於是「資料表還沒建立」（正常）與「欄位打錯、DB 損毀、被鎖住」
（不正常）長得一模一樣。updater 拿到 `None` 就從預設起日重跑，
把一個查詢錯誤變成一次靜默的全量回補。

唯一該回 `None` 的是「表不存在」與「表是空的」，兩者都先明確判斷，其餘往外拋。
"""


class SQLiteUtils:
    @staticmethod
    def check_table_exist(conn: sqlite3.Connection, table_name: str) -> bool:
        """檢查 table 是否存在（實作在 `core.dao.base.table_exists()`，呼叫端改走 DAO 後刪除）"""

        return table_exists(conn, table_name)

    @staticmethod
    def get_table_earliest_value(
        conn: sqlite3.Connection,
        table_name: str,
        col_name: str,
    ) -> Optional[datetime.date]:
        """Retrieve the earliest value in the table for the column; None if not found"""

        if not SQLiteUtils.check_table_exist(conn=conn, table_name=table_name):
            logger.debug(
                f"Table '{table_name}' does not exist yet. "
                f"This is normal for first-time updates."
            )
            return None

        query: str = (
            f"SELECT {col_name} FROM {table_name} ORDER BY {col_name} ASC LIMIT 1"
        )
        cursor: sqlite3.Cursor = conn.execute(query)
        result: Optional[Tuple[Any, ...]] = cursor.fetchone()

        if result is None or result[0] is None:
            logger.debug(
                f"No value found for column '{col_name}' in table: '{table_name}'. "
                f"Table is empty or column has no data. This is normal for first-time updates."
            )
            return None

        return result[0]

    @staticmethod
    def get_table_latest_value(
        conn: sqlite3.Connection,
        table_name: str,
        col_name: str,
    ) -> Optional[Any]:
        """
        - Description: Retrieve the latest value in the table.

        - Parameters:
            - conn: sqlite3.Connection
                SQLite database connection object.
            - table_name: str
                Name of the table to query.
            - col_name: str
                Name of the value column to search.

        - Return: Optional[Any]
            - The latest value in the column, or None if the table does not exist
              or has no data.

        - Raise:
            - sqlite3.Error
                表存在但查詢失敗（欄位不存在、DB 損毀、被鎖住）。
                **不可吞掉**：吞掉會讓 updater 從預設起日重跑整段回補。
        """

        if not SQLiteUtils.check_table_exist(conn=conn, table_name=table_name):
            logger.debug(
                f"Table '{table_name}' does not exist yet. "
                f"This is normal for first-time updates."
            )
            return None

        query: str = (
            f"SELECT {col_name} FROM {table_name} ORDER BY {col_name} DESC LIMIT 1"
        )
        cursor: sqlite3.Cursor = conn.execute(query)
        result: Optional[Tuple[Any, ...]] = cursor.fetchone()

        if result is None or result[0] is None:
            logger.debug(
                f"No value found for column '{col_name}' in table: '{table_name}'. "
                f"Table is empty or column has no data. This is normal for first-time updates."
            )
            return None
        return result[0]

    @staticmethod
    def get_max_secondary_value_by_primary(
        conn: sqlite3.Connection,
        table_name: str,
        primary_col: str,
        secondary_col: str,
        default_primary_value: int,
        default_secondary_value: int,
    ) -> Tuple[int, int]:
        """
        - Description:
            查詢指定 table 中最大 primary_col 的值，並找出其對應的最大 secondary_col 值。

        - Parameters:
            - conn (sqlite3.Connection): 資料庫連線
            - table_name (str): 資料表名稱
            - primary_col (str): 主欄位名稱（如 'year'）
            - secondary_col (str): 次欄位名稱（如 'month', 'season'）
            - default_primary_value (int): 查詢失敗時回傳的主欄位預設值
            default_secondary_value (int): 查詢失敗時回傳的次欄位預設值

        - Returns:
            Tuple[int, int]: (主欄位最大值, 對應的次欄位最大值)，若查詢失敗則回傳預設值
        """
        latest_primary: Optional[Any] = SQLiteUtils.get_table_latest_value(
            conn=conn,
            table_name=table_name,
            col_name=primary_col,
        )

        if latest_primary is None:
            return default_primary_value, default_secondary_value

        query: str = f"""
            SELECT {secondary_col}
            FROM {table_name}
            WHERE {primary_col} = ?
            ORDER BY CAST({secondary_col} AS INTEGER) DESC
            LIMIT 1
        """

        # 這裡同樣不吞例外：`latest_primary` 查得到就代表表存在且有資料，
        # 此時再查 secondary 還會失敗，就是真的有問題（欄位名打錯等），必須浮出來
        cursor: sqlite3.Cursor = conn.execute(query, (latest_primary,))
        result: Optional[Tuple[Any, ...]] = cursor.fetchone()
        latest_secondary: Optional[Any] = (
            result[0] if result and result[0] is not None else None
        )

        if latest_secondary is None:
            return default_primary_value, default_secondary_value

        return int(latest_primary), int(latest_secondary)

    @staticmethod
    def drop_table(conn: sqlite3.Connection, table_name: str) -> bool:
        """
        - Description:
            刪除指定 SQLite 資料庫中的資料表。

        - Parameters:
            conn (sqlite3.Connection): 資料庫連線
            table_name (str): 要刪除的資料表名稱

        - Returns:
            bool: True if table dropped successfully, False otherwise
        """
        try:
            # 先檢查 table 是否存在
            if not SQLiteUtils.check_table_exist(conn=conn, table_name=table_name):
                logger.warning(f"Table '{table_name}' does not exist. Skip drop.")
                return False

            query: str = f"DROP TABLE IF EXISTS {table_name}"
            conn.execute(query)
            conn.commit()
            logger.info(f"Table '{table_name}' dropped successfully.")
            return True

        except sqlite3.Error as e:
            logger.error(f"Failed to drop table '{table_name}': {e}")
            return False
