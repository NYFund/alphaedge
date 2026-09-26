import datetime
from typing import List, Optional

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.tw.cleaners.stock_margin_cleaner import StockMarginCleaner
from core.pipeline.tw.crawlers.stock_margin_crawler import StockMarginCrawler
from core.pipeline.tw.loaders.stock_margin_loader import StockMarginLoader

"""
信用交易（融資融券餘額）爬蟲資料時間表：
1. TWSE
    - MI_MARGN（selectType=ALL）：官方自民國 90/01/01（2001/01/01）起提供，
      本專案實測 2013/1/1 起可取得且表格結構未再改制
2. TPEX
    - 上櫃融資融券餘額：官方自民國 96/01（2007/01）起提供，
      本專案實測 2013/1/1 起可取得且表格結構未再改制
"""


class StockMarginUpdater(DailyTwoMarketUpdater):
    """Stock Margin Updater"""

    SOURCE: str = "margin"
    SOURCE_LABEL: str = "Margin"
    LOG_FILE_NAME: str = "update_margin.log"

    def __init__(self) -> None:
        super().__init__()

        # **讀（日期規劃）與寫（loader）共用同一個 DAO**：各開一條連線到同一個 DB
        # 會互搶寫入鎖，updater 那條也容易忘了關
        self.dao: StockMarginDAO = StockMarginDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockMarginCrawler = StockMarginCrawler()
        self.cleaner: StockMarginCleaner = StockMarginCleaner()
        self.loader: StockMarginLoader = StockMarginLoader(dao=self.dao)

        self.setup()

    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """以 `price` 表的交易日為日曆（與 `chip`／`margin` 另一支共用同一份實作）"""

        return self.plan_dates_by_price_calendar(progress, start_date, end_date)
