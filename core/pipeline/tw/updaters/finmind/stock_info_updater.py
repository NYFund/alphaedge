from typing import Optional

import pandas as pd
from loguru import logger

from core.pipeline.tw.updaters.finmind.common import FinMindContext
from core.pipeline.utils import FinMindQuotaExhaustedError

"""台股總覽（不含權證／含權證）的更新流程"""


class StockInfoUpdater:
    """台股總覽 Updater：`taiwan_stock_info` 與 `taiwan_stock_info_with_warrant`"""

    def __init__(self, context: FinMindContext) -> None:
        self.context: FinMindContext = context

    def update_stock_info(self) -> None:
        """更新台股總覽資料（不含權證）"""

        logger.info("* Start Updating Taiwan Stock Info...")

        # Step 1: Crawl
        try:
            df: Optional[pd.DataFrame] = self.context.crawler.crawl_stock_info()
        except FinMindQuotaExhaustedError as e:
            logger.error(
                "⚠️ FinMind API quota exhausted. Please wait for quota reset and retry later. %s",
                e,
            )
            return
        if df is None or df.empty:
            logger.warning("No stock info data to update")
            return

        # Step 2: Clean
        cleaned_df: Optional[pd.DataFrame] = self.context.cleaner.clean_stock_info(df)
        if cleaned_df is None or cleaned_df.empty:
            logger.warning("Cleaned stock info data is empty")
            return

        # Step 3: Load
        # 確保 loader 有連接
        self.context.ensure_loader_connected()
        self.context.loader.load_stock_info()
        self.context.loader.commit()

        logger.info("✅ Taiwan Stock Info updated successfully")

    def update_stock_info_with_warrant(self) -> None:
        """更新台股總覽(含權證)資料"""

        logger.info("* Start Updating Taiwan Stock Info With Warrant...")

        # Step 1: Crawl
        try:
            df: Optional[pd.DataFrame] = (
                self.context.crawler.crawl_stock_info_with_warrant()
            )
        except FinMindQuotaExhaustedError as e:
            logger.error(
                "⚠️ FinMind API quota exhausted. Please wait for quota reset and retry later. %s",
                e,
            )
            return
        if df is None or df.empty:
            logger.warning("No stock info with warrant data to update")
            return

        # Step 2: Clean
        cleaned_df: Optional[pd.DataFrame] = (
            self.context.cleaner.clean_stock_info_with_warrant(df)
        )
        if cleaned_df is None or cleaned_df.empty:
            logger.warning("Cleaned stock info with warrant data is empty")
            return

        # Step 3: Load
        # 確保 loader 有連接
        self.context.ensure_loader_connected()
        self.context.loader.load_stock_info_with_warrant()
        self.context.loader.commit()

        logger.info("✅ Taiwan Stock Info With Warrant updated successfully")
