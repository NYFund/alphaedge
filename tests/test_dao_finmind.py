import sqlite3
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO
from core.pipeline.utils import FinMindDataType
from core.pipeline.utils.exceptions import DataLoadError

"""
FinMind 四表 DAO

1. `commit=False` 真的延後提交：舊版 `to_sql` 寫完就自行 commit，參數形同虛設
2. 寫到一半失敗只回滾本批，同一交易內先前的批次不受影響
3. 股票／券商清單表不存在回空、其他查詢錯誤往外拋（舊版一律吞掉回空清單）
4. updater 與 loader 共用同一條連線；部分失敗時其他已成功的寫入仍會落地

不連網路、不碰正式的 `tw_stock.db`。
"""


def make_broker_rows(stock_id: str, trader_id: str, dates: List[str]) -> pd.DataFrame:
    """組出券商分點統計的 DataFrame"""

    return pd.DataFrame(
        [
            {
                "securities_trader": f"券商{trader_id}",
                "securities_trader_id": trader_id,
                "stock_id": stock_id,
                "date": date,
                "buy_volume": 100,
                "sell_volume": 50,
                "buy_price": 500.0,
                "sell_price": 501.0,
            }
            for date in dates
        ]
    )


def count_rows(db_path: Path, table: str) -> int:
    """以另一條連線數列數：只看得到已 commit 的資料"""

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    count: int = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


@pytest.fixture
def db(tmp_path: Path):
    """已建好券商分點表的暫存檔案 DB，回傳 (conn, db_path)"""

    db_path: Path = tmp_path / "tw_stock.db"
    conn: sqlite3.Connection = sqlite3.connect(db_path)
    BrokerTradingDAO(conn=conn).ensure_table()
    yield conn, db_path
    conn.close()


# === 券商分點：提交時點與回滾 ===
def test_commit_false_really_defers_commit(db) -> None:
    """`commit=False` 時其他連線看不到，呼叫端 commit 之後才看得到"""

    from core.pipeline.tw.loaders.finmind import broker_trading_loader

    conn, db_path = db
    inserted: int = broker_trading_loader.load_from_dataframe(
        conn,
        make_broker_rows("2330", "1020", ["2024-01-02", "2024-01-03"]),
        commit=False,
    )

    assert inserted == 2
    assert count_rows(db_path, BrokerTradingDAO.TABLE_NAME) == 0

    conn.commit()
    assert count_rows(db_path, BrokerTradingDAO.TABLE_NAME) == 2


def test_failed_batch_does_not_discard_earlier_uncommitted_batches(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    第二批寫到一半失敗：只回滾第二批，第一批（尚未 commit）照樣留著

    批次更新每 50 個組合才 commit 一次；少了 savepoint，一個壞組合就會連帶
    讓前面幾十個組合的寫入跟著消失，或是半份寫入被下一次 commit 帶進去。
    """

    from core.pipeline.tw.loaders.finmind import broker_trading_loader

    conn, db_path = db
    broker_trading_loader.load_from_dataframe(
        conn, make_broker_rows("2330", "1020", ["2024-01-02"]), commit=False
    )

    original_insert = BrokerTradingDAO.insert_or_ignore

    def insert_then_fail(self: BrokerTradingDAO, df: pd.DataFrame):
        original_insert(self, df)
        raise OSError("disk I/O error")

    monkeypatch.setattr(BrokerTradingDAO, "insert_or_ignore", insert_then_fail)

    with pytest.raises(DataLoadError):
        broker_trading_loader.load_from_dataframe(
            conn, make_broker_rows("2317", "1020", ["2024-01-02"]), commit=False
        )

    conn.commit()
    rows = conn.execute(
        f"SELECT stock_id FROM {BrokerTradingDAO.TABLE_NAME}"
    ).fetchall()
    assert rows == [("2330",)]


def test_csv_path_commits_good_files_before_reporting_failure(tmp_path: Path) -> None:
    """CSV 路徑有壞檔時拋 `DataLoadError`，但其他檔案已寫入的資料必須落地"""

    from core.pipeline.tw.loaders.finmind import broker_trading_loader

    db_path: Path = tmp_path / "tw_stock.db"
    conn: sqlite3.Connection = sqlite3.connect(db_path)
    BrokerTradingDAO(conn=conn).ensure_table()

    finmind_dir: Path = tmp_path / "finmind"
    broker_dir: Path = (
        finmind_dir / FinMindDataType.BROKER_TRADING.value.lower() / "1020"
    )
    broker_dir.mkdir(parents=True)
    make_broker_rows("2330", "1020", ["2024-01-02"]).to_csv(
        broker_dir / "2330.csv", index=False
    )
    pd.DataFrame([{"unexpected_column": 1}]).to_csv(
        broker_dir / "2317.csv", index=False
    )

    with pytest.raises(DataLoadError):
        broker_trading_loader.load_from_files(conn, finmind_dir)
    conn.close()

    assert count_rows(db_path, BrokerTradingDAO.TABLE_NAME) == 1


def test_date_ranges_and_index(db) -> None:
    """metadata 重建用的日期範圍，以及 `GROUP BY` 用的索引"""

    conn, _ = db
    dao: BrokerTradingDAO = BrokerTradingDAO(conn=conn)
    dao.insert_or_ignore(make_broker_rows("2330", "1020", ["2024-01-03", "2024-01-02"]))

    ranges: pd.DataFrame = dao.get_date_ranges_by_trader_stock()
    assert ranges.to_dict("records") == [
        {
            "securities_trader_id": "1020",
            "stock_id": "2330",
            "earliest_date": "2024-01-02",
            "latest_date": "2024-01-03",
        }
    ]

    indexes: List[str] = [
        row[1]
        for row in conn.execute(
            f"PRAGMA index_list('{BrokerTradingDAO.TABLE_NAME}')"
        ).fetchall()
    ]
    assert "idx_broker_trading_secid_stock_date" in indexes


# === 參考資料表 ===
def test_reference_table_failure_keeps_earlier_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    門面依序載入三張參考表：後一張壞掉時，前一張已寫入的資料不可消失

    舊版靠 `to_sql` 每張表各自 commit；改成 savepoint 之後若忘了逐表 commit，
    拋出的 `DataLoadError` 會讓門面走不到最後那次 commit。
    """

    from core.pipeline.tw.loaders.finmind_loader import FinMindLoader

    db_path: Path = tmp_path / "tw_stock.db"
    finmind_dir: Path = tmp_path / "finmind"
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.finmind_loader.FINMIND_DOWNLOADS_PATH", finmind_dir
    )
    stock_info_dir: Path = finmind_dir / FinMindDataType.STOCK_INFO.value.lower()
    warrant_dir: Path = (
        finmind_dir / FinMindDataType.STOCK_INFO_WITH_WARRANT.value.lower()
    )
    stock_info_dir.mkdir(parents=True)
    warrant_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "industry_category": "半導體業",
                "stock_id": "2330",
                "stock_name": "台積電",
                "type": "twse",
                "date": "2026-01-01",
            }
        ]
    ).to_csv(stock_info_dir / "taiwan_stock_info.csv", index=False)
    pd.DataFrame([{"unexpected_column": 1}]).to_csv(
        warrant_dir / "taiwan_stock_info_with_warrant.csv", index=False
    )

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    loader: FinMindLoader = FinMindLoader(conn=conn)
    loader.finmind_dir = finmind_dir

    with pytest.raises(DataLoadError):
        loader.add_to_db()

    assert loader.conn is conn, "共用連線不可被 loader 關掉"
    conn.close()

    assert count_rows(db_path, StockInfoDAO.TABLE_NAME) == 1


