from pathlib import Path
from typing import Any, List, Optional, Set, Type

import pandas as pd
from loguru import logger

from core.config import FUTURES_PRICE_DOWNLOADS_PATH, TW_FUTURES_DB_PATH
from core.dao.connection import DBError
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.utils.exceptions import PipelineError

"""
Futures Price Loader

**與其他 loader 唯一的結構性差異：寫入 `tw_futures.db` 而非 `tw_stock.db`**。
期貨的主鍵是合約（product ＋ expiry ＋ session），與 `stock_id` 語意不同，
混在同一個 DB 會讓「這張表的主鍵是什麼」失去單一答案。
"""


class FuturesPriceLoader(BaseDataLoader):
    """Futures Price Loader"""

    def __init__(self, dao: Optional[FuturesPriceDAO] = None) -> None:
        """
        - Description:
            建立期貨每日行情 loader
        - Parameters:
            - dao: Optional[FuturesPriceDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        # Downloads directory Path
        self.futures_price_dir: Path = FUTURES_PRICE_DOWNLOADS_PATH

        super().__init__(dao)

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.futures_price_dir.mkdir(parents=True, exist_ok=True)

    DAO_CLASS: Type[Any] = FuturesPriceDAO

    def db_path(self) -> Path:
        """路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫"""

        return Path(TW_FUTURES_DB_PATH)

    def create_missing_tables(self) -> None:
        """確保台期貨每日行情資料表存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """
        - Description:
            將資料夾中的所有 CSV 檔存入 tw_futures.db 的每日行情表；有任何檔案失敗就拋
            `DataLoadError`

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾，
            不會被迴圈結束後的 `commit()` 一起寫進去。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
            - only_dates: Optional[Set[str]]
                只處理這些日期（`YYYYMMDD`）的檔案；None 表示整個目錄
        """

        if self.dao is None:
            self.connect()

        self.create_missing_tables()

        file_cnt: int = 0
        failed_files: List[str] = []
        partial_files: List[str] = []
        skipped_cnt: int = 0

        for file_path in self.select_csv_files(self.futures_price_dir, only_dates):
            try:
                # product／expiry／session 一律當字串：expiry 可能是 202609 或
                # 202609W1，讓 pandas 自行推斷會把前者變成 202609.0 而主鍵走樣
                df: pd.DataFrame = pd.read_csv(
                    file_path,
                    dtype={"product": str, "expiry": str, "session": str},
                )
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
            except (OSError, ValueError, KeyError, DBError, PipelineError) as e:
                # 讀不到檔（`OSError`）、CSV 解析失敗（`ValueError`，`ParserError`
                # 與 `EmptyDataError` 都是它的子類）、來源改了欄位名（`KeyError`）、
                # 入庫失敗（`DBError`，即 `sqlite3.Error`——`core/pipeline/` 不得
                # 直接 import 驅動，由 `core.dao` 提供具名別名）。
                # **`PipelineError` 一定要收**：清洗與驗證階段自己拋的那些
                # （例如 `SymbolNameConflictError`）也屬於單檔失敗，
                # 漏收會讓它直接逃出去，整批在第一個壞檔就中止。
                # **單檔失敗不中止整批**，跑完由 `finish_load()` 一次報出
                logger.warning(f"Error saving {file_path}: {e}")
                failed_files.append(str(file_path))

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source="futures_price",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=FUTURES_PRICE_DOWNLOADS_PATH,
            skipped_files=skipped_cnt,
            partial_files=partial_files,
        )
