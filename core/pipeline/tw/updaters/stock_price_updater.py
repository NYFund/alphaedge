import datetime
from typing import Callable, List, Optional, Set

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.pipeline.tw.cleaners.stock_price_cleaner import StockPriceCleaner
from core.pipeline.tw.crawlers.stock_price_crawler import StockPriceCrawler
from core.pipeline.tw.loaders.stock_price_loader import StockPriceLoader

"""
TWSE 網站提供資料日期：
1. 2004/2/11 ~ present

TPEX 網站提供資料日期：
1. 上櫃資料從 96/7/2 以後才提供
2. 從 109/4/30 開始後 csv 檔的 column 不一樣
"""


class StockPriceUpdater(DailyTwoMarketUpdater):
    """Stock Price Updater"""

    SOURCE: str = "price"
    SOURCE_LABEL: str = "Price"
    LOG_FILE_NAME: str = "update_price.log"

    # 清洗後最少筆數（少於此不處理）
    MIN_DF_ROWS_AFTER_CLEAN: int = 2

    def __init__(self) -> None:
        super().__init__()

        # **讀（日期規劃）與寫（loader）共用同一個 DAO**：舊版 updater 與 loader 各開
        # 一條連線到同一個 DB，updater 那條從不關閉，兩條連線還會互搶寫入鎖
        self.dao: StockPriceDAO = StockPriceDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockPriceCrawler = StockPriceCrawler()
        self.cleaner: StockPriceCleaner = StockPriceCleaner()
        self.loader: StockPriceLoader = StockPriceLoader(dao=self.dao)

        self.setup()

    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        以平日為母集合，再把補行交易日補回來

        候選日期＝平日 − 表內已有 − 已確認無資料。**`price` 表自己就是日曆來源**，
        故沒有外部日曆可用。補行交易日（開市的週六）不在平日裡，改由 chip／margin
        手上已有的週末日期補回——否則 `price` 被刪掉的補行交易日永遠不會再被請求。
        """

        traded_weekends: Set[datetime.date] = DatePlanner.get_weekend_dates(
            [StockChipDAO(conn=self.dao.conn), StockMarginDAO(conn=self.dao.conn)],
            start_date,
            end_date,
        )
        return DatePlanner.plan(
            dao=self.dao,
            start_date=start_date,
            end_date=end_date,
            no_data_dates=progress.no_data,
            incomplete_dates=progress.incomplete,
            extra_dates=traded_weekends,
        )

    def clean_day(
        self, date: datetime.date, twse: CrawlResult, tpex: CrawlResult
    ) -> bool:
        """
        清洗單日兩個市場的收盤行情，並多擋一道原始列數門檻

        **只剩表頭或合計列時視為失敗**：那種表清洗後不會報錯，只會入庫幾列垃圾。
        跳過清洗卻照常入庫另一邊，就是半個市場。
        """

        cleaned: bool = True
        for result, label in ((twse, "TWSE"), (tpex, "TPEX")):
            if not result.is_ok:
                continue
            clean: Callable[..., Optional[pd.DataFrame]] = getattr(
                self.cleaner, f"clean_{label.lower()}_price"
            )
            if len(result.data) <= self.MIN_DF_ROWS_AFTER_CLEAN:
                logger.error(
                    f"[{label}] {date} 原始表只有 {len(result.data)} 列，"
                    f"本日計為失敗、下次執行會重試"
                )
                cleaned = False
                continue
            cleaned &= self.clean_one(clean, result.data, date, label)
        return cleaned
