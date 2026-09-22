from pathlib import Path
from typing import Any, Optional, Type

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
Market Holiday Loader

資料量一年只有數十列，**不落地 CSV**：crawler → cleaner → 直接入庫。
重跑同一年度是「整年替換」而不是追加，站方更正公告時表內才不會殘留舊列。
"""


class MarketHolidayLoader(BaseDataLoader):
    """Market Holiday Loader"""

    def __init__(self, dao: Optional[MarketHolidayDAO] = None) -> None:
        """
        - Description:
            建立開休市日期 loader
        - Parameters:
            - dao: Optional[MarketHolidayDAO]
                共用的 DAO（通常由 updater 傳入）。指定時 loader 不擁有它，
                `disconnect()` 不會關閉；未指定時 loader 自行建立
        """

        super().__init__(dao)

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()
        self.create_missing_tables()

    DAO_CLASS: Type[Any] = MarketHolidayDAO

    def db_path(self) -> Path:
        """路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫"""

        return Path(TW_STOCK_DB_PATH)

    def create_missing_tables(self) -> None:
        """確保開休市日期表存在"""

        self.dao.ensure_table()

    def add_to_db(self, df: pd.DataFrame, year: int) -> int:
        """
        - Description:
            整年替換寫入並 commit；寫到一半出錯時整年回滾，舊資料保持原樣
        - Parameters:
            - df: pd.DataFrame
                cleaner 產出的單一年度資料
            - year: int
                西元年
        - Return:
            - int
                寫入的列數
        """

        with self.dao.savepoint("market_holiday_year"):
            inserted: int = self.dao.replace_year(year, df)
        self.dao.commit()

        logger.info(f"[market_holiday] {year} 年寫入 {inserted} 列（整年替換）")
        return inserted
