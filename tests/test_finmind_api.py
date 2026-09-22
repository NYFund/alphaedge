import datetime
import sqlite3
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from core.api.tw.finmind_api import FinMindAPI
from core.dao.base import BaseDAO
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
`FinMindAPI` 的離線覆蓋

這一檔在 2026-09-05 是**全 repo 唯一覆蓋率 0% 的 `core/api` 檔**——原有的
人工腳本（現在在 `scripts/manual/manual_finmind_api.py`）需要真的 `tw_stock.db`，
pytest 不會收集它。
本檔以 in-memory SQLite 灌樣本，不連任何實體資料庫。

一併驗連線注入：`__init__(conn=...)` 是其他 `core/api/tw/` 的共通慣例，
`FinMindAPI` 原本沒有，於是 DataFeed 想共用連線時它會自己再開一條。
"""


DAY_1: str = "2024-01-02"
DAY_2: str = "2024-01-03"


@pytest.fixture
def conn(
    dao_factory: Callable[..., BaseDAO], memory_conn: sqlite3.Connection
) -> sqlite3.Connection:
    """建立含四張 FinMind 資料表樣本的 in-memory SQLite"""

    dao_factory(
        StockInfoDAO,
        records=[
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "industry_category": "半導體業",
                "type": "twse",
                "date": DAY_1,
            },
            {
                "stock_id": "2317",
                "stock_name": "鴻海",
                "industry_category": "電子零組件業",
                "type": "twse",
                "date": DAY_1,
            },
        ],
    )

    dao_factory(
        StockInfoWithWarrantDAO,
        records=[
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "industry_category": "半導體業",
                "type": "twse",
                "date": DAY_1,
            },
            {
                "stock_id": "030001",
                "stock_name": "元大元大",
                "industry_category": "Warrant",
                "type": "twse",
                "date": DAY_1,
            },
        ],
    )

    dao_factory(
        SecuritiesTraderInfoDAO,
        records=[
            {
                "securities_trader_id": "9A00",
                "securities_trader": "永豐金證券",
                "date": DAY_1,
                "address": "台北市",
                "phone": "02-0000-0000",
            },
        ],
    )

    dao_factory(
        BrokerTradingDAO,
        records=[
            {
                "date": DAY_1,
                "stock_id": "2330",
                "securities_trader": "永豐金證券",
                "securities_trader_id": "9A00",
                "buy_volume": 1000,
                "sell_volume": 200,
            },
            {
                "date": DAY_2,
                "stock_id": "2330",
                "securities_trader": "永豐金證券",
                "securities_trader_id": "9A00",
                "buy_volume": 500,
                "sell_volume": 900,
            },
            {
                "date": DAY_1,
                "stock_id": "2317",
                "securities_trader": "元大證券",
                "securities_trader_id": "9800",
                "buy_volume": 300,
                "sell_volume": 100,
            },
        ],
    )

    return memory_conn


@pytest.fixture
def api(conn: sqlite3.Connection) -> FinMindAPI:
    """以注入的連線建立 API（不碰 `tw_stock.db`）"""

    return FinMindAPI(conn=conn)


# === 連線注入===
def test_injected_connection_is_used(conn: sqlite3.Connection) -> None:
    """傳進去的連線就是實際查詢用的那一條，不會另開一條連 tw_stock.db"""

    api: FinMindAPI = FinMindAPI(conn=conn)

    assert api.conn is conn
    assert api.owns_conn is False


def test_close_does_not_close_shared_connection(conn: sqlite3.Connection) -> None:
    """
    共用連線由建立者負責關閉

    `BaseDataAPI.close()` 靠 `owns_conn` 判斷；沒有這個屬性時它會**預設為
    True 而關掉別人的連線**，其他持有者接著就拿到已關閉的連線。
    """

    api: FinMindAPI = FinMindAPI(conn=conn)
    api.close()

    # 連線仍可用即代表沒有被關掉
    assert conn.execute("SELECT 1").fetchone() == (1,)


# === 台股總覽 ===
def test_get_stock_info(api: FinMindAPI) -> None:
    """單檔查得到；查無代號時回空表而不是拋錯"""

    df: pd.DataFrame = api.get_stock_info("2330")

    assert len(df) == 1
    assert df.iloc[0]["stock_name"] == "台積電"
    assert api.get_stock_info("9999").empty


def test_get_all_stock_info_excludes_warrant(api: FinMindAPI) -> None:
    """兩張總覽表是不同來源：不含權證那張沒有權證代號"""

    plain: pd.DataFrame = api.get_all_stock_info()
    with_warrant: pd.DataFrame = api.get_all_stock_info_with_warrant()

    assert set(plain["stock_id"]) == {"2330", "2317"}
    assert "030001" in set(with_warrant["stock_id"])


def test_get_stock_info_with_warrant(api: FinMindAPI) -> None:
    """含權證那張查得到權證代號"""

    df: pd.DataFrame = api.get_stock_info_with_warrant("030001")

    assert len(df) == 1
    assert df.iloc[0]["industry_category"] == "Warrant"


# === 證券商資訊 ===
def test_get_broker_info(api: FinMindAPI) -> None:
    """依券商代號查得到；不存在的代號回空表"""

    df: pd.DataFrame = api.get_broker_info("9A00")

    assert len(df) == 1
    assert df.iloc[0]["securities_trader"] == "永豐金證券"
    assert api.get_broker_info("XXXX").empty


def test_get_all_broker_info(api: FinMindAPI) -> None:
    """全部券商資訊"""

    assert len(api.get_all_broker_info()) == 1


# === 券商分點日報 ===
def test_get_broker_trading_by_date(api: FinMindAPI) -> None:
    """單日全市場：同一天的兩檔都要回來"""

    df: pd.DataFrame = api.get_broker_trading_by_date(datetime.date(2024, 1, 2))

    assert len(df) == 2
    assert set(df["stock_id"]) == {"2330", "2317"}


def test_get_broker_trading_range(api: FinMindAPI) -> None:
    """區間取全部；起訖顛倒回空表，不是把兩個參數對調後照查"""

    df: pd.DataFrame = api.get_broker_trading_range(
        datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert len(df) == 3
    assert api.get_broker_trading_range(
        datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
    ).empty


def test_get_broker_trading_for_stock(api: FinMindAPI) -> None:
    """單檔單日與單檔區間"""

    on_date: pd.DataFrame = api.get_broker_trading_for_stock_on_date(
        "2330", datetime.date(2024, 1, 2)
    )
    in_range: pd.DataFrame = api.get_broker_trading_for_stock_in_range(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert len(on_date) == 1
    assert on_date.iloc[0]["buy_volume"] == 1000
    assert len(in_range) == 2
    assert api.get_broker_trading_for_stock_in_range(
        "2330", datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
    ).empty


def test_get_broker_trading_by_broker_and_date(api: FinMindAPI) -> None:
    """依券商中文名稱查當日全部股票；名稱不符即回空表"""

    df: pd.DataFrame = api.get_broker_trading_by_broker_and_date(
        "永豐金證券", datetime.date(2024, 1, 2)
    )

    assert len(df) == 1
    assert df.iloc[0]["stock_id"] == "2330"
    assert api.get_broker_trading_by_broker_and_date(
        "不存在證券", datetime.date(2024, 1, 2)
    ).empty
