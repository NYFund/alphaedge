import datetime
from typing import List, Optional, Set

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
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

        # **讀（日期規劃）與寫（loader）共用同一個 DAO**：舊版 updater 與 loader 各開
        # 一條連線到同一個 DB，updater 那條從不關閉，兩條連線還會互搶寫入鎖
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
        """
        以 `price` 表的交易日為日曆

        比「非週末」精確，涵蓋國定假日與補行交易日。`price` 尚未更新到的區間
        會少幾天，下次執行自然補上。
        """

        # 日曆來源（`price`）與目標表同庫，共用本 updater 的連線
        calendar_dates: Set[datetime.date] = DatePlanner.get_trading_dates(
            StockPriceDAO(conn=self.dao.conn), start_date, end_date
        )
        return DatePlanner.plan(
            dao=self.dao,
            start_date=start_date,
            end_date=end_date,
            no_data_dates=progress.no_data,
            incomplete_dates=progress.incomplete,
            calendar_dates=calendar_dates or None,
        )
