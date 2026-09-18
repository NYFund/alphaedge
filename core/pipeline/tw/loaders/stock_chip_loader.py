from pathlib import Path
from typing import List, Optional, Set

import pandas as pd
from loguru import logger

from core.config import CHIP_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.pipeline.shared.base_loader import BaseDataLoader


class StockChipLoader(BaseDataLoader):
    """Stock Chip Loader"""

    def __init__(self, dao: Optional[StockChipDAO] = None) -> None:
        """
        - Description:
            建立 chip loader
        - Parameters:
            - dao: Optional[StockChipDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.dao: Optional[StockChipDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態
        self.conn: Optional[DBConnection] = dao.conn if dao else None

        # Downloads directory Path
        self.chip_dir: Path = CHIP_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.chip_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.dao = StockChipDAO(db_path=TW_STOCK_DB_PATH)
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
        """創建三大法人盤後籌碼db"""

        self.dao.create_table()

    def create_missing_tables(self) -> None:
        """確保三大法人盤後籌碼資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """
        - Description:
            將資料夾中的所有 CSV 檔入庫；有任何檔案失敗就拋 `DataLoadError`

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾。
            少了這層，前面已寫入的列會被迴圈結束後的 `commit()` 一起寫進去，
            資料表多出半份檔案，回報卻說這個檔案失敗。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
            - only_dates: Optional[Set[str]]
                只處理這些日期（`YYYYMMDD`）的檔案；None 表示整個目錄
        """

        if self.dao is None:
            self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        file_cnt: int = 0

        failed_files: List[str] = []
        partial_files: List[str] = []
        skipped_cnt: int = 0
        for file_path in self.select_csv_files(self.chip_dir, only_dates):
            try:
                # `dtype` 不指定的話，全數字的代號會被推斷成整數，
                # `0050` 入庫變成 `50`（與 margin、price 同一個做法）
                df: pd.DataFrame = pd.read_csv(file_path, dtype={"stock_id": str})
                inserted: int
                skipped: int
                with self.dao.savepoint():
                    inserted, skipped = self.dao.insert_or_ignore(df)
                if inserted == 0 and skipped > 0:
                    # 整檔已在資料庫中：loader 每次都掃全目錄，重跑必然走到這裡
                    skipped_cnt += 1
                    continue
                if skipped > 0:
                    partial_files.append(str(file_path))
                logger.info(f"Save {file_path} into database")
                file_cnt += 1
            except Exception as e:
                logger.warning(f"Error saving {file_path}: {e}")
                failed_files.append(str(file_path))

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source="chip",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=CHIP_DOWNLOADS_PATH,
            skipped_files=skipped_cnt,
            partial_files=partial_files,
        )
