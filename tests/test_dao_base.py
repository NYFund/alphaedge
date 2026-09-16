import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pytest

from core.dao.base import BaseDAO, to_sql_params
from core.dao.connection import connect_sqlite

"""
DAO 底座的契約

- 連線所有權：傳入的連線 `close()` 不關，自己開的才關
- 「表不存在」回空值，查詢錯誤一律往外拋（不可吞）
- savepoint：區塊失敗只回滾該區塊，不 commit、也不影響同一交易內先前完成的區塊

不碰正式 DB：一律用 `:memory:` 或 `tmp_path`。
"""


class _DemoDAO(BaseDAO):
    """測試用的最小 DAO"""

    TABLE_NAME: str = "demo"
    DEFAULT_DB_PATH: Optional[Path] = None

    def create_table(self) -> None:
        """建立測試表"""

        self.conn.execute(
            "CREATE TABLE demo (date TEXT, stock_id TEXT, value REAL, "
            "PRIMARY KEY (date, stock_id))"
        )
        self.conn.commit()

    def get_latest_date(self) -> Optional[str]:
        """最新日期"""

        return self._get_latest_value("date")

    def get_latest_missing_column(self) -> Optional[str]:
        """查一個不存在的欄位，用來驗證錯誤不被吞掉"""

        return self._get_latest_value("no_such_column")


def make_rows(dates: List[str], stock_id: str = "2330") -> pd.DataFrame:
    """建立測試列"""

    return pd.DataFrame(
        {"date": dates, "stock_id": [stock_id] * len(dates), "value": 1.0}
    )


def count_rows(conn: sqlite3.Connection) -> int:
    """測試表的列數"""

    return conn.execute("SELECT count(*) FROM demo").fetchone()[0]


# === 連線所有權 ===
def test_shared_connection_is_not_closed() -> None:
    """傳入的連線屬於建立者，DAO 的 `close()` 不可關掉它"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    dao: _DemoDAO = _DemoDAO(conn=conn)

    dao.close()

    assert conn.execute("SELECT 1").fetchone() == (1,)
    conn.close()


def test_owned_connection_is_closed(tmp_path: Path) -> None:
    """自己開的連線要自己關，關完 `conn` 為 None"""

    dao: _DemoDAO = _DemoDAO(db_path=tmp_path / "test.db")
    conn: sqlite3.Connection = dao.conn

    dao.close()

    assert dao.conn is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_missing_path_raises() -> None:
    """沒有連線也沒有路徑時當場拋出，而不是等到第一次查詢才炸"""

    with pytest.raises(ValueError, match="DEFAULT_DB_PATH"):
        _DemoDAO()


# === 查詢 ===
def test_table_exists() -> None:
    """表存在與不存在要分得開"""

    dao: _DemoDAO = _DemoDAO(conn=sqlite3.connect(":memory:"))
    assert dao.table_exists() is False

    dao.create_table()
    assert dao.table_exists() is True


def test_latest_value_semantics() -> None:
    """表不存在、表為空回 None；查詢錯誤往外拋"""

    dao: _DemoDAO = _DemoDAO(conn=sqlite3.connect(":memory:"))
    assert dao.get_latest_date() is None

    dao.create_table()
    assert dao.get_latest_date() is None

    dao.insert_or_ignore(make_rows(["2024-01-02", "2024-01-05", "2024-01-03"]))
    assert dao.get_latest_date() == "2024-01-05"

    with pytest.raises(sqlite3.OperationalError):
        dao.get_latest_missing_column()


def test_distinct_dates_sorted_and_deduplicated() -> None:
    """交易日查詢：去重、排序、轉 `datetime.date`；區間顛倒回空"""

    dao: _DemoDAO = _DemoDAO(conn=sqlite3.connect(":memory:"))
    dao.create_table()
    dao.insert_or_ignore(make_rows(["2024-01-03", "2024-01-02"], "2330"))
    dao.insert_or_ignore(make_rows(["2024-01-03", "2024-01-08"], "2317"))

    days: List[datetime.date] = dao.get_distinct_dates(
        datetime.date(2024, 1, 1), datetime.date(2024, 1, 5)
    )

    assert days == [datetime.date(2024, 1, 2), datetime.date(2024, 1, 3)]
    assert (
        dao.get_distinct_dates(datetime.date(2024, 1, 5), datetime.date(2024, 1, 1))
        == []
    )


def test_to_sql_params_converts_dates() -> None:
    """`date`／`datetime` 轉 ISO 日期字串，其他型別原樣通過"""

    assert to_sql_params(
        datetime.date(2024, 1, 2),
        datetime.datetime(2024, 1, 2, 13, 30),
        "2330",
        5,
    ) == ("2024-01-02", "2024-01-02", "2330", 5)


# === 寫入與交易 ===
def test_insert_or_ignore_reports_counts() -> None:
    """回傳（寫入, 跳過）；重複主鍵靜靜跳過"""

    dao: _DemoDAO = _DemoDAO(conn=sqlite3.connect(":memory:"))
    dao.create_table()

    assert dao.insert_or_ignore(make_rows(["2024-01-02", "2024-01-03"])) == (2, 0)
    assert dao.insert_or_ignore(make_rows(["2024-01-03", "2024-01-04"])) == (1, 1)
    assert dao.insert_or_ignore(make_rows([])) == (0, 0)


def test_failed_savepoint_rolls_back_only_its_block(tmp_path: Path) -> None:
    """
    第二個檔案寫到一半出錯：它寫進去的列全部回滾，第一個檔案的列 commit 後仍在

    這是 loader「單檔失敗整檔不留」的底層保證。舊寫法沒有 savepoint，
    失敗檔案已寫入的列會被最後的 `commit()` 一起寫進去。
    """

    dao: _DemoDAO = _DemoDAO(db_path=tmp_path / "test.db")
    dao.create_table()

    with dao.savepoint():
        dao.insert_or_ignore(make_rows(["2024-01-02"], "2330"))

    with pytest.raises(RuntimeError, match="寫到一半"):
        with dao.savepoint():
            dao.insert_or_ignore(make_rows(["2024-01-02", "2024-01-03"], "2317"))
            raise RuntimeError("寫到一半")

    dao.commit()

    reader: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    rows: List[tuple] = reader.execute("SELECT stock_id FROM demo").fetchall()
    reader.close()
    dao.close()

    assert rows == [("2330",)]


def test_savepoint_does_not_commit(tmp_path: Path) -> None:
    """savepoint 正常結束不等於 commit：另一條連線在 commit 前看不到資料"""

    dao: _DemoDAO = _DemoDAO(db_path=tmp_path / "test.db")
    dao.create_table()
    reader: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")

    with dao.savepoint():
        dao.insert_or_ignore(make_rows(["2024-01-02"]))

    assert count_rows(reader) == 0

    dao.commit()
    assert count_rows(reader) == 1

    reader.close()
    dao.close()


def test_read_only_connection_cannot_write_or_create(tmp_path: Path) -> None:
    """唯讀連線不可寫入，檔案不存在時也不可默默建出空 DB"""

    db_path: Path = tmp_path / "test.db"
    with pytest.raises(sqlite3.OperationalError):
        connect_sqlite(db_path, read_only=True)
    assert not db_path.exists()

    sqlite3.connect(db_path).close()
    conn: sqlite3.Connection = connect_sqlite(db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE x (y INTEGER)")
    conn.close()
