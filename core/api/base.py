from abc import ABC
from pathlib import Path
from typing import Any, Dict, Optional, Type

import pandas as pd

from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH
from core.dao.base import BaseDAO
from core.dao.connection import DBConnection, connect_sqlite
from core.utils.log_manager import LogManager

"""Abstract base class for data access APIs. Provides a common interface for querying data from the database"""


# `noqa: B024`：骨架收回基底之後已經沒有抽象方法，但仍保留 ABC——
# 它標示的是「這個類別不該被直接實例化」，而不是「有方法待實作」
class BaseDataAPI(ABC):  # noqa: B024
    """
    Base Class of Data API

    **建構骨架由基底負責**：開連線、建 DAO、設 log 三件事在每支 API 都是同一段，
    只差 DAO 類別、資料庫與 log 檔名——子類宣告下面三個常數即可，不必各寫一次。

    **連線的「開」與「關」由同一層負責**：`owns_conn` 在基底設定、也在基底的
    `close()` 判讀（共用連線不關、自建的才關）。子類若自行開連線而漏設
    `owns_conn`，連線就再也關不掉。

    需要多個 DAO 或額外快取的子類覆寫 `setup()`，**先呼叫 `super().setup()`**
    再補自己的；需要額外建構參數的覆寫 `__init__()`，同樣先設好自己的屬性
    再呼叫 `super().__init__(conn)`——基底的 `__init__()` 最後才呼叫 `setup()`。

    **價格序列只有一種形狀**：各市場的 `get_close_series()` 一律經 `build_close_series()`
    回傳 index 為 `datetime.date`、dtype float、name 為「收盤價」、依日期排序的 Series。
    三支 API 原本各回一種（字串 index 帶 name、字串 index 不帶 name、`date` index），
    呼叫端只能各自補轉換，而字串日期比大小與排序都容易出錯。
    """

    # 價格序列的統一名稱；與各市場收盤欄的欄名相同
    CLOSE_SERIES_NAME: str = "收盤價"
    # 價格序列 index 的名稱；沿用資料表的日期欄名
    CLOSE_SERIES_INDEX_NAME: str = "date"

    # 子類宣告：自建連線時要連哪個資料庫、單一 DAO 的類別、log 檔名。
    # `DAO_CLASS` 留 None 代表「不只一個 DAO」或「DAO 延遲建立」，由子類自理
    DEFAULT_DB_PATH: Optional[Path] = None
    DAO_CLASS: Optional[Type[BaseDAO]] = None
    LOG_FILE_NAME: str = ""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        """
        - Description:
            建立 API；連線由呼叫端傳入或自行建立
        - Parameters:
            - conn: Optional[DBConnection]
                共用連線（通常來自 DataFeed）；未指定時自行建立並負責關閉
        """

        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        self.setup()

    def setup(self) -> None:
        """開連線、建 DAO、設定 log；子類要加東西時先呼叫 `super().setup()`"""

        if self.owns_conn and self.DEFAULT_DB_PATH is not None:
            self.conn = connect_sqlite(self.DEFAULT_DB_PATH)

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        if self.DAO_CLASS is not None:
            self.dao = self.DAO_CLASS(conn=self.conn)

        if self.LOG_FILE_NAME:
            LogManager.setup_logger(
                self.LOG_FILE_NAME,
                log_dir=API_LOGS_DIR_PATH,
                level=API_LOG_FILE_LEVEL,
            )

    @staticmethod
    def build_column_map(df: pd.DataFrame, column: str) -> Dict[str, Any]:
        """
        - Description:
            由單日全市場 DataFrame 建立 `{stock_id: 欄位值}` 對照表

            具名查詢方法的共用底座。策略若自行「逐檔建 mask 再取欄位」，
            不但是 O(n²)，還會把資料表欄位名洩漏到策略層。

            同一檔重複出現時固定取**第一筆**；改成取最後一筆會讓歷史回歸逐筆對不上。

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

    @classmethod
    def empty_close_series(cls) -> pd.Series:
        """查無資料時的價格序列：與有資料時同一個 name 與 dtype，呼叫端以 `empty`／`len()` 判斷"""

        return pd.Series(
            dtype=float,
            name=cls.CLOSE_SERIES_NAME,
            index=pd.Index([], name=cls.CLOSE_SERIES_INDEX_NAME),
        )

    @classmethod
    def build_close_series(
        cls, df: pd.DataFrame, date_column: str, close_column: str
    ) -> pd.Series:
        """
        - Description:
            把含日期欄與收盤欄的 DataFrame 轉成**價格序列**；各市場 `get_close_series()`
            的回傳契約都在這裡

            契約：index 為 `datetime.date`（name 為 `date`）、dtype float、
            name 為「收盤價」、依日期穩定排序；查無資料時回同一個形狀的空 Series。

            **不去重**：同一日期多列（期貨 `session=None` 時日夜盤各一列）維持原樣，
            要哪一列由呼叫端決定——在這裡挑等於替呼叫端選了時段。

            無法轉成數值的價格轉成 NaN 而不是拋錯：缺資料與「有資料但值異常」
            要分得開（與 `build_column_map()` 同一個理由）。
        - Parameters:
            - df: pd.DataFrame
                含日期欄與收盤欄的查詢結果
            - date_column: str
                日期欄名（字串或 datetime 皆可）
            - close_column: str
                收盤欄名
        - Return:
            - pd.Series
                價格序列；`df` 為空或缺欄位時為空 Series
        """

        if df.empty or date_column not in df.columns or close_column not in df.columns:
            return cls.empty_close_series()

        ordered: pd.DataFrame = df.sort_values(date_column, kind="stable")
        series: pd.Series = pd.to_numeric(
            ordered[close_column], errors="coerce"
        ).astype(float)
        series.index = pd.Index(
            pd.to_datetime(ordered[date_column]).dt.date,
            name=cls.CLOSE_SERIES_INDEX_NAME,
        )
        series.name = cls.CLOSE_SERIES_NAME
        return series

    def close(self) -> None:
        """
        - Description:
            關閉資料連線

            不關閉的話，單次回測會累積 8~10 條互不相干且不再釋放的 SQLite 連線。
            預設實作關掉 `self.conn`；非 SQLite 的資料源（如台股 tick 的 TimescaleDB）自行覆寫。
        """

        if not getattr(self, "owns_conn", True):
            # 共用連線由建立者（DataFeed）負責關閉，避免其他持有者拿到已關閉的連線
            return

        conn: Optional[DBConnection] = getattr(self, "conn", None)
        if conn is not None:
            conn.close()
            self.conn = None

    def __enter__(self) -> "BaseDataAPI":
        """支援 with 語法，離開區塊即關閉連線"""

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """離開 with 區塊時關閉連線"""

        self.close()
