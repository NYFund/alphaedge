import datetime
import shutil
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

try:
    import dolphindb as ddb
except ModuleNotFoundError:
    print("Warning: dolphindb module is not installed")

from core.config import (
    API_KEYS,
    API_SECRET_KEYS,
    TICK_DOWNLOADS_PATH,
    TICK_METADATA_DIR_PATH,
    TICK_METADATA_PATH,
)
from core.pipeline.utils.data_utils import DataUtils
from core.utils import ShioajiAPI

"""
台股 tick 的 DolphinDB 與 metadata 工具

**綁定台股，故與 `url_manager.py` 同樣歸在 `tw/` 底下**：報價來源是 Shioaji
（台灣券商），metadata 的鍵是台股代號；每層目錄只承載一條軸，
跨市場共用的 `core/pipeline/utils/` 不放只有台股用得到的東西。
"""


class StockTickUtils:
    """台股 tick 的下載進度工具：掃描中繼檔並維護 `tick_metadata.json`"""

    # 類別層級的鎖：多執行緒下載時保護 metadata 檔的讀寫
    _metadata_lock: Lock = Lock()

    # 無法從 metadata 取得日期時的預設 fallback 日期
    TICK_DEFAULT_FALLBACK_DATE: datetime.date = datetime.date(2020, 4, 1)

    @staticmethod
    def get_table_latest_date() -> datetime.date:
        """從 tick_metadata.json 中取得 tick table 的最新日期"""

        time_data: Dict[str, Any] = DataUtils.load_json(TICK_METADATA_PATH)
        if time_data is None:
            return StockTickUtils.TICK_DEFAULT_FALLBACK_DATE

        # 各股票的進度不一定同步，取全部之中最新的那一天
        latest_date: Optional[datetime.date] = None
        for stock_info in time_data.get("stocks", {}).values():
            if "last_date" in stock_info:
                stock_date: datetime.date = datetime.date.fromisoformat(
                    stock_info["last_date"]
                )
                if latest_date is None or stock_date > latest_date:
                    latest_date = stock_date
        return latest_date if latest_date else StockTickUtils.TICK_DEFAULT_FALLBACK_DATE

    @staticmethod
    def generate_tick_metadata_backup() -> None:
        """建立 tick_metadata 的備份檔案"""

        # 首次執行時 metadata 還不存在，先補一份空的，`copy2` 才有東西可備份
        if not TICK_METADATA_PATH.exists():
            TICK_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            default_metadata: Dict[str, Dict[str, Any]] = {"stocks": {}}
            DataUtils.save_json(
                default_metadata, TICK_METADATA_PATH, ensure_ascii=False, indent=4
            )

        backup_suffix: str = "_backup"
        backup_name: Path = TICK_METADATA_PATH.with_name(
            TICK_METADATA_PATH.stem + backup_suffix + TICK_METADATA_PATH.suffix
        )
        shutil.copy2(TICK_METADATA_PATH, backup_name)

    @staticmethod
    def setup_shioaji_apis() -> List[ShioajiAPI]:
        """
        - Description:
            依設定檔中的金鑰組建立所有 Shioaji API 連線

            逐筆行情有每把金鑰的請求上限，故備多組金鑰輪流下載；
            金鑰與密鑰以 `zip` 配對，數量不一致時以短的那一邊為準。
        - Return:
            - List[ShioajiAPI]
                依設定順序建立的 API 清單
        """

        api_list: List[ShioajiAPI] = []
        for key, secret in zip(API_KEYS, API_SECRET_KEYS):
            api: ShioajiAPI = ShioajiAPI(key, secret)
            api_list.append(api)
        return api_list

    @staticmethod
    def scan_tick_downloads_folder() -> Dict[str, str]:
        """
        掃描 tick 下載資料夾，記錄每個已下載檔案的股票代號和最後一筆資料的日期

        - Return:
            - Dict[str, str]
                股票代號 -> 最後一筆資料日期（`YYYY-MM-DD`）
        """

        stock_last_dates: Dict[str, str] = {}

        if not TICK_DOWNLOADS_PATH.exists():
            logger.warning(
                f"Tick downloads folder does not exist: {TICK_DOWNLOADS_PATH}"
            )
            return stock_last_dates

        csv_files: List[Path] = list(TICK_DOWNLOADS_PATH.glob("*.csv"))
        logger.info(f"Scanning {len(csv_files)} CSV files in tick downloads folder...")

        for csv_file in csv_files:
            stock_id: str = csv_file.stem  # 取得檔名（不含副檔名）作為股票代號

            try:
                # 只讀 `time` 欄：單檔 tick 動輒數十萬列，整份讀進來純屬浪費
                df: pd.DataFrame = pd.read_csv(csv_file, usecols=["time"])

                if df.empty:
                    logger.warning(f"File {csv_file.name} is empty. Skipping.")
                    continue

                # tick 依時間遞增寫入，最後一列即當檔最新的一筆
                last_time_str: str = df["time"].iloc[-1]

                # 解析時間字串（格式：YYYY-MM-DD HH:MM:SS.ffffff）
                try:
                    last_time: pd.Timestamp = pd.to_datetime(last_time_str)
                    last_date: datetime.date = last_time.date()
                    stock_last_dates[stock_id] = last_date.isoformat()
                    logger.debug(
                        f"Stock {stock_id}: last date = {last_date.isoformat()}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to parse time '{last_time_str}' in {csv_file.name}: {e}"
                    )
                    continue

            except Exception as e:
                logger.error(f"Error reading file {csv_file.name}: {e}")
                continue

        logger.info(f"Scanned {len(stock_last_dates)} stock files successfully")
        return stock_last_dates

    @staticmethod
    def update_tick_metadata_from_csv() -> None:
        """
        掃描 tick 下載資料夾，把各股票最後一筆資料的日期寫回 `tick_metadata.json`

        **只更新有 CSV 檔案的股票，其餘沿用舊值**：下載是分批進行的，
        以本次掃描的結果整份覆寫會讓沒排到的股票憑空倒退回未下載狀態。
        寫入前先備份成 `tick_metadata_backup.json`，並以暫存檔 rename 達成原子性；
        整段以類別層級的鎖保護，多執行緒下載時才不會互相蓋掉。

        產生的 JSON 結構：
        {
            "stocks": {
                "2330": {
                    "last_date": "2024-01-15"
                },
                "2317": {
                    "last_date": "2024-01-20"
                }
            }
        }

        - stocks：key 為股票代號（字串），value 為該股票的資訊物件
        - last_date：該股票 CSV 中最後一筆資料的日期，格式 `YYYY-MM-DD`
        """

        with StockTickUtils._metadata_lock:
            TICK_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)

            # 備份失敗只記 warning：備份是保險，不該讓本次更新整個做不成
            backup_path: Path = TICK_METADATA_DIR_PATH / "tick_metadata_backup.json"
            if TICK_METADATA_PATH.exists():
                try:
                    shutil.copy2(TICK_METADATA_PATH, backup_path)
                    logger.info(f"Backed up tick_metadata.json to {backup_path}")
                except Exception as e:
                    logger.warning(f"Failed to backup tick_metadata.json: {e}")

            existing_metadata: Dict[str, Dict[str, str]] = (
                StockTickUtils.load_tick_metadata_stocks()
            )

            stock_last_dates: Dict[str, str] = (
                StockTickUtils.scan_tick_downloads_folder()
            )

            # 舊值打底、本次掃到的才覆蓋：沒排到的股票不可憑空倒退回未下載
            metadata: Dict[str, Any] = {
                "stocks": existing_metadata.copy() if existing_metadata else {}
            }
            for stock_id, last_date in stock_last_dates.items():
                metadata["stocks"][stock_id] = {"last_date": last_date}

            # 先寫暫存檔再 rename：寫到一半中斷時，原檔仍是完整的上一版
            temp_path: Path = TICK_METADATA_PATH.with_suffix(".tmp")
            try:
                DataUtils.save_json(metadata, temp_path, ensure_ascii=False, indent=4)
                temp_path.replace(TICK_METADATA_PATH)
                logger.info("Successfully updated tick_metadata.json")
            except Exception as e:
                # 寫入失敗就清掉暫存檔。
                # **只吞檔案系統的錯**：清不掉暫存檔不該蓋掉真正的失敗原因，
                # 而裸 except 連 KeyboardInterrupt 都吞得下去
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise e

            updated_count: int = len(stock_last_dates)
            total_count: int = len(metadata["stocks"])
            logger.info(
                f"Updated tick metadata: {updated_count} stocks updated from CSV files, "
                f"{total_count} total stocks in metadata"
            )

    @staticmethod
    def load_tick_metadata_stocks() -> Dict[str, Dict[str, str]]:
        """
        讀取 `tick_metadata.json` 中的股票資訊（以鎖保護，可在多執行緒下呼叫）

        - Return:
            - Dict[str, Dict[str, str]]
                股票代號 -> 該股票的資訊（目前只有 `last_date`）；
                檔案不存在或讀取失敗時回空字典

                {
                    "1101": {
                        "last_date": "2024-05-15"
                    },
                    "2330": {
                        "last_date": "2024-01-15"
                    }
                }
        """

        with StockTickUtils._metadata_lock:
            if not TICK_METADATA_PATH.exists():
                return {}

            try:
                metadata: Dict[str, Any] = DataUtils.load_json(TICK_METADATA_PATH)
                if metadata is None:
                    return {}
                return metadata.get("stocks", {})
            except Exception as e:
                logger.warning(
                    f"Failed to load tick downloads metadata: {e}. Returning empty dict."
                )
                return {}

    @staticmethod
    def check_date_crawled(stock_id: str, date: datetime.date) -> bool:
        """
        - Description:
            檢查某檔股票的某個日期是否已經爬取過（資料已在資料庫中）

            判斷依據是 `tick_metadata.json` 記錄的該股票最新日期：
            **只要不晚於該日期就視為已爬**，因為 tick 是逐日往後補的。
        - Parameters:
            - stock_id: str
                股票代號
            - date: datetime.date
                要檢查的日期
        - Return:
            - bool
                True 表示日期已爬取（資料已存在於資料庫），False 表示需要爬取
        """

        stocks_metadata: Dict[str, Dict[str, str]] = (
            StockTickUtils.load_tick_metadata_stocks()
        )

        # 不在 metadata 中即代表從未下載過
        if stock_id not in stocks_metadata:
            return False

        stock_info: Dict[str, str] = stocks_metadata[stock_id]
        last_date_str: Optional[str] = stock_info.get("last_date")

        if not last_date_str:
            return False

        try:
            last_date: datetime.date = datetime.date.fromisoformat(last_date_str)
            return date <= last_date
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Failed to parse last_date '{last_date_str}' for stock {stock_id}: {e}"
            )
            return False
