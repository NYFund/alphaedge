import datetime
from pathlib import Path
from typing import Any, Optional

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
                逐筆成交；查無資料（非交易日、停牌、代號已不在合約表）時為 None
        - Raise:
            - ConnectionError
                Shioaji 取資料失敗

            **失敗不可回 None**：呼叫端會把 None 當成「這天沒有成交」，
            那一天就被當成已處理，之後再也不會重爬。
        """

        contract: Optional[Any] = api.Contracts.Stocks.get(code)
        if contract is None:
            logger.warning(f"{code} 不在 Shioaji 合約表（可能已下市），略過 {date}")
            return None

        try:
            ticks: Ticks = api.ticks(contract=contract, date=date.isoformat())
        except Exception as e:
            # **刻意的盲捕**：Shioaji 1.7 沒有公開的例外型別（`shioaji.error` 裡沒有任何
            # 例外類別），連線、逾時與伺服器端錯誤各自拋什麼無從列舉。
            # 一律轉成 `ConnectionError` 交給 updater 計為失敗，型別名留在訊息裡
            raise ConnectionError(
                f"Shioaji 取 tick 失敗：{code} {date}（{type(e).__name__}: {e}）"
            ) from e

        tick_df: pd.DataFrame = pd.DataFrame({**ticks})
        return tick_df if not tick_df.empty else None
