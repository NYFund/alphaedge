import datetime
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple, Union

import pandas as pd

from core.dao.connection import connect_sqlite

"""
DAO 共用底座：連線所有權、查詢、寫入與交易控制

一張資料表（或一組緊密相關的表）一個 DAO，SQL 只寫在 DAO 裡：

| 層 | 負責 | 不負責 |
|----|------|--------|
| DAO | 建表與索引、寫入、查詢、交易（savepoint／commit） | 業務規則、日誌檔設定 |
| API | 公開查詢介面、業務規則（還原係數、保證金公式） | SQL、連線細節 |
| loader／updater | 讀 CSV、決定日期、逐檔彙報、持有並關閉 DAO | SQL |

模組層級的函式（`table_exists`、`insert_or_ignore` 等）是 `BaseDAO` 方法的實作本體，
也給一次操作多張表、不值得各建一個 DAO 的呼叫端直接使用。
"""


def to_sql_params(*values: Any) -> Tuple[Any, ...]:
    """
    - Description:
        把查詢參數轉成 SQLite 收得下的型別；`date`／`datetime` 轉 ISO 字串

        **不轉的話是靠 Python 3.12 已 deprecated 的預設 date adapter**：
        那個 adapter 隨時可能被移除，屆時每一支查詢都會在同一天壞掉，
        而且錯誤訊息只會說「型別不支援」。

        資料表的 `date` 欄一律是 `TEXT`（`YYYY-MM-DD`），ISO 字串本來就是
        正確的比較對象；`datetime` 只取日期部分，與欄位格式對齊。
    - Parameters:
        - values: Any
            查詢參數；非日期型別原樣通過
    - Return:
        - Tuple[Any, ...]
            可直接傳給 `params=` 的 tuple
    """

    converted: List[Any] = []
    for value in values:
        if isinstance(value, datetime.datetime):
            converted.append(value.date().isoformat())
        elif isinstance(value, datetime.date):
            converted.append(value.isoformat())
        else:
            converted.append(value)
    return tuple(converted)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """
    - Description:
        檢查資料表是否存在

        **「表還沒建」與「查詢出錯」必須分得開**：舊版期貨 API 用
        `except sqlite3.OperationalError: return None` 收掉整類錯誤，於是「尚未跑過 ETL」
        （正常）與「資料庫被鎖住、schema 壞掉、欄名打錯」（不正常）長得一模一樣，
        回測期間只會讓策略拿到 `None`、少開幾筆倉而沒有任何錯誤。
        呼叫端應先用本函式判斷表在不在，其餘錯誤一律往外拋。
    - Parameters:
        - conn: sqlite3.Connection
            資料庫連線
        - table_name: str
            資料表名稱
    - Return:
        - bool
            資料表存在為 True
    """

    query: str = "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?"
    result: Tuple[int] = conn.execute(query, (table_name,)).fetchone()
    return result[0] == 1


