import datetime
import sqlite3
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.utils.exceptions import DataLoadError

"""
`price` 表 DAO 試點

1. DAO 本身：建表冪等、查詢區間邊界、表不存在的回傳約定
2. loader／updater 共用同一個 DAO：一次更新只開一條連線，loader 入庫後不可把它關掉
3. 單檔失敗整檔回滾：寫到一半出錯的檔案不可留下任何列

不連網路、不碰正式的 `tw_stock.db`。
"""

PRICE_COLUMNS: List[str] = [
    "date",
    "stock_id",
    "證券名稱",
    "開盤價",
    "最高價",
    "最低價",
    "收盤價",
    "漲跌價差",
    "成交股數",
    "成交金額",
    "成交筆數",
    "最後揭示買價",
    "最後揭示買量",
    "最後揭示賣價",
    "最後揭示賣量",
    "本益比",
]


def make_price_rows(
    date: str, stock_ids: List[str], close: float = 10.0
) -> pd.DataFrame:
    """建立多列最小可用的 price 資料"""

    return pd.DataFrame(
        [[date, sid, f"股票{sid}"] + [close] * 13 for sid in stock_ids],
        columns=PRICE_COLUMNS,
    )


@pytest.fixture
def dao() -> StockPriceDAO:
    """已建表、內含三天資料的記憶體 DAO"""

    price_dao: StockPriceDAO = StockPriceDAO(conn=sqlite3.connect(":memory:"))
    price_dao.ensure_table()
    price_dao.insert_or_ignore(make_price_rows("2024-01-02", ["2330", "2317"], 100.0))
    price_dao.insert_or_ignore(make_price_rows("2024-01-03", ["2330"], 101.0))
    price_dao.insert_or_ignore(make_price_rows("2024-01-05", ["2330", "2317"], 102.0))
    price_dao.commit()
    return price_dao


# === DAO ===
def test_ensure_table_is_idempotent_and_creates_index() -> None:
    """重複建表不報錯，且 `(stock_id, date)` 索引存在"""

    price_dao: StockPriceDAO = StockPriceDAO(conn=sqlite3.connect(":memory:"))
    price_dao.ensure_table()
    price_dao.ensure_table()

    indexes: List[str] = [
        row[1]
        for row in price_dao.conn.execute("PRAGMA index_list('price')").fetchall()
    ]
    assert "idx_price_stock_id_date" in indexes


def test_date_queries_include_both_ends(dao: StockPriceDAO) -> None:
    """`BETWEEN` 兩端皆含；區間顛倒回空表"""

    assert len(dao.get_by_date(datetime.date(2024, 1, 2))) == 2
    assert len(dao.get_range(datetime.date(2024, 1, 2), datetime.date(2024, 1, 3))) == 3
    assert (
        len(
            dao.get_by_stock(
                "2317", datetime.date(2024, 1, 2), datetime.date(2024, 1, 5)
            )
        )
        == 2
    )
    assert dao.get_range(datetime.date(2024, 1, 5), datetime.date(2024, 1, 2)).empty


def test_trading_days_come_from_price_rows(dao: StockPriceDAO) -> None:
    """有日 K 的日期才是交易日（01-04 沒有資料就不是）"""

    assert dao.get_trading_days(
        datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    ) == [
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        datetime.date(2024, 1, 5),
    ]


def test_close_prices_filter_by_start_date(dao: StockPriceDAO) -> None:
    """收盤價查詢只回三欄，`start_date` 含當日"""

    everything: pd.DataFrame = dao.get_close_prices()
    since: pd.DataFrame = dao.get_close_prices(datetime.date(2024, 1, 3))

    assert list(everything.columns) == ["date", "stock_id", "收盤價"]
    assert len(everything) == 5
    assert set(since["date"]) == {"2024-01-03", "2024-01-05"}


def test_latest_date_is_none_without_table() -> None:
    """表不存在時回 None（初次更新的正常狀態）"""

    price_dao: StockPriceDAO = StockPriceDAO(conn=sqlite3.connect(":memory:"))
    assert price_dao.get_latest_date() is None

    price_dao.ensure_table()
    price_dao.insert_or_ignore(make_price_rows("2024-01-02", ["2330"]))
    assert price_dao.get_latest_date() == "2024-01-02"


# === loader／updater 共用連線 ===
def make_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 price 的 downloads 目錄改到暫存區，回傳該目錄"""

    import core.pipeline.tw.cleaners.stock_price_cleaner as cleaner_module
    import core.pipeline.tw.loaders.stock_price_loader as loader_module

    downloads: Path = tmp_path / "price"
    downloads.mkdir(exist_ok=True)
    monkeypatch.setattr(loader_module, "PRICE_DOWNLOADS_PATH", downloads)
    monkeypatch.setattr(cleaner_module, "PRICE_DOWNLOADS_PATH", downloads)
    return downloads


def test_updater_and_loader_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    updater 與其 loader 必須是同一條連線

    舊版各開一條連線到同一個 DB、updater 那條從不關閉；FinMind 線甚至得先 commit
    loader 的連線才能避開寫入鎖。
    """

    import core.pipeline.tw.updaters.stock_price_updater as updater_module

    make_downloads(tmp_path, monkeypatch)
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))

    updater = updater_module.StockPriceUpdater()

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_loader_keeps_shared_dao_open_after_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共用 DAO 下入庫完成不可關連線：updater 接著還要用它查最新日期"""

    from core.pipeline.tw.loaders.stock_price_loader import StockPriceLoader

    downloads: Path = make_downloads(tmp_path, monkeypatch)
    make_price_rows("2024-01-02", ["2330"]).to_csv(
        downloads / "twse_20240102.csv", index=False
    )

    price_dao: StockPriceDAO = StockPriceDAO(db_path=tmp_path / "test.db")
    loader: StockPriceLoader = StockPriceLoader(dao=price_dao)
    loader.price_dir = downloads

    loader.add_to_db()

    assert price_dao.conn is not None
    assert price_dao.get_latest_date() == "2024-01-02"
    price_dao.close()


def test_failed_file_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    寫到一半出錯的檔案不可留下任何列，其他檔案照常入庫並拋 `DataLoadError`

    以替身模擬「寫進一部分之後才失敗」（例如磁碟 I/O 錯誤）：先真的寫入，再拋出。
    """

    import core.pipeline.tw.loaders.stock_price_loader as loader_module

    downloads: Path = make_downloads(tmp_path, monkeypatch)
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    make_price_rows("2024-01-02", ["2330", "2317"]).to_csv(
        downloads / "twse_20240102.csv", index=False
    )
    make_price_rows("2024-01-03", ["2330", "2317"]).to_csv(
        downloads / "twse_20240103.csv", index=False
    )

    original_insert = StockPriceDAO.insert_or_ignore

    def insert_then_fail(self: StockPriceDAO, df: pd.DataFrame):
        result = original_insert(self, df)
        if (df["date"] == "2024-01-03").any():
            raise OSError("disk I/O error")
        return result

    monkeypatch.setattr(StockPriceDAO, "insert_or_ignore", insert_then_fail)

    loader = loader_module.StockPriceLoader()
    loader.price_dir = downloads

    with pytest.raises(DataLoadError) as exc_info:
        loader.add_to_db()

    assert exc_info.value.failed_files == ["twse_20240103.csv"]

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dates: List[str] = [
        row[0] for row in conn.execute("SELECT DISTINCT date FROM price").fetchall()
    ]
    conn.close()

    assert dates == ["2024-01-02"]
