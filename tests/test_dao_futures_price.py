import datetime
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator, List, Set, Tuple

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.tw.loaders.futures_price_loader import FuturesPriceLoader
from core.pipeline.tw.updaters.futures_price_updater import FuturesPriceUpdater
from core.pipeline.utils.exceptions import DataLoadError

"""
台期貨每日行情（`futures_price_daily` 表）DAO

1. 續跑起點與交易日查詢：表不存在回空、其他查詢錯誤往外拋（舊版吞掉 `sqlite3.Error`）
2. 補行交易日改走唯讀的 `StockPriceDAO`，由 updater 持有並關閉（舊版 `with` 只 commit 不關）
3. loader／updater 共用同一個 DAO；壞檔整檔回滾

不連網路、不碰正式的 `tw_futures.db`／`tw_stock.db`。
"""

PRICE_COLUMNS: List[str] = [
    "date",
    "product",
    "expiry",
    "session",
    "開盤價",
    "最高價",
    "最低價",
    "收盤價",
    "成交量",
    "結算價",
    "未沖銷契約量",
    "最後最佳買價",
    "最後最佳賣價",
]


def make_rows(date: str, product: str = "TX", expiry: str = "202609") -> pd.DataFrame:
    """一筆日盤、一筆夜盤（夜盤沒有結算價與未沖銷契約量）"""

    return pd.DataFrame(
        [
            [date, product, expiry, "day", 1.0, 2.0, 0.5, 1.5, 10, 1.5, 100, 1.4, 1.6],
            [
                date,
                product,
                expiry,
                "night",
                1.0,
                2.0,
                0.5,
                1.5,
                5,
                None,
                None,
                1.4,
                1.6,
            ],
        ],
        columns=PRICE_COLUMNS,
    )


@pytest.fixture
def dao() -> FuturesPriceDAO:
    """已建表、內含兩天 TX 行情的記憶體 DAO"""

    price_dao: FuturesPriceDAO = FuturesPriceDAO(conn=sqlite3.connect(":memory:"))
    price_dao.ensure_table()
    price_dao.insert_or_ignore(make_rows("2026-08-27"))
    price_dao.insert_or_ignore(make_rows("2026-08-28"))
    price_dao.commit()
    return price_dao


# === DAO ===
def test_session_filter_and_summary(dao: FuturesPriceDAO) -> None:
    """session 為 None 時不過濾；摘要回（列數, 最早, 最晚）"""

    day: datetime.date = datetime.date(2026, 8, 27)

    assert len(dao.get_by_date(day, product="TX", session="day")) == 1
    assert len(dao.get_by_date(day, product="TX", session=None)) == 2
    assert dao.get_product_summary("TX") == (4, "2026-08-27", "2026-08-28")
    assert dao.get_product_summary("MTX") is None
    assert dao.get_latest_date_by_product("TX") == "2026-08-28"
    assert dao.get_latest_date_by_product("MTX") is None


def test_missing_table_is_not_an_error() -> None:
    """尚未跑過 `--target futures_price` 是全新環境的正常狀態"""

    price_dao: FuturesPriceDAO = FuturesPriceDAO(conn=sqlite3.connect(":memory:"))

    assert (
        price_dao.get_trading_days(
            datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
        )
        == []
    )
    assert price_dao.get_latest_date_by_product("TX") is None
    assert price_dao.get_product_summary("TX") is None


