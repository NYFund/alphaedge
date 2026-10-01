from pathlib import Path
from typing import Any, Optional, Set, Type

from core.config import SHORT_SALE_LIST_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.tw.stock_short_sale_list_dao import StockShortSaleListDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""台股平盤下得融（借）券賣出名單入庫：落地端，schema 與寫入語意全在 `StockShortSaleListDAO`"""


class StockShortSaleListLoader(BaseDataLoader):
    """Stock ShortSale List Loader"""

    DAO_CLASS: Type[Any] = StockShortSaleListDAO
    SOURCE: str = "short_sale_list"

    def db_path(self) -> Path:
        """路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫"""

        return Path(TW_STOCK_DB_PATH)

    def downloads_path(self) -> Path:
        """CSV 來源目錄；路徑在呼叫當下才讀，測試才能以 monkeypatch 改寫"""

        return SHORT_SALE_LIST_DOWNLOADS_PATH

    def create_missing_tables(self) -> None:
        """確保名單資料表與 `(stock_id, date)` 索引存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """把 downloads 內的 CSV 入庫；骨架見 `BaseDataLoader.load_csv_directory()`"""

        self.load_csv_directory(remove_files=remove_files, only_dates=only_dates)
