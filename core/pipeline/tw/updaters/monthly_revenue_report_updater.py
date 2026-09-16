import random
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.tw.cleaners.monthly_revenue_report_cleaner import (
    MonthlyRevenueReportCleaner,
)
from core.pipeline.tw.crawlers.monthly_revenue_report_crawler import (
    MonthlyRevenueReportCrawler,
)
from core.pipeline.tw.loaders.monthly_revenue_report_loader import (
    MonthlyRevenueReportLoader,
)
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
資料區間
- 上市: 102（2013）年前資料無區分國內外（目前先從 102 年開始爬）
- 上櫃: 102（2013）年前資料無區分國內外（目前先從 102 年開始爬）
"""


class MonthlyRevenueReportUpdater(BaseDataUpdater):
    """TWSE & TPEX Monthly Revenue Report Updater"""

    BATCH_SLEEP_EVERY_N_FILES: int = 10
    BATCH_SLEEP_DURATION_SECONDS: int = 30
    BATCH_RANDOM_DELAY_MIN: int = 1
    BATCH_RANDOM_DELAY_MAX: int = 5

    def __init__(self) -> None:
        super().__init__()

        # 讀（最新年月）與寫（loader）共用同一個 DAO，一次更新只開一條連線
        self.dao: MonthlyRevenueDAO = MonthlyRevenueDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: MonthlyRevenueReportCrawler = MonthlyRevenueReportCrawler()
        self.cleaner: MonthlyRevenueReportCleaner = MonthlyRevenueReportCleaner()
        self.loader: MonthlyRevenueReportLoader = MonthlyRevenueReportLoader(
            dao=self.dao
        )

        # Data Directory
        self.mmr_dir: Path = MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        # 設定 log 檔案儲存路徑
        LogManager.setup_logger("update_monthly_revenue_report.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def update(
        self,
        start_year: int,
        end_year: int,
        start_month: int,
        end_month: int,
    ) -> None:
        """Update the Database"""

        logger.info("* Start Updating TWSE & TPEX Monthly Revenue Report Data...")

        # Step 1: Crawl
        # 取得要開始更新的年份、月份
        start_year: int
        start_month: int
        start_year, start_month = self.get_actual_update_start_year_month(
            default_year=start_year,
            default_month=start_month,
        )

        logger.info(f"Latest data date in database: {start_year}/{start_month}")
        # Set Up Update Period
        # **不可用 years × months 的笛卡兒積**：起點 2025/03、終點 2026/12 時
        # `months` 只會是 [3..12]，2026/01 與 2026/02 不會被爬；起點月份大於終點
        # 月份時 `months` 甚至是空清單，整輪什麼都不做。兩種情況都不會有任何錯誤
        # ——那些年月只是從來沒出現在迴圈裡
        year_months: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
            start_year, start_month, end_year, end_month, periods_per_year=12
        )
        file_cnt: int = 0
        stats: UpdateStats = UpdateStats()

        for year, month in year_months:
            logger.info(f"* {year}/{month}")
            result: CrawlResult = self.crawler.crawl(year, month)
            stats.record(result)

            # Step 2: Clean
            if not result.is_ok:
                continue

            cleaned_df: pd.DataFrame = self.cleaner.clean_monthly_revenue(
                result.tables, year, month
            )

            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned monthly revenue report dataframe empty on {year}/{month}"
                )
                continue

            file_cnt += 1
            if file_cnt == self.BATCH_SLEEP_EVERY_N_FILES:
                logger.info("Sleep 30 seconds...")
                file_cnt = 0
                time.sleep(self.BATCH_SLEEP_DURATION_SECONDS)
            else:
                delay: int = random.randint(
                    self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
                )
                time.sleep(delay)

        # `requested` 這裡的單位是「年月」而不是「天」
        stats.report("mrr（單位：年月）")

        # Step 3: Load
        self.loader.add_to_db(remove_files=False)

        # 更新後重新取得最新年月
        latest: Optional[Tuple[int, int]] = self.dao.get_latest_year_month()

        if latest is not None:
            logger.info(
                f"Monthly revenue data updated. Latest available date: {latest[0]}/{latest[1]}"
            )
        else:
            logger.warning("No new monthly revenue data was updated")

    def get_actual_update_start_year_month(
        self,
        default_year: int = 2025,
        default_month: int = 1,
    ) -> Tuple[int, int]:
        """
        - Description:
            回傳下一筆應更新的 (year, month)；表不存在或為空時回傳預設值

            **查詢錯誤一律往外拋**：舊版 `except Exception` 後回傳預設值，
            「DB 被鎖住、欄位打錯」會被當成「表是空的」，從預設起點靜默重跑整段回補。
        - Parameters:
            - default_year: int
                無資料時的起始年
            - default_month: int
                無資料時的起始月
        - Return:
            - Tuple[int, int]
                下一個要更新的（year, month）
        """

        # Step 1: 先取得資料表中最新的（year, month）
        latest: Optional[Tuple[int, int]] = self.dao.get_latest_year_month()
        if latest is None:
            return default_year, default_month

        latest_year: int
        latest_month: int
        latest_year, latest_month = latest

        # Step 2: 計算下一個月份（處理進位）
        if latest_month == 12:
            return latest_year + 1, 1
        else:
            return latest_year, latest_month + 1
