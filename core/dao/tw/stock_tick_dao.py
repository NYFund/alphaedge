import datetime
import io
import re
from types import ModuleType
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config.schema import STOCK_TICK_LOAD_LOG_TABLE_NAME, STOCK_TICK_TABLE_NAME
from core.dao.timescale import connect_tick_db, get_connectorx_uri

if TYPE_CHECKING:
    import psycopg

"""
台股 tick（TimescaleDB）的資料存取：建表、壓縮與 policy 控制

`stock_tick` 是以 `time` 切 chunk 的 hypertable，`stock_tick_load_log` 以「股票 × 交易日」
記錄每次寫入，取代以 CSV 掃出來的 `tick_metadata.json`。

**不繼承 `BaseDAO`**：`BaseDAO` 綁的是 `sqlite3.Connection`，這裡是 psycopg 連線；
連線所有權沿用同一個 `owns_conn` 慣例（傳入連線就不負責關閉）。

**所有表名都帶 schema**：正式資料在 `public`，整合測試在各自的暫存 schema 裡建表，
彼此不會碰到。只靠 `search_path` 切換的話，讀取端的 ConnectorX 拿不到同一個設定。
"""


def _psycopg_sql() -> ModuleType:
    """
    取得 `psycopg.sql`（組 identifier 用）

    psycopg 屬 `[tick]` 選用相依，模組層級 import 會讓只跑日線的機器在
    import loader／updater 時就失敗，所以延到真的要組 SQL 時才 import。
    """

    from psycopg import sql

    return sql


