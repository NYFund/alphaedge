import datetime
import os
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pandas as pd
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

from core.api.tw.stock_tick_api import StockTickAPI  # noqa: E402
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


# === 寫入（replace_day） ===
TRADE_DATE: datetime.date = datetime.date(2024, 5, 8)


def _day_frame(rows: List[Tuple]) -> pd.DataFrame:
    """以 (time, seq, close, volume, bid_price) 組出 `replace_day()` 要的當日資料"""

    return pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "time": pd.Timestamp(t),
                "seq": seq,
                "close": close,
                "volume": volume,
                "bid_price": bid_price,
                "bid_volume": 2,
                "ask_price": close,
                "ask_volume": 3,
                "tick_type": 1,
            }
            for t, seq, close, volume, bid_price in rows
        ]
    )


def _stored(dao: StockTickDAO) -> List[Tuple]:
    """目前存著的 (time, seq, close, volume, bid_price)，依 time、seq 排序"""

    with dao.conn.transaction():
        return dao.conn.execute(
            f"SELECT time, seq, close, volume, bid_price "
            f'FROM "{dao.schema}".stock_tick ORDER BY time, seq'
        ).fetchall()


def _load_log(dao: StockTickDAO) -> List[Tuple]:
    """目前的 load_log：(stock_id, trade_date, source_rows, row_count, source_file)"""

    with dao.conn.transaction():
        return dao.conn.execute(
            f"SELECT stock_id, trade_date, source_rows, row_count, source_file "
            f'FROM "{dao.schema}".stock_tick_load_log ORDER BY stock_id, trade_date'
        ).fetchall()


def test_replace_day_is_idempotent_and_values_round_trip(dao: StockTickDAO) -> None:
    """
    同一天寫兩次列數不變；microsecond 時間、2 位小數價格、bid 為 0 都原樣存回
    """

    day: pd.DataFrame = _day_frame(
        [
            ("2024-05-08 09:00:01.123456", 0, 6.56, 1, 0.0),
            ("2024-05-08 09:00:01.123456", 1, 6.56, 5, 6.55),
        ]
    )

    assert dao.replace_day("2330", TRADE_DATE, day, 3, "2330.csv") == 2
    assert dao.replace_day("2330", TRADE_DATE, day, 3, "2330.csv") == 2

    assert _stored(dao) == [
        (datetime.datetime(2024, 5, 8, 9, 0, 1, 123456), 0, 6.56, 1, 0.0),
        (datetime.datetime(2024, 5, 8, 9, 0, 1, 123456), 1, 6.56, 5, 6.55),
    ]
    assert _load_log(dao) == [("2330", TRADE_DATE, 3, 2, "2330.csv")]


def test_replace_day_replaces_old_rows_completely(dao: StockTickDAO) -> None:
    """同一天以較少列的新資料重寫時，舊列要全部消失；別天的資料不受影響"""

    dao.replace_day(
        "2330",
        TRADE_DATE,
        _day_frame(
            [
                ("2024-05-08 09:00:01", 0, 800.0, 1, 799.0),
                ("2024-05-08 13:30:00", 1, 801.0, 9, 800.0),
            ]
        ),
        2,
        "old.csv",
    )
    dao.replace_day(
        "2330",
        datetime.date(2024, 5, 9),
        _day_frame([("2024-05-09 09:00:01", 0, 805.0, 1, 804.0)]),
        1,
        "old.csv",
    )

    dao.replace_day(
        "2330",
        TRADE_DATE,
        _day_frame([("2024-05-08 09:00:05", 0, 802.0, 2, 801.0)]),
        1,
        "new.csv",
    )

    assert _stored(dao) == [
        (datetime.datetime(2024, 5, 8, 9, 0, 5), 0, 802.0, 2, 801.0),
        (datetime.datetime(2024, 5, 9, 9, 0, 1), 0, 805.0, 1, 804.0),
    ]
    assert _load_log(dao)[0] == ("2330", TRADE_DATE, 1, 1, "new.csv")


