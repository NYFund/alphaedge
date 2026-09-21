import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import shioaji as sj
from loguru import logger
from shioaji import Ticks

from core.config import TICK_DOWNLOADS_PATH
from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.utils.log_manager import LogManager

"""
Shioaji 台股 ticks 資料時間表：
From: 2020/03/02 ~ Today

目前資料庫資料時間：
From 2020/04/01 ~ 2024/05/10
"""


class StockTickCrawler(BaseDataCrawler):
    """爬取上市櫃股票 ticks"""

    def __init__(self) -> None:
        """初始化爬蟲設定"""

        super().__init__()

        self.tick_dir: Path = TICK_DOWNLOADS_PATH
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""

        # Set logger
        LogManager.setup_logger("crawl_stock_tick.log")

        # Create the tick downloads directory
        self.tick_dir.mkdir(parents=True, exist_ok=True)

    def crawl(self) -> None:
        """Crawl Tick Data"""
        pass

    def crawl_stock_tick(
        self,
        api: sj.Shioaji,
        date: datetime.date,
        code: str,
    ) -> Optional[pd.DataFrame]:
        """
        透過 Shioaji 爬取指定個股的 tick data

        注意：API 配額檢查應在調用此方法前進行，以統一管理配額檢查邏輯
        """

        try:
            ticks: Ticks = api.ticks(
                contract=api.Contracts.Stocks.get(code), date=date.isoformat()
            )
            tick_df: pd.DataFrame = pd.DataFrame({**ticks})

            return tick_df if not tick_df.empty else None

        except Exception as e:
            logger.error(f"Error Crawling Tick Data: {code} {date} | {e}")
            return None
