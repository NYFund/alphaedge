import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import pytest

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.config import FUTURES_PRICE_DAILY_TABLE_NAME
from core.pipeline.tw.loaders.futures_price_loader import FuturesPriceLoader
from core.utils import FuturesSession

"""
台期貨行情 API 的查詢測試

**本 API 與 `StockPriceAPI` 最大的差別是「一天不只一列」**：同一天同一商品有
多個到期月在交易，日盤與夜盤又是兩筆獨立行情。本檔把三件事釘住——
1. 單日查詢會回傳**當日所有到期月**（不是只回近月，也不是只回一列）；
2. 預設只取日盤，夜盤要明講；
3. 夜盤的結算價與未沖銷契約量是 NULL，不可被補成 0。

建表走真正的 loader（schema 只有一處宣告），資料則以固定 fixture 直接寫入，
不連網路、不碰正式的 tw_futures.db。
"""

DATE_1: datetime.date = datetime.date(2026, 8, 26)
DATE_2: datetime.date = datetime.date(2026, 8, 27)

# (date, product, expiry, session, 開, 高, 低, 收, 量, 結算價, 未沖銷, 買, 賣)
ROWS: List[Tuple] = [
    # 2026-08-26 日盤：近月與次月兩個合約
    (
        DATE_1,
        "TX",
        "202609",
        "day",
        46000,
        46500,
        45900,
        46100,
        50000,
        46090,
        100000,
        46095,
        46105,
    ),
    (
        DATE_1,
        "TX",
        "202610",
        "day",
        45900,
        46400,
        45800,
        46000,
        3000,
        45990,
        8000,
        45995,
        46005,
    ),
    # 2026-08-26 夜盤：只有近月有量，且結算價／未沖銷為 NULL
    (
        DATE_1,
        "TX",
        "202609",
        "night",
        46100,
        46200,
        45800,
        46050,
        26000,
        None,
        None,
        46045,
        46055,
    ),
    # 2026-08-27 日盤：同樣兩個合約
    (
        DATE_2,
        "TX",
        "202609",
        "day",
        46100,
        46600,
        46000,
        46500,
        48000,
        46490,
        101000,
        46495,
        46505,
    ),
    (
        DATE_2,
        "TX",
        "202610",
        "day",
        46000,
        46500,
        45900,
        46400,
        2500,
        46390,
        8200,
        46395,
        46405,
    ),
]


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesPriceAPI:
    """建好表並塞入 fixture 資料，API 指向同一個暫存 DB"""

    db_path: Path = tmp_path / "tw_futures.db"
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_price_loader.TW_FUTURES_DB_PATH", db_path
    )
    # 建表走 loader，避免測試自己抄一份 schema
    loader: FuturesPriceLoader = FuturesPriceLoader()
    loader.futures_price_dir = tmp_path / "price"
    loader.futures_price_dir.mkdir(parents=True, exist_ok=True)
    loader.connect()
    loader.create_missing_tables()

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    conn.executemany(
        f"INSERT INTO {FUTURES_PRICE_DAILY_TABLE_NAME} "
        f'("date", product, expiry, session, 開盤價, 最高價, 最低價, 收盤價, '
        f"成交量, 結算價, 未沖銷契約量, 最後最佳買價, 最後最佳賣價) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(str(r[0]), *r[1:]) for r in ROWS],
    )
    conn.commit()
    conn.close()

    return FuturesPriceAPI(conn=sqlite3.connect(db_path))


# === 一天不只一列 ===
def test_get_returns_every_expiry_of_the_day(api: FuturesPriceAPI) -> None:
    """單日查詢回傳當日所有到期月，不是只回近月"""

    df: pd.DataFrame = api.get(DATE_1, product="TX")

    assert list(df["expiry"]) == ["202609", "202610"]


def test_get_defaults_to_day_session(api: FuturesPriceAPI) -> None:
    """
    預設只取日盤

    日盤是一般交易時段，「這一天的行情」通常指它；夜盤是另一段獨立行情，
    混在一起會讓同一個 (product, expiry) 出現兩列而下游取值取到哪一筆全看運氣。
    """

    df: pd.DataFrame = api.get(DATE_1, product="TX")

    assert set(df["session"]) == {"day"}
    assert len(df) == 2


def test_session_none_returns_both_sessions(api: FuturesPriceAPI) -> None:
    """`session=None` 是「我知道要處理兩筆」的明確表態"""

    df: pd.DataFrame = api.get(DATE_1, product="TX", session=None)

    assert len(df) == 3
    assert set(df["session"]) == {"day", "night"}


def test_night_session_can_be_queried_alone(api: FuturesPriceAPI) -> None:
    """夜盤要明講才拿得到"""

    df: pd.DataFrame = api.get(DATE_1, product="TX", session=FuturesSession.NIGHT)

    assert list(df["expiry"]) == ["202609"]


