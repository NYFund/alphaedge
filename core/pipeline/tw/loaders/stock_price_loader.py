import sqlite3
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd
from loguru import logger

from core.config import PRICE_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_loader import BaseDataLoader


class StockPriceLoader(BaseDataLoader):
    """Stock Price Loader"""

    def __init__(self, dao: Optional[StockPriceDAO] = None) -> None:
        """
        - Description:
            建立 price loader
        - Parameters:
            - dao: Optional[StockPriceDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.dao: Optional[StockPriceDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態
        self.conn: Optional[sqlite3.Connection] = dao.conn if dao else None

        # Downloads directory Path
        self.price_dir: Path = PRICE_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.price_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.dao = StockPriceDAO(db_path=TW_STOCK_DB_PATH)
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
        """Create New Database"""

        self.dao.create_table()

    def create_missing_tables(self) -> None:
        """確保股票價格資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """
        - Description:
            將 downloads 內的 CSV 入庫；**有任何檔案失敗就拋 `DataLoadError`**

            舊版逐檔 `except Exception` 後只記 `logger.error` 並 `error_cnt += 1`，
            跑完照樣印一行 summary 就結束，行程結束碼是 0。
            這與 2026-08-16 margin 事故是同一個形狀：缺的列要事後逐日比對才會發現。

            **去重走 `INSERT OR IGNORE`**：舊版每批都把整張 `price` 表的主鍵
            （近 1,300 萬列）讀進記憶體建 set。改用資料庫自己的主鍵約束，
            記憶體不再隨資料量成長，且「重跑」與「真的出錯」仍分得開——
            重複列靜靜跳過，欄位不符、檔案損毀才會拋出。

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾。
            少了這層，前面已寫入的列會被迴圈結束後的 `commit()` 一起寫進去，
            資料表多出半份檔案，回報卻說這個檔案失敗。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
            - only_dates: Optional[Set[str]]
                只處理這些日期（`YYYYMMDD`）的檔案；None 表示整個目錄
        - Raise:
            - DataLoadError
                有任何檔案入庫失敗
        """

        if self.dao is None:
            self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        # 取得要處理的 CSV 並排序，確保處理順序一致
        csv_files: List[Path] = self.select_csv_files(self.price_dir, only_dates)
        total_files: int = len(csv_files)

        if total_files == 0:
            logger.info("No CSV files found in price directory")
            return

        logger.info(f"Found {total_files} CSV files to process")

        succeeded: int = 0
        skipped_files: int = 0
        failed_files: List[str] = []

        for idx, file_path in enumerate(csv_files, start=1):
            try:
                logger.info(f"Processing [{idx}/{total_files}] {file_path.name}...")

                df: pd.DataFrame = pd.read_csv(file_path)

                if df.empty:
                    logger.warning(f"Skipped {file_path.name} (file is empty)")
                    skipped_files += 1
                    continue

                # 同一檔內的重複列先去掉：`INSERT OR IGNORE` 擋得掉，
                # 但先去掉才數得準「這檔到底寫進去幾列」
                original_count: int = len(df)
                df = df.drop_duplicates(
                    subset=list(StockPriceDAO.PRIMARY_KEY_COLUMNS), keep="first"
                )
                if len(df) < original_count:
                    logger.debug(
                        f"Removed {original_count - len(df)} duplicate rows "
                        f"within {file_path.name}"
                    )

                inserted: int
                ignored: int
                with self.dao.savepoint():
                    inserted, ignored = self.dao.insert_or_ignore(df)
            except Exception as e:
                logger.error(f"Error saving {file_path.name}: {e}")
                failed_files.append(file_path.name)
                continue

            if inserted == 0:
                logger.info(f"Skipped {file_path.name} (all data already exists)")
                skipped_files += 1
                continue

            if ignored:
                # **不進 `partial_files`**：`INSERT OR IGNORE` 只知道「主鍵已存在」，
                # 不知道值有沒有不同。重跑一個部分入庫過的日期（例如 `--from`
                # 往前拉）本來就會有大量 ignored，把它當成「同鍵不同值」示警
                # 只會訓練讀 log 的人忽略那行警告
                logger.info(
                    f"Saved {file_path.name} into database "
                    f"({inserted} new rows, {ignored} already existed)"
                )
            else:
                logger.info(f"Saved {file_path.name} into database ({inserted} rows)")
            succeeded += 1

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source="price",
            succeeded=succeeded,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=self.price_dir,
            skipped_files=skipped_files,
        )
