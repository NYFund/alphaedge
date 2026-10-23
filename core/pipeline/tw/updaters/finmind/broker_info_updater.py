from typing import Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.tw.updaters.finmind.common import FinMindContext
from core.pipeline.utils import FinMindQuotaExhaustedError

"""證券商資訊表的更新流程"""


class BrokerInfoUpdater:
    """證券商資訊 Updater：`taiwan_securities_trader_info`"""

    def __init__(self, context: FinMindContext) -> None:
        self.context: FinMindContext = context

    def update_broker_info(self) -> None:
        """更新證券商資訊表資料"""

        logger.info("* Start Updating Broker Info...")

        # Step 1：爬取
        try:
            result: CrawlResult = self.context.crawler.crawl_broker_info()
        except FinMindQuotaExhaustedError as e:
            # **配額用盡必須往外拋**：在這裡 `return` 的話，「這次沒更新到」會以結束碼 0 收場
            logger.error(
                f"⚠️ FinMind API quota exhausted. Please wait for quota reset and retry later. {e}"
            )
            raise
        if not result.is_ok:
            logger.warning(f"No broker info data to update（{result.reason}）")
            return

        # Step 2：清洗
        cleaned_df: Optional[pd.DataFrame] = self.context.cleaner.clean_broker_info(
            result.data
        )
        if cleaned_df is None or cleaned_df.empty:
            logger.warning("Cleaned broker info data is empty")
            return

        # Step 3：入庫
        self.context.ensure_loader_connected()
        self.context.loader.load_broker_info()
        self.context.loader.commit()

        logger.info("✅ Broker Info updated successfully")