def test_trading_days_query_error_is_raised() -> None:
    """表存在但查詢出錯時往外拋，不可當成「沒有交易日」"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    # 故意缺 date 欄
    conn.execute(f"CREATE TABLE {FuturesPriceDAO.TABLE_NAME} (product TEXT)")

    with pytest.raises(sqlite3.OperationalError):
        FuturesPriceDAO(conn=conn).get_trading_days(
            datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
        )


# === updater ===
@pytest.fixture
def updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FuturesPriceUpdater]:
    """DB 與 downloads 都指向暫存區的 updater"""

    import core.pipeline.tw.loaders.futures_price_loader as loader_module
    import core.pipeline.tw.updaters.futures_price_updater as updater_module

    monkeypatch.setattr(
        updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", tmp_path / "tw_stock.db")
    monkeypatch.setattr(
        loader_module, "FUTURES_PRICE_DOWNLOADS_PATH", tmp_path / "price"
    )

    futures_updater: FuturesPriceUpdater = updater_module.FuturesPriceUpdater()
    yield futures_updater
    futures_updater.close()


def test_start_date_query_error_is_raised(updater: FuturesPriceUpdater) -> None:
    """
    查續跑起點失敗時往外拋

    舊版吞掉 `sqlite3.Error` 後改用預設起日：「DB 被鎖住」會讓該商品從 1998 年
    靜默重跑整段回補，數千次請求。
    """

    updater.dao.conn.execute(f"DROP TABLE {FuturesPriceDAO.TABLE_NAME}")
    # 故意缺 date 欄
    updater.dao.conn.execute(
        f"CREATE TABLE {FuturesPriceDAO.TABLE_NAME} (product TEXT)"
    )

    with pytest.raises(sqlite3.OperationalError):
        updater.get_actual_update_start_date("TX", datetime.date(1998, 7, 21))


def test_updater_and_loader_share_one_connection(updater: FuturesPriceUpdater) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_traded_weekends_without_stock_db(
    updater: FuturesPriceUpdater, tmp_path: Path
) -> None:
    """只跑期貨的環境沒有 tw_stock.db：跳過週末，且不可替它建出一個空檔案"""

    weekends: Set[datetime.date] = updater.get_traded_weekend_dates(
        datetime.date(2017, 6, 1), datetime.date(2017, 6, 4)
    )

    assert weekends == set()
    assert not (tmp_path / "tw_stock.db").exists()


def test_traded_weekends_come_from_read_only_price_dao(
    updater: FuturesPriceUpdater, tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """補行交易日取自 `price` 表的週末；連線唯讀且由 updater 的 `close()` 關閉"""

    stock_conn: sqlite3.Connection = sqlite3.connect(tmp_path / "tw_stock.db")
    dao_factory(
        StockPriceDAO,
        records=[
            {"date": "2017-06-02", "stock_id": "2330"},
            {"date": "2017-06-03", "stock_id": "2330"},
        ],
        conn=stock_conn,
    )
    stock_conn.close()

    weekends: Set[datetime.date] = updater.get_traded_weekend_dates(
        datetime.date(2017, 6, 1), datetime.date(2017, 6, 4)
    )

    assert weekends == {datetime.date(2017, 6, 3)}

    stock_price_dao: StockPriceDAO = updater.stock_price_dao
    with pytest.raises(sqlite3.OperationalError):
        stock_price_dao.conn.execute("DELETE FROM price")

    updater.close()
    assert updater.stock_price_dao is None
    assert stock_price_dao.conn is None


# === loader ===
def test_loader_failed_file_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫到一半出錯的檔案不可留下任何列，其他檔案照常入庫並拋 `DataLoadError`"""

    import core.pipeline.tw.loaders.futures_price_loader as loader_module

    downloads: Path = tmp_path / "price"
    downloads.mkdir()
    monkeypatch.setattr(loader_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db")
    monkeypatch.setattr(loader_module, "FUTURES_PRICE_DOWNLOADS_PATH", downloads)
    make_rows("2026-08-27").to_csv(
        downloads / "futures_price_20260827.csv", index=False
    )
    make_rows("2026-08-28").to_csv(
        downloads / "futures_price_20260828.csv", index=False
    )

    original_insert: Callable[..., int] = FuturesPriceDAO.insert_or_ignore

    def insert_then_fail(self: FuturesPriceDAO, df: pd.DataFrame) -> int:
        """照常寫入後，遇到 2026-08-28 的檔案就模擬寫到一半失敗"""

        result: int = original_insert(self, df)
        if (df["date"] == "2026-08-28").any():
            raise OSError("disk I/O error")
        return result

    monkeypatch.setattr(FuturesPriceDAO, "insert_or_ignore", insert_then_fail)

    loader: FuturesPriceLoader = loader_module.FuturesPriceLoader()
    loader.futures_price_dir = downloads

    with pytest.raises(DataLoadError):
        loader.add_to_db()

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "tw_futures.db")
    dates: List[Tuple[Any, ...]] = conn.execute(
        f"SELECT DISTINCT date FROM {FuturesPriceDAO.TABLE_NAME}"
    ).fetchall()
    conn.close()

    assert dates == [("2026-08-27",)]
