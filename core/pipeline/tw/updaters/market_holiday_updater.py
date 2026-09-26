import datetime
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.crawlers.market_holiday_crawler import MarketHolidayCrawler
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader
from core.pipeline.utils.exceptions import DataLoadError
from core.utils.log_manager import LogManager

"""
Market Holiday Updater：每次重抓「去年、今年、明年」三個年度

三次請求就結束，不做增量判斷：每次整年替換最簡單，站方更正公告時也不會留下舊列。
去年要一起抓，是因為一月初跑的時候，去年年底的休市日仍可能被回頭查到。
（颱風假等臨時停市不在公告表上，本表無法反映。）
"""


class MarketHolidayUpdater(BaseDataUpdater):
    """Market Holiday Updater"""

    def __init__(self) -> None:
        super().__init__()

        # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
        TW_STOCK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.dao: MarketHolidayDAO = MarketHolidayDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: MarketHolidayCrawler = MarketHolidayCrawler()
        self.cleaner: MarketHolidayCleaner = MarketHolidayCleaner()
        self.loader: MarketHolidayLoader = MarketHolidayLoader(dao=self.dao)

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_market_holiday.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def update(self, today: Optional[datetime.date] = None) -> None:
        """
        - Description:
            更新前一年、今年、明年的開休市日期

            **三種結果分開處理**：
            - 站方尚未公告（明年的表通常 12 月才出來）→ 記一行 info 跳過，屬正常。
            - 取不到或清洗失敗 → 該年度跳過、其餘年度照跑，**全部跑完後拋
              `DataLoadError`**：實盤靠這張表判定交易日，缺一年不該以成功結束。
        - Parameters:
            - today: Optional[datetime.date]
                基準日；None 表示今天
        - Raise:
            - DataLoadError
                有年度取不到或清洗失敗
        """

        base_year: int = (today or datetime.date.today()).year
        failures: List[str] = []
        loaded: int = 0

        for year in (base_year - 1, base_year, base_year + 1):
            result: CrawlResult = self.crawler.crawl(year)

            if result.is_no_data:
                logger.info(f"[market_holiday] {year} 年站方尚未公告，本次略過")
                continue

            if result.is_failed:
                logger.warning(f"[market_holiday] {year} 年取不到：{result.reason}")
                failures.append(f"{year}: {result.reason}")
                continue

            try:
                cleaned: pd.DataFrame = self.cleaner.clean(result.data, year)
            except Exception as error:
                # **這層盲捕不可收斂**：`cleaner.clean()` 是各來源自己實作的，
                # 與 `BaseDataUpdater.clean_one()` 同一種外掛邊界。
                # 收斂等於要求清洗器只能拋我們列得出來的那幾種
                logger.error(
                    f"[market_holiday] {year} 年清洗失敗（{type(error).__name__}: "
                    f"{error}），本年度不入庫"
                )
                failures.append(f"{year}: clean_error")
                continue

            self.loader.add_to_db(cleaned, year)
            loaded += 1

        logger.info(
            f"[market_holiday] 已涵蓋年度：{sorted(self.dao.get_covered_years())}"
        )

        if failures:
            raise DataLoadError("market_holiday", failures, succeeded=loaded)