# === 單一合約才是「一天一列」 ===
def test_contract_price_is_one_row_per_date(api: FuturesPriceAPI) -> None:
    """固定 (product, expiry, session) 之後，主鍵只剩 date"""

    df: pd.DataFrame = api.get_contract_price(
        product="TX", expiry="202609", start_date=DATE_1, end_date=DATE_2
    )

    assert list(df["date"]) == [str(DATE_1), str(DATE_2)]
    assert list(df["收盤價"]) == [46100, 46500]


def test_close_series_is_indexed_by_date(api: FuturesPriceAPI) -> None:
    """技術指標的共通輸入：index 為 `datetime.date`（不是字串）、值為 float 收盤價"""

    series: pd.Series = api.get_close_series(
        product="TX", expiry="202610", start_date=DATE_1, end_date=DATE_2
    )

    assert list(series.index) == [DATE_1, DATE_2]
    assert list(series) == [46000.0, 46400.0]
    assert series.name == "收盤價"
    assert series.dtype == float


def test_empty_result_when_start_after_end(api: FuturesPriceAPI) -> None:
    """區間顛倒時回傳空結果而不是查整張表"""

    assert api.get_range(DATE_2, DATE_1).empty
    assert api.get_contract_price("TX", "202609", DATE_2, DATE_1).empty


# === 交易日與合約清單 ===
def test_trading_days_ignores_session(api: FuturesPriceAPI) -> None:
    """夜盤成交的那一天同樣是交易日，且日期不重複"""

    assert api.get_trading_days(DATE_1, DATE_2) == [DATE_1, DATE_2]


def test_expiries_are_sorted_by_maturity(api: FuturesPriceAPI) -> None:
    """`YYYYMM` 的字典序即到期先後"""

    assert api.get_expiries(DATE_1, product="TX") == ["202609", "202610"]


def test_products_lists_what_is_actually_in_the_table(api: FuturesPriceAPI) -> None:
    """商品清單取自資料，不是設定檔——設定檔列了但沒回補的商品不該出現"""

    assert api.get_products() == ["TX"]


# === 具名查詢 ===
def test_close_map_is_keyed_by_expiry(api: FuturesPriceAPI) -> None:
    """期貨在固定商品之後的自然鍵是到期月，不是 stock_id"""

    assert api.get_close_map(DATE_1, product="TX") == {
        "202609": 46100,
        "202610": 46000,
    }


def test_volume_map_is_in_lots_of_contracts(api: FuturesPriceAPI) -> None:
    """成交量單位是「口」，不像股票要再除以 `Units.LOT`"""

    assert api.get_volume_map(DATE_1, product="TX") == {
        "202609": 50000,
        "202610": 3000,
    }


def test_night_settlement_stays_null(api: FuturesPriceAPI) -> None:
    """
    夜盤沒有結算價與未沖銷契約量，值必須維持 NULL

    補成 0 會讓「沒有結算價」與「結算價為 0」混為一談，而後者在計算保證金
    與逐日盯市時是完全不同的意思。
    """

    settlement = api.get_settlement_map(
        DATE_1, product="TX", session=FuturesSession.NIGHT
    )
    open_interest = api.get_open_interest_map(
        DATE_1, product="TX", session=FuturesSession.NIGHT
    )

    assert pd.isna(settlement["202609"])
    assert pd.isna(open_interest["202609"])


def test_day_settlement_has_values(api: FuturesPriceAPI) -> None:
    """日盤則有結算價，用來與夜盤的 NULL 對照"""

    assert api.get_settlement_map(DATE_1, product="TX") == {
        "202609": 46090,
        "202610": 45990,
    }


def test_maps_are_empty_when_no_data(api: FuturesPriceAPI) -> None:
    """查無資料時回傳空 dict，而不是拋錯或回傳半成品"""

    missing_date: datetime.date = datetime.date(2026, 8, 25)

    assert api.get_close_map(missing_date, product="TX") == {}
    assert api.get_expiries(missing_date, product="TX") == []


# === 商品過濾 ===
def test_product_none_returns_all_products(api: FuturesPriceAPI) -> None:
    """`product=None` 不過濾商品；目前表內只有 TX，行為仍須明確"""

    df: pd.DataFrame = api.get(DATE_1)

    assert set(df["product"]) == {"TX"}
    assert len(df) == 2


def test_unknown_product_returns_empty(api: FuturesPriceAPI) -> None:
    """沒有資料的商品回傳空表，不是回傳別的商品"""

    df: Optional[pd.DataFrame] = api.get(DATE_1, product="MTX")

    assert df.empty
