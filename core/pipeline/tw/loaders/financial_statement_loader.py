from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import (
    FINANCIAL_STATEMENT_DOWNLOADS_PATH,
    FINANCIAL_STATEMENT_META_DIR_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.utils import FinancialStatementType
from core.pipeline.utils.data_utils import DataUtils


class FinancialStatementLoader(BaseDataLoader):
    """Financial Statement Loader"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        """
        - Description:
            建立財報 loader
        - Parameters:
            - conn: Optional[DBConnection]
                共用連線（通常由 updater 傳入，讓讀寫走同一條連線）。
                四張表共用一條連線，故收連線而不是單一 DAO；指定時 loader 不擁有它，
                `disconnect()` 不會關閉；未指定時 loader 自行建立，入庫完成即關閉
        """

        # **不呼叫 `super().__init__()`**：本 loader 收的是連線或多個 DAO，
        # 與基底「單一 DAO」的建構骨架不同形，連線與建表一律自理
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # Reports Cleaned Columns Path
        self.balance_sheet_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.BALANCE_SHEET.lower()
            / f"{FinancialStatementType.BALANCE_SHEET.lower()}_cleaned_columns.json"
        )
        self.comprehensive_income_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.COMPREHENSIVE_INCOME.lower()
            / f"{FinancialStatementType.COMPREHENSIVE_INCOME.lower()}_cleaned_columns.json"
        )
        self.cash_flow_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.CASH_FLOW.lower()
            / f"{FinancialStatementType.CASH_FLOW.lower()}_cleaned_columns.json"
        )
        self.equity_change_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.EQUITY_CHANGE.lower()
            / f"{FinancialStatementType.EQUITY_CHANGE.lower()}_cleaned_columns.json"
        )

        self.cleaned_cols_paths: dict[str, Path] = {
            FinancialStatementType.BALANCE_SHEET: self.balance_sheet_cleaned_cols_path,
            FinancialStatementType.COMPREHENSIVE_INCOME: self.comprehensive_income_cleaned_cols_path,
            FinancialStatementType.CASH_FLOW: self.cash_flow_cleaned_cols_path,
            FinancialStatementType.EQUITY_CHANGE: self.equity_change_cleaned_cols_path,
        }

        # Downloads directory Path
        self.fs_dir: Path = FINANCIAL_STATEMENT_DOWNLOADS_PATH
        self.balance_sheet_dir: Path = (
            self.fs_dir / FinancialStatementType.BALANCE_SHEET.lower()
        )
        self.comprehensive_income_dir: Path = (
            self.fs_dir / FinancialStatementType.COMPREHENSIVE_INCOME.lower()
        )
        self.cash_flow_dir: Path = (
            self.fs_dir / FinancialStatementType.CASH_FLOW.lower()
        )
        self.equity_change_dir: Path = (
            self.fs_dir / FinancialStatementType.EQUITY_CHANGE.lower()
        )

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        # Connect Database
        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.fs_dir.mkdir(parents=True, exist_ok=True)
        self.balance_sheet_dir.mkdir(parents=True, exist_ok=True)
        self.comprehensive_income_dir.mkdir(parents=True, exist_ok=True)
        self.cash_flow_dir.mkdir(parents=True, exist_ok=True)
        self.equity_change_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.conn is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
            self.owns_conn = True

    def disconnect(self) -> None:
        """Disconnect the Database；共用連線由建立者關閉"""

        if not self.owns_conn:
            return

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_dao(self, table_name: str) -> FinancialStatementDAO:
        """取得指定財報表的 DAO（共用本 loader 的連線）"""

        return FinancialStatementDAO(table_name, conn=self.conn)

    def create_db(self, table_name: str, cleaned_cols_path: Path) -> None:
        """
        - Description:
            依清洗器產出的欄位清單建立財報表
        - Parameters:
            - table_name: str
                財報表名稱
            - cleaned_cols_path: Path
                `*_cleaned_columns.json` 路徑
        """

        cols: List[str] = DataUtils.load_json(file_path=cleaned_cols_path)
        self.get_dao(table_name).create_table(cols)

    def create_missing_tables(self) -> None:
        """確保所有財報類型的資料表存在"""

        for fs_type in FinancialStatementType:
            table_name: str = fs_type.lower()
            cleaned_cols_path: Path = self.cleaned_cols_paths[fs_type]

            if not cleaned_cols_path.exists():
                # 欄位定義由 cleaner 產出，缺檔就建不出表；靜默跳過會讓入庫階段
                # 才炸在「no such table」，離真正的原因太遠
                logger.warning(
                    f"Cleaned columns not found for {table_name}: {cleaned_cols_path}"
                )
                continue

            if not self.get_dao(table_name).table_exists():
                self.create_db(
                    table_name=table_name, cleaned_cols_path=cleaned_cols_path
                )

    def add_to_db(
        self,
        dir_path: Path,
        table_name: str,
        remove_files: bool = False,
        only_files: Optional[List[Path]] = None,
    ) -> None:
        """
        - Description:
            Add Data into Database；有任何檔案失敗就拋 `DataLoadError`

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾，
            不會被迴圈結束後的 `commit()` 一起寫進去。

            `only_files` 給分批入庫用：權益變動表是逐檔查詢，整段回補會落地上千個
            CSV，若每一批都掃整個目錄，重複讀取的成本會隨批次數線性長大。
        - Parameters:
            - dir_path: Path
                CSV 所在目錄
            - table_name: str
                目標資料表
            - remove_files: bool
                成功後是否刪除來源目錄
            - only_files: Optional[List[Path]]
                只入庫這些檔案；None 表示掃整個目錄
        """

        if self.conn is None:
            self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        dao: FinancialStatementDAO = self.get_dao(table_name)
        file_cnt: int = 0

        failed_files: List[str] = []
        partial_files: List[str] = []
        skipped_cnt: int = 0
        target_files: List[Path] = (
            list(dir_path.iterdir()) if only_files is None else only_files
        )
        for file_path in target_files:
            # Skip non-CSV files
            if file_path.suffix != ".csv":
                continue
            try:
                df: pd.DataFrame = pd.read_csv(file_path)
                inserted: int
                skipped: int
                with dao.savepoint():
                    inserted, skipped = dao.insert_or_ignore(df)
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

        dao.commit()
        self.disconnect()

        self.finish_load(
            source="fs",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=dir_path,
            skipped_files=skipped_cnt,
            partial_files=partial_files,
        )
