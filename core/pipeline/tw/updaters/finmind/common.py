import datetime
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.dao.connection import DBConnection
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO
from core.pipeline.tw.cleaners.finmind_cleaner import FinMindCleaner
from core.pipeline.tw.crawlers.finmind_crawler import FinMindCrawler
from core.pipeline.tw.loaders.finmind_loader import FinMindLoader
from core.pipeline.utils.data_utils import DataUtils
from core.utils import TimeUtils

"""
FinMind 各資料集共用的執行環境與 metadata 儲存

拆檔的切法是**按資料集**（台股總覽、證券商、券商分點），而不是按技術層次；
真正跨資料集共用的只有兩件事，故都放在本檔：

1. `FinMindContext`：ETL 三件組（crawler／cleaner／loader）、DB 連線，以及
   **API quota 控制**——四個資料集打的是同一把 FinMind token，quota 是共享資源。
2. `BrokerTradingMetadataStore`：券商分點的 resume 依據。目前只有券商分點在用，
   但它是「哪些 (broker, stock, date) 已經有了」的唯一真相，語意上不屬於任何
   單一更新流程，故與 quota 並列。
"""


class FinMindContext:
    """FinMind 四個資料集共用的執行環境：ETL 三件組、DB 連線與 API quota 狀態"""

    # API Quota 相關常數（供 wait_for_quota_reset 使用）
    QUOTA_RESET_INTERVAL_SECONDS: int = (
        3600  # 配額重置間隔（秒），用於 fallback 推算下次重置時間
    )
    MIN_REMAINING_QUOTA_TO_RESUME: int = (
        3000  # 剩餘 quota 至少達此值才視為已恢復、繼續更新
    )
    DEFAULT_API_QUOTA_LIMIT: int = (
        20000  # 每小時最大 API 調用次數（無法從 API 取得時使用）
    )
    SECONDS_PER_MINUTE: int = 60  # 分鐘轉秒（用於配額輪詢間隔等）

    # 配額用盡後等待恢復的預設參數
    QUOTA_CHECK_INTERVAL_MINUTES: int = 10  # 每隔幾分鐘查詢一次 API usage
    QUOTA_MAX_WAIT_MINUTES: int = 120  # 最大等待時間（分鐘）

    # API 剩餘配額查詢：有效值下界（usage 與 limit 的合法性檢查）
    MIN_VALID_API_USAGE: int = 0
    MIN_VALID_API_LIMIT: int = 1

    def __init__(
        self,
        crawler: FinMindCrawler,
        cleaner: FinMindCleaner,
        loader: FinMindLoader,
        conn: Optional[DBConnection] = None,
    ) -> None:
        # ETL
        self.crawler: FinMindCrawler = crawler
        self.cleaner: FinMindCleaner = cleaner
        self.loader: FinMindLoader = loader

        # 資料連線（讀取股票／券商清單）；與 loader 寫入共用同一條
        self.conn: Optional[DBConnection] = conn

        # API Quota（配額用盡由 FinMindQuotaExhaustedError 處理，此處只記狀態）
        self.api_quota_limit: int = self.DEFAULT_API_QUOTA_LIMIT
        self.quota_reset_time: float = (
            time.time() + self.QUOTA_RESET_INTERVAL_SECONDS
        )  # 下次重置時間（無法從 API 取得剩餘時用）

    def refresh_api_quota_limit(self) -> None:
        """向 FinMind 問出每小時配額上限；問不到就沿用預設值"""

        try:
            if self.crawler.api and hasattr(self.crawler.api, "api_usage_limit"):
                self.api_quota_limit: int = self.crawler.api.api_usage_limit
                logger.info(
                    f"FinMind API quota limit retrieved: {self.api_quota_limit} calls per hour"
                )
            else:
                logger.warning(
                    f"Could not retrieve API quota limit from FinMind API. Using default: {self.api_quota_limit}"
                )
        except Exception as e:
            # **這裡的盲捕不可收斂**：`api_usage_limit` 是會發 HTTP 請求的 property，
            # 而 FinMind 的 `request_get()` 內部吞掉所有例外、重試 10 次之後
            # ①「非 200」直接 `raise Exception(...)`（字面上的基底類別）、
            # ② 全部重試失敗時回 `None`，接著 `.json()` 拋 `AttributeError`。
            # 改成 `requests.RequestException` 兩者都攔不到，配額查詢失敗會打死整批 ETL
            logger.warning(
                f"Error retrieving API quota limit: {e}. Using default: {self.api_quota_limit}"
            )

    def ensure_loader_connected(self) -> None:
        """確保 loader 有連線（各資料集的 Load 階段之前一律先呼叫）"""

        if self.loader.conn is None:
            self.loader.connect()

    def get_api_remaining_quota(self) -> Optional[int]:
        """
        - Description:
            從 FinMind API 查詢剩餘的 API quota

            FinMind DataLoader 提供 `api_usage`（已使用次數）與 `api_usage_limit`
            （每小時總次數），剩餘次數 = limit - usage。
        - Return:
            - Optional[int]
                剩餘的 API 調用次數，若無法查詢則為 None
        """

        try:
            if not self.crawler.api:
                return None
            if not hasattr(self.crawler.api, "api_usage") or not hasattr(
                self.crawler.api, "api_usage_limit"
            ):
                return None
            usage: int = self.crawler.api.api_usage
            limit: int = self.crawler.api.api_usage_limit
            if not (
                isinstance(usage, int)
                and isinstance(limit, int)
                and usage >= self.MIN_VALID_API_USAGE
                and limit >= self.MIN_VALID_API_LIMIT
            ):
                return None
            remaining: int = max(self.MIN_VALID_API_USAGE, limit - usage)
            logger.info(f"📊 目前使用次數 / 總次數: {usage} / {limit}")
            return remaining
        except Exception as e:
            # 同 `refresh_api_quota_limit()`：FinMind 的 `request_get()` 會拋出
            # 字面上的 `Exception`，收斂成具名例外反而攔不住
            logger.debug(f"Could not query API remaining quota from FinMind API: {e}")
        return None

    def wait_for_quota_reset(self) -> bool:
        """
        - Description:
            等待 API quota 重置，每隔 `QUOTA_CHECK_INTERVAL_MINUTES` 查詢一次 usage
        - Return:
            - bool
                True 表示 quota 已恢復；False 表示達到最大等待時間或發生錯誤
        """

        check_interval_seconds: int = (
            self.QUOTA_CHECK_INTERVAL_MINUTES * self.SECONDS_PER_MINUTE
        )
        max_wait_seconds: int = self.QUOTA_MAX_WAIT_MINUTES * self.SECONDS_PER_MINUTE
        start_wait_time: float = time.time()

        logger.info(
            f"⏳ Waiting for API quota reset. Checking every {self.QUOTA_CHECK_INTERVAL_MINUTES} minutes..."
        )

        while True:
            elapsed: float = time.time() - start_wait_time
            if elapsed >= max_wait_seconds:
                logger.warning(
                    f"⚠️ Maximum wait time ({self.QUOTA_MAX_WAIT_MINUTES} minutes) reached. Stopping wait."
                )
                return False

            remaining: Optional[int] = self.get_api_remaining_quota()

            if remaining is not None:
                if remaining >= self.MIN_REMAINING_QUOTA_TO_RESUME:
                    self.quota_reset_time = (
                        time.time() + self.QUOTA_RESET_INTERVAL_SECONDS
                    )
                    logger.info(
                        f"✅ API quota has been reset! Resuming update. "
                        f"Remaining quota: {remaining} calls."
                    )
                    return True
            else:
                # 如果無法查詢 API usage，使用時間判斷（fallback）
                current_time: float = time.time()
                if current_time >= self.quota_reset_time:
                    self.quota_reset_time = (
                        current_time + self.QUOTA_RESET_INTERVAL_SECONDS
                    )
                    logger.info("✅ API quota reset time reached. Resuming update.")
                    return True

            logger.info(
                f"⏳ Quota not yet reset. Next check in {self.QUOTA_CHECK_INTERVAL_MINUTES} minutes. "
                f"(Elapsed: {elapsed / self.SECONDS_PER_MINUTE:.1f} minutes)"
            )

            time.sleep(check_interval_seconds)

    def get_stock_list(self) -> List[str]:
        """
        - Description:
            從資料庫取得所有股票代碼列表（使用 stock_info，不含權證）

            表不存在時回空清單，**其他查詢錯誤一律往外拋**：全部吞成空清單的話，
            「DB 被鎖住」會被當成「沒有股票，請先更新 stock info」，行程照樣成功結束。
        """

        stock_list: List[str] = StockInfoDAO(conn=self.conn).get_stock_ids()
        logger.info(f"Retrieved {len(stock_list)} stocks from database")
        return stock_list

    def get_securities_trader_list(self) -> List[str]:
        """從資料庫取得所有券商代碼列表；表不存在時回空清單，其他查詢錯誤往外拋"""

        securities_trader_list: List[str] = SecuritiesTraderInfoDAO(
            conn=self.conn
        ).get_trader_ids()
        logger.info(
            f"Retrieved {len(securities_trader_list)} securities traders from database"
        )
        return securities_trader_list


