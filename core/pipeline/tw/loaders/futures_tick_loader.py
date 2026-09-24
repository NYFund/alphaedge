import time
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd
from loguru import logger

try:
    import dolphindb as ddb
except ModuleNotFoundError:
    logger.info("Warning: dolphindb module is not installed")

from core.config import (
    DDB_HOST,
    DDB_PASSWORD,
    DDB_PATH,
    DDB_PORT,
    DDB_USER,
    FUTURES_TICK_DOWNLOADS_PATH,
    FUTURES_TICK_TABLE_NAME,
    TICK_DB_NAME,
    TICK_DB_PATH,
    require_tick_db_path,
)
from core.pipeline.shared.base_loader import BaseDataLoader
from core.utils.constant import FileEncoding

"""
台期貨 Tick Loader（DolphinDB）

**與股票 tick 同一個資料庫、不同表**：資料庫（`tickDB`）的分割設定
（依日期 VALUE ＋ 依代號 HASH）對兩者都適用，但表結構不同——

| | 股票 | 期貨 |
|---|------|------|
| 識別 | `stock_id` | `product` ＋ `expiry`（同商品多個契約） |
| 時段 | 無 | `session`（日盤／夜盤是兩段獨立行情） |

塞同一張表的話，分割鍵會失去意義（期貨的 `stock_id` 要塞什麼？），
而且每次查詢都得先過濾「這是股票還是期貨」。

⚠️ **本檔的寫入路徑尚未於本機實測**：DolphinDB server 未啟動
（`localhost:8848` 連線被拒），`dolphindb` 套件也未安裝（屬 `[tick]` 選用相依）。
結構完全比照已在生產跑過的 `StockTickLoader`，但**在真的跑起來之前不要當成
已驗證**。
"""