# === 股票／券商清單 ===
def make_context(conn: sqlite3.Connection):
    """只帶連線的 FinMindContext（清單查詢用不到 ETL 三件組）"""

    from core.pipeline.tw.updaters.finmind.common import FinMindContext

    return FinMindContext(crawler=None, cleaner=None, loader=None, conn=conn)


def test_lists_are_empty_when_tables_are_missing() -> None:
    """尚未跑過 stock_info／broker_info 是正常狀態：回空清單，不拋錯"""

    context = make_context(sqlite3.connect(":memory:"))

    assert context.get_stock_list() == []
    assert context.get_securities_trader_list() == []


def test_list_query_errors_are_raised() -> None:
    """
    查清單失敗要往外拋

    舊版 `except Exception: return []`，「DB 被鎖住、欄位打錯」會變成
    「沒有股票，請先更新 stock info」，整段券商分點回補一筆都沒跑。
    """

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    # 兩張表都故意缺主鍵欄
    conn.execute(f"CREATE TABLE {StockInfoDAO.TABLE_NAME} (stock_name TEXT)")
    conn.execute(f"CREATE TABLE {SecuritiesTraderInfoDAO.TABLE_NAME} (phone TEXT)")
    context = make_context(conn)

    with pytest.raises(pd.errors.DatabaseError):
        context.get_stock_list()
    with pytest.raises(pd.errors.DatabaseError):
        context.get_securities_trader_list()


# === updater／loader 共用連線 ===
def test_updater_and_loader_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater、loader 與 context 是同一條連線，`close()` 後一併關閉"""

    from core.pipeline.tw.crawlers.finmind_crawler import FinMindCrawler

    monkeypatch.setattr(FinMindCrawler, "setup", lambda self, *a, **k: None)
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.finmind_updater.TW_STOCK_DB_PATH",
        tmp_path / "tw_stock.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.finmind_updater.BROKER_TRADING_METADATA_PATH",
        tmp_path / "broker_trading_metadata.json",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.finmind_loader.FINMIND_DOWNLOADS_PATH",
        tmp_path / "finmind",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.finmind_cleaner.FINMIND_DOWNLOADS_PATH",
        tmp_path / "finmind",
    )

    from core.pipeline.tw.updaters.finmind_updater import FinMindUpdater

    updater: FinMindUpdater = FinMindUpdater()

    assert updater.loader.conn is updater.conn
    assert updater.context.conn is updater.conn
    assert updater.metadata.conn is updater.conn

    updater.close()
    assert updater.conn is None
