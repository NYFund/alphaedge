import shutil
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.dao.base import create_symbol_date_index, insert_or_ignore
from core.pipeline.utils.exceptions import DataLoadError

"""Abstract base class for all data loaders that write processed data to a storage system"""


class BaseDataLoader(ABC):
    """Base Class of Data Loader"""

    def __init__(self) -> None:
        pass

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Loader"""
        pass

    @abstractmethod
    def connect(self) -> None:
        """Connect to the Database"""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect the Database"""
        pass

    @abstractmethod
    def create_db(self, *args, **kwargs) -> None:
        """Create New Database"""
        pass

    @abstractmethod
    def create_missing_tables(self) -> None:
        """Ensure Database Tables Exist"""
        pass

    @abstractmethod
    # 有些 loader 回傳新增列數（期貨線），有些不回傳（台股線），故標 `Any`
    def add_to_db(self, *args, **kwargs) -> Any:
        """Add Data into Database"""
        pass

    @staticmethod
    def create_symbol_date_index(conn: "sqlite3.Connection", table_name: str) -> None:
        """
        - Description:
            建立 `(stock_id, date)` 索引並 commit

            實作在 `core.dao.base.create_symbol_date_index()`（為什麼需要這個索引見該函式）；
            loader 全部改走 DAO 後刪除。
        - Parameters:
            - conn: sqlite3.Connection
                資料庫連線
            - table_name: str
                目標資料表
        """

        create_symbol_date_index(conn, table_name)

    @staticmethod
    def select_csv_files(
        directory: Path, only_dates: Optional[Set[str]] = None
    ) -> List[Path]:
        """
        - Description:
            挑出這次要入庫的 CSV；`only_dates` 為 None 時取整個目錄

            **分批入庫需要它**：updater 改為每 N 天就入庫一次之後，若每批仍掃整個
            downloads 目錄，13 年的回補會變成「160 批 × 6,600 檔」的重複讀取。
            傳入本批的日期即可只處理該批產出的檔案。

            檔名慣例為 `{exchange}_{YYYYMMDD}.csv`（`twse`／`tpex`，三個高風險
            來源一致），故以底線後的最後一段比對日期，不依賴交易所前綴。
        - Parameters:
            - directory: Path
                downloads 目錄
            - only_dates: Optional[Set[str]]
                `YYYYMMDD` 字串集合；None 表示不過濾
        - Return:
            - List[Path]
                依檔名排序的 CSV 清單
        """

        files: List[Path] = sorted(
            path for path in directory.iterdir() if path.suffix == ".csv"
        )
        if only_dates is None:
            return files

        return [path for path in files if path.stem.split("_")[-1] in only_dates]

    @staticmethod
    def insert_dataframe(
        conn: "sqlite3.Connection", table_name: str, df: "pd.DataFrame"
    ) -> Tuple[int, int]:
        """
        - Description:
            以 `INSERT OR IGNORE` 寫入，回傳實際寫入與被跳過的列數；不 commit

            實作在 `core.dao.base.insert_or_ignore()`（為什麼不用 `to_sql` 見該函式）；
            loader 全部改走 DAO 後刪除。
        - Parameters:
            - conn: sqlite3.Connection
                目標資料庫連線
            - table_name: str
                目標資料表
            - df: pd.DataFrame
                欄位需與資料表一致
        - Return:
            - Tuple[int, int]
                （實際寫入列數, 因主鍵重複被跳過的列數）
        """

        return insert_or_ignore(conn, table_name, df)

    @staticmethod
    def finish_load(
        source: str,
        succeeded: int,
        failed_files: List[str],
        remove_files: bool = False,
        downloads_path: Optional[Path] = None,
        skipped_files: int = 0,
        partial_files: Optional[List[str]] = None,
    ) -> None:
        """
        - Description:
            彙報單次入庫結果，並在有失敗時讓呼叫端無法忽略

            **逐檔 `except` 之後只記 warning、迴圈照跑、最後印成功，是本專案實際
            出過事的樣式**：2026-08-16 的 margin 回補有 2 個檔案入庫失敗，行程仍以
            結束碼 0 回報成功，缺的 1,553 列是事後逐日比對列數才發現的。

            「單檔失敗不中止整批」本身是對的——其餘檔案仍該入庫；錯的是**跑完之後
            不吭聲**。故此處在全部處理完才拋出，兩者兼顧。

            `remove_files` 的刪除動作也收在這裡：**有失敗時一律不刪來源**，
            否則會把還沒成功入庫的資料一起刪掉，連重試的機會都沒有。

            三種結果要分清楚，否則「重跑」會被誤判為「出錯」：
            - **已存在而整檔跳過**：重跑的正常結果，只記一行摘要。
            - **同一檔部分跳過**：同鍵不同值，資料本身可能有問題，發出警告。
            - **拋出例外**：欄位不符、檔案損毀等真正的失敗，才會讓行程非零結束。
        - Parameters:
            - source: str
                資料來源名稱，用於訊息辨識（例如 "margin"）
            - succeeded: int
                成功入庫的檔案數
            - failed_files: List[str]
                入庫失敗的檔案清單
            - remove_files: bool
                是否在成功後刪除來源檔案目錄
            - downloads_path: Optional[Path]
                來源檔案目錄；`remove_files` 為 True 時必填
            - skipped_files: int
                因資料已存在而整檔跳過的檔案數（重跑的正常結果）
            - partial_files: Optional[List[str]]
                只有部分列被寫入的檔案；代表同鍵不同值，值得檢查
        - Raise:
            - DataLoadError
                `failed_files` 非空時拋出
        """

        if partial_files:
            logger.warning(
                f"[{source}] {len(partial_files)} 個檔案只有部分列寫入（同鍵不同值），"
                f"請確認資料是否有衝突：{partial_files[:10]}"
            )

        if failed_files:
            logger.error(
                f"[{source}] 入庫未完全成功：成功 {succeeded} 檔、失敗 "
                f"{len(failed_files)} 檔；失敗清單：{failed_files[:20]}"
                + ("…（僅列前 20 筆）" if len(failed_files) > 20 else "")
            )
            if remove_files:
                logger.error(f"[{source}] 因有失敗檔案，已略過刪除來源目錄")
            raise DataLoadError(source, failed_files, succeeded)

        if remove_files and downloads_path is not None:
            shutil.rmtree(downloads_path)

        logger.info(
            f"[{source}] 入庫完成：新寫入 {succeeded} 檔、"
            f"已存在跳過 {skipped_files} 檔、失敗 0 檔"
        )
