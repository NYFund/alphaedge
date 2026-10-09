import datetime
import os
import uuid
from typing import Iterator, List, Tuple

import pytest

"""
台股 tick 的 TimescaleDB 整合測試

**需要真的 TimescaleDB**：未設定 `TICK_DATABASE_URL`（CI、只跑日線的機器）時整個模組 skip。
本機以 `docker compose up -d postgres` 啟動後，設定環境變數再跑：

    TICK_DATABASE_URL=postgresql://alphaedge:alphaedge@localhost:5432/alphaedge \\
        uv run --no-sync pytest tests/test_stock_tick_timescale.py

每個測試在自己的暫存 schema 裡建表、結束時整個 drop，**不碰正式的 `public.stock_tick`**。
"""

pytestmark = pytest.mark.skipif(
    not os.getenv("TICK_DATABASE_URL"),
    reason="未設定 TICK_DATABASE_URL，跳過 TimescaleDB 整合測試",
)

psycopg = pytest.importorskip("psycopg")

from core.dao.tw.stock_tick_dao import StockTickDAO  # noqa: E402


@pytest.fixture
def dao() -> Iterator[StockTickDAO]:
    """在暫存 schema 建好表的 DAO；測試結束後連同 schema 一起刪掉"""

    schema: str = f"test_{uuid.uuid4().hex[:12]}"
    tick_dao: StockTickDAO = StockTickDAO(schema=schema)
    tick_dao.create_tables()
    try:
        yield tick_dao
    finally:
        tick_dao.conn.rollback()
        tick_dao.conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        tick_dao.conn.commit()
        tick_dao.close()


def _insert_rows(dao: StockTickDAO, rows: List[Tuple]) -> None:
    """直接寫幾列測試資料（寫入路徑還沒有正式介面時用）"""

    with dao.conn.transaction():
        with dao.conn.cursor() as cursor:
            cursor.executemany(
                f'INSERT INTO "{dao.schema}".stock_tick VALUES '
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )


# === 建表 ===
def test_create_tables_is_idempotent(dao: StockTickDAO) -> None:
    """重跑建表不報錯，hypertable、chunk 間隔與壓縮設定都在"""

    dao.create_tables()

    with dao.conn.transaction():
        hypertables: List[Tuple] = dao.conn.execute(
            """
            SELECT compression_enabled FROM timescaledb_information.hypertables
            WHERE hypertable_schema = %s AND hypertable_name = 'stock_tick'
            """,
            (dao.schema,),
        ).fetchall()
        interval: datetime.timedelta = dao.conn.execute(
            """
            SELECT time_interval FROM timescaledb_information.dimensions
            WHERE hypertable_schema = %s AND hypertable_name = 'stock_tick'
            """,
            (dao.schema,),
        ).fetchone()[0]
    assert hypertables == [(True,)]
    assert interval == StockTickDAO.CHUNK_INTERVAL
    assert dao.table_exists()


def test_time_column_is_naive_timestamp(dao: StockTickDAO) -> None:
    """
    `time` 必須是不帶時區的 `TIMESTAMP`

    用 `TIMESTAMPTZ` 的話 ConnectorX 讀出來是 UTC 的 tz-aware 欄位，
    回測拿去比日期會差 8 小時。
    """

    with dao.conn.transaction():
        data_type: str = dao.conn.execute(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'stock_tick' AND column_name = 'time'
            """,
            (dao.schema,),
        ).fetchone()[0]
    assert data_type == "timestamp without time zone"


def test_duplicate_stock_time_rows_are_allowed(dao: StockTickDAO) -> None:
    """同一瞬間的多筆成交（連所有欄位都相同）都要留下，不可被唯一鍵擋掉"""

    row: Tuple = ("2330", datetime.datetime(2024, 5, 8, 9, 0, 1), 0)
    rest: Tuple = (800.0, 1, 799.0, 2, 800.0, 3, 1)
    _insert_rows(dao, [row + rest, (row[0], row[1], 1) + rest])

    with dao.conn.transaction():
        count: int = dao.conn.execute(
            f'SELECT count(*) FROM "{dao.schema}".stock_tick'
        ).fetchone()[0]
    assert count == 2


# === 壓縮 ===
def test_pause_and_resume_compression_policy(dao: StockTickDAO) -> None:
    """歷史匯入期間要能停掉壓縮 policy，結束後恢復"""

    assert dao.is_compression_policy_scheduled()

    dao.pause_compression_policy()
    assert not dao.is_compression_policy_scheduled()

    dao.resume_compression_policy()
    assert dao.is_compression_policy_scheduled()


def test_compress_chunks_before_compresses_old_chunks(dao: StockTickDAO) -> None:
    """早於指定日期的 chunk 被壓縮、資料不變；重跑時略過已壓縮的 chunk"""

    _insert_rows(
        dao,
        [
            ("2330", datetime.datetime(2024, 5, 8, 9, 0, 1), 0)
            + (800.0, 1, 799.0, 2, 800.0, 3, 1)
        ],
    )

    assert dao.compress_chunks_before(datetime.date(2024, 5, 20)) == 1
    assert dao.compress_chunks_before(datetime.date(2024, 5, 20)) == 1

    with dao.conn.transaction():
        compressed: int = dao.conn.execute(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = %s AND is_compressed",
            (dao.schema,),
        ).fetchone()[0]
        count: int = dao.conn.execute(
            f'SELECT count(*) FROM "{dao.schema}".stock_tick'
        ).fetchone()[0]
    assert compressed == 1
    assert count == 1


def test_shared_connection_is_not_closed_by_dao(dao: StockTickDAO) -> None:
    """傳入的連線不歸 DAO 管，`close()` 不可關掉它"""

    borrower: StockTickDAO = StockTickDAO(conn=dao.conn, schema=dao.schema)
    borrower.close()

    assert not dao.conn.closed