class StockTickDAO:
    """
    - Description:
        `stock_tick` hypertable 與 `stock_tick_load_log` 的建表、壓縮與 policy 控制
    """

    TABLE_NAME: str = STOCK_TICK_TABLE_NAME
    LOAD_LOG_TABLE_NAME: str = STOCK_TICK_LOAD_LOG_TABLE_NAME

    # 7 天一個 chunk：2020-04～2024-05 約 220 個。1 天一個會多出約 1,500 個 chunk（含非交易日），
    # planning 成本偏高；單日查詢靠壓縮 batch 的 min/max `time` 跳過無關資料，不需要 chunk 切在一天
    CHUNK_INTERVAL: datetime.timedelta = datetime.timedelta(days=7)

    # 壓縮 policy 只壓 14 天前的 chunk：每日更新寫入的最近兩個 chunk 保持未壓縮，
    # 重跑最近幾天時不用先解壓
    COMPRESS_AFTER: datetime.timedelta = datetime.timedelta(days=14)

    # 讀取介面回傳的欄位、順序與 dtype；`seq` 只用來排序，不回傳
    READ_DTYPES: Dict[str, str] = {
        "stock_id": "object",
        "time": "datetime64[ns]",
        "close": "float64",
        "volume": "int64",
        "bid_price": "float64",
        "bid_volume": "int64",
        "ask_price": "float64",
        "ask_volume": "int64",
        "tick_type": "int64",
    }

    # 寫入的欄位與順序（含 `seq`）
    WRITE_COLUMNS: Tuple[str, ...] = (
        "stock_id",
        "time",
        "seq",
        "close",
        "volume",
        "bid_price",
        "bid_volume",
        "ask_price",
        "ask_volume",
        "tick_type",
    )

    def __init__(
        self,
        conn: Optional["psycopg.Connection"] = None,
        schema: str = "public",
    ) -> None:
        """
        - Description:
            建立 DAO
        - Parameters:
            - conn: Optional[psycopg.Connection]
                共用連線；指定時本 DAO 不擁有它。必須是 autocommit 關閉的連線：
                寫入以交易包住「刪除後重寫」，autocommit 會讓中途失敗留下半天的資料
            - schema: str
                資料表所在的 schema；正式資料用 `public`，整合測試用暫存 schema
        """

        # ConnectorX 不支援參數佔位符，schema 名稱會直接嵌進 SQL，只接受一般識別字
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError(f"schema 名稱不合法：{schema!r}")

        self.owns_conn: bool = conn is None
        self.conn: psycopg.Connection = conn if conn is not None else connect_tick_db()
        self.schema: str = schema

    def close(self) -> None:
        """關閉自己開的連線；共用連線由建立者負責"""

        if self.owns_conn and not self.conn.closed:
            self.conn.close()

    # === 建表 ===
    def create_tables(self) -> None:
        """
        - Description:
            建立 extension、`stock_tick` hypertable、索引、壓縮設定與 policy、`load_log`；
            全部可重跑，已存在的不會報錯
        """

        sql: ModuleType = _psycopg_sql()
        tick: psycopg.sql.Composed = self._identifier(self.TABLE_NAME)
        load_log: psycopg.sql.Composed = self._identifier(self.LOAD_LOG_TABLE_NAME)

        with self.conn.transaction():
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            self.conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema)
                )
            )
            # `(stock_id, time)` 不唯一：同一瞬間會撮合出多筆成交，所以加 `seq` 記原始列序，
            # 排序才可重現；不設主鍵，冪等由「整天刪除後重寫」保證
            self.conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        stock_id    TEXT             NOT NULL,
                        time        TIMESTAMP        NOT NULL,
                        seq         INTEGER          NOT NULL,
                        close       DOUBLE PRECISION NOT NULL,
                        volume      INTEGER          NOT NULL,
                        bid_price   DOUBLE PRECISION NOT NULL,
                        bid_volume  INTEGER          NOT NULL,
                        ask_price   DOUBLE PRECISION NOT NULL,
                        ask_volume  INTEGER          NOT NULL,
                        tick_type   SMALLINT         NOT NULL
                            CHECK (tick_type IN (0, 1, 2))
                    )
                    """
                ).format(tick)
            )
            self.conn.execute(
                "SELECT create_hypertable(%s, by_range('time', %s), if_not_exists => TRUE)",
                (self._regclass(self.TABLE_NAME), self.CHUNK_INTERVAL),
            )
            # 未壓縮 chunk（最近寫入的資料）按股票查詢用；壓縮後改由 segmentby 定位
            self.conn.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (stock_id, time)").format(
                    sql.Identifier(f"{self.TABLE_NAME}_stock_id_time_idx"), tick
                )
            )
            # 依股票分 segment、segment 內依時間排序：按股票查詢可以直接定位，
            # 價格與時間的 delta 編碼壓縮率也最高
            self.conn.execute(
                sql.SQL(
                    """
                    ALTER TABLE {} SET (
                        timescaledb.compress,
                        timescaledb.compress_segmentby = 'stock_id',
                        timescaledb.compress_orderby   = 'time, seq'
                    )
                    """
                ).format(tick)
            )
            self.conn.execute(
                "SELECT add_compression_policy(%s, %s, if_not_exists => TRUE)",
                (self._regclass(self.TABLE_NAME), self.COMPRESS_AFTER),
            )
            # `source_rows` 是來源 CSV 當天的原始列數，`row_count` 是實際寫入的列數；
            # 兩者的差就是依規則排除的列數，只記一個的話無法和 CSV 對帳
            self.conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        stock_id    TEXT        NOT NULL,
                        trade_date  DATE        NOT NULL,
                        source_rows INTEGER     NOT NULL,
                        row_count   INTEGER     NOT NULL,
                        source_file TEXT        NOT NULL,
                        loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (stock_id, trade_date)
                    )
                    """
                ).format(load_log)
            )
        logger.info(f"TimescaleDB 資料表已就緒：{self.schema}.{self.TABLE_NAME}")

    def table_exists(self) -> bool:
        """`stock_tick` 是否存在"""

        # 讀取也包在交易裡：共用連線上若已有外層交易，這裡只會是 savepoint，
        # 不會替呼叫端 commit 掉還沒完成的寫入
        with self.conn.transaction():
            row: Optional[Tuple[Any, ...]] = self.conn.execute(
                "SELECT to_regclass(%s)", (self._regclass(self.TABLE_NAME),)
            ).fetchone()
        return row is not None and row[0] is not None

    # === 寫入 ===
    def replace_day(
        self,
        stock_id: str,
        trade_date: datetime.date,
        day_df: pd.DataFrame,
        source_rows: int,
        source_file: str,
    ) -> int:
        """
        - Description:
            以「刪除後重寫」寫入一檔股票一個交易日的 tick，並登記 `load_log`；三件事同一個交易

            **冪等靠整天重寫而不是唯一鍵**：同一瞬間的多筆成交（連所有欄位都相同）是真實資料，
            沒有自然唯一鍵可以 `ON CONFLICT`；而來源 CSV 是整段覆寫產生的，同一天再出現時
            應該以新檔為準。整天被規則排除的日子也要登記（`row_count = 0`），續跑時才不會重做。
        - Parameters:
            - stock_id: str
                股票代號
            - trade_date: datetime.date
                交易日
            - day_df: pd.DataFrame
                已正規化、已排除的當日資料，欄位需含 `WRITE_COLUMNS`；可以是空表
            - source_rows: int
                來源 CSV 當天排除前的列數
            - source_file: str
                來源檔名，出問題時回溯用
        - Return:
            - int
                寫入的列數
        """

        sql: ModuleType = _psycopg_sql()
        day_start: datetime.datetime = datetime.datetime.combine(
            trade_date, datetime.time()
        )
        day_end: datetime.datetime = day_start + datetime.timedelta(days=1)

        with self.conn.transaction():
            self.conn.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE stock_id = %s AND time >= %s AND time < %s"
                ).format(self._identifier(self.TABLE_NAME)),
                (stock_id, day_start, day_end),
            )
            if not day_df.empty:
                self._copy_rows(day_df)
            self.conn.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (stock_id, trade_date, source_rows, row_count, source_file)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                        source_rows = EXCLUDED.source_rows,
                        row_count   = EXCLUDED.row_count,
                        source_file = EXCLUDED.source_file,
                        loaded_at   = now()
                    """
                ).format(self._identifier(self.LOAD_LOG_TABLE_NAME)),
                (stock_id, trade_date, source_rows, len(day_df), source_file),
            )
        return len(day_df)

    # === load_log 查詢 ===
    def get_loaded_last_dates(self) -> Dict[str, datetime.date]:
        """
        - Description:
            每檔股票在 `load_log` 登記過的最後一個交易日；updater 據此跳過已載入的日期

            以資料庫實際寫入的紀錄為準：原本的 `tick_metadata.json` 是從 CSV 掃出來的，
            入庫失敗時也會前進，下次就會跳過還沒進資料庫的日子。
        - Return:
            - Dict[str, datetime.date]
                股票代號 → 最後交易日；從未載入過的股票不在其中
        """

        sql: ModuleType = _psycopg_sql()
        with self.conn.transaction():
            rows: List[Tuple[Any, ...]] = self.conn.execute(
                sql.SQL(
                    "SELECT stock_id, max(trade_date) FROM {} GROUP BY stock_id"
                ).format(self._identifier(self.LOAD_LOG_TABLE_NAME))
            ).fetchall()
        return {stock_id: last_date for stock_id, last_date in rows}

    def get_latest_trade_date(self) -> Optional[datetime.date]:
        """`load_log` 裡最新的交易日；還沒有任何紀錄時為 None"""

        sql: ModuleType = _psycopg_sql()
        with self.conn.transaction():
            row: Optional[Tuple[Any, ...]] = self.conn.execute(
                sql.SQL("SELECT max(trade_date) FROM {}").format(
                    self._identifier(self.LOAD_LOG_TABLE_NAME)
                )
            ).fetchone()
        return row[0] if row else None

    def get_source_rows(self, stock_id: str) -> Dict[datetime.date, int]:
        """
        - Description:
            某檔股票每個已登記交易日的來源列數；判斷 CSV 是否已完整入庫用
        - Parameters:
            - stock_id: str
                股票代號
        - Return:
            - Dict[datetime.date, int]
                交易日 → `source_rows`
        """

        sql: ModuleType = _psycopg_sql()
        with self.conn.transaction():
            rows: List[Tuple[Any, ...]] = self.conn.execute(
                sql.SQL(
                    "SELECT trade_date, source_rows FROM {} WHERE stock_id = %s"
                ).format(self._identifier(self.LOAD_LOG_TABLE_NAME)),
                (stock_id,),
            ).fetchall()
        return {trade_date: source_rows for trade_date, source_rows in rows}

    # === 讀取 ===
    def query_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        order_by: Tuple[str, ...],
        stock_id: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        - Description:
            以 ConnectorX 讀取區間內的 tick，直接組成 DataFrame

            **不用 `pd.read_sql`**：它逐列轉成 Python 物件，一天全市場百萬列時慢上數十倍。
            **ConnectorX 不支援參數佔位符**，值只能嵌進 SQL：日期由 `datetime.date` 格式化、
            `stock_id` 先驗證只含英數字、排序欄位只接受讀取欄位與 `seq`，三者都不是任意字串。
            只選讀取欄位、不用 `SELECT *`：壓縮 chunk 依欄位解壓，多選一欄就多解一欄。
        - Parameters:
            - start_date: datetime.date
                起日（含）
            - end_date: datetime.date
                迄日（含）；以「隔天 00:00 之前」的半開區間查詢
            - order_by: Tuple[str, ...]
                排序欄位
            - stock_id: Optional[str]
                只取某檔股票；None 取全市場
        - Return:
            - pd.DataFrame
                `READ_DTYPES` 的欄位與 dtype；沒有資料時是同欄位的空表
        - Raise:
            - ValueError
                `stock_id` 或排序欄位不合法
        """

        allowed_order: Set[str] = set(self.READ_DTYPES) | {"seq"}
        if not order_by or any(column not in allowed_order for column in order_by):
            raise ValueError(f"排序欄位不合法：{order_by}")
        if stock_id is not None and not stock_id.isalnum():
            raise ValueError(f"stock_id 不合法：{stock_id!r}")

        conditions: List[str] = [
            f"time >= '{start_date.isoformat()}'",
            f"time < '{(end_date + datetime.timedelta(days=1)).isoformat()}'",
        ]
        if stock_id is not None:
            conditions.append(f"stock_id = '{stock_id}'")
        query: str = (
            f"SELECT {', '.join(self.READ_DTYPES)} "
            f"FROM {self._regclass(self.TABLE_NAME)} "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY {', '.join(order_by)}"
        )

        # 先取 URI：沒裝 connectorx 時由它拋出附安裝方式的錯誤
        uri: str = get_connectorx_uri()
        import connectorx as cx

        # 走 Arrow 再轉 pandas：ConnectorX 直接產生 pandas 的路徑用的是 pandas 已棄用的
        # 內部 API（`make_block`），pandas 拿掉它那天這條讀取路徑就壞
        ticks: pd.DataFrame = cx.read_sql(uri, query, return_type="arrow").to_pandas()
        # 強制轉 dtype：空表與非空表的欄位型別一致，`SMALLINT`／`INTEGER` 也統一成 int64
        if ticks.empty:
            return pd.DataFrame(
                {
                    column: pd.Series(dtype=dtype)
                    for column, dtype in self.READ_DTYPES.items()
                }
            )
        return ticks.astype(self.READ_DTYPES)

    # === 壓縮 ===
    def pause_compression_policy(self) -> None:
        """
        - Description:
            停用 `stock_tick` 的壓縮 policy

            policy 以「現在」往回算，2020～2024 的歷史 chunk 全部符合條件：
            匯入歷史或做壓縮前後的量測時不停掉它，背景 job 會把還在寫入的 chunk 壓掉，
            之後的寫入都要走壓縮 chunk 的 DML（慢很多）。用完一定要 `resume_compression_policy()`。
        """

        self._set_compression_policy_scheduled(False)

    def resume_compression_policy(self) -> None:
        """恢復 `stock_tick` 的壓縮 policy"""

        self._set_compression_policy_scheduled(True)

    def is_compression_policy_scheduled(self) -> bool:
        """壓縮 policy 目前是否啟用"""

        job_id: int = self._compression_job_id()
        with self.conn.transaction():
            scheduled: bool = self.conn.execute(
                "SELECT scheduled FROM timescaledb_information.jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()[0]
        return scheduled

    def compress_chunks_before(self, older_than: datetime.date) -> int:
        """
        - Description:
            壓縮所有早於 `older_than` 的 chunk（已壓縮的略過）；歷史匯入逐週呼叫
        - Parameters:
            - older_than: datetime.date
                chunk 的時間範圍全部早於這一天 00:00 才壓縮
        - Return:
            - int
                本次處理的 chunk 數（含原本就已壓縮、被略過的）
        """

        with self.conn.transaction():
            rows: List[Tuple[Any, ...]] = self.conn.execute(
                "SELECT compress_chunk(c, if_not_compressed => TRUE) "
                "FROM show_chunks(%s, older_than => %s) c",
                (
                    self._regclass(self.TABLE_NAME),
                    datetime.datetime.combine(older_than, datetime.time()),
                ),
            ).fetchall()
        return len(rows)

    # === 內部 ===
    def _copy_rows(self, day_df: pd.DataFrame) -> None:
        """
        以 `COPY ... FROM STDIN (FORMAT csv)` 整塊寫入（呼叫端負責交易）

        **不逐列 `write_row()`**：歷史匯入是十億列級，逐列由 Python 送出的往返成本會主導總耗時；
        先在 pandas 組成 CSV 文字再一次送出，轉換在 C 層完成。
        時間固定輸出到 microsecond，價格已在正規化時四捨五入到 2 位小數。
        """

        sql: ModuleType = _psycopg_sql()
        buffer: io.StringIO = io.StringIO()
        day_df.loc[:, list(self.WRITE_COLUMNS)].to_csv(
            buffer,
            index=False,
            header=False,
            date_format="%Y-%m-%d %H:%M:%S.%f",
        )
        copy_sql: psycopg.sql.Composed = sql.SQL(
            "COPY {} ({}) FROM STDIN (FORMAT csv)"
        ).format(
            self._identifier(self.TABLE_NAME),
            sql.SQL(", ").join(sql.Identifier(c) for c in self.WRITE_COLUMNS),
        )
        with self.conn.cursor() as cursor:
            with cursor.copy(copy_sql) as copy:
                copy.write(buffer.getvalue())

    def _identifier(self, table_name: str) -> "psycopg.sql.Composed":
        """帶 schema 的表名（SQL identifier）"""

        sql: ModuleType = _psycopg_sql()
        return sql.Identifier(self.schema, table_name)

    def _regclass(self, table_name: str) -> str:
        """
        帶 schema 的表名字串，給 TimescaleDB 函式的 `regclass` 參數用

        以 `quote_ident` 的規則加雙引號，schema 名稱含大寫或特殊字元時才不會被折成小寫。
        """

        return f'"{self.schema}"."{table_name}"'

    def _compression_job_id(self) -> int:
        """`stock_tick` 壓縮 policy 的 job id；policy 不存在時拋出"""

        with self.conn.transaction():
            row: Optional[Tuple[Any, ...]] = self.conn.execute(
                """
                SELECT job_id FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_compression'
                  AND hypertable_schema = %s AND hypertable_name = %s
                """,
                (self.schema, self.TABLE_NAME),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                f"{self.schema}.{self.TABLE_NAME} 沒有壓縮 policy，請先執行 create_tables()"
            )
        return row[0]

    def _set_compression_policy_scheduled(self, scheduled: bool) -> None:
        """啟用或停用壓縮 policy"""

        job_id: int = self._compression_job_id()
        with self.conn.transaction():
            self.conn.execute(
                "SELECT alter_job(%s, scheduled => %s)", (job_id, scheduled)
            )
        logger.info(
            f"{self.schema}.{self.TABLE_NAME} 壓縮 policy："
            f"{'啟用' if scheduled else '停用'}（job {job_id}）"
        )
