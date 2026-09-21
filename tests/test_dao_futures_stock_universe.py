import datetime
import sqlite3
from pathlib import Path
from typing import Callable, Iterator, List

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.tw.updaters.futures_price_updater import FuturesPriceUpdater
from core.pipeline.tw.updaters.futures_stock_universe_updater import (
    FuturesStockUniverseUpdater,
)

"""
股期標的池（`futures_stock_universe` 表）DAO

1. 快照日只有一份實作：`<=`／`<`／退回最早一份
2. `get_contract_size` 兩種查法：回測乘數看「該日全表快照」、保證金試算看「該商品自己的快照」
3. updater 與 loader 共用同一條連線；現股代號比對以唯讀開 tw_stock.db、不建出空檔

不連網路、不碰正式的 `tw_futures.db`／`tw_stock.db`。
"""

UNIVERSE_COLUMNS: List[str] = [
    "snapshot_date",
    "product_id",
    "base_code",
    "product_type",
    "underlying_stock_id",
    "underlying_name",
    "underlying_listing_board",
    "contract_size",
    "day_session_time",
    "night_session_time",
]


def make_snapshot(snapshot_date: str, contracts: List[tuple]) -> pd.DataFrame:
    """contracts 為 (product_id, underlying_stock_id, contract_size)"""

    return pd.DataFrame(
        [
            [
                snapshot_date,
                product_id,
                product_id[:2],
                "個股期貨",
                stock_id,
                f"標的{stock_id}",
                "上市",
                size,
                "08:45~13:45",
                None,
            ]
            for product_id, stock_id, size in contracts
        ],
        columns=UNIVERSE_COLUMNS,
    )


@pytest.fixture
def dao() -> FuturesStockUniverseDAO:
    """
    兩份快照：CDF 在第二份被調整成 2,150 股；OLF 只出現在第一份（已下市）；
    NEF 只出現在第二份（新掛牌）
    """

    universe_dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
        conn=sqlite3.connect(":memory:")
    )
    universe_dao.ensure_table()
    universe_dao.insert_or_ignore(
        make_snapshot("2026-08-01", [("CDF", "2330", 2000), ("OLF", "1101", 2000)])
    )
    universe_dao.insert_or_ignore(
        make_snapshot("2026-08-29", [("CDF", "2330", 2150), ("NEF", "2603", 100)])
    )
    universe_dao.commit()
    return universe_dao


# === 快照日 ===
def test_snapshot_date_resolution(dao: FuturesStockUniverseDAO) -> None:
    """含當日、不含當日、早於第一份時退回最早一份"""

    assert dao.get_latest_snapshot_date() == "2026-08-29"
    assert dao.get_latest_snapshot_date(datetime.date(2026, 8, 29)) == "2026-08-29"
    assert (
        dao.get_latest_snapshot_date(datetime.date(2026, 8, 29), inclusive=False)
        == "2026-08-01"
    )
    assert dao.get_latest_snapshot_date(datetime.date(2020, 1, 1)) is None
    assert dao.resolve_snapshot_date(datetime.date(2020, 1, 1)) == "2026-08-01"
    assert dao.is_snapshot_loaded(datetime.date(2026, 8, 1))
    assert not dao.is_snapshot_loaded(datetime.date(2026, 8, 2))


def test_missing_table_returns_none() -> None:
    """尚未跑過標的池 ETL 是正常狀態"""

    universe_dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
        conn=sqlite3.connect(":memory:")
    )

    assert universe_dao.resolve_snapshot_date() is None
    assert not universe_dao.is_snapshot_loaded(datetime.date(2026, 8, 1))
    assert universe_dao.get_contract_size("CDF", per_product=True) is None


# === 契約單位的兩種查法 ===
def test_contract_size_by_snapshot_excludes_unlisted_products(
    dao: FuturesStockUniverseDAO,
) -> None:
    """回測乘數：該日適用的快照中沒有這個商品就回 None（那天不在列）"""

    after_adjustment: datetime.date = datetime.date(2026, 9, 1)

    assert dao.get_contract_size("CDF", datetime.date(2026, 8, 15)) == 2000
    assert dao.get_contract_size("CDF", after_adjustment) == 2150
    assert dao.get_contract_size("OLF", after_adjustment) is None
    assert dao.get_contract_size("NEF", datetime.date(2026, 8, 15)) is None


def test_contract_size_per_product_uses_its_own_snapshots(
    dao: FuturesStockUniverseDAO,
) -> None:
    """保證金試算：以該商品自己最近的快照為準，早於它第一份時退回最早一份"""

    assert (
        dao.get_contract_size("OLF", datetime.date(2026, 9, 1), per_product=True)
        == 2000
    )
    assert (
        dao.get_contract_size("NEF", datetime.date(2026, 8, 15), per_product=True)
        == 100
    )
    assert (
        dao.get_contract_size("CDF", datetime.date(2026, 9, 1), per_product=True)
        == 2150
    )
    assert (
        dao.get_contract_size("NOPE", datetime.date(2026, 9, 1), per_product=True)
        is None
    )


