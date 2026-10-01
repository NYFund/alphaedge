import datetime
from typing import List, Optional

from loguru import logger

from core.config import SHORT_SALE_LIST_START_DATE, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_short_sale_list_dao import StockShortSaleListDAO
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.tw.cleaners.stock_trading_list_cleaner import (
    StockTradingListCleaner,
)
from core.pipeline.tw.crawlers.stock_trading_list_crawler import (
    StockTradingListCrawler,
)
from core.pipeline.tw.loaders.stock_short_sale_list_loader import (
    StockShortSaleListLoader,
)

"""台股平盤下得融（借）券賣出名單：逐日爬上市＋上櫃、清洗、分批入庫（端點與陷阱見 `StockTradingListCrawler`）"""


class StockShortSaleListUpdater(DailyTwoMarketUpdater):
    """Stock ShortSale List Updater"""

    SOURCE: str = "short_sale_list"
    SOURCE_LABEL: str = "Short Sale List"
    LOG_FILE_NAME: str = "update_short_sale_list.log"

    def __init__(self) -> None:
        super().__init__()

        # 讀（日期規劃）與寫（loader）共用同一個 DAO，理由同 `StockMarginUpdater`
        self.dao: StockShortSaleListDAO = StockShortSaleListDAO(
            db_path=TW_STOCK_DB_PATH
        )
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockTradingListCrawler = StockTradingListCrawler()
        self.cleaner: StockTradingListCleaner = StockTradingListCleaner()
        self.loader: StockShortSaleListLoader = StockShortSaleListLoader(dao=self.dao)

        self.setup()

    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        以 `price` 表的交易日為日曆，起日不早於名單的起點

        **起點之前一律不請求**：TPEX 在起點之前回 `ok`、0 列，爬了只會被記成失敗、
        每次執行都重試一輪，而那些日子永遠不會有資料。
        """

        if start_date < SHORT_SALE_LIST_START_DATE:
            logger.info(
                f"Short Sale List 自 {SHORT_SALE_LIST_START_DATE} 起提供，起日 {start_date} 調整為該日"
            )
            start_date = SHORT_SALE_LIST_START_DATE

        return self.plan_dates_by_price_calendar(progress, start_date, end_date)
