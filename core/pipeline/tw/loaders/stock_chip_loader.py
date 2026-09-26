from pathlib import Path
from typing import Any, Optional, Set, Type

from core.config import CHIP_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""台股三大法人籌碼入庫：落地端，schema 與寫入語意全在 `StockChipDAO`"""


class StockChipLoader(BaseDataLoader):
    """Stock Chip Loader"""

    DAO_CLASS: Type[Any] = StockChipDAO
    SOURCE: str = "chip"

    def db_path(self) -> Path:
        """路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫"""

        return Path(TW_STOCK_DB_PATH)

    def setup(self) -> None:
        """先備好來源目錄屬性再走基底的連線與建表"""

        # **屬性名保留**：既有呼叫端與測試以 `chip_dir` 指定來源目錄
        self.chip_dir: Path = CHIP_DOWNLOADS_PATH

        super().setup()

    def downloads_path(self) -> Path:
        """CSV 來源目錄；路徑在呼叫當下才讀，測試才能以 monkeypatch 改寫"""

        return self.chip_dir

    def create_missing_tables(self) -> None:
        """確保三大法人盤後籌碼資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """把 downloads 內的 CSV 入庫；骨架見 `BaseDataLoader.load_csv_directory()`"""

        self.load_csv_directory(remove_files=remove_files, only_dates=only_dates)
