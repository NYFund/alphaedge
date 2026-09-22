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
    """Tick DolphinDB Tools"""

    # 類級別的鎖，用於保護 metadata 文件的讀寫操作
    _metadata_lock: Lock = Lock()

    # 無法從 metadata 取得日期時的預設 fallback 日期
    TICK_DEFAULT_FALLBACK_DATE: datetime.date = datetime.date(2020, 4, 1)

    @staticmethod
    def get_table_latest_date() -> datetime.date:
        """從 tick_metadata.json 中取得 tick table 的最新日期"""

        time_data: Dict[str, Any] = DataUtils.load_json(TICK_METADATA_PATH)
        if time_data is None:
            return StockTickUtils.TICK_DEFAULT_FALLBACK_DATE
        # 從所有股票中找出最新的日期
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

        # 如果檔案不存在，先創建一個預設的 metadata 檔案
        if not TICK_METADATA_PATH.exists():
            # 確保目錄存在
            TICK_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 創建預設的 metadata 檔案（新格式）
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
        # Add API from 11 ~ 17 and add API_1 (Mine)
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
                股票代號 -> 最後一筆資料日期 (YYYY-MM-DD 格式)
        """
        stock_last_dates: Dict[str, str] = {}

        # 確保資料夾存在
        if not TICK_DOWNLOADS_PATH.exists():
            logger.warning(
                f"Tick downloads folder does not exist: {TICK_DOWNLOADS_PATH}"
            )
            return stock_last_dates

        # 掃描所有 CSV 檔案
        csv_files: List[Path] = list(TICK_DOWNLOADS_PATH.glob("*.csv"))
        logger.info(f"Scanning {len(csv_files)} CSV files in tick downloads folder...")

        for csv_file in csv_files:
            stock_id: str = csv_file.stem  # 取得檔名（不含副檔名）作為股票代號

            try:
                # 讀取 CSV 檔案（只讀取 time 欄位以提升效能）
                df: pd.DataFrame = pd.read_csv(csv_file, usecols=["time"])

                if df.empty:
                    logger.warning(f"File {csv_file.name} is empty. Skipping.")
                    continue

                # 取得最後一筆資料的時間
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
        掃描 tick 下載資料夾並更新 tick_metadata.json 中的股票資訊
        記錄每個已下載檔案的股票代號和最後一筆資料的日期
        此函數會保留舊的 metadata，只更新有 CSV 檔案的股票資訊
        在更新前會先備份現有的 tick_metadata.json 到 tick_metadata_backup.json

        此方法使用線程安全的鎖機制來保護 metadata 文件的讀寫操作

        產生的 JSON Schema 範例：
        {
            "stocks": {
                "2330": {
                    "last_date": "2024-01-15"
                },
                "2317": {
                    "last_date": "2024-01-20"
                },
                "2454": {
                    "last_date": "2024-01-18"
                }
            }
        }

        說明：
        - stocks: 物件，key 為股票代號（字串），value 為該股票的資訊物件
        - last_date: 字串，格式為 YYYY-MM-DD，表示該股票 CSV 檔案中最後一筆資料的日期
        """
        # 使用線程安全的鎖來保護 metadata 更新操作
        with StockTickUtils._metadata_lock:
            # 確保目錄存在
            TICK_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)

            # 備份現有的 metadata（如果存在）
            backup_path: Path = TICK_METADATA_DIR_PATH / "tick_metadata_backup.json"
            if TICK_METADATA_PATH.exists():
                try:
                    shutil.copy2(TICK_METADATA_PATH, backup_path)
                    logger.info(f"Backed up tick_metadata.json to {backup_path}")
                except Exception as e:
                    logger.warning(f"Failed to backup tick_metadata.json: {e}")

            # 讀取現有的 metadata（保留舊資料）
            existing_metadata: Dict[str, Dict[str, str]] = (
                StockTickUtils.load_tick_metadata_stocks()
            )

            # 掃描資料夾，取得所有 CSV 檔案中的股票資訊
            stock_last_dates: Dict[str, str] = (
                StockTickUtils.scan_tick_downloads_folder()
            )

            # 保留舊的 metadata，只更新有 CSV 檔案的股票
            metadata: Dict[str, Any] = {
                "stocks": existing_metadata.copy() if existing_metadata else {}
            }
            for stock_id, last_date in stock_last_dates.items():
                metadata["stocks"][stock_id] = {"last_date": last_date}

            # 寫入更新後的 metadata（使用臨時文件確保原子性）
            temp_path: Path = TICK_METADATA_PATH.with_suffix(".tmp")
            try:
                DataUtils.save_json(metadata, temp_path, ensure_ascii=False, indent=4)
                # 原子性操作：將臨時文件移動到目標位置
                temp_path.replace(TICK_METADATA_PATH)
                logger.info("Successfully updated tick_metadata.json")
            except Exception as e:
                # 如果寫入失敗，刪除臨時文件
                try:
                    temp_path.unlink()
                except:
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
        讀取 tick_metadata.json 中的股票資訊（線程安全）

        - Return:
            - Dict[str, Dict[str, str]]
                股票代號 -> 股票資訊（包含 last_date）

        回傳格式範例：
        {
            "1101": {
                "last_date": "2024-05-15"
            },
            "1102": {
                "last_date": "2024-05-15"
            },
            "2330": {
                "last_date": "2024-01-15"
            }
        }

        說明：
        - 外層 key: 股票代號（字串）
        - 內層 value: 包含 last_date 的字典，last_date 格式為 YYYY-MM-DD
        - 如果檔案不存在或讀取失敗，回傳空字典 {}
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
        檢查某個股票的某個日期是否已經爬取過（已存在於資料庫中）
        此函數會從 tick_metadata.json 讀取每檔股票在資料庫中的最新日期（線程安全）

        - Parameters:
            - stock_id: str
                股票代號
            - date: datetime.date
                要檢查的日期
        - Return:
            - bool
                True 表示日期已爬取（資料已存在於資料庫），False 表示需要爬取
        """
        # 讀取 metadata（線程安全）
        stocks_metadata: Dict[str, Dict[str, str]] = (
            StockTickUtils.load_tick_metadata_stocks()
        )

        # 如果該股票不在 metadata 中，表示沒有下載過，需要爬取
        if stock_id not in stocks_metadata:
            return False

        # 取得該股票的最後一筆資料日期
        stock_info: Dict[str, str] = stocks_metadata[stock_id]
        last_date_str: Optional[str] = stock_info.get("last_date")

        if not last_date_str:
            return False

        try:
            last_date: datetime.date = datetime.date.fromisoformat(last_date_str)
            # 如果要爬取的日期小於或等於最後一筆資料日期，則跳過
            return date <= last_date
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Failed to parse last_date '{last_date_str}' for stock {stock_id}: {e}"
            )
            return False
