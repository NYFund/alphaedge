import datetime
import inspect
import sqlite3
from typing import Any, Dict, List

import pandas as pd
import pytest

from core.api.base import BaseDataAPI
from core.api.tw.futures_chip_api import FuturesChipAPI
from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_margin_api import StockMarginAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.dao.tw.stock_margin_dao import StockMarginDAO

"""
盤後才公布的資料在回測裡的可知時點：站在某一天早上只能知道前一次公布的那份

融券餘額原本在 bar 開始時取**當日**的「融券今日餘額」做券源檢核，但那份資料收盤後才公布；
期貨籌碜早就在 API 層強制「嚴格早於當日」，股票籌碼與融券卻靠呼叫端自己記得。
現在由 `BaseDataAPI.PUBLISHED_AFTER_CLOSE` 宣告、`get_available()` 統一提供。
"""

T1: datetime.date = datetime.date(2024, 1, 2)
T2: datetime.date = datetime.date(2024, 1, 3)

# 宣告為盤後公布的資料集；新增一個就要同時想清楚它的 `get_available()` 語意
PUBLISHED_AFTER_CLOSE_APIS: List[type] = [StockChipAPI, StockMarginAPI, FuturesChipAPI]


def margin_row(
    date: datetime.date, stock_id: str, short_balance: int
) -> Dict[str, Any]:
    """一列 `margin` 表資料；只有融券今日餘額有意義，其餘欄位補 0"""

    return {
        "date": date.isoformat(),
        "stock_id": stock_id,
        "證券名稱": "測試",
        "融資買進": 0,
        "融資賣出": 0,
        "融資現金償還": 0,
        "融資前日餘額": 0,
        "融資今日餘額": 1000,
        "融資限額": 0,
        "融券買進": 0,
        "融券賣出": 0,
        "融券現券償還": 0,
        "融券前日餘額": 0,
        "融券今日餘額": short_balance,
        "融券限額": 0,
        "資券互抵": 0,
        "券資比": 0.0,
        "註記": None,
    }


@pytest.fixture
def margin_api() -> StockMarginAPI:
    """記憶體內的 `margin` 表：2330 在 T1 公布 100 張、T2 公布 50 張"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    dao: StockMarginDAO = StockMarginDAO(conn=conn)
    dao.ensure_table()
    dao.insert_or_replace(
        pd.DataFrame([margin_row(T1, "2330", 100), margin_row(T2, "2330", 50)])
    )
    conn.commit()
    return StockMarginAPI(conn=conn)


def test_available_balance_is_the_previous_publication(
    margin_api: StockMarginAPI,
) -> None:
    """T2 早上能知道的是 T1 公布的 100 張；當天公布的 50 張要收盤後才看得到"""

    assert margin_api.get_available_short_balance_map(T2) == {"2330": 100}
    assert margin_api.get_short_balance_map(T2) == {"2330": 50}
    assert margin_api.get_latest_available_date(T2) == T1.isoformat()


def test_no_earlier_publication_means_no_data_not_zero(
    margin_api: StockMarginAPI,
) -> None:
    """第一天之前沒有任何公布：回空 dict（跳過檢核），不可回 0（借不到券）"""

    assert margin_api.get_available_short_balance_map(T1) == {}
    assert margin_api.get_latest_available_date(T1) is None
    assert margin_api.get_available(T1).empty


def test_generic_get_available_returns_the_previous_rows(
    margin_api: StockMarginAPI,
) -> None:
    """基底的 `get_available()` 回前一次公布的整張表"""

    available: pd.DataFrame = margin_api.get_available(T2)

    assert list(available["date"]) == [T1.isoformat()]
    assert int(available["融券今日餘額"].iloc[0]) == 100


def test_intraday_datasets_have_no_availability_semantics() -> None:
    """盤中就有的資料（價格）呼叫 `get_available()` 要拋錯，不可悄悄回前一天"""

    api: StockPriceAPI = StockPriceAPI(conn=sqlite3.connect(":memory:"))

    assert api.PUBLISHED_AFTER_CLOSE is False
    with pytest.raises(NotImplementedError):
        api.get_latest_available_date(T2)


def test_after_close_datasets_are_declared() -> None:
    """三個盤後公布的資料集都宣告了；基底預設是盤中可知"""

    assert BaseDataAPI.PUBLISHED_AFTER_CLOSE is False
    for api_cls in PUBLISHED_AFTER_CLOSE_APIS:
        assert api_cls.PUBLISHED_AFTER_CLOSE is True, api_cls.__name__


def test_backtest_feed_reads_the_available_balance() -> None:
    """回測資料源的券源檢核走「前一次公布」那條，不再直接取當日餘額"""

    source: str = inspect.getsource(TwStockDataFeed.get_short_balance)

    assert "get_available_short_balance_map" in source
    assert "get_short_balance_map(" not in source
