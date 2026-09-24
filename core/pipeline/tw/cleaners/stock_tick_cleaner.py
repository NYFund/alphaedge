import os
import shutil
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import TICK_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner


class StockTickCleaner(BaseDataCleaner):
    """Stock Tick Cleaner (Transform)"""

    # 類級別的文件鎖字典，用於保護每個股票的文件寫入操作
    _file_locks: Dict[str, Lock] = {}
    _locks_lock: Lock = Lock()  # 保護 _file_locks 字典本身的鎖

    # Windows 關檔等待與儲存重試
    FILE_CLOSE_WAIT_SECONDS: float = 0.01
    MAX_SAVE_RETRIES: int = 3
    INITIAL_RETRY_DELAY: float = 0.1
    RETRY_BACKOFF_MULTIPLIER: int = 2

    def __init__(self) -> None:
        super().__init__()

        # Downloads directory Path
        self.tick_dir: Path = TICK_DOWNLOADS_PATH
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Create the tick downloads directory
        self.tick_dir.mkdir(parents=True, exist_ok=True)

    def clean_stock_tick(
        self,
        df: pd.DataFrame,
        stock_id: str,
    ) -> Optional[pd.DataFrame]:
        """
        Clean Stock Tick Data

        使用臨時文件和文件鎖定機制來確保線程安全：
        1. 先寫入臨時文件
        2. 使用文件鎖保護寫入操作
        3. 成功後再覆蓋目標文件
        """

        try:
            # 時間格式轉換，加強錯誤處理
            try:
                df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
                # 檢查是否有無效的時間值
                if df["ts"].isna().any():
                    invalid_count: int = df["ts"].isna().sum()
                    logger.warning(
                        f"Stock {stock_id}: {invalid_count} rows have invalid timestamp, will be dropped"
                    )
                    df = df.dropna(subset=["ts"])
                    if df.empty:
                        logger.error(
                            f"Stock {stock_id}: All rows have invalid timestamp"
                        )
                        return None
            except Exception as e:
                logger.error(f"Stock {stock_id}: Error converting timestamp: {e}")
                return None

            new_df: pd.DataFrame = self.format_tick_data(df, stock_id)
            new_df = self.format_time_to_microsec(new_df)

            if new_df is None or new_df.empty:
                logger.warning(f"Stock {stock_id}: Cleaned dataframe is empty")
                return None

            # 獲取或創建該股票的文件鎖
            with self._locks_lock:
                if stock_id not in self._file_locks:
                    self._file_locks[stock_id] = Lock()
                file_lock: Lock = self._file_locks[stock_id]

            # 使用文件鎖保護寫入操作
            with file_lock:
                csv_path: Path = self.tick_dir / f"{stock_id}.csv"

                # 使用臨時文件，成功後再覆蓋目標文件
                temp_fd: int
                temp_path: str
                temp_fd, temp_path = tempfile.mkstemp(
                    suffix=".csv", dir=self.tick_dir, prefix=f"{stock_id}_"
                )
                temp_file: Path = Path(temp_path)

                try:
                    # 先關閉臨時文件描述符，讓 pandas 可以正常寫入
                    os.close(temp_fd)
                    temp_fd = None  # type: ignore

                    # 寫入臨時文件
                    new_df.to_csv(temp_file, index=False)

                    # 確保檔案已完全寫入並關閉
                    # 在 Windows 上，需要確保檔案句柄已釋放
                    if os.name == "nt":  # Windows
                        # 等待一下確保檔案已完全關閉
                        time.sleep(self.FILE_CLOSE_WAIT_SECONDS)

                    # 在 Windows 上，如果目標檔案存在且被鎖定，先嘗試刪除
                    max_retries: int = self.MAX_SAVE_RETRIES
                    retry_delay: float = self.INITIAL_RETRY_DELAY

                    for attempt in range(max_retries):
                        try:
                            # 在 Windows 上，如果目標檔案存在，先刪除再移動
                            # 在 Unix 系統上，可以直接使用 replace（原子操作）
                            if os.name == "nt":  # Windows
                                if csv_path.exists():
                                    csv_path.unlink()
                                # 使用 shutil.move 移動檔案
                                shutil.move(str(temp_file), str(csv_path))
                            else:  # Unix/Linux/Mac
                                # 使用 replace 進行原子性操作
                                temp_file.replace(csv_path)

                            logger.info(
                                f"Successfully saved {stock_id}.csv to {TICK_DOWNLOADS_PATH} "
                                f"({len(new_df)} rows)"
                            )
                            break  # 成功，跳出重試循環

                        except (PermissionError, OSError) as e:
                            if attempt < max_retries - 1:
                                logger.warning(
                                    f"Attempt {attempt + 1}/{max_retries} failed to replace "
                                    f"{stock_id}.csv (file may be in use), retrying in {retry_delay}s..."
                                )
                                time.sleep(retry_delay)
                                retry_delay *= self.RETRY_BACKOFF_MULTIPLIER  # 指數退避
                            else:
                                # 最後一次嘗試失敗
                                raise e

                except Exception as e:
                    # 如果寫入失敗，刪除臨時文件。
                    # **只吞檔案系統的錯**：清不掉暫存檔不該蓋掉真正的失敗原因，
                    # 但裸 except 連 KeyboardInterrupt 都吞，Ctrl+C 會變成什麼都沒發生
                    try:
                        if temp_file.exists():
                            temp_file.unlink()
                    except OSError:
                        pass
                    raise e
                finally:
                    # 確保臨時文件描述符已關閉
                    if temp_fd is not None:
                        try:
                            os.close(temp_fd)
                        except OSError:
                            pass

            return new_df

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error processing or saving tick data for stock {stock_id} | {e}",
            )
            return None

    def format_tick_data(
        self,
        df: pd.DataFrame,
        stock_id: str,
    ) -> pd.DataFrame:
        """
        - Description:
            統一 tick data 的格式
        - Parameters:
            - df: pd.DataFrame
                tick data
            - stock_id: str
                股票代號
        - Returns:
            - pd.DataFrame
                統一後的 tick data
        - Notes:
            - Volume: Unit: Lot
        """

        df.rename(columns={"ts": "time"}, inplace=True)
        df["stock_id"] = stock_id
        new_columns_order: List[str] = [
            "stock_id",
            "time",
            "close",
            "volume",
            "bid_price",
            "bid_volume",
            "ask_price",
            "ask_volume",
            "tick_type",
        ]
        df = df[new_columns_order]

        return df

    def format_time_to_microsec(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        將 tick dataframe 時間格式格式化至微秒（才能存進 dolphinDB）

        加強時間格式驗證和錯誤處理
        """

        try:
            # 檢查 time 欄位是否存在
            if "time" not in df.columns:
                logger.error("DataFrame missing 'time' column")
                return df

            # 轉換為 datetime 格式（如果還不是）
            if not pd.api.types.is_datetime64_any_dtype(df["time"]):
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                # 檢查是否有無效的時間值
                if df["time"].isna().any():
                    invalid_count: int = df["time"].isna().sum()
                    logger.warning(
                        f"Found {invalid_count} rows with invalid time format, will be dropped"
                    )
                    df = df.dropna(subset=["time"])
                    if df.empty:
                        logger.error("All rows have invalid time format")
                        return df

            # 檢查是否已經精確到微秒
            time_str: pd.Series = df["time"].astype(str)
            # 使用更嚴格的檢查：必須包含微秒（6位小數）
            has_microsec: pd.Series = time_str.str.contains(
                r"\.\d{6}", regex=True, na=False
            )

            if not has_microsec.all():
                # 將 'time' 欄位轉換為 datetime 格式，並補足到微秒
                df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )
                # 再次檢查是否有無效值
                if df["time"].isna().any():
                    logger.warning(
                        "Some time values could not be formatted to microsecond precision"
                    )
                    df = df.dropna(subset=["time"])

            return df

        except Exception as e:
            logger.opt(exception=True).error(
                f"Error formatting time to microsecond: {e}"
            )
            return df
