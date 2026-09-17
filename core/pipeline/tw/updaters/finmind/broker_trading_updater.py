import datetime
from typing import Dict, List, Optional, Set, Union

import pandas as pd
from loguru import logger

from core.pipeline.tw.updaters.finmind.common import (
    BrokerTradingMetadataStore,
    FinMindContext,
)
from core.pipeline.utils import (
    DataLoadError,
    FinMindPermissionError,
    FinMindQuotaExhaustedError,
    UpdateStatus,
)
from core.utils import TimeUtils
from core.utils.instrument import StockUtils

"""
券商分點統計表的更新流程（FinMind 四個資料集中唯一需要逐組合迴圈的）

與另外三個資料集的差別：其餘三張表都是「一次查完整個市場」，本表是
**(券商 × 股票) 的雙層迴圈**，動輒數十萬個組合，因此需要
1. 逐組合的 resume（靠 `BrokerTradingMetadataStore`）、
2. 配額用盡時的等待與重試、
3. 定期 commit 與定期回寫 metadata（中斷時不整段重來）。
"""


class BrokerTradingUpdater:
    """券商分點 Updater：`taiwan_stock_trading_daily_report_secid_agg`"""

    # 券商分點批量更新：進度記錄、metadata 更新、commit 間隔
    BATCH_LOG_PROGRESS_INTERVAL: int = 50  # 每處理 N 筆記錄一次進度
    BATCH_UPDATE_METADATA_INTERVAL: int = 500  # 每處理 N 筆更新一次 metadata
    BATCH_COMMIT_INTERVAL: int = 50  # 每處理 N 筆 commit 一次

    def __init__(
        self,
        context: FinMindContext,
        metadata: BrokerTradingMetadataStore,
    ) -> None:
        self.context: FinMindContext = context
        self.metadata: BrokerTradingMetadataStore = metadata

    def update(
        self,
        start_date: Union[datetime.date, str],
        end_date: Union[datetime.date, str],
    ) -> None:
        """
        批量更新當日券商分點統計表資料

        此方法會：
        1. Loop 所有券商 ID 和股票 ID，批量更新所有組合
        2. 對每個 (券商, 股票) 組合，使用 metadata 判斷需要更新的日期範圍

        Args:
            start_date: 起始日期
            end_date: 結束日期
        """
        logger.info(
            f"* Start Updating Broker Trading Daily Report: {start_date} to {end_date}"
        )

        def _to_date(value: Union[datetime.date, str]) -> datetime.date:
            """將 date 或字串轉為 datetime.date；字串須為 YYYY-MM-DD 格式。"""
            if isinstance(value, datetime.date):
                return value
            try:
                return datetime.datetime.strptime(value, "%Y-%m-%d").date()
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Invalid date format: expected datetime.date or 'YYYY-MM-DD' string, got {type(value).__name__!r}"
                ) from e

        start_date_obj: datetime.date = _to_date(start_date)
        end_date_obj: datetime.date = _to_date(end_date)

        # 取得股票列表和券商列表
        stock_list: List[str] = self.context.get_stock_list()
        securities_trader_list: List[str] = self.context.get_securities_trader_list()

        logger.info(
            f"Retrieved stock list: {len(stock_list)} stocks, "
            f"securities trader list: {len(securities_trader_list)} traders"
        )

        if not stock_list:
            logger.warning(
                "No stocks found in database. Please update stock info first."
            )
            return

        # 過濾出一般股票（排除 ETF、權證等）
        stock_list: List[str] = StockUtils.filter_common_stocks(stock_list)
        logger.info(
            f"Filtered to {len(stock_list)} common stocks (excluding ETFs, warrants, etc.)"
        )

        if not stock_list:
            logger.warning(
                "No common stocks found after filtering. Please check stock info data."
            )
            return

        if not securities_trader_list:
            logger.warning(
                "No securities traders found in database. Please update broker info first."
            )
            return

        # 初始化時更新 metadata（從資料庫讀取）
        logger.info("Initializing broker trading metadata from database...")
        self.metadata.refresh_from_database()

        total_combinations: int = len(securities_trader_list) * len(stock_list)
        logger.info(
            f"Total update combinations: {len(securities_trader_list)} traders × {len(stock_list)} stocks = {total_combinations}"
        )
        logger.info(
            f"Requested date range: {start_date_obj.strftime('%Y-%m-%d')} to {end_date_obj.strftime('%Y-%m-%d')} "
            f"(each combination will use its own start date based on metadata)"
        )

        # Loop: 券商 -> 股票
        processed_count: int = 0
        quota_exhausted: bool = False
        # 帳號等級不足：每個組合都會失敗，遇到第一次就中止整批
        permission_error: Optional[FinMindPermissionError] = None

        # 統計各種狀態
        stats: Dict[str, int] = {
            UpdateStatus.SUCCESS.value: 0,
            UpdateStatus.NO_DATA.value: 0,
            UpdateStatus.ALREADY_UP_TO_DATE.value: 0,
            UpdateStatus.ERROR.value: 0,
        }

        # 輔助函數：記錄進度並定期更新 metadata
        def log_progress_and_update_metadata() -> None:
            """記錄處理進度並在需要時更新 metadata（避免程式意外中斷時遺失進度）"""
            if processed_count % self.BATCH_LOG_PROGRESS_INTERVAL == 0:
                logger.info(
                    f"Progress: {processed_count}/{total_combinations} combinations processed | "
                    f"Stats: success={stats[UpdateStatus.SUCCESS.value]}, no_data={stats[UpdateStatus.NO_DATA.value]}, "
                    f"error={stats[UpdateStatus.ERROR.value]}, already_up_to_date={stats[UpdateStatus.ALREADY_UP_TO_DATE.value]}"
                )
            # 定期更新 metadata（避免程式意外中斷時遺失進度）
            if processed_count % self.BATCH_UPDATE_METADATA_INTERVAL == 0:
                logger.debug(
                    f"Periodically updating metadata at {processed_count} combinations..."
                )
                # 先 commit 再重建：metadata 反映的是已落地的資料，
                # 也讓程式在重建期間意外中斷時，前面的組合不必重抓
                self.context.loader.commit()
                self.metadata.refresh_from_database()

        for securities_trader_id in securities_trader_list:
            for stock_id in stock_list:
                # 每個組合開始處理時就增加計數（無論是否跳過都會被計入）
                processed_count += 1

                # 記錄正在處理的券商和股票（改為 debug 減少 I/O，進度已由 log_progress_and_update_metadata 每 50 筆 log 一次）
                logger.debug(
                    f"Processing: trader_id={securities_trader_id}, stock_id={stock_id}"
                )

                # 為每個組合決定起始日期（基於該組合的 metadata，而非整個表）
                # 從 metadata 取得該組合的最新日期
                metadata: Dict[str, Dict[str, Dict[str, str]]] = self.metadata.load()

                # 檢查該組合是否在 metadata 中
                has_metadata: bool = (
                    securities_trader_id in metadata
                    and stock_id in metadata[securities_trader_id]
                    and "latest_date" in metadata[securities_trader_id][stock_id]
                )

                update_start_date: datetime.date = start_date_obj

                if has_metadata:
                    try:
                        # 如果 metadata 中有該組合的資料，從最新日期+1開始
                        latest_date_str: str = metadata[securities_trader_id][stock_id][
                            "latest_date"
                        ]
                        latest_date: datetime.date = datetime.datetime.strptime(
                            latest_date_str, "%Y-%m-%d"
                        ).date()
                        update_start_date = latest_date + datetime.timedelta(days=1)
                    except (ValueError, KeyError) as e:
                        logger.debug(
                            f"Error parsing latest_date from metadata for {securities_trader_id}/{stock_id}: {e}"
                        )
                        update_start_date = start_date_obj

                update_start_date = max(update_start_date, start_date_obj)

                # 起始日期已超過結束日期：已是最新或日期範圍無效，跳過
                if update_start_date > end_date_obj:
                    if not has_metadata:
                        logger.warning(
                            f"Invalid date range for new combination {securities_trader_id}/{stock_id}: "
                            f"start_date={update_start_date} > end_date={end_date_obj}. Skipping."
                        )
                    stats[UpdateStatus.ALREADY_UP_TO_DATE.value] += 1
                    log_progress_and_update_metadata()
                    continue

                # 檢查是否需要更新（檢查 metadata 中是否已包含所有日期）
                existing_dates: Set[str] = self.metadata.get_existing_dates(
                    securities_trader_id=securities_trader_id,
                    stock_id=stock_id,
                )

                # 產生目標日期範圍的所有日期
                target_dates: List[datetime.date] = TimeUtils.generate_date_range(
                    update_start_date, end_date_obj
                )

                # 如果日期範圍為空（例如 start_date > end_date），跳過
                if not target_dates:
                    logger.warning(
                        f"Empty date range for {securities_trader_id}/{stock_id}: "
                        f"start_date={update_start_date}, end_date={end_date_obj}. Skipping."
                    )
                    stats[UpdateStatus.ALREADY_UP_TO_DATE.value] += 1
                    log_progress_and_update_metadata()
                    continue

                target_date_strs: Set[str] = {
                    d.strftime("%Y-%m-%d") for d in target_dates
                }

                # 檢查是否所有日期都已存在
                missing_dates: Set[str] = target_date_strs - existing_dates

                if not missing_dates:
                    # 所有日期都已存在，跳過此組合
                    # 但如果是新組合（不在 metadata 中），這不應該發生，記錄警告
                    if not has_metadata:
                        logger.warning(
                            f"Unexpected: combination {securities_trader_id}/{stock_id} not in metadata "
                            f"but all dates {target_date_strs} appear to exist. This may indicate a logic error."
                        )
                    stats[UpdateStatus.ALREADY_UP_TO_DATE.value] += 1
                    log_progress_and_update_metadata()
                    continue

                # 配額用盡時會等待恢復並重試「本組合」，成功或未恢復才往下一組合
                while True:
                    try:
                        status: UpdateStatus = self.update_combination(
                            stock_id=stock_id,
                            securities_trader_id=securities_trader_id,
                            start_date=update_start_date,
                            end_date=end_date_obj,
                            do_commit=False,
                        )
                        if status == UpdateStatus.NO_DATA:
                            logger.debug(
                                f"No data for trader={securities_trader_id}, stock={stock_id} "
                                f"(date range: {update_start_date} to {end_date_obj})"
                            )
                        if status.value in stats:
                            stats[status.value] += 1
                        else:
                            logger.warning(f"Unknown status returned: {status}")
                            stats[UpdateStatus.ERROR.value] += 1
                        break
                    except FinMindQuotaExhaustedError as e:
                        logger.warning(
                            f"⚠️ FinMind API quota exhausted. "
                            f"Progress: {processed_count}/{total_combinations}. "
                            f"Current: trader={securities_trader_id}, stock={stock_id}. {e}"
                        )
                        # 等待前先落地已寫入的組合：等不回來就會直接結束
                        self.context.loader.commit()
                        self.metadata.refresh_from_database()
                        quota_restored: bool = self.context.wait_for_quota_reset()
                        if not quota_restored:
                            quota_exhausted = True
                            logger.error(
                                "❌ API quota not restored within max wait time. Please check API and restart later."
                            )
                            break
                        logger.info(
                            f"🔄 Quota restored. Retrying current combination: trader={securities_trader_id}, stock={stock_id}"
                        )
                    except FinMindPermissionError as e:
                        stats[UpdateStatus.ERROR.value] += 1
                        permission_error = e
                        break
                    except Exception as e:
                        stats[UpdateStatus.ERROR.value] += 1
                        logger.opt(exception=True).error(
                            f"Error updating broker trading daily report for trader={securities_trader_id}, stock={stock_id}: {e}",
                        )
                        break

                if quota_exhausted or permission_error is not None:
                    break

                log_progress_and_update_metadata()
                if processed_count % self.BATCH_COMMIT_INTERVAL == 0:
                    self.context.loader.commit()

            if quota_exhausted or permission_error is not None:
                break

        # 將尚未 commit 的寫入一次提交，再更新 metadata
        self.context.loader.commit()
        # 更新 metadata（無論是否完成）
        logger.info("Updating broker trading metadata after batch update...")
        self.metadata.refresh_from_database()

        # 如果 quota 用完，記錄狀態
        if quota_exhausted:
            logger.warning(
                f"⚠️ Batch update paused due to API quota exhaustion. "
                f"Processed {processed_count}/{total_combinations} combinations. "
                f"Please wait for quota reset and resume from where it stopped."
            )
        elif permission_error is None:
            logger.info(
                f"✅ Batch update completed. Processed {processed_count} combinations"
            )

        # 輸出詳細統計
        logger.info(
            f"📊 Update Statistics: "
            f"Success={stats[UpdateStatus.SUCCESS.value]}, "
            f"No Data={stats[UpdateStatus.NO_DATA.value]} (API returned empty result), "
            f"Already Up-to-date={stats[UpdateStatus.ALREADY_UP_TO_DATE.value]}, "
            f"Errors={stats[UpdateStatus.ERROR.value]}"
        )

        # **有錯誤就必須讓行程非零結束**：舊版只把錯誤數印在統計行裡，
        # `update_db` 照樣印 `✅ Database Update Completed`。單一組合失敗不中止
        # 整批（其餘組合仍該更新），但跑完之後不能當作沒發生。
        error_count: int = stats[UpdateStatus.ERROR.value]
        failures: List[str] = []
        if permission_error is not None:
            logger.error(
                f"❌ FinMind 帳號等級不足，券商分點批次更新中止"
                f"（處理到 {processed_count}/{total_combinations}）：{permission_error}"
            )
            failures.append(
                f"FinMind 帳號等級不足，無券商分點資料集權限，批次在第 "
                f"{processed_count}/{total_combinations} 個組合中止"
            )
        elif error_count:
            failures.append(f"{error_count} 個 (券商, 股票) 組合更新失敗，詳見上方 log")

        # **配額等不回來也是「這次沒跑完」**：舊版只記 warning，於是一次只做了
        # 三成的更新仍以結束碼 0 結束，排程看不出需要重跑
        if quota_exhausted:
            failures.append(
                f"API 配額未在等待時限內恢復，只處理了 "
                f"{processed_count}/{total_combinations} 個組合"
            )

        if failures:
            raise DataLoadError(
                "broker_trading",
                failures,
                succeeded=stats[UpdateStatus.SUCCESS.value],
            )

    def update_combination(
        self,
        stock_id: str,
        securities_trader_id: str,
        start_date: Union[datetime.date, str],
        end_date: Union[datetime.date, str],
        do_commit: bool = True,
    ) -> UpdateStatus:
        """
        核心方法：更新券商分點統計表資料（給定股票、券商與日期區間，不包含時間判斷邏輯）

        Args:
            stock_id: 股票代碼
            securities_trader_id: 券商代碼
            start_date: 起始日期
            end_date: 結束日期
            do_commit: 是否在寫入後立即 commit；批次更新時由呼叫端傳 False 並定期 commit

        Returns:
            UpdateStatus: 更新狀態
                - UpdateStatus.SUCCESS: 成功更新（含 API 有回傳但本批皆為重複、saved_count==0 之情況）
                - UpdateStatus.NO_DATA: 沒有資料（API 返回空結果）
                - UpdateStatus.ERROR: 發生錯誤
        """
        logger.info(
            f"Crawling and saving broker trading daily report: "
            f"trader={securities_trader_id}, stock={stock_id}, "
            f"date={start_date} to {end_date}"
        )

        try:
            # Step 1: Crawl（配額用盡時由上層迴圈捕捉並等待重置）
            df: Optional[pd.DataFrame] = (
                self.context.crawler.crawl_broker_trading_daily_report(
                    stock_id=stock_id,
                    securities_trader_id=securities_trader_id,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            if df is None or df.empty:
                logger.debug(
                    f"No broker trading daily report data for stock_id={stock_id}, "
                    f"securities_trader_id={securities_trader_id}, "
                    f"date={start_date} to {end_date}"
                )
                return UpdateStatus.NO_DATA

            # Step 2: Clean
            # 不寫 CSV：清洗結果下一步就直接入庫，resume 依據是 DB ＋ metadata 而非 CSV；
            # 代價是 `broker_trading/` 下的 CSV 不再反映本流程新抓的資料
            cleaned_df: Optional[pd.DataFrame] = (
                self.context.cleaner.clean_broker_trading_daily_report(
                    df, write_csv=False
                )
            )
            if cleaned_df is None or cleaned_df.empty:
                logger.warning("Cleaned broker trading daily report data is empty")
                return UpdateStatus.NO_DATA

            # Step 3: Load - 將資料保存到資料庫
            # 使用 loader 的方法來載入資料（do_commit=False 時由批次迴圈定期 commit）
            saved_count: int = self.context.loader.load_broker_trading_daily_report(
                df=cleaned_df, commit=do_commit
            )

            if saved_count == 0:
                # API 有回傳且已清洗，但本批無新寫入（例如皆為重複）；視為成功、不報錯
                logger.debug("No new data was saved to database")
                return UpdateStatus.SUCCESS

            # 成功後用當次 DataFrame 的 date 最大值 log，避免額外查詢 DB
            if "date" in cleaned_df.columns and not cleaned_df.empty:
                latest_date_from_df: str = str(cleaned_df["date"].max())
                logger.info(
                    f"✅ Broker trading daily report updated successfully. Latest date in batch: {latest_date_from_df}"
                )
            else:
                logger.info("✅ Broker trading daily report updated successfully.")
            return UpdateStatus.SUCCESS

        except (FinMindQuotaExhaustedError, FinMindPermissionError):
            # 配額用盡、帳號等級不足：不在此處處理，向上拋出由批次迴圈統一等待／中斷
            raise
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error updating broker trading daily report: {e}",
            )
            return UpdateStatus.ERROR
