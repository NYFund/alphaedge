import datetime
import random
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

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

    # 月營收的申報期限：上市櫃公司須於**次月 10 日**前申報上月營收。
    # 寬限天數的意義與財報三表相同——逾期申報與申請延期都會落在期限之後，
    # 而把「還沒送件的公司」當成「來源就是沒有」的代價是它送件後再也補不進來
    FILING_DEADLINE_DAY: int = 10
    FILING_GRACE_DAYS: int = 30

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
        year_months: List[Tuple[int, int]] = self.plan_pending_year_months(
            start_year, start_month, end_year, end_month
        )
        logger.info(f"本次待更新年月：{len(year_months)} 個")
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

    def plan_pending_year_months(
        self,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
    ) -> List[Tuple[int, int]]:
        """
        - Description:
            算出這次要請求的年月：區間內所有年月 − 表內**已收齊**的年月

            **不可用「表內最新年月 +1」起跑**：中間某個月失敗被跳過之後，只要
            下一個月成功入庫，`MAX` 就越過它，那個月從此不會再被請求。財報三表
            已改成差集，月營收沒有跟進——`docs/pipeline/etl-ingestion.md` 的對照表
            寫的也是差集，程式與文件在此之前不一致。

            **不可用 years × months 的笛卡兒積**：起點 2025/03、終點 2026/12 時
            `months` 只會是 [3..12]，2026/01 與 2026/02 不會被爬；起點月份大於
            終點月份時甚至是空清單，整輪什麼都不做而沒有任何錯誤。
        - Parameters:
            - start_year / start_month: int
                區間起點
            - end_year / end_month: int
                區間終點
        - Return:
            - List[Tuple[int, int]]
                由早到晚排序的 (year, month)
        """

        year_months: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
            start_year, start_month, end_year, end_month, periods_per_year=12
        )
        existing: Set[Tuple[int, int]] = self.dao.get_existing_year_months()

        # **申報期還沒關閉的月份不算收齊**：月營收是逐家公司在次月 10 日前申報的，
        # 申報期內入庫的只有「已送件的那批公司」。當成收齊的話，年月差集從此跳過
        # 它，後送件的公司永遠補不進來——而每個月初跑一次日常更新就會踩到。
        # 判準與財報三表同一套（見 `FinancialStatementUpdater.is_season_settled()`），
        # 只是週期不同；寫入走 `INSERT OR IGNORE`，重問不會產生重複列
        incomplete: Set[Tuple[int, int]] = {
            year_month
            for year_month in existing
            if not self.is_month_settled(*year_month)
        }
        if incomplete:
            logger.info(
                f"[mrr] {len(incomplete)} 個月份仍在申報期內"
                f"（{sorted(f'{y}/{m:02d}' for y, m in incomplete)}），"
                f"本次一併重問以補進後送件的公司"
            )

        settled: Set[Tuple[int, int]] = existing - incomplete
        pending: List[Tuple[int, int]] = [
            year_month for year_month in year_months if year_month not in settled
        ]

        # 只有夾在表內最早與最新之間的才算缺口：早於最早的是來源本就沒有的月份，
        # 晚於最新的是尚未公布的月份，兩者每輪都會出現，報出來只是噪音
        if existing:
            earliest: Tuple[int, int] = min(existing)
            latest: Tuple[int, int] = max(existing)
            gaps: List[str] = [
                f"{year}/{month:02d}"
                for year, month in pending
                if earliest < (year, month) < latest
            ]
            if gaps:
                logger.warning(
                    f"[mrr] 偵測到 {len(gaps)} 個年月缺口"
                    f"（表內最新為 {latest[0]}/{latest[1]:02d}），本次一併回補：{gaps[:10]}"
                )

        return pending

    @classmethod
    def is_month_settled(
        cls, year: int, month: int, today: Optional[datetime.date] = None
    ) -> bool:
        """
        - Description:
            該月份的營收申報期是否已關閉（含寬限期）

            上市櫃公司須於**次月 10 日**前申報上月營收。在那之前（以及寬限期內）
            拿到的結果只是「已送件的那批公司」，不代表來源只有這麼多——
            兩者無法區分正是本判準要解的問題，見
            `FinancialStatementUpdater.is_season_settled()`。
        - Parameters:
            - year / month: int
                年月
            - today: Optional[datetime.date]
                今天；None 取系統日期（測試可覆寫）
        - Return:
            - bool
                申報期已關閉為 True
        """

        deadline_year: int = year + 1 if month == 12 else year
        deadline_month: int = 1 if month == 12 else month + 1
        deadline: datetime.date = datetime.date(
            deadline_year, deadline_month, cls.FILING_DEADLINE_DAY
        ) + datetime.timedelta(days=cls.FILING_GRACE_DAYS)

        return (today or datetime.date.today()) > deadline

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
