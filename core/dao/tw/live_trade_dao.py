import contextlib
import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from core.config import (
    LIVE_ACCOUNT_SNAPSHOT_TABLE_NAME,
    LIVE_FILL_TABLE_NAME,
    LIVE_ORDER_EVENT_TABLE_NAME,
    LIVE_ORDER_TABLE_NAME,
    LIVE_PARITY_DIFF_TABLE_NAME,
    LIVE_PENDING_ACTION_TABLE_NAME,
    LIVE_POSITION_LOT_TABLE_NAME,
    LIVE_POSITION_SNAPSHOT_TABLE_NAME,
    LIVE_RISK_EVENT_TABLE_NAME,
    LIVE_RUN_TABLE_NAME,
    LIVE_STRATEGY_MODE_TABLE_NAME,
    TW_TRADING_DB_PATH,
)
from core.dao.base import BaseDAO, to_sql_params
from core.dao.connection import connect_live_trading

"""
實盤交易紀錄庫（`tw_trading.db`）的資料存取

**獨立一庫，不併進 `tw_stock.db`**：研究資料重跑爬蟲就回來，交易紀錄不能重建。
分庫之後，任何一次「砍掉重建研究庫」都不可能誤傷委託與成交。

本 DAO 管 11 張表，全部是同一件事的不同切面（一次交易從委託到對帳），拆成 11 個 DAO
只會讓每個呼叫端都要開 11 條連線。建表一律冪等（`IF NOT EXISTS`）。

**寫入的冪等性靠主鍵**，不靠呼叫端記得檢查：成交以 `(broker_seqno, broker_trade_id)`
為主鍵並走 `INSERT OR IGNORE`，同一筆重放幾次都只會有一列。實盤的回報會重複推送，
而重複記一筆成交等於帳上多了一個不存在的部位。

**時間欄位存完整時間（含時區），日期欄位存日期**：研究庫共用的 `to_sql_params()`
會把 `datetime` 截成日期，那是日頻資料要的；但這裡同一天會有好幾段執行，
截成日期之後「哪一筆最後結束」「事件先後」全都分不出來。
依日期篩選一律用 `substr(欄位, 1, 10)` 而不是 SQLite 的 `date()`：後者會先把
帶時區的時間換算成 UTC，台北早上 08:00 以前的紀錄會被歸到前一天。
"""


def _to_live_params(*values: Any) -> Tuple[Any, ...]:
    """
    - Description:
        實盤紀錄庫的參數轉換：`datetime` 保留完整 ISO 時間，其餘沿用 `to_sql_params()`

        `date` 與 `datetime` 要分開判斷且先判 `datetime`（它是 `date` 的子類別）。
        呼叫端寫日期欄位（`open_date`、`due_date`、快照的 `date`）時傳的是 `date`，
        寫時間欄位時傳的是 `datetime`，型別本身就決定了格式。
    - Parameters:
        - values: Any
            查詢參數
    - Return:
        - Tuple[Any, ...]
            可直接傳給 `execute()` 的 tuple
    """

    return tuple(
        value.isoformat()
        if isinstance(value, datetime.datetime)
        else to_sql_params(value)[0]
        for value in values
    )


