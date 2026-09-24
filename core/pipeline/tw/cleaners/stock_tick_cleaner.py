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

    # 每檔股票一把鎖：多執行緒同時清同一檔時，落地的 CSV 會互相覆蓋
    _file_locks: Dict[str, Lock] = {}
    _locks_lock: Lock = Lock()  # 保護 _file_locks 這個 dict 本身

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
        - Description:
            清洗單一檔股票的逐筆成交，並落地成 `{stock_id}.csv`

            **先寫暫存檔、再原子替換，全程持有該檔股票的鎖**：多執行緒同時清同
            一檔時，直接寫目標檔會讓兩邊的內容交錯，產生一個半新半舊、
            但格式完全合法的 CSV。
        - Parameters:
            - df: pd.DataFrame
                Shioaji 回傳的原始 ticks
            - stock_id: str
                股票代號
        - Return:
            - Optional[pd.DataFrame]
                清洗後的資料；時間戳全數無效或落地失敗時為 None
        """

        try:
            try:
                df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
                # 時間是 tick 唯一的排序依據，無法解析的列一律丟掉不補值
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

            with self._locks_lock:
                if stock_id not in self._file_locks:
                    self._file_locks[stock_id] = Lock()
                file_lock: Lock = self._file_locks[stock_id]

            with file_lock:
                csv_path: Path = self.tick_dir / f"{stock_id}.csv"

                temp_fd: int
                temp_path: str
                temp_fd, temp_path = tempfile.mkstemp(
                    suffix=".csv", dir=self.tick_dir, prefix=f"{stock_id}_"
                )
                temp_file: Path = Path(temp_path)

                try:
                    # 先關掉 mkstemp 的 fd，pandas 才能自己開檔寫入
                    os.close(temp_fd)
                    temp_fd = None  # type: ignore

                    new_df.to_csv(temp_file, index=False)

                    # Windows 不保證 to_csv 回傳時 handle 已釋放，稍等一下再替換
                    if os.name == "nt":  # Windows
                        time.sleep(self.FILE_CLOSE_WAIT_SECONDS)

                    max_retries: int = self.MAX_SAVE_RETRIES
                    retry_delay: float = self.INITIAL_RETRY_DELAY

                    for attempt in range(max_retries):
                        try:
                            # Windows 不允許覆蓋既有檔案，只能先刪再搬（非原子）；
                            # Unix 直接用 replace() 一步原子替換
                            if os.name == "nt":  # Windows
                                if csv_path.exists():
                                    csv_path.unlink()
                                shutil.move(str(temp_file), str(csv_path))
                            else:  # Unix/Linux/Mac
                                temp_file.replace(csv_path)

                            logger.info(
                                f"Successfully saved {stock_id}.csv to {TICK_DOWNLOADS_PATH} "
                                f"({len(new_df)} rows)"
                            )
                            break

                        except (PermissionError, OSError) as e:
                            if attempt < max_retries - 1:
                                logger.warning(
                                    f"Attempt {attempt + 1}/{max_retries} failed to replace "
                                    f"{stock_id}.csv (file may be in use), retrying in {retry_delay}s..."
                                )
                                time.sleep(retry_delay)
                                retry_delay *= self.RETRY_BACKOFF_MULTIPLIER  # 指數退避
                            else:
                                raise e

                except Exception as e:
                    # 寫入失敗時清掉暫存檔。
                    # **只吞檔案系統的錯**：清不掉暫存檔不該蓋掉真正的失敗原因，
                    # 但裸 except 連 KeyboardInterrupt 都吞，Ctrl+C 會變成什麼都沒發生
                    try:
                        if temp_file.exists():
                            temp_file.unlink()
                    except OSError:
                        pass
                    raise e
                finally:
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
            統一 tick data 的欄位名稱與順序；`volume` 的單位為張（Lot）
        - Parameters:
            - df: pd.DataFrame
                Shioaji 回傳的原始 ticks
            - stock_id: str
                股票代號
        - Return:
            - pd.DataFrame
                統一後的 tick data
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
        把 `time` 欄補足到微秒精度（DolphinDB 的 tick 表要求固定精度）

        補不出來的列一律丟掉：時間是 tick 唯一的排序依據，猜一個值會讓成交順序錯亂。
        """

        try:
            if "time" not in df.columns:
                logger.error("DataFrame missing 'time' column")
                return df

            if not pd.api.types.is_datetime64_any_dtype(df["time"]):
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                if df["time"].isna().any():
                    invalid_count: int = df["time"].isna().sum()
                    logger.warning(
                        f"Found {invalid_count} rows with invalid time format, will be dropped"
                    )
                    df = df.dropna(subset=["time"])
                    if df.empty:
                        logger.error("All rows have invalid time format")
                        return df

            # 微秒必須是完整 6 位小數，少一位在 DolphinDB 端就會對不上精度
            time_str: pd.Series = df["time"].astype(str)
            has_microsec: pd.Series = time_str.str.contains(
                r"\.\d{6}", regex=True, na=False
            )

            if not has_microsec.all():
                df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )
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
