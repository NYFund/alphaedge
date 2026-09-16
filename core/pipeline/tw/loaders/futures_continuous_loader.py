import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_CONTINUOUS_DOWNLOADS_PATH, TW_FUTURES_DB_PATH
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
Futures Continuous Loader

**本表是衍生表，不是爬回來的**：來源是同一個 DB 裡的 `futures_price_daily`，
故整組 ETL 沒有 crawler 與 cleaner，只有「建表的 updater」與本 loader。

**主鍵含 `method` 與 `roll_rule`**（`(date, product, session, method, roll_rule)`）：
連續合約不是唯一的——調整方式與換月規則各三種，同一天可以有多條合法的序列。
把兩者塞進主鍵，三種調整方式與三種換月規則可以並存於同一張表，
研究時直接 `WHERE method = ? AND roll_rule = ?` 取用，不必為每種組合建一張表。

**欄位語言沿用來源**（中文 OHLC）：本表的數字直接來自 `futures_price_daily`，
欄名跟著來源走；
主鍵與旗標欄則一律英文。

**本 loader 不 commit**：updater 對同一組（商品, 換月規則）的各種調整方式寫完才 commit 一次，
讓一條序列的幾種調整結果要嘛一起落地、要嘛一起不落地。
"""


class FuturesContinuousLoader(BaseDataLoader):
    """把建好的連續合約序列寫進 `futures_continuous`"""

    def __init__(self, dao: Optional[FuturesContinuousDAO] = None) -> None:
        """
        - Description:
            建立連續合約 loader
        - Parameters:
            - dao: Optional[FuturesContinuousDAO]
                共用的 DAO（通常由 updater 傳入）。指定時 loader 不擁有它，
                `disconnect()` 不會關閉
        """

        super().__init__()

        self.dao: Optional[FuturesContinuousDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端以它判斷連線狀態（指向 tw_futures.db）
        self.conn: Optional[sqlite3.Connection] = dao.conn if dao else None
        self.continuous_dir: Path = FUTURES_CONTINUOUS_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()
        self.create_missing_tables()
        self.continuous_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.dao = FuturesContinuousDAO(db_path=TW_FUTURES_DB_PATH)
            self.owns_dao = True
        self.conn = self.dao.conn

    def disconnect(self) -> None:
        """Disconnect the Database；共用的 DAO 由建立者關閉"""

        if not self.owns_dao:
            return

        if self.dao is not None:
            self.dao.close()
            self.dao = None
        self.conn = None

    def create_db(self) -> None:
        """Create New Database Table"""

        self.dao.ensure_table()

    def create_missing_tables(self) -> None:
        """Ensure Database Tables Exist"""

        self.dao.ensure_table()

    def commit(self) -> None:
        """提交目前的交易（由 updater 決定時點）"""

        self.dao.commit()

    def add_to_db(self, df: pd.DataFrame) -> int:
        """
        - Description:
            寫入連續合約序列；**不 commit**

            **用 `INSERT OR REPLACE` 而不是 `IGNORE`**：本表是衍生表，
            重建時同一組主鍵的值**應該**被新的結果覆蓋——調整方式的實作修正後，
            舊值若被 `IGNORE` 留著，表裡會混著兩代結果且無從分辨。
            寫入包在 savepoint 內，失敗時只回滾這一批。
        - Parameters:
            - df: pd.DataFrame
                已建好的序列
        - Return:
            - int
                寫入列數
        """

        if df is None or df.empty:
            logger.warning("[Futures Continuous] 沒有資料可寫入")
            return 0

        written: int
        with self.dao.savepoint("futures_continuous"):
            written = self.dao.insert_or_replace(df)

        logger.info(f"[Futures Continuous] 寫入 {written} 列")
        return written

    def save_csv(self, df: pd.DataFrame, file_name: str) -> Optional[Path]:
        """
        把序列另存一份 CSV 供稽核

        衍生表沒有「原始下載檔」可以回溯，出錯時只能重跑；留一份中繼檔
        至少能直接 diff 兩次建表的差異。
        """

        if df is None or df.empty:
            return None

        path: Path = self.continuous_dir / file_name
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
