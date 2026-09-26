import datetime
from typing import List, Optional

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.tw.cleaners.stock_chip_cleaner import StockChipCleaner
from core.pipeline.tw.crawlers.stock_chip_crawler import StockChipCrawler
from core.pipeline.tw.loaders.stock_chip_loader import StockChipLoader

"""
三大法人爬蟲資料時間表：
1. TWSE
    - TWSE: 2012/5/2 開始提供
    - TWSE 改制時間: 2014/12/1, 2017/12/18
2. TPEX
    - TPEX: 2007/4/20 開始提供
    - TPEX 改制時間: 2018/1/15
"""


class StockChipUpdater(DailyTwoMarketUpdater):
    """Stock Chip Updater"""

    SOURCE: str = "chip"
    SOURCE_LABEL: str = "Chip"
    LOG_FILE_NAME: str = "update_chip.log"

    def __init__(self) -> None:
        super().__init__()

        # **讀（日期規劃）與寫（loader）共用同一個 DAO**：各開一條連線到同一個 DB
        # 會互搶寫入鎖，updater 那條也容易忘了關
        self.dao: StockChipDAO = StockChipDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockChipCrawler = StockChipCrawler()
        self.cleaner: StockChipCleaner = StockChipCleaner()
        self.loader: StockChipLoader = StockChipLoader(dao=self.dao)

        self.setup()

    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """以 `price` 表的交易日為日曆（與 `chip`／`margin` 另一支共用同一份實作）"""

        return self.plan_dates_by_price_calendar(progress, start_date, end_date)