def test_replace_day_with_empty_frame_logs_zero_rows(dao: StockTickDAO) -> None:
    """整天被排除的日子：清掉舊資料、登記 `row_count = 0`"""

    dao.replace_day(
        "2330",
        TRADE_DATE,
        _day_frame([("2024-05-08 09:00:01", 0, 800.0, 1, 799.0)]),
        1,
        "old.csv",
    )

    written: int = dao.replace_day(
        "2330",
        TRADE_DATE,
        _day_frame([]).reindex(columns=StockTickDAO.WRITE_COLUMNS),
        7,
        "new.csv",
    )

    assert written == 0
    assert _stored(dao) == []
    assert _load_log(dao) == [("2330", TRADE_DATE, 7, 0, "new.csv")]


def test_create_tables_rerun_after_compression(dao: StockTickDAO) -> None:
    """已有壓縮 chunk 時重跑建表（loader 每次啟動都會跑）不報錯、資料不變"""

    dao.replace_day(
        "2330",
        TRADE_DATE,
        _day_frame([("2024-05-08 09:00:01", 0, 800.0, 1, 799.0)]),
        1,
        "2330.csv",
    )
    dao.compress_chunks_before(datetime.date(2024, 5, 20))

    dao.create_tables()

    assert len(_stored(dao)) == 1


