import datetime
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import shioaji as sj
from loguru import logger

from core.broker.tw.shioaji_session import ShioajiSession, login_read_only_sessions
from core.config import TICK_DOWNLOADS_PATH
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.stock_tick_cleaner import StockTickCleaner
from core.pipeline.tw.crawlers.stock_info_crawler import StockInfoCrawler
from core.pipeline.tw.crawlers.stock_tick_crawler import StockTickCrawler
from core.pipeline.tw.loaders.stock_tick_loader import StockTickLoader
from core.pipeline.tw.utils.stock_tick_utils import StockTickUtils
from core.pipeline.utils.exceptions import DataLoadError
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
Shioaji 台股 ticks 資料時間表：
From: 2020/03/02 ~ Today

目前資料庫資料時間：
From 2020/04/01 ~ 2024/05/10
"""


class StockTickUpdater(BaseDataUpdater):
    """Stock Tick Updater"""

    # API 剩餘用量低於此值（MB）即停止爬取
    TICK_API_MIN_REMAINING_MB: float = 20.0

    def __init__(self) -> None:

        super().__init__()

        # ETL
        self.crawler: StockTickCrawler = StockTickCrawler()
        self.cleaner: StockTickCleaner = StockTickCleaner()
        self.loader: StockTickLoader = StockTickLoader()

        # Crawler Setting
        # Shioaji 連線（多組金鑰輪替）；`api_list` 是給爬蟲用的原生 API 物件
        self.sessions: List[ShioajiSession] = []
        self.api_list: List[sj.Shioaji] = []

        # 爬取所有上市櫃股票清單
        self.all_stock_list: List[str] = StockInfoCrawler.crawl_stock_list()

        # 可用的 API 數量 = 可開的 thread 數
        self.num_threads: int = 0

        # 股票清單分組（後續給多線程用）
        self.split_stock_list: List[List[str]] = []

        self.tick_dir: Path = TICK_DOWNLOADS_PATH

        # 全局統計信息
        self.global_stats: Dict[str, Any] = {
            "start_time": 0.0,
            "total_stocks_processed": 0,
            "successful_stocks": 0,
            "failed_stocks": 0,
            "skipped_stocks": 0,
            "total_dates_processed": 0,
            "successful_dates": 0,
            "failed_dates": 0,
            "skipped_dates": 0,
        }

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        # 每組金鑰各登入一次正式環境（不啟用憑證，只查資料）；失敗的帳號略過
        self.sessions = login_read_only_sessions(
            [
                (account.api_key, account.api_secret_key)
                for account in StockTickUtils.setup_shioaji_apis()
            ]
        )
        self.api_list = [session.api for session in self.sessions]

        # Set up number of threads
        self.num_threads: int = len(self.api_list)

        # Generate tick_metadata backup
        StockTickUtils.generate_tick_metadata_backup()

        # 設定 log 檔案儲存路徑
        LogManager.setup_logger("update_tick.log")

    def update(
        self,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
    ) -> None:
        """
        - Description:
            更新 tick 資料

            跑完若有任何股票爬取失敗即拋 `DataLoadError`——失敗數只印在統計表裡
            的話，行程仍以結束碼 0 回報成功。
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日（預設值不可在 def 行求值）
        """

        end_date: datetime.date = end_date or datetime.date.today()

        # 重置全局統計信息
        self.global_stats: Dict[str, Any] = {
            "start_time": time.time(),
            "total_stocks_processed": 0,
            "successful_stocks": 0,
            "failed_stocks": 0,
            "skipped_stocks": 0,
            "total_dates_processed": 0,
            "successful_dates": 0,
            "failed_dates": 0,
            "skipped_dates": 0,
        }

        try:
            # 清理已載入資料庫的 CSV 文件，避免重複載入
            # 讀取 tick_metadata.json 來判斷哪些 CSV 已經載入過
            stocks_metadata: Dict[str, Dict[str, str]] = (
                StockTickUtils.load_tick_metadata_stocks()
            )

            all_csv_files: List[Path] = list(self.tick_dir.glob("*.csv"))
            deleted_count: int = 0

            for csv_file in all_csv_files:
                stock_id: str = csv_file.stem

                # 只處理檔名為純數字的 CSV 文件（股票代號）
                if not stock_id.isdigit():
                    continue

                # 檢查該股票是否在 metadata 中
                if stock_id in stocks_metadata:
                    stock_info: Dict[str, str] = stocks_metadata[stock_id]
                    last_date_str: Optional[str] = stock_info.get("last_date")

                    if last_date_str:
                        try:
                            # 讀取 CSV 文件的最後一筆資料日期
                            df: pd.DataFrame = pd.read_csv(csv_file, usecols=["time"])
                            if not df.empty:
                                last_time_str: str = df["time"].iloc[-1]
                                csv_last_date: datetime.date = pd.to_datetime(
                                    last_time_str
                                ).date()
                                metadata_last_date: datetime.date = (
                                    datetime.date.fromisoformat(last_date_str)
                                )

                                # 如果 CSV 的最後日期 <= metadata 的最後日期，說明已載入，可以刪除
                                if csv_last_date <= metadata_last_date:
                                    try:
                                        csv_file.unlink()
                                        deleted_count += 1
                                        logger.debug(
                                            f"Deleted CSV file {csv_file.name} "
                                            f"(already in database, last_date: {csv_last_date})"
                                        )
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to delete CSV file {csv_file.name}: {e}"
                                        )
                        except Exception as e:
                            logger.warning(
                                f"Failed to check CSV file {csv_file.name}: {e}. "
                                f"Skipping deletion."
                            )

            if deleted_count > 0:
                logger.info(
                    f"Cleaned up {deleted_count} CSV files that were already in database"
                )

            remaining_csv_files: List[Path] = list(self.tick_dir.glob("*.csv"))
            if remaining_csv_files:
                logger.info(
                    f"Found {len(remaining_csv_files)} CSV files to be loaded into database"
                )

            # 使用傳入的日期區間作為更新範圍
            logger.info(
                f"Update date range: {start_date.isoformat()} ~ {end_date.isoformat()}"
            )

            # Set Up Update Period
            dates: List[datetime.date] = TimeUtils.generate_date_range(
                start_date, end_date
            )

            # 檢查日期範圍是否有效
            if not dates:
                logger.warning(
                    f"No dates to update. Start date ({start_date}) is after end date ({end_date}). "
                    f"Database is already up to date or end_date needs to be adjusted."
                )
                logger.info("Skipping crawl process. Proceeding to database loading...")
                # 即使沒有新日期，也嘗試載入現有的 CSV 文件
            else:
                # Step 1: Crawl + Clean（會使用 tick_metadata.json 來跳過已存在的日期）
                logger.info("=" * 80)
                logger.info("Starting multi-threaded update process...")
                logger.info("=" * 80)

                self.update_multithreaded(dates)

            # Step 2: Load - 存入資料庫
            logger.info("=" * 80)
            logger.info("Starting database loading process...")
            logger.info("=" * 80)

            try:
                self.loader.add_to_db(remove_files=False)
                logger.info("Database loading completed successfully")
            except Exception as e:
                logger.opt(exception=True).error(f"Database loading failed: {e}")
                raise

            # Step 3: 確定都存完後，掃描 tick 資料夾內所有的 .csv 以更新 tick_metadata.json
            logger.info("Scanning CSV files and updating tick metadata...")
            try:
                StockTickUtils.update_tick_metadata_from_csv()
                logger.info("Tick metadata updated successfully")
            except Exception as e:
                logger.opt(exception=True).error(f"Failed to update tick metadata: {e}")
                # 不中斷流程，因為 metadata 更新失敗不影響數據本身

            # 更新後從 tick_metadata.json 取得最新日期並記錄
            latest_date_from_metadata: Optional[datetime.date] = (
                StockTickUtils.get_table_latest_date()
            )
            if latest_date_from_metadata:
                logger.info(
                    f"* Tick data updated. Latest available date: {latest_date_from_metadata}"
                )
                # 如果最新日期小於目標結束日期，記錄警告
                if latest_date_from_metadata < end_date:
                    logger.warning(
                        f"* Warning: Latest date ({latest_date_from_metadata}) is before target end_date ({end_date}). "
                        f"Some dates may have no data (non-trading days or API issues)."
                    )
            else:
                logger.warning("* No new stock tick data was updated")

            # 輸出完整的統計報告
            self._print_update_summary(self.global_stats, start_date, end_date)

            failed_stocks: int = self.global_stats["failed_stocks"]
            if failed_stocks:
                raise DataLoadError(
                    "tick",
                    [f"{failed_stocks} 檔股票爬取失敗，詳見上方 log"],
                    succeeded=self.global_stats["successful_stocks"],
                )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Update process failed with exception: {e}"
            )
            self._print_update_summary(self.global_stats, start_date, end_date)
            raise
        finally:
            # Step 4: Cleanup - 確保登出所有 API 連接（即使發生異常也會執行）
            self.cleanup()

    def update_thread(
        self,
        api: sj.Shioaji,
        dates: List[datetime.date],
        stock_list: List[str],
    ) -> None:
        """
        - Description:
            單一 thread 任務：爬 + 清洗

        - Parameters:
            - api: sj.Shioaji
                Shioaji API
            - dates: List[datetime.date]
                日期 List
            - stock_list: List[str]
                Stock List

        - Return: List[pd.DataFrame]
            - 每個 df 是一檔股票日期區間內的所有 tick
        """

        # 統計信息
        stats: Dict[str, Any] = {
            "total_stocks": len(stock_list),
            "successful_stocks": 0,
            "failed_stocks": 0,
            "skipped_stocks": 0,
        }

        # Crawl
        for stock_id in stock_list:
            # 判斷 api 用量（統一檢查，避免重複）
            try:
                remaining_mb: float = api.usage().remaining_bytes / 1024**2
                if remaining_mb < self.TICK_API_MIN_REMAINING_MB:
                    logger.warning(
                        f"API quota low ({remaining_mb:.2f} MB remaining) for {api}. "
                        f"Stopped crawling at stock {stock_id}."
                    )
                    break
            except Exception as e:
                logger.warning(f"Failed to check API quota: {e}. Continuing...")

            logger.info(f"Start crawling stock: {stock_id}")

            df_list: List[pd.DataFrame] = []
            stock_successful_dates: List[
                datetime.date
            ] = []  # 追蹤當前股票成功爬取的日期
            skipped_dates: List[datetime.date] = []  # 追蹤被跳過的日期
            failed_dates: List[datetime.date] = []  # 追蹤爬取失敗的日期

            for date in dates:
                # 檢查是否已經存在該日期的資料，如果存在則跳過
                if StockTickUtils.check_date_crawled(stock_id, date):
                    skipped_dates.append(date)
                    logger.debug(
                        f"Skipping {stock_id} on {date.isoformat()} (data already exists in CSV)"
                    )
                    continue

                # 統一 API 配額檢查（在每次爬取前檢查，因為配額是動態變化的）
                try:
                    remaining_mb: float = api.usage().remaining_bytes / 1024**2
                    if remaining_mb < self.TICK_API_MIN_REMAINING_MB:
                        logger.warning(
                            f"API quota low ({remaining_mb:.2f} MB remaining) for {api}. "
                            f"Stopped crawling {stock_id} at date {date.isoformat()}."
                        )
                        break  # 跳出日期循環，繼續下一個股票
                except Exception as e:
                    logger.warning(
                        f"Failed to check API quota before crawling {stock_id} on {date}: {e}. "
                        f"Continuing..."
                    )

                try:
                    df: Optional[pd.DataFrame] = self.crawler.crawl_stock_tick(
                        api, date, stock_id
                    )

                    if df is None or df.empty:
                        skipped_dates.append(date)
                        logger.debug(
                            f"No tick data for {stock_id} on {date.isoformat()} "
                            f"(may be non-trading day or no data)"
                        )
                        continue

                    df_list.append(df)
                    stock_successful_dates.append(date)  # 記錄成功爬取的日期

                except Exception as e:
                    failed_dates.append(date)
                    logger.warning(
                        f"Failed to crawl {stock_id} on {date.isoformat()}: {e}"
                    )
                    continue

            # 改進邏輯：即使部分日期失敗，也保存成功的數據
            if not df_list:
                # **先看有沒有失敗的日期**：舊版先判斷 `skipped_dates`，於是
                # 「有幾天連不上、其餘幾天本來就沒資料」的股票會被算成 skipped，
                # 失敗在統計表裡完全看不見
                if failed_dates:
                    logger.warning(
                        f"Stock {stock_id}: {len(failed_dates)} 個日期爬取失敗、"
                        f"{len(skipped_dates)} 個日期無資料或已存在"
                    )
                    stats["failed_stocks"] += 1
                elif skipped_dates:
                    logger.info(
                        f"Stock {stock_id}: All dates skipped (already exist or no data). "
                        f"Total skipped: {len(skipped_dates)}"
                    )
                    stats["skipped_stocks"] += 1
                else:
                    # 安全地訪問 dates 列表，避免 index out of range
                    date_range_str: str = (
                        f"{dates[0]} to {dates[-1]}" if dates else "no dates available"
                    )
                    logger.warning(
                        f"No tick data found for stock {stock_id} from {date_range_str}. "
                        f"Failed dates: {len(failed_dates)}"
                    )
                    stats["failed_stocks"] += 1
                continue

            # 記錄詳細的日期統計
            logger.info(
                f"Stock {stock_id}: Successfully crawled {len(stock_successful_dates)} dates, "
                f"skipped {len(skipped_dates)} dates, failed {len(failed_dates)} dates"
            )
            if stock_successful_dates:
                logger.debug(
                    f"Stock {stock_id}: Successful date range: "
                    f"{min(stock_successful_dates).isoformat()} ~ {max(stock_successful_dates).isoformat()}"
                )
            if failed_dates:
                logger.warning(
                    f"Stock {stock_id}: Failed dates: "
                    f"{min(failed_dates).isoformat()} ~ {max(failed_dates).isoformat()}"
                )

            # 合併所有成功的數據
            try:
                merged_df: pd.DataFrame = pd.concat(df_list, ignore_index=True)
                logger.debug(
                    f"Stock {stock_id}: Merged {len(df_list)} dataframes, "
                    f"total rows: {len(merged_df)}"
                )
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Stock {stock_id}: Error merging dataframes: {e}"
                )
                stats["failed_stocks"] += 1
                continue

            # Clean
            try:
                cleaned_df: pd.DataFrame = self.cleaner.clean_stock_tick(
                    merged_df, stock_id
                )

                if cleaned_df is None or cleaned_df.empty:
                    logger.warning(
                        f"Stock {stock_id}: Cleaned dataframe is empty after processing"
                    )
                    stats["failed_stocks"] += 1
                else:
                    stats["successful_stocks"] += 1
                    logger.info(
                        f"Stock {stock_id}: Successfully processed and saved "
                        f"({len(cleaned_df)} rows)"
                    )

            except Exception as e:
                logger.opt(exception=True).error(
                    f"Stock {stock_id}: Error cleaning tick data: {e}"
                )
                stats["failed_stocks"] += 1

        # 記錄線程統計信息
        logger.info(
            f"Thread completed. Stats: {stats['successful_stocks']} successful, "
            f"{stats['failed_stocks']} failed, {stats['skipped_stocks']} skipped "
            f"out of {stats['total_stocks']} stocks"
        )

        # 返回統計信息供匯總
        return stats

    def update_multithreaded(self, dates: List[datetime.date]) -> None:
        """使用 Multi-threading 的方式 Update Tick Data"""

        logger.info(
            f"Start multi-thread Updating. Total stocks: {len(self.all_stock_list)}, "
            f"Total dates: {len(dates)}, Threads: {self.num_threads}"
        )
        start_time: float = time.time()  # 開始計時

        # 將 Stock list 均分給各個 thread 進行爬蟲
        self.split_stock_list: List[List[str]] = self.split_list(
            self.all_stock_list, self.num_threads
        )

        # Multi-threading
        futures: List[Future] = []
        thread_results: List[Dict[str, Any]] = []  # 收集每個線程的統計信息

        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            for api, stock_list in zip(self.api_list, self.split_stock_list):
                futures.append(
                    executor.submit(
                        self.update_thread,
                        api=api,
                        dates=dates,
                        stock_list=stock_list,
                    )
                )

            # 等待所有 thread 結束並收集結果
            for i, future in enumerate(futures):
                try:
                    thread_stats: Optional[Dict[str, Any]] = (
                        future.result()
                    )  # 若有 exception 會在這邊被 raise 出來
                    if thread_stats:
                        thread_results.append(thread_stats)
                except Exception as e:
                    logger.opt(exception=True).error(
                        f"Thread {i + 1} task failed with exception: {e}"
                    )
                    # 記錄失敗的線程統計
                    thread_results.append(
                        {
                            "successful_stocks": 0,
                            "failed_stocks": (
                                len(self.split_stock_list[i])
                                if i < len(self.split_stock_list)
                                else 0
                            ),
                            "skipped_stocks": 0,
                        }
                    )

        # 匯總所有線程的統計信息
        for thread_stat in thread_results:
            self.global_stats["successful_stocks"] += thread_stat.get(
                "successful_stocks", 0
            )
            self.global_stats["failed_stocks"] += thread_stat.get("failed_stocks", 0)
            self.global_stats["skipped_stocks"] += thread_stat.get("skipped_stocks", 0)
            self.global_stats["total_stocks_processed"] += thread_stat.get(
                "total_stocks", 0
            )

        total_time: float = time.time() - start_time
        total_file: int = len(list(TICK_DOWNLOADS_PATH.glob("*.csv")))
        logger.info(
            f"All crawling tasks completed. Total CSV files: {total_file}, "
            f"Total time: {total_time:.2f} seconds ({total_time / 60:.2f} minutes)"
        )

    def split_list(
        self,
        target_list: List[Any],
        n_parts: int,
    ) -> List[List[str]]:
        """將 list 均分成 n 個 list"""

        num_list: int
        rem: int
        num_list, rem = divmod(len(target_list), n_parts)
        return [
            target_list[
                i * num_list + min(i, rem) : (i + 1) * num_list + min(i + 1, rem)
            ]
            for i in range(n_parts)
        ]

    def cleanup(self) -> None:
        """清理資源：登出所有 Shioaji 連線"""

        if not self.sessions:
            return

        logger.info("Cleaning up API connections...")
        for session in self.sessions:
            try:
                session.close()
            except Exception as e:
                # 如果登出失敗（例如連接已關閉或超時），記錄警告但不中斷程序
                # 這些錯誤通常在程序結束時發生，可以安全忽略
                logger.debug(f"API logout warning (can be safely ignored): {e}")
        self.sessions = []
        self.api_list = []

        # 清空 API 列表
        self.api_list.clear()
        logger.info("All API connections closed")

    def _print_update_summary(
        self, stats: Dict[str, Any], start_date: datetime.date, end_date: datetime.date
    ) -> None:
        """打印更新過程的完整統計報告"""

        total_time: float = time.time() - stats["start_time"]

        logger.info("=" * 80)
        logger.info("UPDATE SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Date Range: {start_date.isoformat()} ~ {end_date.isoformat()}")
        logger.info(
            f"Total Time: {total_time:.2f} seconds ({total_time / 60:.2f} minutes)"
        )
        logger.info("")
        logger.info("Stock Statistics:")
        logger.info(f"  - Total Processed: {stats['total_stocks_processed']}")
        logger.info(f"  - Successful: {stats['successful_stocks']}")
        logger.info(f"  - Failed: {stats['failed_stocks']}")
        logger.info(f"  - Skipped: {stats['skipped_stocks']}")
        logger.info("")
        if stats["total_stocks_processed"] > 0:
            success_rate: float = (
                stats["successful_stocks"] / stats["total_stocks_processed"]
            ) * 100
            logger.info(f"Success Rate: {success_rate:.2f}%")
        logger.info("=" * 80)