def test_margin_api_uses_per_product_contract_size(
    dao: FuturesStockUniverseDAO,
) -> None:
    """`FuturesMarginAPI.get_contract_size()` 走「該商品自己的快照」那一種"""

    from core.api.tw.futures_margin_api import FuturesMarginAPI

    api: FuturesMarginAPI = FuturesMarginAPI(conn=dao.conn)

    assert api.get_contract_size("OLF", datetime.date(2026, 9, 1)) == 2000


# === updater／loader ===
@pytest.fixture
def updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FuturesStockUniverseUpdater]:
    """DB 與 downloads 都指向暫存區的標的池 updater"""

    import core.pipeline.tw.loaders.futures_stock_universe_loader as loader_module
    import core.pipeline.tw.updaters.futures_stock_universe_updater as updater_module

    monkeypatch.setattr(
        updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", tmp_path / "tw_stock.db")
    monkeypatch.setattr(
        loader_module, "FUTURES_UNIVERSE_DOWNLOADS_PATH", tmp_path / "universe"
    )

    universe_updater: FuturesStockUniverseUpdater = (
        updater_module.FuturesStockUniverseUpdater()
    )
    yield universe_updater
    universe_updater.close()


def test_updater_and_loader_share_one_connection(
    updater: FuturesStockUniverseUpdater,
) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.dao.insert_or_ignore(make_snapshot("2026-08-01", [("CDF", "2330", 2000)]))
    assert updater.is_snapshot_loaded(datetime.date(2026, 8, 1))
    assert (
        updater.get_latest_snapshot_date(before=datetime.date(2026, 8, 2))
        == "2026-08-01"
    )
    assert updater.get_latest_snapshot_date(before=datetime.date(2026, 8, 1)) is None

    updater.close()
    assert updater.dao.conn is None


@pytest.mark.parametrize("stage", ["crawl", "clean"])
def test_empty_universe_fails_the_target(
    updater: FuturesStockUniverseUpdater,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """
    抓不到或洗完是空的都要拋 `DataLoadError`，讓 target 記成失敗

    以前只記 warning 就回傳，行程以成功結束，下游 `futures_stock_price`
    悄悄沿用舊快照，缺漏要事後對帳才發現。
    """

    from core.pipeline.utils.exceptions import DataLoadError

    raw: pd.DataFrame = pd.DataFrame({"x": [1]})
    monkeypatch.setattr(
        updater.crawler,
        "crawl_stock_universe",
        lambda: None if stage == "crawl" else raw,
    )
    monkeypatch.setattr(
        updater.cleaner, "clean_stock_universe", lambda df, date: pd.DataFrame()
    )

    with pytest.raises(DataLoadError):
        updater.update(snapshot_date=datetime.date(2026, 8, 3))


def test_underlying_match_without_stock_db(
    updater: FuturesStockUniverseUpdater, tmp_path: Path
) -> None:
    """只跑期貨的環境沒有 tw_stock.db：略過比對，且不替它建出空檔"""

    updater.log_underlying_match(make_snapshot("2026-08-01", [("CDF", "2330", 2000)]))

    assert not (tmp_path / "tw_stock.db").exists()


def test_underlying_match_reads_price_table(
    updater: FuturesStockUniverseUpdater,
    tmp_path: Path,
    captured_logs: List[str],
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """有 price 表時回報對得上的檔數"""

    stock_conn: sqlite3.Connection = sqlite3.connect(tmp_path / "tw_stock.db")
    dao_factory(
        StockPriceDAO,
        records=[{"date": "2026-08-01", "stock_id": "2330"}],
        conn=stock_conn,
    )
    stock_conn.close()

    updater.log_underlying_match(
        make_snapshot("2026-08-01", [("CDF", "2330", 2000), ("NYF", "0050", 10000)])
    )

    assert any("1/2 檔對得上" in message for message in captured_logs)


@pytest.fixture
def captured_logs() -> Iterator[List[str]]:
    """收集 loguru 訊息"""

    from loguru import logger

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda message: messages.append(message.record["message"])
    )
    yield messages
    logger.remove(sink_id)


def test_stock_futures_resolver_uses_the_updater_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """股期行情 updater 取商品清單時共用自己的 tw_futures.db 連線，不另開一條"""

    import core.pipeline.tw.loaders.futures_price_loader as price_loader_module
    import core.pipeline.tw.updaters.futures_price_updater as price_updater_module

    monkeypatch.setattr(
        price_updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(
        price_loader_module, "FUTURES_PRICE_DOWNLOADS_PATH", tmp_path / "price"
    )
    price_updater: FuturesPriceUpdater = price_updater_module.FuturesPriceUpdater()

    universe_dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
        conn=price_updater.conn
    )
    universe_dao.ensure_table()
    universe_dao.insert_or_ignore(
        make_snapshot("2026-08-01", [("CDF", "2330", 2000), ("NEF", "2603", 100)])
    )

    opened: List[object] = []
    monkeypatch.setattr(
        "core.dao.connection.sqlite3.connect",
        lambda *args, **kwargs: opened.append(args) or sqlite3.connect(":memory:"),
    )

    assert price_updater.resolve_stock_futures_products(
        None, datetime.date(2026, 8, 15)
    ) == ["CDF", "NEF"]
    assert opened == []
    price_updater.close()
