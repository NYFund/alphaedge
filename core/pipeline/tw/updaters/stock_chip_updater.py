import datetime
import random
import time
from typing import List, Optional, Set

from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.pipeline.tw.cleaners.stock_chip_cleaner import StockChipCleaner
from core.pipeline.tw.crawlers.stock_chip_crawler import StockChipCrawler
from core.pipeline.tw.loaders.stock_chip_loader import StockChipLoader
from core.utils.log_manager import LogManager

"""
三大法人爬蟲資料時間表：
1. TWSE
    - TWSE: 2012/5/2 開始提供
    - TWSE 改制時間: 2014/12/1, 2017/12/18
2. TPEX
    - TPEX: 2007/4/20 開始提供
    - TPEX 改制時間: 2018/1/15
"""


class StockChipUpdater(BaseDataUpdater):
    """Stock Chip Updater"""

    # 每爬幾天就入庫一次。整段爬完才入庫的話，中斷等於前功盡棄——
    # 2013 起的回補有 3,300 個交易日、數小時，中途失敗要全部重來。
    # 分批之後最多只損失最後一批（未入庫的部分），重跑會自動接續。
    LOAD_BATCH_SIZE: int = 100
    BATCH_SLEEP_EVERY_N_FILES: int = 100
    BATCH_SLEEP_DURATION_SECONDS: int = 120
    BATCH_RANDOM_DELAY_MIN: int = 1
    BATCH_RANDOM_DELAY_MAX: int = 5

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

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_chip.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def load_batch(self, batch_dates: List[str]) -> None:
        """
        - Description:
            入庫本批爬取的日期

            **只載入本批的檔案**：loader 預設會掃整個 downloads 目錄，若每批都全掃，
            13 年的回補會變成「數十批 × 數千檔」的重複讀取。
        - Parameters:
            - batch_dates: List[str]
                本批的日期字串（`YYYYMMDD`），對應 downloads 內的檔名後綴
        """

        logger.info(
            f"* Loading batch: {len(batch_dates)} 天（{batch_dates[0]} ~ {batch_dates[-1]}）"
        )
        self.loader.add_to_db(remove_files=False, only_dates=set(batch_dates))

    def update(
        self,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
    ) -> None:
        """
        - Description:
            更新三大法人籌碼

            **以 `price` 表的交易日為日曆**：比「非週末」精確，涵蓋國定假日與
            補行交易日；候選日期是差集而非 `MAX(date)+1`。
            `price` 尚未更新到的區間會少幾天，下次執行自然補上。
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日（預設值不可在 def 行求值）
        """

        logger.info("* Start Updating TWSE & TPEX Chip Data...")

        end_date: datetime.date = end_date or datetime.date.today()

        # Step 1: Crawl
        progress: DateProgressStore = DateProgressStore("chip")
        # 日曆來源（`price`）與目標表同庫，共用本 updater 的連線
        calendar_dates: Set[datetime.date] = DatePlanner.get_trading_dates(
            StockPriceDAO(conn=self.dao.conn), start_date, end_date
        )
        dates: List[datetime.date] = DatePlanner.plan(
            dao=self.dao,
            start_date=start_date,
            end_date=end_date,
            no_data_dates=progress.no_data,
            incomplete_dates=progress.incomplete,
            calendar_dates=calendar_dates or None,
        )
        logger.info(f"本次待更新日期：{len(dates)} 天（{start_date} ~ {end_date}）")

        file_cnt: int = 0
        batch_dates: List[str] = []
        stats: UpdateStats = UpdateStats()
        cleaner_failures: List[datetime.date] = []

        for date in dates:
            logger.info(date.strftime("%Y/%m/%d"))
            twse: CrawlResult = self.crawler.crawl_twse_chip(date)
            tpex: CrawlResult = self.crawler.crawl_tpex_chip(date)
            day_status: CrawlStatus = self.record_market_day(stats, twse, tpex)

            # Step 2: Clean
            # 任一市場沒問到（含一邊查無資料）時兩邊都不清洗：這天反正不入庫，
            # 清洗只會在 downloads 留下半份 CSV
            cleaned: bool = True
            if day_status is not CrawlStatus.FAILED:
                if twse.is_ok:
                    cleaned &= self.clean_one(
                        self.cleaner.clean_twse_chip, twse.data, date, "TWSE"
                    )

                if tpex.is_ok:
                    cleaned &= self.clean_one(
                        self.cleaner.clean_tpex_chip, tpex.data, date, "TPEX"
                    )

            if not cleaned:
                cleaner_failures.append(date)
                day_status = CrawlStatus.FAILED
                stats.count_clean_failure()

            progress.record(date, day_status)

            file_cnt += 1
            # **只有兩個市場都問到的日子才入庫**：只入庫一邊的話，重試成功之前
            # 回測讀到的是半個市場，且不會有任何錯誤。清洗失敗同樣擋下——
            # 另一邊的 CSV 可能已經寫出
            if day_status is CrawlStatus.FAILED:
                self.report_partial_day("chip", date, twse, tpex)
            else:
                batch_dates.append(date.strftime("%Y%m%d"))

            # Step 3: Load（分批）
            if len(batch_dates) >= self.LOAD_BATCH_SIZE:
                self.load_batch(batch_dates)
                batch_dates = []
                # 與入庫同步落盤：中斷時已確認過的休市日不必再問一次
                progress.save()

            if file_cnt == self.BATCH_SLEEP_EVERY_N_FILES:
                logger.info("Sleep 2 minutes...")
                file_cnt = 0
                time.sleep(self.BATCH_SLEEP_DURATION_SECONDS)
            else:
                delay: int = random.randint(
                    self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
                )
                time.sleep(delay)

        # 收尾：載入最後一批未達批量的日期
        if batch_dates:
            self.load_batch(batch_dates)

        progress.save()
        stats.report("chip")
        self.report_cleaner_failures(cleaner_failures)

        # 更新後重新取得Table最新的日期
        table_latest_date: Optional[str] = self.dao.get_latest_date()
        if table_latest_date:
            logger.info(
                f"Stock chip data updated. Latest available date: {table_latest_date}"
            )
        else:
            logger.warning("No new stock chip data was updated")