# === 端到端：updater 依 load_log 續跑 ===
def test_updater_resumes_from_load_log(
    dao: StockTickDAO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    三天入庫後刪掉某檔最後一天的紀錄與資料，再跑一次 `update()`：只有那一檔的那一天被重爬

    crawler 用替身（不連 Shioaji），日 K 用記憶體 SQLite；寫入的是暫存 schema。
    """

    import sqlite3
    from types import SimpleNamespace

    from core.dao.tw.stock_price_dao import StockPriceDAO
    from core.pipeline.tw.cleaners.stock_tick_cleaner import StockTickCleaner
    from core.pipeline.tw.loaders import stock_tick_loader as loader_module
    from core.pipeline.tw.loaders.stock_tick_loader import StockTickLoader
    from core.pipeline.tw.updaters.stock_tick_updater import StockTickUpdater

    days: List[datetime.date] = [
        datetime.date(2024, 5, 8),
        datetime.date(2024, 5, 9),
        datetime.date(2024, 5, 10),
    ]
    stocks: List[str] = ["1101", "2330"]

    price_dao: StockPriceDAO = StockPriceDAO(conn=sqlite3.connect(":memory:"))
    price_dao.create_table()
    price_dao.conn.executemany(
        'INSERT INTO price ("date", stock_id, "證券名稱") VALUES (?, ?, ?)',
        [(day.isoformat(), stock, stock) for day in days for stock in stocks],
    )
    monkeypatch.setattr(loader_module, "TICK_DOWNLOADS_PATH", tmp_path)

    crawled: List[Tuple[str, datetime.date]] = []

    class Crawler:
        """記錄每次爬取，回一筆 Shioaji 格式的 tick"""

        def crawl_stock_tick(
            self, api: object, date: datetime.date, code: str
        ) -> pd.DataFrame:
            crawled.append((code, date))
            return pd.DataFrame(
                {
                    "ts": [pd.Timestamp(f"{date} 09:00:01.123456")],
                    "close": [800.0],
                    "volume": [1],
                    "bid_price": [799.0],
                    "bid_volume": [2],
                    "ask_price": [800.0],
                    "ask_volume": [3],
                    "tick_type": [1],
                }
            )

    cleaner: StockTickCleaner = StockTickCleaner.__new__(StockTickCleaner)
    cleaner.tick_dir = tmp_path
    updater: StockTickUpdater = StockTickUpdater.__new__(StockTickUpdater)
    updater.crawler = Crawler()
    updater.cleaner = cleaner
    updater.loader = StockTickLoader(dao=dao, price_dao=price_dao)
    updater.tick_dir = tmp_path
    updater.sessions = []
    updater.api_list = [
        SimpleNamespace(usage=lambda: SimpleNamespace(remaining_bytes=10**12))
    ]
    updater.num_threads = 1
    updater.all_stock_list = stocks
    updater.loaded_last_dates = {}

    updater.update(days[0], days[-1])
    assert sorted(crawled) == sorted((s, d) for s in stocks for d in days)
    assert len(_load_log(dao)) == 6

    with dao.conn.transaction():
        dao.conn.execute(
            f'DELETE FROM "{dao.schema}".stock_tick_load_log '
            "WHERE stock_id = '2330' AND trade_date = %s",
            (days[-1],),
        )
        dao.conn.execute(
            f'DELETE FROM "{dao.schema}".stock_tick '
            "WHERE stock_id = '2330' AND time >= %s",
            (datetime.datetime(2024, 5, 10),),
        )
    crawled.clear()

    updater.update(days[0], days[-1])

    assert crawled == [("2330", days[-1])]
    assert len(_load_log(dao)) == 6
    with dao.conn.transaction():
        count: int = dao.conn.execute(
            f'SELECT count(*) FROM "{dao.schema}".stock_tick'
        ).fetchone()[0]
    assert count == 6
    price_dao.conn.close()


# === 讀取（StockTickAPI） ===
def _tick(stock_id: str, t: str, seq: int, close: float, volume: int = 1) -> Dict:
    """一列寫入用的 tick"""

    return {
        "stock_id": stock_id,
        "time": pd.Timestamp(t),
        "seq": seq,
        "close": close,
        "volume": volume,
        "bid_price": close,
        "bid_volume": 2,
        "ask_price": close,
        "ask_volume": 3,
        "tick_type": 1,
    }


@pytest.fixture
def loaded_api(dao: StockTickDAO) -> Iterator[StockTickAPI]:
    """
    寫好三檔股票兩天資料的 API（同一時間戳記有多筆、跨日邊界各一筆）

    每筆成交量都不同，累計量算錯（漏加、重複加、跨股票或跨日沒歸零）時數字才會對不上。
    """

    rows: List[Dict] = [
        # 5/8：2330 同一瞬間兩筆（seq 0、1），1101 與 2330 同時間戳記
        _tick("2330", "2024-05-08 09:00:01", 0, 800.0, 5),
        _tick("2330", "2024-05-08 09:00:01", 1, 801.0, 7),
        _tick("1101", "2024-05-08 09:00:01", 0, 40.0, 11),
        # 夾在 2330 兩段之間：依時間排與依股票排的結果才會不同
        _tick("1101", "2024-05-08 09:00:05", 1, 40.5, 13),
        _tick("2603", "2024-05-08 09:00:06", 0, 150.0, 17),
        _tick("2330", "2024-05-08 23:59:59.999999", 2, 802.0, 19),
        # 5/9 00:00:00 整：屬於 5/9，查 5/8 時不可被包進來；累計量從這一筆重新起算
        _tick("2330", "2024-05-09 00:00:00", 0, 803.0, 23),
        _tick("1101", "2024-05-09 09:00:02", 0, 41.0, 29),
    ]
    frame: pd.DataFrame = pd.DataFrame(rows)
    for (stock_id, day), group in frame.groupby(
        [frame["stock_id"], frame["time"].dt.date]
    ):
        dao.replace_day(stock_id, day, group, len(group), "fixture.csv")

    api: StockTickAPI = StockTickAPI(dao=dao)
    yield api
    api.close()


def test_get_ordered_ticks_orders_by_time_then_stock_then_seq(
    loaded_api: StockTickAPI,
) -> None:
    """同一時間戳記跨股票以代號、同股以 seq 固定順序；日期區間是半開的"""

    day: datetime.date = datetime.date(2024, 5, 8)

    ticks: pd.DataFrame = loaded_api.get_ordered_ticks(day, day)

    assert list(ticks.columns) == [
        "stock_id",
        "time",
        "close",
        "volume",
        "bid_price",
        "bid_volume",
        "ask_price",
        "ask_volume",
        "tick_type",
        "cum_volume",
    ]
    assert list(zip(ticks["stock_id"], ticks["close"])) == [
        ("1101", 40.0),
        ("2330", 800.0),
        ("2330", 801.0),
        ("1101", 40.5),
        ("2603", 150.0),
        ("2330", 802.0),
    ]
    assert str(ticks["time"].dtype) == "datetime64[ns]"
    assert ticks["time"].dt.tz is None
    assert str(ticks["volume"].dtype) == "int64"
    assert str(ticks["tick_type"].dtype) == "int64"


def test_get_groups_by_stock_and_get_stock_ticks_filters(
    loaded_api: StockTickAPI,
) -> None:
    """`get()` 依股票再依時間排序；`get_stock_ticks()` 只回該檔；`get_last_tick()` 取最後一筆"""

    start: datetime.date = datetime.date(2024, 5, 8)
    end: datetime.date = datetime.date(2024, 5, 9)

    all_ticks: pd.DataFrame = loaded_api.get(start, end)
    stock_ticks: pd.DataFrame = loaded_api.get_stock_ticks("2330", start, end)
    last: pd.DataFrame = loaded_api.get_last_tick("2330", start)

    assert all_ticks["stock_id"].tolist() == ["1101"] * 3 + ["2330"] * 4 + ["2603"]
    assert stock_ticks["close"].tolist() == [800.0, 801.0, 802.0, 803.0]
    assert last["close"].tolist() == [802.0]


def test_empty_result_keeps_columns_and_dtypes(loaded_api: StockTickAPI) -> None:
    """沒有資料的日子回空表，但欄位與 dtype 和有資料時相同"""

    day: datetime.date = datetime.date(2024, 5, 10)

    empty: pd.DataFrame = loaded_api.get_ordered_ticks(day, day)

    assert empty.empty
    assert str(empty["time"].dtype) == "datetime64[ns]"
    assert str(empty["volume"].dtype) == "int64"
    assert str(empty["cum_volume"].dtype) == "int64"


def test_get_ordered_ticks_with_stock_ids_equals_per_stock_queries(
    loaded_api: StockTickAPI,
) -> None:
    """
    多檔查詢的結果，等於逐檔查詢合併後再依 `(time, stock_id)` 排序

    策略清單模式一天只查一次；結果必須和逐檔查的一樣，只是少了好幾百次往返。
    """

    start: datetime.date = datetime.date(2024, 5, 8)
    end: datetime.date = datetime.date(2024, 5, 9)
    stock_ids: List[str] = ["2330", "1101"]

    multi: pd.DataFrame = loaded_api.get_ordered_ticks(start, end, stock_ids=stock_ids)
    # 逐檔查回來各自已依 (time, seq) 排序；穩定排序後同一時間戳記內的 seq 順序不變
    per_stock: pd.DataFrame = (
        pd.concat(
            [loaded_api.get_stock_ticks(sid, start, end) for sid in stock_ids],
            ignore_index=True,
        )
        .sort_values(["time", "stock_id"], kind="stable")
        .reset_index(drop=True)
    )

    pd.testing.assert_frame_equal(multi.reset_index(drop=True), per_stock)
    assert "2603" not in set(multi["stock_id"])


def test_get_ordered_ticks_ignores_duplicate_and_missing_stock_ids(
    loaded_api: StockTickAPI,
) -> None:
    """清單裡重複的代號不會讓資料重複；沒有資料的代號只是沒有列"""

    day: datetime.date = datetime.date(2024, 5, 8)

    ticks: pd.DataFrame = loaded_api.get_ordered_ticks(
        day, day, stock_ids=["2603", "2603", "9999"]
    )

    assert ticks["stock_id"].tolist() == ["2603"]


def test_cum_volume_is_daily_running_total_per_stock(
    loaded_api: StockTickAPI,
) -> None:
    """`cum_volume` 是該檔當日到這一筆為止的累計量：含這一筆、跨股票互不影響、隔天歸零"""

    start: datetime.date = datetime.date(2024, 5, 8)
    end: datetime.date = datetime.date(2024, 5, 9)

    ticks: pd.DataFrame = loaded_api.get_ordered_ticks(start, end)
    stock_ticks: pd.DataFrame = loaded_api.get_stock_ticks("2330", start, end)
    filtered: pd.DataFrame = loaded_api.get_ordered_ticks(
        start, end, stock_ids=["2330"]
    )

    assert list(zip(ticks["stock_id"], ticks["volume"], ticks["cum_volume"])) == [
        ("1101", 11, 11),
        ("2330", 5, 5),
        ("2330", 7, 12),
        ("1101", 13, 24),
        ("2603", 17, 17),
        ("2330", 19, 31),
        ("2330", 23, 23),
        ("1101", 29, 29),
    ]
    # 不論查全市場、只查一檔或用清單查，同一筆 tick 的累計量都相同
    assert stock_ticks["cum_volume"].tolist() == [5, 12, 31, 23]
    assert filtered["cum_volume"].tolist() == [5, 12, 31, 23]


def test_api_refuses_when_table_missing(dao: StockTickDAO) -> None:
    """還沒建表時建立 API 就拋出，不讓回測跑完才發現整段沒有報價"""

    with dao.conn.transaction():
        dao.conn.execute(f'CREATE SCHEMA "{dao.schema}_empty"')
    empty_dao: StockTickDAO = StockTickDAO(conn=dao.conn, schema=f"{dao.schema}_empty")
    try:
        with pytest.raises(RuntimeError, match="尚未匯入"):
            StockTickAPI(dao=empty_dao)
    finally:
        with dao.conn.transaction():
            dao.conn.execute(f'DROP SCHEMA "{dao.schema}_empty" CASCADE')


def test_datafeed_returns_one_quote_per_tick(loaded_api: StockTickAPI) -> None:
    """
    回測 DataFeed 以 `get_ordered_ticks()` 取報價：每天的 TickQuote 數＝資料庫列數，
    `time` 經 `itertuples()` 取出後仍是 `datetime`
    """

    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
    from core.utils.constant import Scale

    feed: TwStockDataFeed = TwStockDataFeed()
    feed.tick = loaded_api

    quotes = feed.get_quotes(datetime.date(2024, 5, 8), Scale.TICK)

    assert len(quotes) == 6
    assert all(isinstance(q.tick_quote.time, datetime.datetime) for q in quotes)
    assert [q.close for q in quotes] == [40.0, 800.0, 801.0, 40.5, 150.0, 802.0]


# === 端到端：歷史匯入腳本 ===
def test_history_import_script_end_to_end(
    dao: StockTickDAO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    歷史匯入：跨兩個 chunk 的樣本依 chunk 切檔、載入、壓縮、比對全數通過；
    `--resume` 重跑不重複寫入；列計畫不寫入；DB 少一列時比對失敗
    """

    import sys

    from core.dao.tw.stock_price_dao import StockPriceDAO
    from scripts.manual import manual_tick_history_import as importer

    header: str = "volume,bid_volume,ask_price,tick_type,bid_price,ts,close,ask_volume"
    source: Path = tmp_path / "source"
    source.mkdir()
    (source / "2330.csv").write_text(
        "\n".join(
            [
                header,
                "1,1,800,1,799,2024-05-08 09:00:01.000000,800,1",
                "0,0,0,0,0,2024-05-08 09:00:02.000000,0,0",  # 全零列，排除
                "2,1,801,2,800,2024-05-10 09:00:01.123,801,1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # 興櫃：price 表沒有這檔，整天排除但仍要登記
    (source / "1563.csv").write_text(
        header + "\n1000,1,50,1,49,2024-05-08 09:00:01.000000,50,1\n", encoding="utf-8"
    )
    price_db: Path = tmp_path / "price.db"
    price_dao: StockPriceDAO = StockPriceDAO(db_path=price_db)
    price_dao.create_table()
    price_dao.conn.executemany(
        'INSERT INTO price ("date", stock_id, "證券名稱", "成交股數") VALUES (?, ?, ?, ?)',
        [
            ("2024-05-08", "2330", "台積電", 1000),
            ("2024-05-10", "2330", "台積電", 2000),
        ],
    )
    price_dao.conn.commit()
    price_dao.close()
    work: Path = tmp_path / "work"
    base_argv: List[str] = [
        "manual_tick_history_import",
        "--source-dir",
        str(source),
        "--work-dir",
        str(work),
        "--schema",
        dao.schema,
        "--price-db",
        str(price_db),
    ]

    def run(*extra: str) -> None:
        monkeypatch.setattr(sys, "argv", [*base_argv, *extra])
        importer.main()

    run()  # 列計畫
    assert _load_log(dao) == []

    run("--apply")

    assert _load_log(dao) == [
        ("1563", datetime.date(2024, 5, 8), 1, 0, "1563.csv"),
        ("2330", datetime.date(2024, 5, 8), 2, 1, "2330.csv"),
        ("2330", datetime.date(2024, 5, 10), 1, 1, "2330.csv"),
    ]
    chunks = dao.get_chunk_ranges()
    assert [(start.date(), compressed) for start, _, compressed in chunks] == [
        (datetime.date(2024, 5, 2), True),
        (datetime.date(2024, 5, 9), True),
    ]
    assert not [p for p in work.iterdir() if p.is_dir()]
    assert dao.is_compression_policy_scheduled()

    run("--apply", "--resume")
    assert len(_stored(dao)) == 2

    with dao.conn.transaction():
        dao.conn.execute(
            f'DELETE FROM "{dao.schema}".stock_tick WHERE time >= %s',
            (datetime.datetime(2024, 5, 10),),
        )
    with pytest.raises(SystemExit) as excinfo:
        run("--verify-only")
    assert excinfo.value.code == 1
