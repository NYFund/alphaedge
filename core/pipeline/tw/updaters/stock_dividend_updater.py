from typing import Optional

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.pipeline.shared.base_updater import YearlyRangeTwoMarketUpdater
from core.pipeline.tw.cleaners.stock_dividend_cleaner import StockDividendCleaner
from core.pipeline.tw.crawlers.stock_dividend_crawler import StockDividendCrawler
from core.pipeline.tw.loaders.stock_dividend_loader import StockDividendLoader

"""
除權除息計算結果表爬蟲資料時間表：
1. TWSE（上市）
    - TWT49U：官方自民國 90 年起提供，本專案實測 2013 年起表格結構未再改制
2. TPEX（上櫃）
    - 櫃買中心 `bulletin/exDailyQ`：官方頁面標示資料自 2008/01/02 起提供

兩個來源皆支援日期區間，故一律**以「年」為單位請求**，2013~今日各僅需十餘次請求，
不退化成逐日爬取（一年 250 次 vs 1 次）。流程見 `YearlyRangeTwoMarketUpdater`。
"""


class StockDividendUpdater(YearlyRangeTwoMarketUpdater):
    """Stock Dividend Updater"""

    SOURCE: str = "dividend"
    SOURCE_LABEL: str = "Dividend"
    TABLE_LABEL: str = "Stock dividend"
    LOG_FILE_NAME: str = "update_dividend.log"
    # 除權息的清洗檔名不帶 `.csv`（cleaner 自己補），與公司行動不同
    FILE_SUFFIX: str = ""

    def __init__(self) -> None:
        super().__init__()

        # 讀（最新日期）與寫（loader）共用同一個 DAO，一次更新只開一條連線
        self.dao: StockDividendDAO = StockDividendDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockDividendCrawler = StockDividendCrawler()
        self.cleaner: StockDividendCleaner = StockDividendCleaner()
        self.loader: StockDividendLoader = StockDividendLoader(dao=self.dao)

        self.setup()
