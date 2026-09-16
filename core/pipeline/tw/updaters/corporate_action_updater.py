import datetime
import random
import sqlite3
import time
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.tw.cleaners.corporate_action_cleaner import (
    OUTPUT_COLUMNS,
    CorporateActionCleaner,
)
from core.pipeline.tw.crawlers.corporate_action_crawler import CorporateActionCrawler
from core.pipeline.tw.loaders.corporate_action_loader import CorporateActionLoader
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
非除權息公司行動（減資／面額變更）更新器

兩個來源都支援日期區間，故一律**以「年」為單位請求**：2013~今日各只要十餘次請求，
不退化成逐日爬取。實測全期間上市 360 筆、上櫃 283 筆，量很小。

**每次都掃整個區間，不從 `MAX(date)+1` 續跑**：這類事件是「事後公告」，
而且量少到重掃整段也只有二十幾次請求；用 `MAX(date)+1` 會讓中間任何一年的缺漏
永遠補不回來。入庫走 `INSERT OR REPLACE`，重跑是冪等的。
"""


class CorporateActionUpdater(BaseDataUpdater):
    """Corporate Action Updater"""

    # 區間請求之間的節流（一年一次請求，不需要像逐日爬蟲那樣長時間休息）
    YEAR_REQUEST_DELAY_MIN: int = 3
    YEAR_REQUEST_DELAY_MAX: int = 8

    def __init__(self) -> None:
        super().__init__()

        # 讀（最新日期）與寫（loader）共用同一個 DAO，一次更新只開一條連線
        self.dao: CorporateActionDAO = CorporateActionDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[sqlite3.Connection] = self.dao.conn

        self.crawler: CorporateActionCrawler = CorporateActionCrawler()
        self.cleaner: CorporateActionCleaner = CorporateActionCleaner()
        self.loader: CorporateActionLoader = CorporateActionLoader(dao=self.dao)

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_corporate_action.log")

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
            更新公司行動事件表（減資、面額變更）
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日（預設值不可在 def 行求值）
        """

        logger.info("* Start Updating TWSE & TPEX Corporate Action Data...")

        end_date = end_date or datetime.date.today()

        if start_date > end_date:
            logger.info("Corporate action data is already up to date")
            return

        years: List[int] = TimeUtils.generate_year_range(start_date.year, end_date.year)
        stats: UpdateStats = UpdateStats()

        for year in years:
            year_start: datetime.date = max(start_date, datetime.date(year, 1, 1))
            year_end: datetime.date = min(end_date, datetime.date(year, 12, 31))

            period: str = (
                f"{TimeUtils.format_date(year_start)}_{TimeUtils.format_date(year_end)}"
            )

            twse: CrawlResult = self.crawler.crawl_twse_capital_reduction(
                year_start, year_end
            )
            tpex: CrawlResult = self.crawler.crawl_tpex_capital_reduction(
                year_start, year_end
            )
            stats.record(twse, tpex)

            for result, source in ((twse, "twse"), (tpex, "tpex")):
                if not result.is_ok:
                    continue
                cleaned: Optional[pd.DataFrame] = self.cleaner.clean(
                    result.data, source=source, file_name=f"{source}_{period}.csv"
                )
                if cleaned is None or cleaned.empty:
                    logger.warning(
                        f"Cleaned {source.upper()} dataframe empty for {year}"
                    )

            delay: int = random.randint(
                self.YEAR_REQUEST_DELAY_MIN, self.YEAR_REQUEST_DELAY_MAX
            )
            time.sleep(delay)

        # `requested` 的單位是「年」而不是「天」：本來源支援區間查詢，一年一次請求
        stats.report("corporate_action（單位：年）")

        self.loader.add_to_db(remove_files=False)

        table_latest_date: Optional[str] = self.dao.get_latest_date()
        if table_latest_date:
            logger.info(
                "Corporate action data updated. "
                f"Latest available date: {table_latest_date}"
            )
        else:
            logger.warning("No new corporate action data was updated")

    def load_detected_events(
        self, events: List[Dict[str, object]], file_name: str = "detected.csv"
    ) -> None:
        """
        - Description:
            把**人工確認過**的偵測事件以 `detected` 來源寫入

            ETF 的受益權單位分割不在任何結構化端點裡（S1 查遍 `TWTB8U` 與
            OpenAPI 143 個端點），只能由 `corporate_action_detector` 產出候選、
            人工確認後從這裡進來。0050 的 2025-06-18 一拆四就是這一類。

            **倍率由前收盤與參考價相除得出，與端點來源同一個定義**，
            這樣 S3 的還原係數才不必分辨資料是哪裡來的。
        - Parameters:
            - events: List[Dict[str, object]]
                每筆需含 `date`／`stock_id`／`證券名稱`／`停止買賣前收盤價`／
                `恢復買賣參考價`／`原因`
            - file_name: str
                落地的 CSV 檔名
        """

        if not events:
            logger.info("[corporate_action] 沒有要寫入的偵測事件")
            return

        df: pd.DataFrame = pd.DataFrame(events)
        required: List[str] = [
            "date",
            "stock_id",
            "證券名稱",
            "停止買賣前收盤價",
            "恢復買賣參考價",
            "原因",
        ]
        missing: List[str] = [column for column in required if column not in df.columns]
        if missing:
            raise ValueError(f"偵測事件缺欄位：{missing}")

        df["stock_id"] = df["stock_id"].astype(str)
        df["調整倍率"] = (df["恢復買賣參考價"] / df["停止買賣前收盤價"]).round(6)
        df["資料來源"] = "detected"
        df["事件類型"] = df.apply(
            lambda row: self.cleaner.classify_event(
                reason=row["原因"],
                ratio=row["調整倍率"],
                source="detected",
            ),
            axis=1,
        )

        self.cleaner.save(df[OUTPUT_COLUMNS], file_name)
        self.loader.add_to_db(remove_files=False)
        logger.info(f"[corporate_action] 已寫入 {len(df)} 筆人工確認的偵測事件")
