import datetime
import random
import time
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.tw.cleaners.stock_dividend_cleaner import StockDividendCleaner
from core.pipeline.tw.crawlers.stock_dividend_crawler import StockDividendCrawler
from core.pipeline.tw.loaders.stock_dividend_loader import StockDividendLoader
from core.pipeline.utils.exceptions import CleanFailureError, ColumnLayoutError
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
除權除息計算結果表爬蟲資料時間表：
1. TWSE（上市）
    - TWT49U：官方自民國 90 年起提供，本專案實測 2013 年起表格結構未再改制
2. TPEX（上櫃）
    - 櫃買中心 `bulletin/exDailyQ`：官方頁面標示資料自 2008/01/02 起提供

兩個來源皆支援日期區間，故一律**以「年」為單位請求**，2013~今日各僅需十餘次請求，
不退化成逐日爬取（一年 250 次 vs 1 次）。
"""


class StockDividendUpdater(BaseDataUpdater):
    """Stock Dividend Updater"""

    # TWSE 區間請求之間的節流（一年一次請求，不需要像逐日爬蟲那樣長時間休息）
    YEAR_REQUEST_DELAY_MIN: int = 3
    YEAR_REQUEST_DELAY_MAX: int = 8

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

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_dividend.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def update(
        self,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
    ) -> None:
        """
        - Description:
            更新除權除息計算結果表

            **每次都掃整個區間，不從 `MAX(date)+1` 續跑**：本來源支援區間查詢，
            一年只要一次請求，13 年也只有 26 次；而 `MAX(date)+1` 會讓中間任何
            一年的缺漏永遠補不回來。入庫走 `INSERT OR REPLACE`，
            重跑是冪等的。
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日（預設值不可在 def 行求值）
        """

        logger.info("* Start Updating TWSE & TPEX Dividend Data...")

        end_date: datetime.date = end_date or datetime.date.today()

        if start_date > end_date:
            logger.info("Dividend data is already up to date")
            return

        # TWSE：以年為單位請求，一年一次
        years: List[int] = TimeUtils.generate_year_range(start_date.year, end_date.year)
        stats: UpdateStats = UpdateStats()
        layout_failures: List[str] = []

        for year in years:
            year_start: datetime.date = max(start_date, datetime.date(year, 1, 1))
            year_end: datetime.date = min(end_date, datetime.date(year, 12, 31))

            period: str = (
                f"{TimeUtils.format_date(year_start)}_{TimeUtils.format_date(year_end)}"
            )

            twse: CrawlResult = self.crawler.crawl_twse_dividend(year_start, year_end)
            tpex: CrawlResult = self.crawler.crawl_tpex_dividend(year_start, year_end)
            stats.record(twse, tpex)

            # Step 2: Clean
            # 版面改制逐年隔離：其餘年份照樣清洗入庫，但收尾必須非零結束——
            # 除權息是還原價的輸入，靜靜停止更新不會有任何錯誤訊息
            for result, source in ((twse, "twse"), (tpex, "tpex")):
                if not result.is_ok:
                    continue
                clean = getattr(self.cleaner, f"clean_{source}_dividend")
                try:
                    cleaned: Optional[pd.DataFrame] = clean(
                        result.data, file_name=f"{source}_{period}"
                    )
                except ColumnLayoutError as error:
                    logger.error(
                        f"[dividend] {source.upper()} {year} 版面改制：{error}"
                    )
                    layout_failures.append(f"{source} {year}")
                    stats.count_clean_failure()
                    continue

                if cleaned is None or cleaned.empty:
                    logger.warning(
                        f"Cleaned {source.upper()} dataframe empty for {year}"
                    )

            delay: int = random.randint(
                self.YEAR_REQUEST_DELAY_MIN, self.YEAR_REQUEST_DELAY_MAX
            )
            time.sleep(delay)

        # `requested` 這裡的單位是「年」而不是「天」：本來源支援區間查詢，一年一次請求
        stats.report("dividend（單位：年）")

        # Step 3: Load
        # **先入庫再拋**：沒改制的年份已經清洗好了，不入庫等於這次白跑
        self.loader.add_to_db(remove_files=False)

        # 更新後重新取得Table最新的日期
        table_latest_date: Optional[str] = self.dao.get_latest_date()
        if table_latest_date:
            logger.info(
                f"Stock dividend data updated. Latest available date: {table_latest_date}"
            )
        else:
            logger.warning("No new stock dividend data was updated")

        if layout_failures:
            raise CleanFailureError("dividend", layout_failures)
