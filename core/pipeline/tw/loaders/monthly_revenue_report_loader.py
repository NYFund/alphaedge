from pathlib import Path
from typing import Any, List, Optional, Type

import pandas as pd
from loguru import logger

from core.config import (
    MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH,
    MONTHLY_REVENUE_REPORT_META_DIR_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.utils import DataType
from core.pipeline.utils.data_utils import DataUtils


class MonthlyRevenueReportLoader(BaseDataLoader):
    """TWSE & TPEX Monthly Revenue Report Loader"""

    def __init__(self, dao: Optional[MonthlyRevenueDAO] = None) -> None:
        """
        - Description:
            建立月營收 loader
        - Parameters:
            - dao: Optional[MonthlyRevenueDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        # Downloads directory Path
        self.mrr_dir: Path = MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH

        # MMR Cleaned Columns Path
        self.monthly_revenue_report_cleaned_cols_path: Path = (
            MONTHLY_REVENUE_REPORT_META_DIR_PATH
            / f"{DataType.MRR.lower()}_cleaned_columns.json"
        )

        super().__init__(dao)

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        # Connect Database
        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        # Create the downloads directory
        self.mrr_dir.mkdir(parents=True, exist_ok=True)

    DAO_CLASS: Type[Any] = MonthlyRevenueDAO

    def db_path(self) -> Path:
        """路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫"""

        return Path(TW_STOCK_DB_PATH)

    def load_cleaned_columns(self) -> List[str]:
        """讀取清洗器產出的欄位清單（建表用）"""

        return DataUtils.load_json(
            file_path=self.monthly_revenue_report_cleaned_cols_path
        )

    def create_db(self) -> None:
        """Create New Database"""

        self.dao.create_table(self.load_cleaned_columns())

    def create_missing_tables(self) -> None:
        """確保月營收資料表存在"""

        if not self.dao.table_exists():
            self.create_db()

    def add_to_db(self, remove_files: bool = False) -> None:
        """
        - Description:
            將 downloads 內的月營收 CSV 入庫；有任何檔案失敗就拋 `DataLoadError`

            **去重走 `INSERT OR IGNORE`**：舊版每個 CSV 都把整張表的主鍵讀進記憶體、
            以 merge 找出新列再 `to_sql` 追加——表越大越慢，且同一檔內自己重複的列
            會讓 `to_sql` 撞主鍵、整檔失敗。改用資料庫自己的主鍵約束後兩者皆免。

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾，
            不會被迴圈結束後的 `commit()` 一起寫進去。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
        """

        if self.dao is None:
            self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        file_cnt: int = 0
        skipped_files: int = 0

        failed_files: List[str] = []
        for file_path in self.mrr_dir.iterdir():
            # Skip non-CSV files
            if file_path.suffix != ".csv":
                continue
            try:
                df: pd.DataFrame = pd.read_csv(file_path)

                if df.empty:
                    logger.warning(f"Skip {file_path}: file is empty")
                    skipped_files += 1
                    continue

                # 確保 stock_id 是字串型別，避免與資料庫中的 TEXT 型別不一致
                if "stock_id" in df.columns:
                    df["stock_id"] = df["stock_id"].astype(str)

                inserted: int
                skipped: int
                with self.dao.savepoint():
                    inserted, skipped = self.dao.insert_or_ignore(df)

                if not inserted:
                    # 整檔已存在是重跑的正常結果，不算「新寫入」
                    logger.info(
                        f"Skip {file_path}: all records already exist in database"
                    )
                    skipped_files += 1
                    continue

                logger.info(
                    f"Save {file_path} into database "
                    f"({inserted} new records, {skipped} duplicates skipped)"
                )
                file_cnt += 1
            except Exception as e:
                logger.warning(f"Error saving {file_path}: {e}")
                failed_files.append(str(file_path))

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source="mrr",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=self.mrr_dir,
            skipped_files=skipped_files,
        )
