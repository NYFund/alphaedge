import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import shioaji as sj
from loguru import logger

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.broker.tw.shioaji_session import ShioajiSession, login_read_only_sessions
from core.config import FUTURES_TARGET_PRODUCTS
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.futures_tick_cleaner import FuturesTickCleaner
from core.pipeline.tw.crawlers.futures_tick_crawler import FuturesTickCrawler
from core.pipeline.tw.loaders.futures_tick_loader import FuturesTickLoader
from core.pipeline.tw.utils.stock_tick_utils import StockTickUtils
from core.utils import FuturesSession, TimeUtils
from core.utils.log_manager import LogManager

"""
台期貨 Tick Updater（Shioaji）

**要爬哪些契約由日線行情決定**，不是自己排列組合：`futures_price_daily` 已經
記錄了每一天實際有哪些契約在交易，直接拿來當清單即可——自己用「近月 ＋ 次月」
推會在結算日前後多爬或少爬，而 Shioaji 的配額經不起浪費。

**配額是本 ETL 最硬的限制**：期貨 tick 一天的量比股票大得多（TX 近月一天數十萬
筆），Shioaji 的每日流量上限很容易用完。故：

1. 沿用 `StockTickUtils.setup_shioaji_apis()` 的**多組金鑰**設定。
2. 每爬一個契約前檢查剩餘用量，低於門檻就停手（**不是繼續硬爬**——
   配額用完之後的請求會直接失敗，白白浪費時間）。
3. 一律**先存中繼檔再入庫**：DolphinDB 沒開也不會白爬一輪。

⚠️ **本 updater 尚未於本機完整實測**：DolphinDB server 未啟動、且期貨 tick 會
消耗使用者的 Shioaji 配額，故只驗到「契約清單解析」與「爬取單一契約」。
"""