def insert_or_ignore(
    conn: sqlite3.Connection, table_name: str, df: pd.DataFrame
) -> Tuple[int, int]:
    """
    - Description:
        以 `INSERT OR IGNORE` 寫入，回傳實際寫入與被跳過的列數；**不 commit**

        **為什麼不用 `df.to_sql(if_exists="append")`**：loader 每次都掃整個
        downloads 目錄，已入庫的檔案會再送一次；`append` 會因主鍵衝突整批拋錯，
        使「重跑」與「真的出錯」無法區分。`INSERT OR IGNORE` 讓重複列靜靜跳過，
        真正的錯誤（欄位不符、檔案損毀）才會拋出。

        回傳「跳過幾列」而不是丟掉這個資訊，是為了讓呼叫端能分辨：
        全部跳過（重跑，正常）、部分跳過、全部寫入（新資料）。
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

    if df.empty:
        return 0, 0

    columns: List[str] = list(df.columns)
    quoted: str = ",".join(f'"{col}"' for col in columns)
    placeholders: str = ",".join("?" * len(columns))

    cursor: sqlite3.Cursor = conn.executemany(
        f"INSERT OR IGNORE INTO {table_name} ({quoted}) VALUES ({placeholders})",
        df.itertuples(index=False, name=None),
    )
    inserted: int = cursor.rowcount
    return inserted, len(df) - inserted


def insert_or_replace(
    conn: sqlite3.Connection, table_name: str, df: pd.DataFrame
) -> int:
    """
    - Description:
        以 `INSERT OR REPLACE` 寫入，回傳送出的列數；**不 commit**

        給「同鍵的新值應該蓋掉舊值」的表用（除權息、公司行動）：來源是區間查詢，
        每次回補都會把同一段再取一次，站方更正過的值要進得來；
        `INSERT OR IGNORE` 會把更正擋在門外。
    - Parameters:
        - conn: sqlite3.Connection
            目標資料庫連線
        - table_name: str
            目標資料表
        - df: pd.DataFrame
            欄位需與資料表一致；應先在呼叫端去重
    - Return:
        - int
            送出的列數（含覆蓋既有列）
    """

    if df.empty:
        return 0

    columns: List[str] = list(df.columns)
    quoted: str = ",".join(f'"{col}"' for col in columns)
    placeholders: str = ",".join("?" * len(columns))

    conn.executemany(
        f"INSERT OR REPLACE INTO {table_name} ({quoted}) VALUES ({placeholders})",
        df.itertuples(index=False, name=None),
    )
    return len(df)


def create_symbol_date_index(conn: sqlite3.Connection, table_name: str) -> None:
    """
    - Description:
        建立 `(stock_id, date)` 索引並 commit

        日更表的主鍵都是 `(date, stock_id, ...)`，**date 在前**，所以
        「某一天的全市場」很快，「某一檔的整段歷史」卻要掃過整個 date 範圍。
        而策略研究問的幾乎都是後者。`IF NOT EXISTS` 讓既有資料庫在下次建表檢查時
        自動補上，不需要另外寫遷移腳本。
    - Parameters:
        - conn: sqlite3.Connection
            資料庫連線
        - table_name: str
            目標資料表
    """

    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table_name}_stock_id_date "
        f"ON {table_name} (stock_id, date)"
    )
    conn.commit()


class BaseDAO:
    """
    - Description:
        單一資料表的資料存取物件

        **連線所有權沿用 `owns_conn` 慣例**：建構時傳入 `conn` 就不擁有，`close()`
        不會關它（由建立者負責，例如 DataFeed 或 updater）；沒傳才自己開、自己關。
        與 `BaseDataAPI` 的約定一致，同一條連線可以安全地分給多個 DAO／API。

        子類別必須宣告 `TABLE_NAME` 與 `DEFAULT_DB_PATH`。
    """

    TABLE_NAME: str = ""
    DEFAULT_DB_PATH: Optional[Path] = None

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        db_path: Optional[Union[str, Path]] = None,
        read_only: bool = False,
    ) -> None:
        """
        - Description:
            建立 DAO
        - Parameters:
            - conn: Optional[sqlite3.Connection]
                共用連線；指定時本 DAO 不擁有它
            - db_path: Optional[Union[str, Path]]
                自行開連線時的資料庫路徑；None 取 `DEFAULT_DB_PATH`。
                保留這個參數是為了讓呼叫端在**呼叫當下**決定路徑——既有測試以
                monkeypatch 改寫 loader 模組的路徑常數，寫死在 DAO 裡就改不到
            - read_only: bool
                自行開連線時以唯讀模式開啟（跨庫只讀的場合，例如期貨 ETL 讀台股交易日曆）；
                指定 `conn` 時不使用
        """

        self.owns_conn: bool = conn is None
        self.conn: Optional[sqlite3.Connection] = conn

        if self.owns_conn:
            path: Optional[Union[str, Path]] = db_path or self.DEFAULT_DB_PATH
            if path is None:
                raise ValueError(
                    f"{type(self).__name__} 未指定 conn，也沒有 DEFAULT_DB_PATH 可用"
                )
            self.conn = connect_sqlite(path, read_only=read_only)

    # === 查詢 ===
    def table_exists(self) -> bool:
        """本 DAO 的資料表是否存在"""

        return table_exists(self.conn, self.TABLE_NAME)

    def count_rows(self) -> int:
        """本表的列數；表不存在時為 0（其他查詢錯誤往外拋）"""

        if not self.table_exists():
            return 0
        count: int = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.TABLE_NAME}"
        ).fetchone()[0]
        return count

    def query_df(self, sql: str, params: Tuple[Any, ...] = ()) -> pd.DataFrame:
        """
        - Description:
            執行查詢並回傳 DataFrame；日期參數自動轉 ISO 字串

            **不用 `pd.read_sql_query`**：它在查詢失敗時會對整條連線 `rollback()`，
            共用連線上尚未 commit 的寫入會跟著消失（例如批次寫入中途重建 metadata
            的查詢失敗）。改以 cursor 執行，失敗時只拋 `sqlite3.Error`、交易維持原狀。

            組表方式與 `read_sql_query` 相同（`from_records` + `coerce_float`），
            欄位型別推斷一致；零列時仍帶欄名。
        - Parameters:
            - sql: str
                查詢語句，值一律用 `?` 佔位
            - params: Tuple[Any, ...]
                查詢參數
        - Return:
            - pd.DataFrame
                查詢結果
        """

        cursor: sqlite3.Cursor = self.conn.execute(sql, to_sql_params(*params))
        try:
            columns: List[str] = [column[0] for column in cursor.description]
            return pd.DataFrame.from_records(
                cursor.fetchall(), columns=columns, coerce_float=True
            )
        finally:
            cursor.close()

    def fetch_one(
        self, sql: str, params: Tuple[Any, ...] = ()
    ) -> Optional[Tuple[Any, ...]]:
        """執行查詢並回傳第一列；無結果時為 None"""

        return self.conn.execute(sql, to_sql_params(*params)).fetchone()

    def get_distinct_dates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        - Description:
            取得資料表在區間內有資料的日期（已排序、去重）

            **表不存在時會拋出**，與讀取端既有行為一致；需要容忍「尚未建表」的
            呼叫端（例如 ETL 的日期規劃）應先呼叫 `table_exists()`。
        - Parameters:
            - start_date: datetime.date
                起始日（含）
            - end_date: datetime.date
                結束日（含）
        - Return:
            - List[datetime.date]
                區間內的日期；無資料或 `start_date > end_date` 時回傳空 list
        """

        if start_date > end_date:
            return []

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT date FROM {self.TABLE_NAME}
            WHERE date BETWEEN ? AND ?
            ORDER BY date
            """,
            (start_date, end_date),
        )

        if df.empty:
            return []
        return pd.to_datetime(df["date"]).dt.date.tolist()

    def _get_latest_value(self, col_name: str) -> Optional[Any]:
        """
        - Description:
            取得欄位的最大值；表不存在或表為空時回 None

            **不吞 `sqlite3.Error`**：吞掉會讓 updater 把「欄位打錯、DB 損毀、被鎖住」
            當成「表是空的」，從預設起日靜默重跑整段回補。

            刻意設為受保護方法：欄名會直接組進 SQL，只能由子類別以自己宣告的
            欄名呼叫，不接受外部字串。
        - Parameters:
            - col_name: str
                欄位名稱
        - Return:
            - Optional[Any]
                最大值；表不存在或無資料時為 None
        """

        if not self.table_exists():
            return None

        # **欄名刻意不加雙引號**：SQLite 遇到雙引號包住、卻不存在的欄名時，
        # 會退回把它當成字串字面值，欄名打錯就變成「查得到、值是欄名本身」而不報錯
        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT {col_name} FROM {self.TABLE_NAME} ORDER BY {col_name} DESC LIMIT 1"
        )
        if row is None or row[0] is None:
            return None
        return row[0]

    # === 寫入與交易 ===
    def insert_or_ignore(self, df: pd.DataFrame) -> Tuple[int, int]:
        """以 `INSERT OR IGNORE` 寫入本表，回傳（寫入列數, 跳過列數）；不 commit"""

        return insert_or_ignore(self.conn, self.TABLE_NAME, df)

    def insert_or_replace(self, df: pd.DataFrame) -> int:
        """以 `INSERT OR REPLACE` 寫入本表，回傳送出的列數；不 commit"""

        return insert_or_replace(self.conn, self.TABLE_NAME, df)

    @contextmanager
    def savepoint(self, name: str = "dao_write") -> Iterator[None]:
        """
        - Description:
            以 savepoint 包住一段寫入：區塊拋出例外時只回滾這一段，並把例外往外拋

            **用途是「單檔失敗整檔不留」**：loader 逐檔寫入、最後才 commit，
            舊寫法下某個檔案寫到一半出錯，前面已寫入的列仍留在交易裡，
            被最後那次 `commit()` 一起寫進資料庫——資料表裡多了半份檔案，而
            `finish_load()` 回報的卻是「這個檔案失敗」。

            先確保交易已開始：Python `sqlite3` 不會為 `SAVEPOINT` 自動開交易，
            在交易外建立的 savepoint 一經 `RELEASE` 就等於 commit，
            那樣每個檔案都會各自 commit 一次，失去「最後一次 commit」的語意。
        - Parameters:
            - name: str
                savepoint 名稱；巢狀使用時請取不同名稱
        """

        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
        self.conn.execute(f"SAVEPOINT {name}")

        try:
            yield
        except BaseException:
            # 少數錯誤（例如磁碟已滿）會讓 SQLite 自行回滾整個交易，
            # 此時 savepoint 已不存在，再 ROLLBACK TO 只會把原本的例外蓋掉
            if self.conn.in_transaction:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")

    def commit(self) -> None:
        """提交目前的交易"""

        self.conn.commit()

    # === 生命週期 ===
    def close(self) -> None:
        """關閉連線；共用連線（`owns_conn` 為 False）不關，由建立者負責"""

        if not self.owns_conn:
            return

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "BaseDAO":
        """支援 with 語法，離開區塊即關閉自己擁有的連線"""

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """離開 with 區塊時關閉連線"""

        self.close()
