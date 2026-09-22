import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_CHIP_DOWNLOADS_PATH, TW_FUTURES_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
台期貨籌碼 Loader（三張表）

**三個資料集分三張表**，理由與保證金分兩張表相同：主鍵不同、欄位不同。
硬塞同一張表會讓多數欄位永遠是 NULL，且下游得先判斷「這是哪一種籌碼」
才知道該讀哪一組欄位。

| 表 | 主鍵 | 一天的列數 |
|----|------|-----------|
| `futures_institutional_chip` | (date, product_name, investor) | 商品數 × 3 |
| `futures_large_trader` | (date, product, expiry, trader_type) | 約 1,400 |
| `futures_put_call_ratio` | (date) | 1 |

**建表以第一次入庫的 DataFrame 推導欄位**，只把主鍵與型別釘死（見 `FuturesChipDAO`）。

**本 loader 不 commit**：寫入包在 savepoint 內，何時落地由 updater 決定
（每個月批次寫完 commit 一次）。舊版每次 `add_to_db()` 都自己 commit，
呼叫端無從把幾次寫入綁成一個交易。
"""


class FuturesChipLoader(BaseDataLoader):
    """把清洗後的籌碼資料寫進 tw_futures.db"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        """
        - Description:
            建立期貨籌碼 loader
        - Parameters:
            - conn: Optional[DBConnection]
                共用連線（通常由 updater 傳入）。三張表共用一條連線，故收連線而不是單一 DAO；
                指定時 loader 不擁有它，`disconnect()` 不會關閉
        """

        # **不呼叫 `super().__init__()`**：本 loader 收的是連線或多個 DAO，
        # 與基底「單一 DAO」的建構骨架不同形，連線與建表一律自理
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None
        self.chip_dir: Path = FUTURES_CHIP_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()
        self.chip_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.conn is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
            self.owns_conn = True

    def disconnect(self) -> None:
        """Disconnect the Database；共用連線由建立者關閉"""

        if not self.owns_conn:
            return

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_dao(self, table: str) -> FuturesChipDAO:
        """取得指定籌碼表的 DAO（共用本 loader 的連線；表名不在白名單時 `ValueError`）"""

        return FuturesChipDAO(table, conn=self.conn)

    def commit(self) -> None:
        """提交目前的交易（由 updater 決定時點）"""

        if self.conn is not None:
            self.conn.commit()

    def create_db(self, table: str, df: pd.DataFrame) -> None:
        """依 DataFrame 的欄位建表（若不存在）；主鍵寫死、欄位由資料推導"""

        self.get_dao(table).ensure_table(df)

    def create_missing_tables(self) -> None:
        """三張表的欄位要等第一批資料才知道，故建表延後到 `add_to_db()`"""

        pass

    def add_to_db(self, table: str, df: pd.DataFrame) -> int:
        """
        - Description:
            寫入單一資料集；**不 commit**

            **用 `INSERT OR IGNORE`**：籌碼是既成事實，同一天重跑不該產生第二份，
            也不該覆蓋——與行情表同一種語意。寫入包在 savepoint 內，失敗時整批回滾。
        - Parameters:
            - table: str
                目標表名
            - df: pd.DataFrame
                清洗後的資料
        - Return:
            - int
                實際新增的列數
        """

        if df is None or df.empty:
            return 0

        self.connect()
        dao: FuturesChipDAO = self.get_dao(table)
        dao.ensure_table(df)

        inserted: int
        with dao.savepoint("futures_chip"):
            inserted = dao.insert_new_rows(df)

        logger.info(f"[Futures Chip] {table}：新增 {inserted} 列（共 {len(df)} 列）")
        return inserted

    def count_rows(self, table: str) -> int:
        """表內列數；表還不存在時為 0，其他查詢錯誤往外拋"""

        return self.get_dao(table).count_rows()

    def get_latest_date(self, table: str) -> Optional[str]:
        """表內最新的資料日期；供 updater 續跑（表不存在時為 None，其他錯誤往外拋）"""

        self.connect()
        return self.get_dao(table).get_latest_date()

    def get_earliest_date(self, table: str) -> Optional[str]:
        """表內最早的資料日期（表不存在時為 None）"""

        self.connect()
        return self.get_dao(table).get_earliest_date()

    def get_dates(
        self, table: str, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """表內在區間內有資料的日期（已排序、去重）"""

        self.connect()
        return self.get_dao(table).get_distinct_dates(start_date, end_date)

    def save_csv(self, df: pd.DataFrame, file_name: str) -> Optional[Path]:
        """留一份中繼檔供稽核（與其他 ETL 一致）"""

        if df is None or df.empty:
            return None

        path: Path = self.chip_dir / file_name
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