class LiveTradeDAO(BaseDAO):
    """
    - Description:
        實盤紀錄庫的資料存取

        `TABLE_NAME` 宣告為 `live_order`（底座的 `count_rows()` 等方法以它為對象），
        其餘資料表各有專屬方法。
    """

    TABLE_NAME: str = LIVE_ORDER_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_TRADING_DB_PATH

    # 交易模式的值域；與 `core/live/risk/trading_mode.py` 的狀態機一致
    # 結束原因；存活監控以「是否等於正常結束」判定要不要推播，兩邊共用這個常數
    END_REASON_NORMAL: str = "正常結束"
    END_REASON_CRASHED: str = "CRASHED"  # 非正常結束（上次沒有走到 `finish_run()`）
    MODE_NORMAL: str = "NORMAL"

    # 跨日待辦的狀態
    ACTION_PENDING: str = "PENDING"
    ACTION_DONE: str = "DONE"
    ACTION_ABANDONED: str = "ABANDONED"

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        db_path: Optional[Union[str, Path]] = None,
        read_only: bool = False,
    ) -> None:
        """
        - Description:
            建立 DAO

            自行開連線時走 `connect_live_trading()`（WAL ＋ `synchronous=FULL`），
            不是底座預設的 `connect_sqlite()`——「先寫 DB 再送單」的恢復保證
            建立在那一筆 commit 真的落地。
        - Parameters:
            - conn: Optional[sqlite3.Connection]
                共用連線；指定時本 DAO 不擁有它
            - db_path: Optional[Union[str, Path]]
                自行開連線時的路徑；None 取 `DEFAULT_DB_PATH`
            - read_only: bool
                唯讀開啟（報表與存活監控用）
        """

        if conn is not None:
            super().__init__(conn=conn)
            return

        path: Optional[Union[str, Path]] = db_path or self.DEFAULT_DB_PATH
        if path is None:
            raise ValueError("LiveTradeDAO 未指定 conn，也沒有 DEFAULT_DB_PATH 可用")

        super().__init__(conn=connect_live_trading(path, read_only=read_only))
        self.owns_conn = True

    # === 建表 ===
    def ensure_tables(self) -> None:
        """
        - Description:
            建立全部 11 張表與索引；可重複呼叫

            **欄位刻意不設 `NOT NULL` 以外的限制**（例如 CHECK）：實盤出事時
            最需要的是「把當下的狀態原樣記下來」，而不是因為一個欄位不合預期
            就寫不進去。值域的檢查留在程式層，那裡才有辦法一併寫 `live_risk_event`。
        """

        for statement in self._schema_statements():
            self.conn.execute(statement)
        self.conn.commit()

    @staticmethod
    def _schema_statements() -> Tuple[str, ...]:
        """全部建表與索引的 SQL；SQL 只寫在 DAO 裡"""

        return (
            # 每次啟動一筆。稽核欄位不是可有可無——實盤出事時第一個要回答的是
            # 「那天那張單是哪一版程式、哪一組參數送出的」
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_RUN_TABLE_NAME} (
                run_id              TEXT PRIMARY KEY,
                started_at          TEXT NOT NULL,
                ended_at            TEXT,
                phase               TEXT NOT NULL,
                simulation          INTEGER NOT NULL,
                dry_run             INTEGER NOT NULL DEFAULT 0,
                end_reason          TEXT,
                account_mode        TEXT NOT NULL DEFAULT 'NORMAL',
                notify_enabled      INTEGER NOT NULL DEFAULT 0,
                git_commit          TEXT,
                shioaji_version     TEXT,
                strategy_params_json TEXT,
                risk_config_json    TEXT
            )
            """,
            # 委託單。`strategy_name` 是多策略歸屬鏈的起點，不可為空
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_ORDER_TABLE_NAME} (
                client_order_id   TEXT PRIMARY KEY,
                run_id            TEXT NOT NULL,
                strategy_name     TEXT NOT NULL,
                symbol            TEXT NOT NULL,
                action            TEXT NOT NULL,
                position_type     TEXT NOT NULL,
                price             REAL NOT NULL DEFAULT 0,
                volume            INTEGER NOT NULL DEFAULT 0,
                price_type        TEXT,
                order_type        TEXT,
                order_lot         TEXT,
                order_cond        TEXT,
                timing            TEXT,
                status            TEXT NOT NULL,
                broker_order_id   TEXT,
                broker_seqno      TEXT,
                custom_field      TEXT,
                filled_volume     INTEGER NOT NULL DEFAULT 0,
                avg_fill_price    REAL NOT NULL DEFAULT 0,
                reject_reason     TEXT,
                dry_run           INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL,
                updated_at        TEXT
            )
            """,
            # `broker_seqno` 建唯一索引但**允許 NULL**：在 `place_order()` 回傳前
            # 崩潰的委託沒有 seqno，SQLite 的 UNIQUE 不把多個 NULL 視為重複，正合需要
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_live_order_seqno "
            f"ON {LIVE_ORDER_TABLE_NAME}(broker_seqno)",
            f"CREATE INDEX IF NOT EXISTS idx_live_order_strategy "
            f"ON {LIVE_ORDER_TABLE_NAME}(strategy_name, created_at)",
            f"CREATE INDEX IF NOT EXISTS idx_live_order_custom_field "
            f"ON {LIVE_ORDER_TABLE_NAME}(custom_field)",
            # 狀態轉移歷史（append-only）。有了它才回答得了「這張單什麼時候變成那樣的」
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_ORDER_EVENT_TABLE_NAME} (
                client_order_id TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                from_status     TEXT,
                to_status       TEXT NOT NULL,
                op_type         TEXT,
                op_code         TEXT,
                message         TEXT,
                occurred_at     TEXT NOT NULL,
                PRIMARY KEY (client_order_id, seq)
            )
            """,
            # 成交明細。手續費與稅**估算值與券商實際值分欄保存**：
            # 成交回報不帶費用，盤中只能用 cost model 估，盤後才回填得到實際值。
            # 併成一欄的話，回填之後就再也算不出「估得準不準」
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_FILL_TABLE_NAME} (
                broker_seqno      TEXT NOT NULL,
                broker_trade_id   TEXT NOT NULL,
                client_order_id   TEXT,
                strategy_name     TEXT,
                symbol            TEXT NOT NULL,
                action            TEXT NOT NULL,
                price             REAL NOT NULL,
                volume            INTEGER NOT NULL,
                estimated_fee     REAL,
                estimated_tax     REAL,
                actual_fee        REAL,
                actual_tax        REAL,
                filled_at         TEXT,
                raw_json          TEXT,
                PRIMARY KEY (broker_seqno, broker_trade_id)
            )
            """,
            f"CREATE INDEX IF NOT EXISTS idx_live_fill_order "
            f"ON {LIVE_FILL_TABLE_NAME}(client_order_id)",
            # 策略層部位歸屬帳。券商合併部位拆不回策略，只能本地自己記
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_POSITION_LOT_TABLE_NAME} (
                lot_id          TEXT PRIMARY KEY,
                strategy_name   TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                volume          INTEGER NOT NULL,
                open_date       TEXT NOT NULL,
                open_price      REAL NOT NULL,
                client_order_id TEXT,
                closed_at       TEXT
            )
            """,
            # 未平倉的 lot 才是歸屬與守門要查的；加上 `closed_at IS NULL` 的部分索引
            f"CREATE INDEX IF NOT EXISTS idx_live_lot_open "
            f"ON {LIVE_POSITION_LOT_TABLE_NAME}(symbol, strategy_name) "
            f"WHERE closed_at IS NULL",
            # `source` 為 local（單一策略的歸屬帳）／broker（券商）／account（帳戶層合計）
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_POSITION_SNAPSHOT_TABLE_NAME} (
                date           TEXT NOT NULL,
                strategy_name  TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                source         TEXT NOT NULL,
                direction      TEXT NOT NULL,
                volume         INTEGER NOT NULL,
                avg_price      REAL,
                order_cond     TEXT,
                unrealized_pnl REAL,
                PRIMARY KEY (date, strategy_name, symbol, source)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_ACCOUNT_SNAPSHOT_TABLE_NAME} (
                date              TEXT NOT NULL,
                strategy_name     TEXT NOT NULL,
                source            TEXT NOT NULL,
                available_balance REAL,
                total_equity      REAL,
                realized_pnl      REAL,
                unrealized_pnl    REAL,
                PRIMARY KEY (date, strategy_name, source)
            )
            """,
            # 風控事件。**`severity` 是欄位不是推導值**：推播分級直接讀它，
            # 不要在推播端再寫一份判斷——兩份判斷必然漂移
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_RISK_EVENT_TABLE_NAME} (
                event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT,
                strategy_name   TEXT,
                severity        TEXT NOT NULL,
                category        TEXT NOT NULL,
                symbol          TEXT,
                client_order_id TEXT,
                message         TEXT NOT NULL,
                detail_json     TEXT,
                occurred_at     TEXT NOT NULL
            )
            """,
            f"CREATE INDEX IF NOT EXISTS idx_live_risk_event_time "
            f"ON {LIVE_RISK_EVENT_TABLE_NAME}(occurred_at, severity)",
            # 策略層交易模式。**帳戶層存在 `live_run`，策略層存這裡**：
            # 日頻的 open／close／after_close 是三個獨立行程，不落地的話，
            # 開盤段降級的策略到尾盤段會靜默回到 NORMAL 繼續開新倉
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_STRATEGY_MODE_TABLE_NAME} (
                strategy_name TEXT PRIMARY KEY,
                mode          TEXT NOT NULL,
                reason        TEXT,
                run_id        TEXT,
                changed_at    TEXT NOT NULL
            )
            """,
            # 跨日待辦（平倉單未成交、次日開盤段補平）。
            # **要有 `status` 才冪等**：只記在 `live_run` 上的話，次日開盤段重跑
            # 或崩潰重啟會重複送補平單，而重複的補平單會把部位做反
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_PENDING_ACTION_TABLE_NAME} (
                action_id              TEXT PRIMARY KEY,
                strategy_name          TEXT NOT NULL,
                symbol                 TEXT NOT NULL,
                action                 TEXT NOT NULL,
                position_type          TEXT,
                volume                 INTEGER NOT NULL,
                due_date               TEXT NOT NULL,
                status                 TEXT NOT NULL,
                reason                 TEXT,
                source_client_order_id TEXT,
                created_at             TEXT NOT NULL,
                resolved_at            TEXT
            )
            """,
            f"CREATE INDEX IF NOT EXISTS idx_live_pending_due "
            f"ON {LIVE_PENDING_ACTION_TABLE_NAME}(status, due_date)",
            # 實盤與回測的委託 diff（`ParityChecker` 寫入）。schema 集中在這裡定義，
            # 不散到比對器那邊另外補建表
            f"""
            CREATE TABLE IF NOT EXISTS {LIVE_PARITY_DIFF_TABLE_NAME} (
                date          TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                seq           INTEGER NOT NULL,
                symbol        TEXT,
                side          TEXT,
                category      TEXT NOT NULL,
                live_detail   TEXT,
                backtest_detail TEXT,
                note          TEXT,
                PRIMARY KEY (date, strategy_name, seq)
            )
            """,
        )

    # === 寫入 ===
    def insert_run(self, row: Dict[str, Any]) -> None:
        """寫入一筆啟動紀錄"""

        self._upsert(LIVE_RUN_TABLE_NAME, row, ("run_id",))

    def finish_run(
        self,
        run_id: str,
        ended_at: datetime.datetime,
        end_reason: str,
        account_mode: str,
    ) -> None:
        """
        - Description:
            標記本次啟動結束，並記下結束時的**帳戶層**交易模式

            模式要落地才讀得回來。否則「重啟」就等於偷偷解除 halt——
            這是實盤最常見的事故型態：程式因對帳不一致停下來，值班的人直接重啟。
        - Parameters:
            - run_id: str
                本次啟動的識別碼
            - ended_at: datetime.datetime
                結束時間
            - end_reason: str
                結束原因（正常結束、kill switch、對帳不一致…）
            - account_mode: str
                結束時的帳戶層交易模式
        """

        self.conn.execute(
            f"UPDATE {LIVE_RUN_TABLE_NAME} "
            "SET ended_at = ?, end_reason = ?, account_mode = ? WHERE run_id = ?",
            _to_live_params(ended_at, end_reason, account_mode, run_id),
        )
        self.conn.commit()

    def update_account_mode(self, run_id: str, account_mode: str) -> None:
        """
        - Description:
            即時寫入本次執行的帳戶層交易模式

            **不能等 `finish_run()` 才寫**：崩潰時 `finish_run()` 根本不會被呼叫，
            那一列的 `account_mode` 會停在插入時的 `NORMAL`，
            重啟讀回來就是「什麼事都沒發生」——等於靜默解除 halt。
        - Parameters:
            - run_id: str
                本次啟動的識別碼
            - account_mode: str
                目前的帳戶層交易模式
        """

        self.conn.execute(
            f"UPDATE {LIVE_RUN_TABLE_NAME} SET account_mode = ? WHERE run_id = ?",
            _to_live_params(account_mode, run_id),
        )
        self.conn.commit()

    def mark_crashed_runs(
        self, current_run_id: str, ended_at: datetime.datetime
    ) -> List[str]:
        """
        - Description:
            把還沒結束的舊紀錄標記為非正常結束

            `ended_at IS NULL` 只有兩種可能：正在跑的這一次，或是**上次崩潰了**。
            不標記的話 `get_last_account_mode()` 會跳過那一列（它只讀已結束的），
            於是崩潰前的降級狀態讀不回來——按下重啟鍵就帶著錯誤部位繼續交易。

            **不動 `account_mode`**：那一欄由 `update_account_mode()` 即時維護，
            這裡覆寫等於把崩潰當下的模式擦掉。
        - Parameters:
            - current_run_id: str
                本次啟動的識別碼；它自己不算崩潰
            - ended_at: datetime.datetime
                標記時間
        - Return:
            - List[str]
                被標記的 `run_id`；空 list 表示上次是正常結束
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT run_id FROM {LIVE_RUN_TABLE_NAME} "
            "WHERE ended_at IS NULL AND run_id != ?",
            _to_live_params(current_run_id),
        ).fetchall()
        crashed: List[str] = [str(row[0]) for row in rows]
        if not crashed:
            return []

        self.conn.execute(
            f"UPDATE {LIVE_RUN_TABLE_NAME} "
            "SET ended_at = ?, end_reason = ? WHERE ended_at IS NULL AND run_id != ?",
            _to_live_params(ended_at, self.END_REASON_CRASHED, current_run_id),
        )
        self.conn.commit()
        return crashed

    def get_last_account_mode(self) -> str:
        """
        - Description:
            讀回上一次結束時的帳戶層交易模式

            **取最近一筆「已結束」的紀錄**：正在跑的那一筆（`ended_at IS NULL`）
            可能就是本次自己，讀它等於什麼都沒讀到。

            `rowid` 是次要排序鍵：舊紀錄的 `ended_at` 只有日期，同一天的幾筆
            比不出先後，這時以寫入順序決定，至少不會任挑一筆。
        - Return:
            - str
                交易模式；沒有任何紀錄時為 `NORMAL`
        """

        row: Optional[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT account_mode FROM {LIVE_RUN_TABLE_NAME} "
            "WHERE ended_at IS NOT NULL ORDER BY ended_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else self.MODE_NORMAL

    # 委託列只在第一次寫入時設定的欄位：它們描述「這張單是哪一次 run、何時、
    # 以什麼識別碼送出的」。之後的行程（盤後、重啟接管）以自己的 run 重寫的話，
    # 壓縮碼就再也比對不到券商端，送單時間也變成接管的時間
    ORDER_INSERT_ONLY_COLUMNS: Tuple[str, ...] = (
        "run_id",
        "custom_field",
        "created_at",
    )

    # 委託狀態類欄位：沒有原始訂單可寫時，只更新這些
    ORDER_STATE_COLUMNS: Tuple[str, ...] = (
        "status",
        "broker_order_id",
        "broker_seqno",
        "filled_volume",
        "avg_fill_price",
        "reject_reason",
        "updated_at",
    )

    def upsert_order(self, row: Dict[str, Any]) -> None:
        """寫入或更新一張委託；`client_order_id` 為主鍵，建立資訊只在第一次寫入"""

        self._upsert(
            LIVE_ORDER_TABLE_NAME,
            row,
            ("client_order_id",),
            insert_only=self.ORDER_INSERT_ONLY_COLUMNS,
        )

    def update_order_state(self, client_order_id: str, row: Dict[str, Any]) -> None:
        """
        - Description:
            只更新一張既有委託的狀態類欄位

            給「手上沒有原始訂單」的呼叫端用（例如由紀錄重建、且還原不出訂單的委託）：
            走整列寫入的話，標的、數量、價格會被空值蓋掉，而盤後殘量計算靠的正是它們。
        - Parameters:
            - client_order_id: str
                委託識別碼
            - row: Dict[str, Any]
                欄位字典；只取 `ORDER_STATE_COLUMNS` 內的欄位
        """

        columns: List[str] = [
            column for column in self.ORDER_STATE_COLUMNS if column in row
        ]
        if not columns:
            return
        assignments: str = ",".join(f'"{column}" = ?' for column in columns)
        self.conn.execute(
            f"UPDATE {LIVE_ORDER_TABLE_NAME} SET {assignments} WHERE client_order_id = ?",
            _to_live_params(*(row[column] for column in columns), client_order_id),
        )

    def append_order_event(self, row: Dict[str, Any]) -> None:
        """
        追加一筆狀態轉移

        **append-only**：轉移歷史不可更新也不可刪除，它是事後重建時序的唯一依據。
        """

        self._insert(LIVE_ORDER_EVENT_TABLE_NAME, row)

    def insert_fill(self, row: Dict[str, Any]) -> bool:
        """
        - Description:
            寫入一筆成交；同一筆重放幾次都只會有一列

            走 `INSERT OR IGNORE` 而不是先查再寫：先查再寫在多執行緒下仍會重複，
            而重複記一筆成交等於帳上多了一個不存在的部位。
        - Parameters:
            - row: Dict[str, Any]
                成交欄位
        - Return:
            - bool
                True 代表這是新的一筆
        """

        cursor: sqlite3.Cursor = self._insert(LIVE_FILL_TABLE_NAME, row)
        self.conn.commit()
        return cursor.rowcount > 0

    def backfill_fill_costs(
        self,
        broker_seqno: str,
        broker_trade_id: str,
        actual_fee: float,
        actual_tax: float,
    ) -> None:
        """盤後回填券商的實際手續費與稅；估算值保留不動"""

        self.conn.execute(
            f"UPDATE {LIVE_FILL_TABLE_NAME} SET actual_fee = ?, actual_tax = ? "
            "WHERE broker_seqno = ? AND broker_trade_id = ?",
            _to_live_params(actual_fee, actual_tax, broker_seqno, broker_trade_id),
        )
        self.conn.commit()

    def insert_risk_event(self, row: Dict[str, Any], commit: bool = True) -> None:
        """
        - Description:
            寫入一筆風控事件
        - Parameters:
            - row: Dict[str, Any]
                欄位字典
            - commit: bool
                寫完是否立即 commit；要與其他異動包進同一個交易時傳 False
        """

        self._insert(LIVE_RISK_EVENT_TABLE_NAME, row)
        if commit:
            self.conn.commit()

    def upsert_strategy_mode(self, row: Dict[str, Any]) -> None:
        """寫入或更新某支策略的交易模式"""

        self._upsert(LIVE_STRATEGY_MODE_TABLE_NAME, row, ("strategy_name",))
        self.conn.commit()

    def get_strategy_modes(self) -> Dict[str, str]:
        """
        - Description:
            讀回各策略的交易模式

            日頻的三個段落是三個獨立行程，這張表是策略層降級唯一能跨段落延續的地方。
        - Return:
            - Dict[str, str]
                `{strategy_name: mode}`；沒有紀錄的策略不會出現（視為 NORMAL）
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT strategy_name, mode FROM {LIVE_STRATEGY_MODE_TABLE_NAME}"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # === 部位歸屬帳 ===
    def open_lot(self, row: Dict[str, Any]) -> None:
        """開倉成交時新增一筆 lot"""

        self._insert(LIVE_POSITION_LOT_TABLE_NAME, row)

    def close_lot(self, lot_id: str, closed_at: datetime.datetime) -> None:
        """標記某筆 lot 已平倉"""

        self.conn.execute(
            f"UPDATE {LIVE_POSITION_LOT_TABLE_NAME} SET closed_at = ? WHERE lot_id = ?",
            _to_live_params(closed_at, lot_id),
        )

    def reduce_lot(self, lot_id: str, volume: int) -> None:
        """部分平倉：扣減 lot 的剩餘數量"""

        self.conn.execute(
            f"UPDATE {LIVE_POSITION_LOT_TABLE_NAME} SET volume = volume - ? "
            "WHERE lot_id = ?",
            _to_live_params(volume, lot_id),
        )

    def get_open_lots(
        self, strategy_name: Optional[str] = None, symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        - Description:
            取得未平倉的 lot，依開倉時間排序

            **排序一律固定**（開倉日 → lot_id）：平倉要沖銷哪一筆由順序決定，
            順序不穩定的話，同一天跑兩次會得到不同的已實現損益。
        - Parameters:
            - strategy_name: Optional[str]
                只取某支策略的
            - symbol: Optional[str]
                只取某個標的的
        - Return:
            - List[Dict[str, Any]]
                lot 清單
        """

        conditions: List[str] = ["closed_at IS NULL", "volume > 0"]
        params: List[Any] = []
        if strategy_name is not None:
            conditions.append("strategy_name = ?")
            params.append(strategy_name)
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_POSITION_LOT_TABLE_NAME} "
            f"WHERE {' AND '.join(conditions)} ORDER BY open_date, lot_id",
            tuple(params),
        ).fetchall()
        return self._to_dicts(LIVE_POSITION_LOT_TABLE_NAME, rows)

    def get_symbol_holder(self, symbol: str) -> Optional[str]:
        """
        - Description:
            這個標的目前被哪一支策略持有（D8 的跨策略守門用）
        - Parameters:
            - symbol: str
                商品代號
        - Return:
            - Optional[str]
                策略名；無人持有時為 None
        """

        row: Optional[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT strategy_name FROM {LIVE_POSITION_LOT_TABLE_NAME} "
            "WHERE symbol = ? AND closed_at IS NULL AND volume > 0 "
            "ORDER BY open_date, lot_id LIMIT 1",
            (symbol,),
        ).fetchone()
        return row[0] if row else None

    # === 委託查詢 ===
    def get_unfinished_orders(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得當日尚未終結的委託（重啟接管用）

            終態是 FILLED／CANCELLED／REJECTED／FAILED；其餘都要接管。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                委託清單
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_ORDER_TABLE_NAME} "
            "WHERE substr(created_at, 1, 10) = ? "
            "AND status NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'FAILED') "
            "ORDER BY created_at",
            _to_live_params(run_date),
        ).fetchall()
        return self._to_dicts(LIVE_ORDER_TABLE_NAME, rows)

    def find_order_by_custom_field(self, custom_field: str) -> Optional[Dict[str, Any]]:
        """
        - Description:
            以隨委託往返的壓縮碼反查本地委託

            這是重啟接管能做到**精確比對**的關鍵：壓縮碼可經本地紀錄反查策略，
            策略代號卻反查不到是哪一張單。
        - Parameters:
            - custom_field: str
                壓縮碼
        - Return:
            - Optional[Dict[str, Any]]
                委託；查不到時為 None
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_ORDER_TABLE_NAME} WHERE custom_field = ? LIMIT 1",
            (custom_field,),
        ).fetchall()
        result: List[Dict[str, Any]] = self._to_dicts(LIVE_ORDER_TABLE_NAME, rows)
        return result[0] if result else None

    def get_orders_by_date(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得某一交易日的所有委託（含已終結者）

            與 `get_unfinished_orders()` 的差別是**不過濾狀態**：盤後報表要的是
            「今天送出了什麼」，包含被拒與已撤的——那些正是 parity 比對要歸因的部分。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                委託清單，依建立時間排序
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_ORDER_TABLE_NAME} "
            "WHERE substr(created_at, 1, 10) = ? ORDER BY created_at, client_order_id",
            _to_live_params(run_date),
        ).fetchall()
        return self._to_dicts(LIVE_ORDER_TABLE_NAME, rows)

    def get_risk_events_by_date(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得某一交易日的所有風控事件

            parity 比對以它歸因「實盤為什麼沒送這張單」——被誰擋下來當下就寫進去了，
            事後用猜的一定會把跨策略守門與資金排擠混成同一類。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                事件清單，依發生時間排序
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_RISK_EVENT_TABLE_NAME} "
            "WHERE substr(occurred_at, 1, 10) = ? ORDER BY occurred_at, event_id",
            _to_live_params(run_date),
        ).fetchall()
        return self._to_dicts(LIVE_RISK_EVENT_TABLE_NAME, rows)

    def get_fills_by_seqno(self, broker_seqno: str) -> List[Dict[str, Any]]:
        """
        - Description:
            某張委託的全部成交，依成交時間排序（不限日期）

            盤後校正成本時，股票的已實現交易只帶開倉那張委託的序號，
            開倉價要由它回查；開倉可能在前幾天，故不限日期。
        - Parameters:
            - broker_seqno: str
                券商委託序號
        - Return:
            - List[Dict[str, Any]]
                成交清單；查無時為空
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_FILL_TABLE_NAME} WHERE broker_seqno = ? "
            "ORDER BY filled_at, broker_trade_id",
            (broker_seqno,),
        ).fetchall()
        return self._to_dicts(LIVE_FILL_TABLE_NAME, rows)

    def get_fills_by_date(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得某一交易日的所有成交

            **以 `filled_at` 篩選而不是委託的建立日**：跨段落的委託（開盤段送出、
            尾盤段才成交）在兩種篩法下會落在不同的日子，而帳務要看成交那一天。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                成交清單
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_FILL_TABLE_NAME} "
            "WHERE substr(filled_at, 1, 10) = ? ORDER BY filled_at, broker_trade_id",
            _to_live_params(run_date),
        ).fetchall()
        return self._to_dicts(LIVE_FILL_TABLE_NAME, rows)

    def get_position_snapshots(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得某一交易日的部位快照（含本地、券商與帳戶層三種來源）
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                快照清單
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_POSITION_SNAPSHOT_TABLE_NAME} "
            "WHERE date = ? ORDER BY source, strategy_name, symbol",
            _to_live_params(run_date),
        ).fetchall()
        return self._to_dicts(LIVE_POSITION_SNAPSHOT_TABLE_NAME, rows)

    # === 跨日待辦 ===
    def insert_pending_action(self, row: Dict[str, Any]) -> None:
        """新增一筆跨日待辦（例如「次日開盤段補平」）"""

        self._insert(LIVE_PENDING_ACTION_TABLE_NAME, row)
        self.conn.commit()

    def get_pending_actions(self, due_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            取得到期且尚未處理的待辦

            **只取 `PENDING`**：已處理的不可再送一次——重複的補平單不是多買一點，
            是直接把部位做反。
        - Parameters:
            - due_date: datetime.date
                到期日（含當天以前的逾期項）
        - Return:
            - List[Dict[str, Any]]
                待辦清單
        """

        rows: List[Tuple[Any, ...]] = self.conn.execute(
            f"SELECT * FROM {LIVE_PENDING_ACTION_TABLE_NAME} "
            "WHERE status = ? AND due_date <= ? ORDER BY due_date, action_id",
            _to_live_params(self.ACTION_PENDING, due_date),
        ).fetchall()
        return self._to_dicts(LIVE_PENDING_ACTION_TABLE_NAME, rows)

    def resolve_pending_action(
        self, action_id: str, status: str, resolved_at: datetime.datetime
    ) -> None:
        """把待辦標記為已處理或放棄"""

        self.conn.execute(
            f"UPDATE {LIVE_PENDING_ACTION_TABLE_NAME} "
            "SET status = ?, resolved_at = ? WHERE action_id = ?",
            _to_live_params(status, resolved_at, action_id),
        )
        self.conn.commit()

    def postpone_pending_action(self, action_id: str, due_date: datetime.date) -> None:
        """把待辦延到下一個交易日；狀態維持 `PENDING`"""

        self.conn.execute(
            f"UPDATE {LIVE_PENDING_ACTION_TABLE_NAME} SET due_date = ? "
            "WHERE action_id = ?",
            _to_live_params(due_date, action_id),
        )
        self.conn.commit()

    # === 快照與 parity ===
    def upsert_position_snapshot(self, row: Dict[str, Any]) -> None:
        """寫入部位快照（同一天同一來源重跑會覆寫）"""

        self._upsert(
            LIVE_POSITION_SNAPSHOT_TABLE_NAME,
            row,
            ("date", "strategy_name", "symbol", "source"),
        )

    def upsert_account_snapshot(self, row: Dict[str, Any]) -> None:
        """寫入帳務快照"""

        self._upsert(
            LIVE_ACCOUNT_SNAPSHOT_TABLE_NAME, row, ("date", "strategy_name", "source")
        )

    def upsert_parity_diff(self, row: Dict[str, Any]) -> None:
        """寫入一筆實盤與回測的委託 diff"""

        self._upsert(LIVE_PARITY_DIFF_TABLE_NAME, row, ("date", "strategy_name", "seq"))

    # === 交易 ===
    @contextlib.contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """
        - Description:
            把區塊內的寫入包成一個 savepoint：全部成功才 commit，任何例外整批回滾

            區塊內呼叫的寫入方法**不可自己 commit**（例如 `insert_risk_event` 要傳
            `commit=False`），否則 commit 之前的那些寫入就回滾不掉了。
        - Parameters:
            - name: str
                savepoint 名稱（只能是識別字，不接受外部輸入）
        """

        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        self.conn.execute(f"RELEASE SAVEPOINT {name}")
        self.conn.commit()

    # === 內部 ===
    def _insert(self, table: str, row: Dict[str, Any]) -> sqlite3.Cursor:
        """
        以欄位字典寫入一列（`INSERT OR IGNORE`）

        主鍵重複代表這筆處理過了，那是正常的重放，不是錯誤。
        要更新既有列的路徑一律走 `_upsert()`。
        """

        columns: List[str] = list(row)
        quoted: str = ",".join(f'"{column}"' for column in columns)
        placeholders: str = ",".join("?" * len(columns))

        return self.conn.execute(
            f"INSERT OR IGNORE INTO {table} ({quoted}) VALUES ({placeholders})",
            _to_live_params(*(row[column] for column in columns)),
        )

    def _upsert(
        self,
        table: str,
        row: Dict[str, Any],
        conflict_columns: Tuple[str, ...],
        insert_only: Tuple[str, ...] = (),
    ) -> sqlite3.Cursor:
        """
        - Description:
            以指定的衝突欄位做 UPSERT

            **刻意不用 `INSERT OR REPLACE`。** 它會在**任何** unique 衝突時
            先刪掉舊列再插入——而 `live_order` 的 `broker_seqno` 上有唯一索引。
            兩張不同的單若撞到同一個 seqno（例如恢復流程接管時配錯），
            第一張的委託紀錄會**靜默消失**，而那是唯一能證明它送出去過的東西。

            指定衝突目標之後，非預期的唯一性衝突會拋 `IntegrityError`——
            那才是要的結果：撞到了就要有人知道。
        - Parameters:
            - table: str
                目標資料表
            - row: Dict[str, Any]
                欄位字典
            - conflict_columns: Tuple[str, ...]
                衝突判定的欄位（通常是主鍵）
            - insert_only: Tuple[str, ...]
                只在第一次寫入時設定、衝突時不覆寫的欄位
        - Return:
            - sqlite3.Cursor
                執行結果
        """

        columns: List[str] = list(row)
        quoted: str = ",".join(f'"{column}"' for column in columns)
        placeholders: str = ",".join("?" * len(columns))
        target: str = ",".join(f'"{column}"' for column in conflict_columns)
        assignments: str = ",".join(
            f'"{column}" = excluded."{column}"'
            for column in columns
            if column not in conflict_columns and column not in insert_only
        )

        return self.conn.execute(
            f"INSERT INTO {table} ({quoted}) VALUES ({placeholders}) "
            f"ON CONFLICT({target}) DO UPDATE SET {assignments}",
            _to_live_params(*(row[column] for column in columns)),
        )

    def _to_dicts(
        self, table: str, rows: List[Tuple[Any, ...]]
    ) -> List[Dict[str, Any]]:
        """把查詢結果轉成欄位字典，欄名取自資料表本身"""

        if not rows:
            return []

        columns: List[str] = [
            info[1] for info in self.conn.execute(f"PRAGMA table_info('{table}')")
        ]
        return [dict(zip(columns, row)) for row in rows]
