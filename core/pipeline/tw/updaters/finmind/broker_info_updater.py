from typing import Optional

import pandas as pd
from loguru import logger

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

        # Step 1: Crawl
        try:
            df: Optional[pd.DataFrame] = self.context.crawler.crawl_broker_info()
        except FinMindQuotaExhaustedError as e:
            # 往外拋讓 target 記為失敗：舊版 `return` 讓「這次沒更新到」以結束碼 0 收場
            logger.error(
                f"⚠️ FinMind API quota exhausted. Please wait for quota reset and retry later. {e}"
            )
            raise
        if df is None or df.empty:
            logger.warning("No broker info data to update")
            return

        # Step 2: Clean
        cleaned_df: Optional[pd.DataFrame] = self.context.cleaner.clean_broker_info(df)
        if cleaned_df is None or cleaned_df.empty:
            logger.warning("Cleaned broker info data is empty")
            return

        # Step 3: Load
        # 確保 loader 有連接
        self.context.ensure_loader_connected()
        self.context.loader.load_broker_info()
        self.context.loader.commit()

        logger.info("✅ Broker Info updated successfully")