class FuturesTickLoader(BaseDataLoader):
    """Futures Tick Loader"""

    # 與股票 tick 共用同一個資料庫；分割設定沿用（見模組說明）
    DEFAULT_TICK_DB_START_TIME: str = "2015.01.01"
    DEFAULT_TICK_DB_END_TIME: str = "2030.12.31"
    CONNECT_MAX_RETRIES: int = 3
    CONNECT_RETRY_DELAY: float = 1.0

    def __init__(self) -> None:
        """建立期貨 tick loader（連不上 DolphinDB 時 `session` 留 None）"""

        # **不呼叫 `super().__init__()`**：本 loader 收的是連線或多個 DAO，
        # 與基底「單一 DAO」的建構骨架不同形，連線與建表一律自理
        # 型別標註刻意用 Any：`ddb` 在未安裝 dolphindb 時根本不存在，
        # 而賦值語句的標註會在執行期求值——寫 `Optional[ddb.session]` 會讓
        # 這個類別在沒有該套件的機器上一建立就 NameError
        self.session: Optional[Any] = None
        self.tick_dir: Path = FUTURES_TICK_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.tick_dir.mkdir(parents=True, exist_ok=True)
        self.connect()
        if self.session is not None:
            self.create_missing_tables()

    def connect(
        self,
        max_retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
    ) -> None:
        """
        連線 DolphinDB（含重試）

        **連不上不拋錯而是留 `session=None`**：期貨 tick 是選用功能，
        沒有 DolphinDB 的環境（例如 CI 與只跑日線回測的機器）不該因為
        import 到本類就整個壞掉。實際寫入時才會再檢查一次。
        """

        # `DDB_PATH` 沒設定時會拼出 `"NonetickDB"` 這種看起來像路徑的字串，
        # 錯誤訊息完全指不到真正的原因；在連線之前就攔下來
        require_tick_db_path()

        attempts: int = max_retries or self.CONNECT_MAX_RETRIES
        delay: float = retry_delay or self.CONNECT_RETRY_DELAY

        for attempt in range(1, attempts + 1):
            try:
                session = ddb.session()
                session.connect(DDB_HOST, DDB_PORT, DDB_USER, DDB_PASSWORD)
                self.session = session
                logger.info("Successfully connected to DolphinDB")
                return
            except NameError:
                logger.warning(
                    "[Futures Tick] dolphindb 套件未安裝（選用相依 `[tick]`），"
                    "本次不寫入資料庫"
                )
                return
            except Exception as error:
                if attempt < attempts:
                    logger.warning(
                        f"Connection attempt {attempt}/{attempts} failed: {error}. "
                        f"Retrying in {delay} seconds..."
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        f"[Futures Tick] 連不上 DolphinDB（{attempts} 次皆失敗）："
                        f"{error}；本次只保留中繼檔，不寫入資料庫"
                    )

    def disconnect(self) -> None:
        """Disconnect the Database"""

        if self.session is not None:
            self.session.close()
            self.session = None

    def create_db(self) -> None:
        """
        建立期貨 tick 表（資料庫沿用股票 tick 的 `tickDB`）

        **`keepDuplicates=ALL`**：同一個時間戳可能有多筆成交，去重會直接丟掉
        真實的成交筆數。
        """

        if self.session is None:
            return

        script: str = f"""
        if(!existsDatabase("{TICK_DB_PATH}")){{
            create database "{DDB_PATH}{TICK_DB_NAME}"
            partitioned by VALUE({self.DEFAULT_TICK_DB_START_TIME}..{self.DEFAULT_TICK_DB_END_TIME}), HASH([SYMBOL, 25])
            engine='TSDB'
        }}
        if(!existsTable("{TICK_DB_PATH}", "{FUTURES_TICK_TABLE_NAME}")){{
            create table "{TICK_DB_PATH}"."{FUTURES_TICK_TABLE_NAME}"(
                product SYMBOL
                expiry SYMBOL
                session SYMBOL
                time NANOTIMESTAMP
                close FLOAT
                volume INT
                bid_price FLOAT
                bid_volume INT
                ask_price FLOAT
                ask_volume INT
                tick_type INT
            )
            partitioned by time, product,
            sortColumns=[`product, `expiry, `time],
            keepDuplicates=ALL
        }}
        """
        try:
            self.session.run(script)
            logger.info("[Futures Tick] DolphinDB 表已就緒")
        except Exception as error:
            logger.warning(f"[Futures Tick] 建表失敗：{error}")

    def create_missing_tables(self) -> None:
        """Ensure Database Tables Exist"""

        self.create_db()

    def add_to_db(self, df: pd.DataFrame) -> int:
        """
        - Description:
            把清洗後的逐筆成交寫進 DolphinDB

            **沒有連線時回 0 並記 warning**：中繼檔已經留下來了，之後補寫即可，
            不該讓整條爬蟲因為資料庫沒開而中止。
        - Parameters:
            - df: pd.DataFrame
                清洗後的 tick
        - Return:
            - int
                寫入列數
        """

        if df is None or df.empty:
            return 0

        if self.session is None:
            logger.warning(
                "[Futures Tick] 沒有 DolphinDB 連線，本批只保留中繼檔（未入庫）"
            )
            return 0

        try:
            appender = ddb.PartitionedTableAppender(
                dbPath=TICK_DB_PATH,
                tableName=FUTURES_TICK_TABLE_NAME,
                partitionColName="product",
                dbConnectionPool=ddb.DBConnectionPool(
                    DDB_HOST, DDB_PORT, 1, DDB_USER, DDB_PASSWORD
                ),
            )
            appender.append(df)
            logger.info(f"[Futures Tick] 寫入 {len(df)} 列")
            return len(df)
        except Exception as error:
            logger.warning(f"[Futures Tick] 寫入失敗：{error}")
            return 0

    def load_csv_files(self, files: Optional[List[Path]] = None) -> int:
        """把中繼檔補寫進資料庫（DolphinDB 之後才啟動時使用）"""

        targets: List[Path] = files or sorted(self.tick_dir.glob("*.csv"))
        total: int = 0

        for path in targets:
            df: pd.DataFrame = pd.read_csv(path, encoding=FileEncoding.UTF8_SIG.value)
            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            total += self.add_to_db(df.dropna(subset=["time"]))

        return total