class BrokerTradingMetadataStore:
    """
    券商分點的 resume 依據：每個 (broker_id, stock_id) 的日期範圍

    結構為
    `{"broker_id": {"stock_id": {"earliest_date": ..., "latest_date": ...}}}`；
    內容一律**由資料庫反推**（`refresh_from_database()`），不依賴 CSV 檔案，
    故即使 metadata 檔遺失也能重建。
    """

    def __init__(
        self,
        metadata_path: Path,
        conn: Optional[DBConnection] = None,
    ) -> None:
        self.metadata_path: Path = metadata_path
        self.conn: Optional[DBConnection] = conn

        # Metadata 快取（雙層迴圈內只讀快取，減少重複讀取 JSON；僅在 refresh_from_database 寫入後更新）
        self._cache: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None

    def load(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """讀取 metadata；有快取則直接回傳，減少重複 I/O"""

        if self._cache is not None:
            return self._cache

        if not self.metadata_path.exists():
            return {}

        try:
            metadata: Dict[str, Dict[str, Dict[str, str]]] = DataUtils.load_json(
                self.metadata_path
            )
            return metadata if metadata is not None else {}
        except OSError as e:
            # **只剩檔案系統的錯**：`DataUtils.load_json()` 自己已經處理掉
            # `FileNotFoundError` 與 `JSONDecodeError`（回 `None`，由上一行的
            # `is not None` 接住），走到這裡的是權限不足、路徑是目錄這類問題
            logger.warning(f"Error reading broker trading metadata: {e}")
            return {}

    def refresh_from_database(self) -> None:
        """
        從資料庫讀取實際資料並改寫 metadata

        記錄每個 (broker_id, stock_id) 組合的 earliest_date 與 latest_date；
        資料庫中已不存在的組合會被清掉。
        """

        metadata: Dict[str, Dict[str, Dict[str, str]]] = self.load()

        if self.conn is None:
            logger.error("Database connection is not available")
            return

        updated_count: int = 0
        try:
            df: pd.DataFrame = BrokerTradingDAO(
                conn=self.conn
            ).get_date_ranges_by_trader_stock()

            if df.empty:
                logger.info("No broker trading data found in database")
                metadata = {}
            else:
                existing_combinations: Set[Tuple[str, str]] = set()

                for _, row in df.iterrows():
                    securities_trader_id: str = str(row["securities_trader_id"])
                    stock_id: str = str(row["stock_id"])
                    earliest_date_str: str = str(row["earliest_date"])
                    latest_date_str: str = str(row["latest_date"])

                    existing_combinations.add((securities_trader_id, stock_id))

                    try:
                        earliest_date: datetime.date = datetime.datetime.strptime(
                            earliest_date_str, "%Y-%m-%d"
                        ).date()
                        latest_date: datetime.date = datetime.datetime.strptime(
                            latest_date_str, "%Y-%m-%d"
                        ).date()

                        if securities_trader_id not in metadata:
                            metadata[securities_trader_id] = {}

                        if stock_id not in metadata[securities_trader_id]:
                            # 情況 A：DB 已有此組合但 metadata 遺漏，直接寫入查到的日期範圍
                            metadata[securities_trader_id][stock_id] = {
                                "earliest_date": earliest_date.strftime("%Y-%m-%d"),
                                "latest_date": latest_date.strftime("%Y-%m-%d"),
                            }
                            updated_count += 1
                        else:
                            # 情況 B：metadata 已有此組合，比較並擴展 earliest/latest 日期範圍
                            existing_earliest: Optional[datetime.date] = None
                            existing_latest: Optional[datetime.date] = None

                            if (
                                "earliest_date"
                                in metadata[securities_trader_id][stock_id]
                            ):
                                existing_earliest = datetime.datetime.strptime(
                                    metadata[securities_trader_id][stock_id][
                                        "earliest_date"
                                    ],
                                    "%Y-%m-%d",
                                ).date()
                            if (
                                "latest_date"
                                in metadata[securities_trader_id][stock_id]
                            ):
                                existing_latest = datetime.datetime.strptime(
                                    metadata[securities_trader_id][stock_id][
                                        "latest_date"
                                    ],
                                    "%Y-%m-%d",
                                ).date()

                            if (
                                existing_earliest is None
                                or earliest_date < existing_earliest
                            ):
                                metadata[securities_trader_id][stock_id][
                                    "earliest_date"
                                ] = earliest_date.strftime("%Y-%m-%d")
                                updated_count += 1

                            if existing_latest is None or latest_date > existing_latest:
                                metadata[securities_trader_id][stock_id][
                                    "latest_date"
                                ] = latest_date.strftime("%Y-%m-%d")
                                updated_count += 1

                    except (ValueError, KeyError) as e:
                        logger.debug(
                            f"Error processing metadata for {securities_trader_id}/{stock_id}: {e}"
                        )
                        continue

                # 清理 metadata 中資料庫不存在的記錄
                removed_count: int = 0
                brokers_to_remove: List[str] = []

                for broker_id, stocks in metadata.items():
                    stocks_to_remove: List[str] = []

                    for stock_id in stocks.keys():
                        if (broker_id, stock_id) not in existing_combinations:
                            stocks_to_remove.append(stock_id)
                            removed_count += 1

                    for stock_id in stocks_to_remove:
                        del metadata[broker_id][stock_id]

                    if not metadata[broker_id]:
                        brokers_to_remove.append(broker_id)

                for broker_id in brokers_to_remove:
                    del metadata[broker_id]

                if removed_count > 0:
                    logger.info(
                        f"🧹 Cleaned {removed_count} metadata entries for non-existent database records"
                    )

                if updated_count > 0:
                    logger.info(
                        f"✅ Updated broker trading metadata: {updated_count} entries updated from database"
                    )

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error updating broker trading metadata from database: {e}",
            )

        DataUtils.save_json(
            metadata,
            self.metadata_path,
            ensure_ascii=False,
        )
        # 寫入成功後更新快取，迴圈內後續 load() 只讀快取
        self._cache = metadata

    def get_existing_dates(
        self,
        securities_trader_id: str,
        stock_id: str,
    ) -> Set[str]:
        """
        - Description:
            由 metadata 的日期範圍展開出該組合已存在的所有日期
        - Parameters:
            - securities_trader_id: str
                券商代碼
            - stock_id: str
                股票代碼
        - Return:
            - Set[str]
                已存在的日期集合（格式為 `YYYY-MM-DD`）
        """

        metadata: Dict[str, Dict[str, Dict[str, str]]] = self.load()

        if (
            securities_trader_id not in metadata
            or stock_id not in metadata[securities_trader_id]
        ):
            return set()

        stock_info: Dict[str, str] = metadata[securities_trader_id][stock_id]

        if "earliest_date" not in stock_info or "latest_date" not in stock_info:
            return set()

        try:
            earliest_date: datetime.date = datetime.datetime.strptime(
                stock_info["earliest_date"], "%Y-%m-%d"
            ).date()
            latest_date: datetime.date = datetime.datetime.strptime(
                stock_info["latest_date"], "%Y-%m-%d"
            ).date()

            date_range: List[datetime.date] = TimeUtils.generate_date_range(
                earliest_date, latest_date
            )
            existing_dates: Set[str] = {d.strftime("%Y-%m-%d") for d in date_range}
            return existing_dates
        except (ValueError, KeyError) as e:
            logger.debug(f"Error getting dates from metadata: {e}")
            return set()
