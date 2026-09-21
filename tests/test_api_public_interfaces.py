import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import pytest

from core.api.tw.futures_chip_api import FuturesChipAPI
from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_dividend_api import StockDividendAPI
from core.api.tw.stock_margin_api import StockMarginAPI
from core.config import (
    FUTURES_LARGE_TRADER_TABLE_NAME,
    STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
)
from core.dao.base import BaseDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
`core/api/` 公開介面的覆蓋

本檔涵蓋的九個方法在 2026-09-05 的掃描中是**零呼叫且零測試**——它們沒有壞
（逐一實測過都正常回傳），問題是沒有任何東西盯著它們：下一次資料表欄位一改，
它們會安靜地跟著壞而沒有東西會紅。

`core/api/` 是策略作者的公開介面（見 `core/strategies/README.md`），
「`core/` 內部沒有呼叫端」對它們是正常狀態，**有測試才是維護的證據**。

一律以 in-memory SQLite 灌樣本，不連 `data/db/tw_stock.db`。
"""


DAY_1: str = "2024-01-02"
DAY_2: str = "2024-01-03"
DAY_3: str = "2024-01-04"


# === dividend ===
@pytest.fixture
def dividend_api(dao_factory: Callable[..., BaseDAO]) -> StockDividendAPI:
    """建立含兩檔、三個除權息日的 in-memory dividend 表"""

    dao: BaseDAO = dao_factory(
        StockDividendDAO,
        records=[
            {
                "date": DAY_1,
                "stock_id": "2330",
                "現金股利": 3.0,
                "配股率": 0.0,
                "還原係數": 0.99,
            },
            {
                "date": DAY_2,
                "stock_id": "2330",
                "現金股利": 0.0,
                "配股率": 0.05,
                "還原係數": 0.95,
            },
            {
                "date": DAY_2,
                "stock_id": "2317",
                "現金股利": 1.2,
                "配股率": 0.0,
                "還原係數": 0.98,
            },
            # 同一天重複出現：對照表取第一筆
            {
                "date": DAY_3,
                "stock_id": "2317",
                "現金股利": 0.5,
                "配股率": 0.1,
                "還原係數": 0.97,
            },
        ],
    )

    return StockDividendAPI(conn=dao.conn)


def test_get_stock_dividend_returns_sorted_range(
    dividend_api: StockDividendAPI,
) -> None:
    """單檔取區間依日期排序（還原係數累乘依賴這個順序）"""

    df: pd.DataFrame = dividend_api.get_stock_dividend(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert len(df) == 2
    assert df["date"].tolist() == [DAY_1, DAY_2]
    assert set(df["stock_id"]) == {"2330"}


def test_get_stock_dividend_empty_when_range_reversed(
    dividend_api: StockDividendAPI,
) -> None:
    """起訖顛倒回空表，不是把兩個參數對調後照查"""

    df: pd.DataFrame = dividend_api.get_stock_dividend(
        "2330", datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
    )

    assert df.empty


def test_get_stock_dividend_ratio_map(dividend_api: StockDividendAPI) -> None:
    """配股率對照表：純除息當日為 0，沒有除權息的股票不出現在 key 裡"""

    ratio_map: Dict[str, float] = dividend_api.get_stock_dividend_ratio_map(
        datetime.date(2024, 1, 3)
    )

    assert ratio_map == {"2330": 0.05, "2317": 0.0}
    assert dividend_api.get_stock_dividend_ratio_map(datetime.date(2024, 6, 1)) == {}


def test_get_ex_dividend_dates_are_sorted_and_deduped(
    dividend_api: StockDividendAPI,
) -> None:
    """區間內的除權息日已排序去重；`2024-01-03` 有兩檔但只回一個日期"""

    dates: List[datetime.date] = dividend_api.get_ex_dividend_dates(
        datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert dates == [
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        datetime.date(2024, 1, 4),
    ]
    assert (
        dividend_api.get_ex_dividend_dates(
            datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
        )
        == []
    )


# === margin ===
@pytest.fixture
def margin_api(dao_factory: Callable[..., BaseDAO]) -> StockMarginAPI:
    """建立含融券餘額樣本的 in-memory margin 表"""

    dao: BaseDAO = dao_factory(
        StockMarginDAO,
        records=[
            {
                "date": DAY_1,
                "stock_id": "2330",
                "證券名稱": "台積電",
                "融券今日餘額": 1200,
                "融券限額": 50000,
                "券資比": 1.5,
                "註記": "",
            },
            {
                "date": DAY_2,
                "stock_id": "2330",
                "證券名稱": "台積電",
                "融券今日餘額": 900,
                "融券限額": 50000,
                "券資比": 1.1,
                "註記": "",
            },
            {
                "date": DAY_1,
                "stock_id": "2317",
                "證券名稱": "鴻海",
                "融券今日餘額": 0,
                "融券限額": 30000,
                "券資比": 0.0,
                "註記": "",
            },
        ],
    )

    return StockMarginAPI(conn=dao.conn)


def test_get_stock_margin_returns_range(margin_api: StockMarginAPI) -> None:
    """單檔取區間只回該檔、且只回區間內的日期"""

    df: pd.DataFrame = margin_api.get_stock_margin(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert len(df) == 2
    assert set(df["stock_id"]) == {"2330"}
    assert margin_api.get_stock_margin(
        "2330", datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
    ).empty


def test_get_stock_short_balance_distinguishes_zero_from_missing(
    margin_api: StockMarginAPI,
) -> None:
    """
    餘額 0 與查無資料必須分得開

    回 `0` 是「這檔今天一張券都借不到」，回 `None` 是「這天沒有資料，
    呼叫端得自己決定要不要跳過檢核」——把後者當成 0 會讓整個市場都不能放空。
    """

    assert margin_api.get_stock_short_balance("2330", datetime.date(2024, 1, 2)) == 1200
    assert margin_api.get_stock_short_balance("2317", datetime.date(2024, 1, 2)) == 0
    assert margin_api.get_stock_short_balance("2330", datetime.date(2024, 6, 1)) is None
    assert margin_api.get_stock_short_balance("9999", datetime.date(2024, 1, 2)) is None


# === chip ===
@pytest.fixture
def chip_api(dao_factory: Callable[..., BaseDAO]) -> StockChipAPI:
    """建立含三大法人買賣超與額外欄位的 in-memory chip 表"""

    dao: BaseDAO = dao_factory(
        StockChipDAO,
        records=[
            {
                "date": DAY_1,
                "stock_id": "2330",
                "證券名稱": "台積電",
                "外資買賣超股數": 1_000_000,
                "投信買賣超股數": -200_000,
                "自營商買賣超股數": 50_000,
                # 只有 get_stock_chip 會帶出來，get_stock_net_chip 應濾掉
                "外資買進股數": 5_000_000,
            },
            {
                "date": DAY_2,
                "stock_id": "2330",
                "證券名稱": "台積電",
                "外資買賣超股數": -300_000,
                "投信買賣超股數": 100_000,
                "自營商買賣超股數": 0,
                "外資買進股數": 2_000_000,
            },
        ],
    )

    return StockChipAPI(conn=dao.conn)


def test_get_stock_net_chip_keeps_only_net_columns(chip_api: StockChipAPI) -> None:
    """淨買賣超只保留六個欄位，其餘明細欄位不帶出來"""

    df: pd.DataFrame = chip_api.get_stock_net_chip(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert list(df.columns) == [
        "date",
        "stock_id",
        "證券名稱",
        "外資買賣超股數",
        "投信買賣超股數",
        "自營商買賣超股數",
    ]
    assert len(df) == 2
    assert chip_api.get_stock_net_chip(
        "2330", datetime.date(2024, 1, 31), datetime.date(2024, 1, 1)
    ).empty


# === futures large trader ===
@pytest.fixture
def futures_chip_api(dao_factory: Callable[..., BaseDAO]) -> FuturesChipAPI:
    """建立含大額交易人樣本的 in-memory futures_large_trader 表"""

    dao: BaseDAO = dao_factory(
        FuturesChipDAO,
        table_name=FUTURES_LARGE_TRADER_TABLE_NAME,
        records=[
            {
                "date": DAY_1,
                "product": "TX",
                "expiry": "999999",
                "trader_type": "0",
                "多方前五大交易人合計": 30000,
            },
            # 類別 1 是類別 0 的子集（特定法人），預設不應取到這一列
            {
                "date": DAY_1,
                "product": "TX",
                "expiry": "999999",
                "trader_type": "1",
                "多方前五大交易人合計": 12000,
            },
            {
                "date": DAY_1,
                "product": "MTX",
                "expiry": "999999",
                "trader_type": "0",
                "多方前五大交易人合計": 8000,
            },
        ],
    )

    return FuturesChipAPI(conn=dao.conn)


def test_get_large_trader_defaults_to_all_contracts_top_traders(
    futures_chip_api: FuturesChipAPI,
) -> None:
    """預設取「所有契約合計 ＋ 前五／十大」，不會混到特定法人那一列"""

    row: Optional[Dict[str, Any]] = futures_chip_api.get_large_trader(
        datetime.date(2024, 1, 3), "TX"
    )

    assert row is not None
    assert row["多方前五大交易人合計"] == 30000
    assert row["trader_type"] == "0"


def test_get_large_trader_avoids_lookahead(
    futures_chip_api: FuturesChipAPI,
) -> None:
    """
    籌碼盤後才公布：站在資料日當天早上不該看得到當天的資料

    `2024-01-02` 的資料要到 `2024-01-03` 早上才知道，故查 `2024-01-02` 回 None。
    """

    assert futures_chip_api.get_large_trader(datetime.date(2024, 1, 2), "TX") is None


def test_get_large_trader_returns_none_for_unknown_product(
    futures_chip_api: FuturesChipAPI,
) -> None:
    """查無該商品或該到期月份時回 None，不是回上一個商品的資料"""

    assert futures_chip_api.get_large_trader(datetime.date(2024, 1, 3), "TE") is None
    assert (
        futures_chip_api.get_large_trader(
            datetime.date(2024, 1, 3), "TX", expiry="202401"
        )
        is None
    )


# === stock futures margin ===
@pytest.fixture
def futures_margin_api(dao_factory: Callable[..., BaseDAO]) -> FuturesMarginAPI:
    """建立含股期適用比例與契約單位的 in-memory 表"""

    margin_dao: BaseDAO = dao_factory(
        FuturesMarginDAO,
        records_table=STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
        records=[
            {
                "product_id": "CDF",
                "effective_date": "2023-01-01",
                "結算保證金適用比例": 0.1035,
                "維持保證金適用比例": 0.115,
                "原始保證金適用比例": 0.15,
            },
            {
                "product_id": "CDF",
                "effective_date": "2024-01-03",
                "結算保證金適用比例": 0.138,
                "維持保證金適用比例": 0.1533,
                "原始保證金適用比例": 0.2,
            },
        ],
    )
    dao_factory(
        FuturesStockUniverseDAO,
        records=[
            {
                "product_id": "CDF",
                "snapshot_date": "2023-01-01",
                "contract_size": 2000,
            },
        ],
    )

    return FuturesMarginAPI(conn=margin_dao.conn)


def test_calculate_stock_futures_margin(
    futures_margin_api: FuturesMarginAPI,
) -> None:
    """每口原始保證金 ＝ 標的股價 × 契約單位 × 原始保證金適用比例"""

    margin: Optional[float] = futures_margin_api.calculate_stock_futures_margin(
        "CDF", datetime.date(2024, 1, 2), price=100.0
    )

    assert margin == pytest.approx(100.0 * 2000 * 0.15)


def test_calculate_stock_futures_margin_uses_rate_in_effect(
    futures_margin_api: FuturesMarginAPI,
) -> None:
    """比例是**當日生效中**的那一版，不是最新那一版"""

    assert futures_margin_api.calculate_stock_futures_margin(
        "CDF", datetime.date(2024, 1, 3), price=100.0
    ) == pytest.approx(100.0 * 2000 * 0.2)


def test_calculate_stock_futures_margin_returns_none_when_unavailable(
    futures_margin_api: FuturesMarginAPI,
) -> None:
    """比例或契約單位查不到時回 None，不可用預設比例湊一個數字出來"""

    # 查詢日早於第一份比例，且未開 fallback
    assert (
        futures_margin_api.calculate_stock_futures_margin(
            "CDF", datetime.date(2022, 1, 1), price=100.0
        )
        is None
    )
    # 不存在的商品
    assert (
        futures_margin_api.calculate_stock_futures_margin(
            "ZZZ", datetime.date(2024, 1, 2), price=100.0
        )
        is None
    )


def test_calculate_stock_futures_maintenance_margin(
    futures_margin_api: FuturesMarginAPI,
) -> None:
    """
    每口維持保證金取「維持保證金適用比例」那一欄，不是原始比例打折

    兩個日期各驗一次：取錯欄會在第一個日期就對不上，
    取到最新一版而非當日生效版則會在第二個日期之前就露餡
    """

    assert futures_margin_api.calculate_stock_futures_maintenance_margin(
        "CDF", datetime.date(2024, 1, 2), price=100.0
    ) == pytest.approx(100.0 * 2000 * 0.115)
    assert futures_margin_api.calculate_stock_futures_maintenance_margin(
        "CDF", datetime.date(2024, 1, 3), price=100.0
    ) == pytest.approx(100.0 * 2000 * 0.1533)


# === tick ===
def test_get_last_tick_takes_the_last_row() -> None:
    """
    當日最後一筆 tick ＝ `get_stock_ticks()` 結果的最後一列

    以替身取代 `get_stock_ticks()`，本測試驗的是**取尾與空表處理**這段邏輯，
    不需要 DolphinDB 連線（`StockTickAPI` 的 `__init__` 會連 tick 庫，
    故以 `__new__` 建立空殼）。
    """

    from core.api.tw.stock_tick_api import StockTickAPI

    api: StockTickAPI = StockTickAPI.__new__(StockTickAPI)
    ticks: pd.DataFrame = pd.DataFrame(
        [
            {"stock_id": "2330", "close": 590.0},
            {"stock_id": "2330", "close": 591.0},
            {"stock_id": "2330", "close": 592.0},
        ]
    )
    api.get_stock_ticks = lambda stock_id, start_date, end_date: ticks

    last: pd.DataFrame = api.get_last_tick("2330", datetime.date(2024, 5, 10))

    assert len(last) == 1
    assert last.iloc[0]["close"] == 592.0


def test_get_last_tick_returns_empty_without_ticks() -> None:
    """當日沒有 tick 時回空表，不是回 `iloc[-1:]` 而拋 IndexError"""

    from core.api.tw.stock_tick_api import StockTickAPI

    api: StockTickAPI = StockTickAPI.__new__(StockTickAPI)
    api.get_stock_ticks = lambda stock_id, start_date, end_date: pd.DataFrame()

    assert api.get_last_tick("2330", datetime.date(2024, 5, 10)).empty
