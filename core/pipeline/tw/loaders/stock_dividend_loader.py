from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import DIVIDEND_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.shared.source_priority import dedup_by_source_priority

"""
Stock Dividend Loader

與其他 loader 的差異：本表的三個來源會**合法地重疊**——上櫃同一筆除權息可能同時來自
FinMind 歷史回補與 TPEX 日更。因此入庫走 `INSERT OR REPLACE` 而非 `append`，
且在寫入前先跨檔去重，避免同批資料撞主鍵讓整個檔案被 except 吞掉。
"""


class StockDividendLoader(BaseDataLoader):
    """Stock Dividend Loader"""

    # 同一筆除權息若跨來源重複，保留優先序**最高**的那一筆。
    #
    # **不可依檔名字典序決定**：舊版直接對 `sorted(dir.iterdir())`
    # 的結果 `drop_duplicates(keep="last")`，於是「留下哪一筆」取決於檔名的
    # 字母順序——今天剛好是 `twse_` 勝出，日後多一個來源（例如 `finmind_`）
    # 或檔名改個前綴，勝出的就換人，而且不會有任何跡象。
    #
    # 排序原則：交易所官方資料優先於第三方回補。清單中沒列到的來源排在最後。
    SOURCE_PRIORITY: List[str] = ["finmind", "tpex", "twse"]

    def __init__(self, dao: Optional[StockDividendDAO] = None) -> None:
        """
        - Description:
            建立 dividend loader
        - Parameters:
            - dao: Optional[StockDividendDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.dao: Optional[StockDividendDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態
        self.conn: Optional[DBConnection] = dao.conn if dao else None

        # Downloads directory Path
        self.dividend_dir: Path = DIVIDEND_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.dividend_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.dao = StockDividendDAO(db_path=TW_STOCK_DB_PATH)
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
        """創建除權除息計算結果表 db"""

        self.dao.create_table()

    def create_missing_tables(self) -> None:
        """確保除權除息資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(self, remove_files: bool = False) -> None:
        """
        - Description:
            將資料夾中的所有 CSV 跨檔去重後入庫；有任何檔案讀取失敗就拋 `DataLoadError`

            寫入包在 savepoint 內：`executemany` 寫到一半出錯時，已送出的列整批回滾，
            不會被之後任何一次 commit 帶進資料庫。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
        """

        if self.dao is None:
            self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        file_cnt: int = 0

        failed_files: List[str] = []
        dfs: List[pd.DataFrame] = []
        for file_path in sorted(self.dividend_dir.iterdir()):
            # Skip non-CSV files
            if file_path.suffix != ".csv":
                continue
            try:
                df: pd.DataFrame = pd.read_csv(file_path, dtype={"stock_id": str})
                dfs.append(df)
                file_cnt += 1
            except Exception as e:
                logger.warning(f"Error reading {file_path}: {e}")
                failed_files.append(str(file_path))

        if not dfs:
            logger.warning("No dividend CSV file to load")
            self.disconnect()
            return

        # 跨檔去重：同一筆除權息可能同時來自 FinMind 回補與 TPEX 日更
        merged_df: pd.DataFrame = pd.concat(dfs, ignore_index=True)
        row_cnt: int = len(merged_df)
        merged_df = dedup_by_source_priority(
            merged_df, self.SOURCE_PRIORITY, label="dividend"
        )
        logger.info(f"Dividend rows: {row_cnt} -> {len(merged_df)} after dedup")

        new_rows: int
        try:
            with self.dao.savepoint():
                new_rows = self.upsert(merged_df)
            self.dao.commit()
        finally:
            self.disconnect()

        self.finish_load(
            source="dividend",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=DIVIDEND_DOWNLOADS_PATH,
            new_rows=new_rows,
        )

    def upsert(self, df: pd.DataFrame) -> int:
        """
        - Description:
            以 `INSERT OR REPLACE` 寫入（不 commit），讓重跑與跨來源覆蓋成為冪等操作

            新增列數以寫入前後的列數差計算：`INSERT OR REPLACE` 覆寫既有主鍵時
            列數不變，送出的列數分不出「新資料」與「重跑覆寫」。
        - Parameters:
            - df: pd.DataFrame
                已去重的除權除息資料
        - Return:
            - int
                新增的列數（覆寫既有主鍵的不計）
        """

        before: int = self.dao.count_rows()
        written: int = self.dao.insert_or_replace(df)
        new_rows: int = self.dao.count_rows() - before
        logger.info(
            f"Upserted {written} rows into {self.dao.TABLE_NAME} "
            f"({new_rows} new, {written - new_rows} overwritten)"
        )
        return new_rows
