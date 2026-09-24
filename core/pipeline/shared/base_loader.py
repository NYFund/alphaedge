import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type

import pandas as pd
from loguru import logger

from core.dao.connection import DBError
from core.pipeline.utils.exceptions import (
    DataLoadError,
    PipelineError,
    SymbolNameConflictError,
)
from core.utils.constant import FileEncoding

"""Abstract base class for all data loaders that write processed data to a storage system"""


class BaseDataLoader(ABC):
    """Base Class of Data Loader"""

    # 代號與名稱的欄名在清洗後已標準化，三條台股日頻線（price／chip／margin）一致
    SYMBOL_COLUMN: str = "stock_id"
    SYMBOL_NAME_COLUMN: str = "證券名稱"

    # 全半形空白與全額交割註記只是同一個名稱的不同寫法，不代表另一檔證券。
    # **全形空白要寫成字元本身，不可寫 `r"\u3000"`**：pandas 3 在裝了 pyarrow 時
    # 字串欄走 pyarrow 的 RE2，RE2 不認得 `\u` 跳脫，會直接拋 ArrowInvalid
    # （streamlit 會帶進 pyarrow，前端 extra 一裝，三條日頻 loader 全壞）
    NAME_NOISE_PATTERN: str = "[\\s\u3000*＊]"

    # 子類宣告：自建 DAO 時用哪個類別、入庫摘要要印哪個來源名
    DAO_CLASS: Optional[Type[Any]] = None
    SOURCE: str = ""

    # `read_csv` 的欄位型別。**代號一定要指定成 str**：全數字的代號會被推斷成整數，
    # `0050` 入庫就變成 `50`——而且兩者都查得到，只是查不到同一檔
    READ_CSV_DTYPE: Dict[str, str] = {"stock_id": str}

    def __init__(self, dao: Optional[Any] = None) -> None:
        """
        - Description:
            建立 loader；DAO 由呼叫端傳入或自行建立
        - Parameters:
            - dao: Optional[Any]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        self.dao: Optional[Any] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態
        self.conn: Optional[Any] = dao.conn if dao else None

        self.setup()

    def db_path(self) -> Optional[Path]:
        """
        自建 DAO 時要連哪個資料庫；**必須是方法，不可改成類別常數**

        測試以 `monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", ...)`
        改寫各 loader 模組裡的路徑常數。寫成類別常數的話，值在 import 當下就綁死，
        monkeypatch 再也改不到，整批測試會改去動正式資料庫。
        """

        return None

    def downloads_path(self) -> Optional[Path]:
        """CSV 來源目錄；理由同 `db_path()`，維持呼叫當下才讀"""

        return None

    def setup(self) -> None:
        """連線、建表、確保來源目錄存在；需要額外設定的子類覆寫後呼叫 `super().setup()`"""

        self.connect()
        self.create_missing_tables()

        downloads: Optional[Path] = self.downloads_path()
        if downloads is not None:
            downloads.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None and self.DAO_CLASS is not None:
            self.dao = self.DAO_CLASS(db_path=self.db_path())
            self.owns_dao = True
        if self.dao is not None:
            self.conn = self.dao.conn

    def disconnect(self) -> None:
        """Disconnect the Database；共用的 DAO 由建立者關閉"""

        if not self.owns_dao:
            return

        if self.dao is not None:
            self.dao.close()
            self.dao = None
        self.conn = None

    def create_db(self, *args, **kwargs) -> None:
        """Create New Database"""

        self.dao.create_table()

    @abstractmethod
    def create_missing_tables(self) -> None:
        """Ensure Database Tables Exist；各表的索引不同，一律由子類實作"""
        pass

    def preprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        """入庫前的逐檔調整；預設不動，有需要的 loader 覆寫"""

        return df

    @abstractmethod
    # 有些 loader 回傳新增列數（期貨線），有些不回傳（台股線），故標 `Any`
    def add_to_db(self, *args, **kwargs) -> Any:
        """Add Data into Database"""
        pass

    def load_csv_directory(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """
        - Description:
            把 downloads 目錄裡的 CSV 逐檔入庫；**有任何檔案失敗就拋 `DataLoadError`**

            **單檔失敗不可只記 `logger.error` 就放行**：那樣整個行程會以結束碼 0
            回報成功，缺的列要事後逐日比對列數才看得出來（詳見 `finish_load()`）。

            **去重走 `INSERT OR IGNORE`，不可改成先把整張表的主鍵讀進記憶體建 set**：
            後者的記憶體隨資料量成長。交給資料庫自己的主鍵約束後，「重跑」與
            「真的出錯」仍分得開——重複列靜靜跳過，欄位不符、檔案損毀才會拋出。

            **每個檔案包在 savepoint 內**：檔案寫到一半出錯時整檔回滾。少了這層，
            前面已寫入的列會被迴圈結束後的 `commit()` 一起寫進去，資料表多出半份檔案，
            回報卻說這個檔案失敗。

            **三份 CSV loader 共用這個骨架**：進度日誌、空檔跳過、檔內主鍵去重、
            `ignored` 不誤報為 `partial_files` 這四項改一次三份都有。
            **不可退成三份各自寫法的交集**，那等於把已經修對的東西改回去。
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
            - only_dates: Optional[Set[str]]
                只處理這些日期（`YYYYMMDD`）的檔案；None 表示整個目錄
        - Raise:
            - DataLoadError
                有任何檔案入庫失敗
        """

        if self.dao is None:
            self.connect()

        self.create_missing_tables()

        downloads: Path = self.downloads_path()
        csv_files: List[Path] = self.select_csv_files(downloads, only_dates)
        total_files: int = len(csv_files)

        if total_files == 0:
            logger.info(f"[{self.SOURCE}] downloads 目錄沒有 CSV，本次不入庫")
            return

        logger.info(f"[{self.SOURCE}] 找到 {total_files} 個 CSV 待處理")

        succeeded: int = 0
        skipped_files: int = 0
        failed_files: List[str] = []

        for idx, file_path in enumerate(csv_files, start=1):
            try:
                logger.info(f"處理中 [{idx}/{total_files}] {file_path.name}…")

                df: pd.DataFrame = pd.read_csv(file_path, dtype=self.READ_CSV_DTYPE)

                if df.empty:
                    logger.warning(f"略過 {file_path.name}（空檔）")
                    skipped_files += 1
                    continue

                df = self.preprocess(df)

                # 同一批裡一個代號對到兩個名稱，代表有一檔的前導 0 被吃掉了；
                # 整檔視為失敗、一列都不寫，下次執行重試
                self.check_symbol_name_uniqueness(df, file_path.name)

                # 同一檔內的重複列先去掉：`INSERT OR IGNORE` 擋得掉，
                # 但先去掉才數得準「這檔到底寫進去幾列」。
                # **沒宣告主鍵的 DAO 就跳過**：猜錯主鍵會把不該去的列去掉，
                # 那比少一項摘要精確度嚴重得多
                primary_key: Optional[Tuple[str, ...]] = getattr(
                    self.dao, "PRIMARY_KEY_COLUMNS", None
                )
                if primary_key:
                    original_count: int = len(df)
                    df = df.drop_duplicates(subset=list(primary_key), keep="first")
                    if len(df) < original_count:
                        logger.debug(
                            f"{file_path.name} 檔內去重 {original_count - len(df)} 列"
                        )

                inserted: int
                ignored: int
                with self.dao.savepoint():
                    inserted, ignored = self.dao.insert_or_ignore(df)
            except (OSError, ValueError, KeyError, DBError, PipelineError) as e:
                # 讀檔、CSV 解析、來源改欄位名、入庫失敗四類。
                # `DBError` 是 `core.dao` 提供的 `sqlite3.Error` 具名別名——
                # `core/pipeline/` 不得直接 import 資料庫驅動（分層規則）。
                # **`PipelineError` 一定要收**：清洗與驗證階段自己拋的那些
                # （例如 `SymbolNameConflictError`）也屬於單檔失敗，
                # 漏收會讓它直接逃出去，整批在第一個壞檔就中止。
                # **單檔失敗不中止整批**，跑完由 `finish_load()` 一次報出
                logger.error(f"入庫 {file_path.name} 失敗：{e}")
                failed_files.append(file_path.name)
                continue

            if inserted == 0:
                logger.info(f"略過 {file_path.name}（資料都已存在）")
                skipped_files += 1
                continue

            if ignored:
                # **不進 `partial_files`**：`INSERT OR IGNORE` 只知道「主鍵已存在」，
                # 不知道值有沒有不同。重跑一個部分入庫過的日期本來就會有大量 ignored，
                # 把它當成「同鍵不同值」示警，只會訓練讀 log 的人忽略那行警告
                logger.info(
                    f"已寫入 {file_path.name}（新增 {inserted} 列、"
                    f"已存在 {ignored} 列）"
                )
            else:
                logger.info(f"已寫入 {file_path.name}（{inserted} 列）")
            succeeded += 1

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source=self.SOURCE,
            succeeded=succeeded,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=downloads,
            skipped_files=skipped_files,
        )

    @staticmethod
    def save_csv(df: pd.DataFrame, path: Path) -> Path:
        """
        - Description:
            把 DataFrame 存成 CSV 並回傳路徑

            `utf-8-sig` 不是裝飾性的選擇：少了 BOM，Excel 開中文欄名會是亂碼，
            而這些中繼檔的第一個讀者常常是人。
        - Parameters:
            - df: pd.DataFrame
                要存檔的資料
            - path: Path
                目標路徑（父目錄不存在時自動建立）
        - Return:
            - Path
                實際寫出的路徑
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        return path

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

            **分批入庫需要它**：updater 每 N 天就入庫一次，若每批仍掃整個 downloads
            目錄，13 年的回補會變成「160 批 × 6,600 檔」的重複讀取。
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

            「單檔失敗不中止整批」本身是對的——其餘檔案仍該入庫；不可接受的是
            **跑完之後不吭聲**。故此處在全部處理完才拋出，兩者兼顧。

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