class FuturesTickUpdater(BaseDataUpdater):
    """更新期貨逐筆成交（Shioaji → DolphinDB）"""

    # 剩餘配額低於此值（MB）就停手
    MIN_REMAINING_QUOTA_MB: float = 100.0

    def __init__(self) -> None:
        super().__init__()

        self.crawler: FuturesTickCrawler = FuturesTickCrawler()
        self.cleaner: FuturesTickCleaner = FuturesTickCleaner()
        self.loader: FuturesTickLoader = FuturesTickLoader()
        self.price_api: FuturesPriceAPI = FuturesPriceAPI()

        # Shioaji 連線（多組金鑰輪替）；`api_list` 是給爬蟲用的原生 API 物件
        self.sessions: List[ShioajiSession] = []
        self.api_list: List[sj.Shioaji] = []

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_futures_tick.log")

    def login(self) -> int:
        """
        登入 Shioaji（可多組金鑰）

        **登入放在 `update()` 而不是 `__init__()`**：本類會被 `tasks/update_db.py`
        import，建構時就登入會讓每一次跑其他 target 都白白連一次券商。
        """

        if self.api_list:
            return len(self.api_list)

        # 每組金鑰各登入一次正式環境（不啟用憑證，只查資料）；失敗的帳號略過
        self.sessions = login_read_only_sessions(
            [
                (account.api_key, account.api_secret_key)
                for account in StockTickUtils.setup_shioaji_apis()
            ]
        )
        self.api_list = [session.api for session in self.sessions]
        return len(self.api_list)

    def logout(self) -> None:
        """登出所有 Shioaji 連線並關閉資料連線"""

        for session in self.sessions:
            session.close()
        self.sessions = []
        self.api_list = []

        self.price_api.close()
        self.loader.disconnect()

    def check_quota(self, api: sj.Shioaji) -> bool:
        """
        剩餘配額是否還夠

        **查不到用量時放行**：那是 API 暫時異常，不該因此停掉整段回補；
        真的用完的話下一次請求本來就會失敗。
        """

        try:
            remaining_mb: float = api.usage().remaining_bytes / 1024**2
        except Exception as error:
            logger.warning(f"[Futures Tick] 查不到 API 用量：{error}，本次放行")
            return True

        if remaining_mb < self.MIN_REMAINING_QUOTA_MB:
            logger.warning(f"[Futures Tick] 剩餘配額僅 {remaining_mb:.1f} MB，停止爬取")
            return False

        return True

    def get_contracts(
        self, date: datetime.date, products: List[str]
    ) -> List[Tuple[str, str]]:
        """
        - Description:
            取得當日實際在交易的 `(商品, 到期月)` 清單

            **來源是日線行情表**：那是已經確認存在的契約，比自己推「近月＋次月」
            準確；週契約（帶 `W`）在 Shioaji 是獨立分類，本層先排除。
        - Parameters:
            - date: datetime.date
                交易日
            - products: List[str]
                要爬的商品
        - Return:
            - List[Tuple[str, str]]
                `(product, expiry)` 清單
        """

        contracts: List[Tuple[str, str]] = []

        for product in products:
            expiries: List[str] = self.price_api.get_expiries(
                date, product, session=FuturesSession.DAY
            )
            contracts.extend(
                (product, expiry) for expiry in expiries if "W" not in expiry.upper()
            )

        return contracts

    def update(
        self,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
        products: Optional[List[str]] = None,
        near_month_only: bool = True,
    ) -> Dict[str, int]:
        """
        - Description:
            逐日、逐契約更新期貨 tick

            **預設只爬近月**（`near_month_only`）：期貨的量集中在近月，
            遠月一天可能只有幾百筆卻同樣佔配額。要做價差策略再打開。
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日
            - products: Optional[List[str]]
                商品清單；None 取 `FUTURES_TARGET_PRODUCTS`
            - near_month_only: bool
                是否只爬每個商品的近月契約
        - Return:
            - Dict[str, int]
                統計（爬取契約數、入庫列數、跳過數）
        """

        end_date: datetime.date = end_date or datetime.date.today()

        targets: List[str] = products or list(FUTURES_TARGET_PRODUCTS)
        stats: Dict[str, int] = {"contracts": 0, "rows": 0, "skipped": 0}

        if self.login() == 0:
            logger.warning("[Futures Tick] 沒有可用的 Shioaji 金鑰，本次不執行")
            return stats

        api: sj.Shioaji = self.api_list[0]

        for date in TimeUtils.generate_date_range(start_date, end_date):
            contracts: List[Tuple[str, str]] = self.get_contracts(date, targets)
            if not contracts:
                continue

            if near_month_only:
                contracts = self.keep_near_month(contracts)

            for product, expiry in contracts:
                if not self.check_quota(api):
                    logger.warning("[Futures Tick] 配額不足，提前結束本次回補")
                    return stats

                stats["contracts"] += 1
                raw: Optional[pd.DataFrame] = self.crawler.crawl_futures_tick(
                    api, date, product, expiry
                )
                if raw is None:
                    stats["skipped"] += 1
                    continue

                cleaned: Optional[pd.DataFrame] = self.cleaner.clean(
                    raw, product, expiry
                )
                if cleaned is None:
                    stats["skipped"] += 1
                    continue

                # 先存中繼檔再入庫：DolphinDB 沒開也不會白爬一輪
                self.cleaner.save(cleaned, product, expiry, date)
                stats["rows"] += self.loader.add_to_db(cleaned)

        logger.info(
            f"[Futures Tick] 完成：{stats['contracts']} 個契約、"
            f"入庫 {stats['rows']} 列、跳過 {stats['skipped']} 個"
        )
        return stats

    @staticmethod
    def keep_near_month(contracts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """每個商品只留最近的到期月（`YYYYMM` 的字典序即時間序）"""

        near: Dict[str, str] = {}
        for product, expiry in contracts:
            if product not in near or expiry < near[product]:
                near[product] = expiry

        return sorted(near.items())
