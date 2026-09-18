import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from loguru import logger

from core.pipeline.utils.exceptions import DataLoadError, SymbolNameConflictError

"""Abstract base class for all data loaders that write processed data to a storage system"""


class BaseDataLoader(ABC):
    """Base Class of Data Loader"""

    # 代號與名稱的欄名在清洗後已標準化，三條台股日頻線（price／chip／margin）一致
    SYMBOL_COLUMN: str = "stock_id"
    SYMBOL_NAME_COLUMN: str = "證券名稱"

    # 全半形空白與全額交割註記只是同一個名稱的不同寫法，不代表另一檔證券
    NAME_NOISE_PATTERN: str = r"[\s\u3000*＊]"

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

    @classmethod
    def check_symbol_name_uniqueness(cls, df: pd.DataFrame, label: str) -> None:
        """
        - Description:
            入庫前確認這一批裡「一個證券代號只對到一個證券名稱」

            **這是前導 0 被吃掉之後唯一擋得住的環節**：`pd.read_csv()` 只要看到
            某份檔案的代號全是數字就整欄推斷成整數，`006201`（元大富櫃50）少掉兩個
            0 剛好是合法的上市代號 `6201`（亞弘電）。寫入端的 `dtype` 已經擋住一層，
            這裡擋的是來源本身就給錯、或日後新增的讀檔路徑忘了指定型別。

            事後稽核只對主鍵含證券名稱的表有效（`price`、`chip` 會並存兩列）；
            `margin` 的主鍵是 `(date, stock_id)`，冒名的那一列在 `INSERT OR IGNORE`
            當下就消失，表裡永遠看不出來。**入庫前檢查沒有這個分別。**

            來源當天的表裡同一個代號只會有一個名稱——改名是跨時間的，同一天不會有
            兩個名字——所以命中時一定是錯的，直接拋出讓整批失敗，不留半份資料。
        - Parameters:
            - df: pd.DataFrame
                清洗後、準備寫入的單批資料（清洗前的欄名尚未標準化）
            - label: str
                批次描述（通常是檔名），只用於錯誤訊息
        - Raise:
            - SymbolNameConflictError
                有代號對到兩個以上的證券名稱
        """

        if (
            df.empty
            or cls.SYMBOL_COLUMN not in df.columns
            or cls.SYMBOL_NAME_COLUMN not in df.columns
        ):
            return

        work: pd.DataFrame = pd.DataFrame(
            {
                "symbol": df[cls.SYMBOL_COLUMN].astype(str),
                "name": df[cls.SYMBOL_NAME_COLUMN]
                .fillna("")
                .astype(str)
                .str.replace(cls.NAME_NOISE_PATTERN, "", regex=True),
            }
        )

        # 名稱缺漏的列不參與比對：空字串不是「另一個名稱」
        work = work[work["name"] != ""]

        name_counts: pd.Series = work.groupby("symbol")["name"].nunique()
        conflicted: List[str] = sorted(name_counts[name_counts > 1].index)
        if not conflicted:
            return

        conflicts: Dict[str, List[str]] = {
            symbol: sorted(set(work.loc[work["symbol"] == symbol, "name"]))
            for symbol in conflicted
        }
        raise SymbolNameConflictError(label, conflicts)

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
    def finish_load(
        source: str,
        succeeded: int,
        failed_files: List[str],
        remove_files: bool = False,
        downloads_path: Optional[Path] = None,
        skipped_files: int = 0,
        partial_files: Optional[List[str]] = None,
        new_rows: Optional[int] = None,
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
            - new_rows: Optional[int]
                跨檔合併後整批寫入的 loader 用：`succeeded` 此時是**讀檔數**，
                檔案與寫入沒有一對一關係，「新寫入 N 檔」無從算起；改以新增列數摘要，
                重跑時才看得出「讀了 N 檔、其實沒有新資料」
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

        if new_rows is not None:
            logger.info(
                f"[{source}] 入庫完成：讀取 {succeeded} 檔、新增 {new_rows} 列、失敗 0 檔"
            )
            return

        logger.info(
            f"[{source}] 入庫完成：新寫入 {succeeded} 檔、"
            f"已存在跳過 {skipped_files} 檔、失敗 0 檔"
        )
