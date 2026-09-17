from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import CORPORATE_ACTION_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.shared.source_priority import dedup_by_source_priority

"""
Corporate Action Loader

**入庫走 `INSERT OR REPLACE`**：同一筆事件會在每次回補時再取一次（端點是區間查詢，
一次一整年），`append` 會因主鍵衝突整批拋錯，讓「重跑」與「真的出錯」無法區分。

`資料來源` 有三種：`twse`／`tpex` 來自交易所端點，`detected` 是由行情偵測補的
（ETF 受益權單位分割不在任何結構化端點裡，0050 的 2025-06-18 就是這一類）。
跨來源重複時**交易所優先**——偵測出來的倍率是從價格反推的近似值，
端點給的是官方參考價。
"""


class CorporateActionLoader(BaseDataLoader):
    """把清洗後的公司行動事件寫入資料庫"""

    # 同一筆事件若跨來源重複，保留優先序**最高**的那一筆。
    # 偵測值是從價格反推的近似，交易所給的才是官方參考價
    SOURCE_PRIORITY: List[str] = ["detected", "tpex", "twse"]

    def __init__(self, dao: Optional[CorporateActionDAO] = None) -> None:
        """
        - Description:
            建立 corporate_action loader
        - Parameters:
            - dao: Optional[CorporateActionDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.dao: Optional[CorporateActionDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態
        self.conn: Optional[DBConnection] = dao.conn if dao else None
        self.corporate_action_dir: Path = CORPORATE_ACTION_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()
        self.create_missing_tables()
        self.corporate_action_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.dao = CorporateActionDAO(db_path=TW_STOCK_DB_PATH)
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
        """建立公司行動事件表"""

        self.dao.create_table()

    def create_missing_tables(self) -> None:
        """確保公司行動資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(self, remove_files: bool = False) -> None:
        """
        - Description:
            把下載目錄中的所有 CSV 跨檔去重後寫入資料表

            寫入包在 savepoint 內：`executemany` 寫到一半出錯時，已送出的列整批回滾，
            不會被之後任何一次 commit 帶進資料庫。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
        """

        if self.dao is None:
            self.connect()

        self.create_missing_tables()

        file_cnt: int = 0
        failed_files: List[str] = []
        dfs: List[pd.DataFrame] = []

        for file_path in sorted(self.corporate_action_dir.iterdir()):
            if file_path.suffix != ".csv":
                continue
            try:
                df: pd.DataFrame = pd.read_csv(file_path, dtype={"stock_id": str})
                dfs.append(df)
                file_cnt += 1
            except Exception as error:
                logger.warning(f"Error reading {file_path}: {error}")
                failed_files.append(str(file_path))

        if not dfs:
            logger.warning("No corporate action CSV file to load")
            self.disconnect()
            return

        merged_df: pd.DataFrame = pd.concat(dfs, ignore_index=True)
        row_cnt: int = len(merged_df)
        merged_df = dedup_by_source_priority(
            merged_df, self.SOURCE_PRIORITY, label="corporate_action"
        )
        logger.info(f"Corporate action rows: {row_cnt} -> {len(merged_df)} after dedup")

        new_rows: int
        try:
            with self.dao.savepoint():
                new_rows = self.upsert(merged_df)
            self.dao.commit()
        finally:
            self.disconnect()

        self.finish_load(
            source="corporate_action",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=CORPORATE_ACTION_DOWNLOADS_PATH,
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
                要寫入的資料
        - Return:
            - int
                新增的列數（覆寫既有主鍵的不計）
        """

        if df.empty:
            return 0

        before: int = self.dao.count_rows()
        written: int = self.dao.insert_or_replace(df)
        new_rows: int = self.dao.count_rows() - before
        logger.info(
            f"[corporate_action] 寫入 {written} 列（新增 {new_rows}、覆寫 {written - new_rows}）"
        )
        return new_rows
