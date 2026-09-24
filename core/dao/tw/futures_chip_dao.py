import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

from core.config import (
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_LARGE_TRADER_TABLE_NAME,
    FUTURES_PUT_CALL_RATIO_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.base import BaseDAO

"""
台期貨籌碼三張表（`tw_futures.db`）的資料存取

| 表 | 主鍵 | 一天的列數 |
|----|------|-----------|
| `futures_institutional_chip` | (date, product_name, investor) | 商品數 × 3 |
| `futures_large_trader` | (date, product, expiry, trader_type) | 約 1,400 |
| `futures_put_call_ratio` | (date) | 1 |

三張表共用同一組查詢，**表名由建構子傳入，一律走白名單**：表名無法用 `?` 佔位、
只能拼進 SQL，不檢查就等於讓呼叫端的字串直接進查詢。
"""


class FuturesChipDAO(BaseDAO):
    """
    - Description:
        單一籌碼表的建表、寫入與查詢

        **欄位由第一批資料推導、主鍵寫死**：三個來源的欄位數多且會隨交易所調整
        （三大法人 15 欄、大額 10 欄），逐欄寫死 schema 會在來源加欄位時整批失敗；
        但主鍵不能推導——推錯會讓重跑產生重複列而不是被擋下。
    """

    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH

    # {表名: 主鍵欄位}；同時是表名白名單
    PRIMARY_KEYS: Dict[str, Tuple[str, ...]] = {
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME: ("date", "product_name", "investor"),
        FUTURES_LARGE_TRADER_TABLE_NAME: ("date", "product", "expiry", "trader_type"),
        FUTURES_PUT_CALL_RATIO_TABLE_NAME: ("date",),
    }

    def __init__(
        self,
        table_name: str,
        conn: Optional[sqlite3.Connection] = None,
        db_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        - Description:
            建立指定籌碼表的 DAO
        - Parameters:
            - table_name: str
                籌碼表名稱；必須在 `PRIMARY_KEYS` 內
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
        """確認表名是三張籌碼表之一；不是就當場拋出 `ValueError`"""

        if table_name not in cls.PRIMARY_KEYS:
            raise ValueError(
                f"不支援的籌碼資料表：{table_name!r}；可用的有 {sorted(cls.PRIMARY_KEYS)}"
            )
        return table_name

    # === 建表與寫入 ===
    def ensure_table(self, df: pd.DataFrame) -> None:
        """
        - Description:
            依 DataFrame 的欄位建表（若不存在）並 commit；主鍵與名稱類欄位存文字，其餘存數值
        - Parameters:
            - df: pd.DataFrame
                本次要寫入的資料（用來推導欄位）
        """

        keys: Tuple[str, ...] = self.PRIMARY_KEYS[self.TABLE_NAME]
        columns: List[str] = []
        for column in df.columns:
            if column in keys:
                sql_type: str = "TEXT NOT NULL"
            elif column.endswith("_name"):
                sql_type = "TEXT"
            else:
                sql_type = "REAL"
            columns.append(f'"{column}" {sql_type}')

        primary_key: str = ", ".join(f'"{key}"' for key in keys)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} ({', '.join(columns)}, "
            f"PRIMARY KEY ({primary_key}));"
        )
        self.conn.commit()

    def insert_new_rows(self, df: pd.DataFrame) -> int:
        """
        - Description:
            以 `INSERT OR IGNORE` 寫入，回傳**實際新增**的列數；不 commit

            籌碼是既成事實，同一天重跑不該產生第二份、也不該覆蓋。`NaN` 轉成 NULL
            再寫入——轉不動的數值留空，不猜 0。
        - Parameters:
            - df: pd.DataFrame
                清洗後的資料
        - Return:
            - int
                實際新增的列數
        """

        if df is None or df.empty:
            return 0

        columns: str = ", ".join(f'"{column}"' for column in df.columns)
        placeholders: str = ", ".join("?" for _ in df.columns)

        before: int = self.count_rows()
        self.conn.executemany(
            f"INSERT OR IGNORE INTO {self.TABLE_NAME} ({columns}) VALUES ({placeholders})",
            df.astype(object)
            .where(pd.notna(df), None)
            .itertuples(index=False, name=None),
        )
        return self.count_rows() - before

    # === 查詢 ===
    def count_rows(self) -> int:
        """
        - Description:
            表內列數；**只有表還沒建才回 0**

            查詢錯誤不可一起吞成 0：寫入是用「入庫後列數 − 入庫前列數」算新增筆數，
            後一次查詢失敗會印出負數列數。
        """

        if not self.table_exists():
            return 0
        return self.conn.execute(f"SELECT COUNT(*) FROM {self.TABLE_NAME}").fetchone()[
            0
        ]

    def get_latest_date(self) -> Optional[str]:
        """
        - Description:
            表內最新的資料日期；表不存在時為 None

            **回 None 的代價是整段重爬**：updater 拿到 None 就退回預設起日。故只有
            「表還沒建」回 None，`database is locked` 等錯誤一律往外拋。
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MAX(date) FROM {self.TABLE_NAME}"
        )
        return row[0] if row else None

    def get_earliest_date(self) -> Optional[str]:
        """表內最早的資料日期；表不存在時為 None（缺口偵測的下界）"""

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MIN(date) FROM {self.TABLE_NAME}"
        )
        return row[0] if row else None

    def get_latest_date_before(self, date: datetime.date) -> Optional[str]:
        """
        - Description:
            **嚴格早於** `date` 的最近一個有籌碼的日期；表不存在或沒有時為 None

            籌碼盤後才公布，當天的資料當天不可能知道——`<=` 那一個等號就是前視偏差。
        - Parameters:
            - date: datetime.date
                回測當前日
        - Return:
            - Optional[str]
                日期字串
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MAX(date) FROM {self.TABLE_NAME} WHERE date < ?", (date,)
        )
        return row[0] if row and row[0] else None

    def get_by_date(self, date: Union[datetime.date, str]) -> pd.DataFrame:
        """取得某一天公布的籌碼；表不存在時為空表"""

        if not self.table_exists():
            return pd.DataFrame()

        return self.query_df(
            f"SELECT * FROM {self.TABLE_NAME} WHERE date = ?",
            (date,),
        )

    def get_covered_date_range(self) -> Optional[Tuple[str, str]]:
        """表的（最早, 最晚）資料日期；表不存在或為空時為 None"""

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MIN(date), MAX(date) FROM {self.TABLE_NAME}"
        )
        if row is None or row[0] is None:
            return None
        return row[0], row[1]
