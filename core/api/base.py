import sqlite3
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from core.dao.base import table_exists, to_sql_params

"""Abstract base class for data access APIs. Provides a common interface for querying data from the database"""


class BaseDataAPI(ABC):
    """Base Class of Data API"""

    def __init__(self) -> None:
        pass

    @abstractmethod
    def setup(self) -> None:
        """Set Up the Config of Data API"""
        pass

    @staticmethod
    def sql_params(*values: Any) -> Tuple[Any, ...]:
        """
        - Description:
            把查詢參數轉成 SQLite 收得下的型別；`date`／`datetime` 轉 ISO 字串

            實作在 `core.dao.base.to_sql_params()`；尚未改走 DAO 的 API 仍呼叫本方法，
            全部改完後刪除。
        - Parameters:
            - values: Any
                查詢參數；非日期型別原樣通過
        - Return:
            - Tuple[Any, ...]
                可直接傳給 `params=` 的 tuple
        """

        return to_sql_params(*values)

    @staticmethod
    def check_table_exist(conn: sqlite3.Connection, table_name: str) -> bool:
        """
        - Description:
            檢查資料表是否存在

            實作在 `core.dao.base.table_exists()`（「表還沒建」與「查詢出錯」為何要分開，
            見該函式說明）；尚未改走 DAO 的 API 仍呼叫本方法，全部改完後刪除。
        - Parameters:
            - conn: sqlite3.Connection
                資料庫連線
            - table_name: str
                資料表名稱
        - Return:
            - bool
                資料表存在為 True
        """

        return table_exists(conn, table_name)

    @staticmethod
    def build_column_map(df: pd.DataFrame, column: str) -> Dict[str, Any]:
        """
        - Description:
            由單日全市場 DataFrame 建立 `{stock_id: 欄位值}` 對照表

            具名查詢方法的共用底座。策略若自行「逐檔建 mask 再取欄位」，
            不但是 O(n²)，還會把資料表欄位名洩漏到策略層。

            同一檔重複出現時取**第一筆**，與原本 `df.loc[mask, col].iloc[0]`
            的取值一致——改成取最後一筆會讓歷史回歸逐筆對不上。

            **值維持資料庫原樣不做轉型**（含 `NaN`）：缺資料與「有資料但值異常」
            是兩件事，前者以 key 不存在表示，後者留給呼叫端依自身門檻判斷。
        - Parameters:
            - df: pd.DataFrame
                單日全市場資料
            - column: str
                要取的欄位名
        - Return:
            - Dict[str, Any]
                對照表；`df` 為空或無該欄位時回傳空 dict
        """

        if df.empty or column not in df.columns:
            return {}

        deduped: pd.DataFrame = df.drop_duplicates(subset="stock_id", keep="first")
        return dict(zip(deduped["stock_id"], deduped[column]))

    def close(self) -> None:
        """
        - Description:
            關閉資料連線

            單次回測原本會開出 8~10 條互不相干的 SQLite 連線且從不關閉
            。預設實作關掉 `self.conn`；
            非 SQLite 的資料源（如 DolphinDB）自行覆寫。
        """

        if not getattr(self, "owns_conn", True):
            # 共用連線由建立者（DataFeed）負責關閉，避免其他持有者拿到已關閉的連線
            return

        conn: Optional[sqlite3.Connection] = getattr(self, "conn", None)
        if conn is not None:
            conn.close()
            self.conn = None

    def __enter__(self) -> "BaseDataAPI":
        """支援 with 語法，離開區塊即關閉連線"""

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """離開 with 區塊時關閉連線"""

        self.close()
