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
台股 tick 爬蟲（Shioaji）

1. Shioaji 提供的 tick 起始日為 2020/03/02，更早的日期一律查無資料。
2. 資料庫既有區間（截至最後一次回補）：2020/04/01 ~ 2024/05/10。
3. Shioaji 的資料 API 有每日流量上限，配額檢查與多組金鑰輪替由 updater 統一負責。
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

        LogManager.setup_logger("crawl_stock_tick.log")

        self.tick_dir.mkdir(parents=True, exist_ok=True)

    def crawl(self) -> None:
        """本爬蟲沒有統一入口，逐檔取資料請用 `crawl_stock_tick()`"""
        pass

    def crawl_stock_tick(
        self,
        api: sj.Shioaji,
        date: datetime.date,
        code: str,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            透過 Shioaji 取得單一個股單日的逐筆成交

            **配額檢查在呼叫端**：多檔多日回補時由 updater 統一管理，
            分散在此會讓每一次呼叫都要重查一次用量。
        - Parameters:
            - api: sj.Shioaji
                已登入的 Shioaji API
            - date: datetime.date
                交易日
            - code: str
                證券代號
        - Return:
            - Optional[pd.DataFrame]
                逐筆成交；查無資料或取得失敗時為 None
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
