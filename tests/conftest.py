import datetime
import inspect
import sqlite3
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set, Type

import pandas as pd
import pytest

from core.dao.base import BaseDAO, insert_or_ignore
from core.models import StockQuote
from core.utils import Scale
from core.utils.log_manager import LogManager

"""
測試期間不寫 `logs/`

`LogManager.setup_logger()` 會 `logger.add()` 一個檔案 sink，而 `core/api/`、
`core/pipeline/` 的每個類別都在 `setup()` 裡呼叫它。跑一次測試就會在專案下
產生一整片 `logs/pipeline/*.log`、`logs/api/*.log`——那是**測試的副作用**，
不是測試的產物。

以 `pytest_sessionstart` 換成 no-op，是因為它在 collection 之前執行；
用 fixture 來不及——模組層級就會有類別被實例化。
"""


def _noop(*args, **kwargs) -> None:
    """測試期間的 `setup_logger` 替身：什麼都不做"""


def pytest_sessionstart(session: pytest.Session) -> None:
    """
    整個測試 session 開始前，把日誌檔案 sink 的設定換成 no-op

    **原函式保留在 `real_setup_logger`**：少數測試要驗的正是 sink 本身的行為
    （例如 `watch=True` 在檔案被刪除後重建它），沒有原函式就只能複製一份
    `logger.add(...)` 參數到測試裡——那份副本會與 `LogManager` 悄悄漂移，
    測試因此可能在 production 已經壞掉時仍然全綠。
    """

    LogManager.real_setup_logger = staticmethod(LogManager.setup_logger)
    LogManager.setup_logger = staticmethod(_noop)
    LogManager.setup_backtest_logger = staticmethod(_noop)


# -----------------------------------------------------------------------
# === 資料表 fixture：建表一律走 DAO，不在測試裡抄 schema ===
# -----------------------------------------------------------------------
#
# 舊寫法是各測試檔自己 `CREATE TABLE` 一份縮水版 schema：正式 schema 改一次，
# 散在 30 個檔案裡的副本不會跟著改，測試跑的是一張早就不存在的表。
# 需要「故意壞掉的 schema」（缺欄位、被鎖住）的測試才手寫。


def complete_rows(
    conn: sqlite3.Connection, table: str, records: List[Dict[str, Any]]
) -> pd.DataFrame:
    """
    - Description:
        把只給了關心欄位的紀錄補成可以寫進正式 schema 的 DataFrame

        `NOT NULL` 或主鍵欄沒給值時補預設值（文字欄 `""`、其餘 `0`）；可為 NULL 的欄位
        不補。測試只需要寫出它在意的欄位，其餘交給 schema 決定，schema 改了也不必跟著改。

        **給了 schema 沒有的欄位直接報錯**：默默丟掉的話，欄名打錯或 schema 改名時
        測試照樣綠，驗的卻是一張少了那一欄的表。
    - Parameters:
        - conn: sqlite3.Connection
            已建表的連線
        - table: str
            目標資料表
        - records: List[Dict[str, Any]]
            每列只含測試關心的欄位
    - Return:
        - pd.DataFrame
            欄位順序與資料表一致
    """

    info: List[tuple] = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    known_columns: Set[str] = {column[1] for column in info}
    rows: List[Dict[str, Any]] = []
    for record in records:
        unknown: Set[str] = set(record) - known_columns
        if unknown:
            raise ValueError(f"{table} 沒有這些欄位：{sorted(unknown)}")
        row: Dict[str, Any] = {}
        for _, name, col_type, not_null, _, primary_key in info:
            if name in record:
                row[name] = record[name]
            elif not_null or primary_key:
                row[name] = "" if "TEXT" in str(col_type).upper() else 0
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture
def memory_conn() -> Iterator[sqlite3.Connection]:
    """記憶體資料庫連線，測試結束即關閉"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def dao_factory(memory_conn: sqlite3.Connection) -> Callable[..., BaseDAO]:
    """
    - Description:
        以正式 schema 建表並灌入資料的 DAO 工廠

        `dao_factory(StockPriceDAO, records=[{"date": ..., "stock_id": ...}])`：
        建立 DAO → 呼叫它自己的建表方法 → 以 `complete_rows()` 補齊欄位後寫入 → commit。
        預設用 `memory_conn`；要寫進檔案 DB（例如 updater 會以路徑重開）時傳 `conn=`。
        表名參數化的 DAO（財報、期貨籌碼）以 `table_name=` 傳入。
        一個 DAO 管多張表時（期貨保證金的金額表／比例表），以 `records_table=` 指定
        `records` 寫進哪一張；未指定時寫進 `TABLE_NAME`。
    """

    def make(
        dao_cls: Type[BaseDAO],
        records: Optional[List[Dict[str, Any]]] = None,
        conn: Optional[sqlite3.Connection] = None,
        columns: Optional[List[str]] = None,
        records_table: Optional[str] = None,
        **dao_kwargs: Any,
    ) -> BaseDAO:
        """建立 `dao_cls` 實例，以其自身的建表方法建表並寫入補齊欄位的 `records`"""

        dao: BaseDAO = dao_cls(conn=conn or memory_conn, **dao_kwargs)
        records = records or []

        if hasattr(dao, "ensure_tables"):
            dao.ensure_tables()
        else:
            parameters: Mapping[str, inspect.Parameter] = inspect.signature(
                dao.ensure_table
            ).parameters
            if not parameters:
                dao.ensure_table()
            elif "df" in parameters:
                # 欄位由資料推導的表（期貨籌碼）：以第一批資料建表
                dao.ensure_table(pd.DataFrame(records, columns=columns))
            else:
                # 欄位清單由呼叫端提供的表（財報、月營收）
                dao.ensure_table(columns or list(records[0].keys()))

        if records:
            table: str = records_table or dao.TABLE_NAME
            insert_or_ignore(dao.conn, table, complete_rows(dao.conn, table, records))
        dao.commit()
        return dao

    return make


# -----------------------------------------------------------------------
# === 報價 fixture：放 root conftest 供各子目錄共用 ===
# -----------------------------------------------------------------------
#
# 原本住在 `tests/backtest/conftest.py`，只有 `tests/backtest/` 看得到。
# `sizing.py` 搬到 `core/portfolio/` 後，`tests/portfolio/` 也要用同一個 factory；
# 複製一份到那邊必然與這份漂移，故上移到 root，全專案共用同一份。


@pytest.fixture
def make_quote() -> Callable[..., StockQuote]:
    """建立 StockQuote 的 factory；未指定的 OHLC 一律沿用 cur_price"""

    def _make_quote(
        stock_id: str = "2330",
        date: Optional[datetime.date] = None,
        cur_price: float = 100.0,
        open: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
        close: Optional[float] = None,
        volume: int = 1000,
        scale: Scale = Scale.DAY,
    ) -> StockQuote:
        return StockQuote(
            stock_id=stock_id,
            scale=scale,
            date=date or datetime.date(2024, 1, 2),
            cur_price=cur_price,
            volume=volume,
            open=open if open is not None else cur_price,
            high=high if high is not None else cur_price,
            low=low if low is not None else cur_price,
            close=close if close is not None else cur_price,
        )

    return _make_quote
