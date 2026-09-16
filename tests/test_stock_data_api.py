import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator

import pandas as pd
import pytest

from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.config import CHIP_TABLE_NAME, PRICE_TABLE_NAME
from core.config.schema import ChipColumn, PriceColumn

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""具名查詢方法的單元測試

以 in-memory SQLite 灌入固定樣本，涵蓋「查得到」「查不到」「值異常」三種情況。
"""


DAY_1: str = "2024-01-02"
DAY_2: str = "2024-01-03"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """建立含 price 與 chip 樣本的 in-memory SQLite"""

    connection: sqlite3.Connection = sqlite3.connect(":memory:")

    price_df: pd.DataFrame = pd.DataFrame(
        [
            # 正常樣本
            {
                "date": DAY_1,
                "stock_id": "2330",
                PriceColumn.CLOSE.value: 590.0,
                PriceColumn.SHARES.value: 25_000_000,
            },
            # 收盤價為 0：查得到但值異常，判斷交給策略
            {
                "date": DAY_1,
                "stock_id": "2317",
                PriceColumn.CLOSE.value: 0.0,
                PriceColumn.SHARES.value: 1_500_000,
            },
            # 成交股數為 NULL：收盤價仍應查得到，成交量則不納入
            {
                "date": DAY_1,
                "stock_id": "2454",
                PriceColumn.CLOSE.value: 1000.0,
                PriceColumn.SHARES.value: None,
            },
            # 同一檔重複出現：應取第一筆
            {
                "date": DAY_1,
                "stock_id": "1101",
                PriceColumn.CLOSE.value: 30.0,
                PriceColumn.SHARES.value: 5_000_000,
            },
            {
                "date": DAY_1,
                "stock_id": "1101",
                PriceColumn.CLOSE.value: 99.0,
                PriceColumn.SHARES.value: 9_000_000,
            },
            # 次日樣本，供區間序列使用
            {
                "date": DAY_2,
                "stock_id": "2330",
                PriceColumn.CLOSE.value: 600.0,
                PriceColumn.SHARES.value: 30_000_000,
            },
        ]
    )
    price_df.to_sql(PRICE_TABLE_NAME, connection, index=False)

    chip_df: pd.DataFrame = pd.DataFrame(
        [
            {
                "date": DAY_1,
                "stock_id": "2330",
                ChipColumn.TRUST_NET_SHARES.value: 1_200_000,
            },
            {
                "date": DAY_1,
                "stock_id": "2317",
                ChipColumn.TRUST_NET_SHARES.value: -800_000,
            },
        ]
    )
    chip_df.to_sql(CHIP_TABLE_NAME, connection, index=False)

    yield connection

    connection.close()


@pytest.fixture
def price_api(conn: sqlite3.Connection) -> StockPriceAPI:
    """使用共用連線的 StockPriceAPI（不自行建立連線）"""

    return StockPriceAPI(conn=conn)


@pytest.fixture
def chip_api(conn: sqlite3.Connection) -> StockChipAPI:
    """使用共用連線的 StockChipAPI"""

    return StockChipAPI(conn=conn)


def test_get_close_map_returns_all_stocks(price_api: StockPriceAPI) -> None:
    """查得到的股票都要在對照表內，含收盤價為 0 者"""

    close_map: Dict[str, Any] = price_api.get_close_map(DAY_1)

    assert close_map["2330"] == 590.0
    # 值異常不等於缺資料：0 仍須回傳，由策略自行判斷
    assert close_map["2317"] == 0.0


def test_get_close_map_missing_stock_absent(price_api: StockPriceAPI) -> None:
    """查不到的股票不得出現在對照表，策略以 key 不存在判斷缺資料"""

    close_map: Dict[str, Any] = price_api.get_close_map(DAY_1)

    assert "9999" not in close_map


def test_get_close_map_keeps_first_on_duplicate(price_api: StockPriceAPI) -> None:
    """同一檔重複出現時取第一筆，與原本 .iloc[0] 一致（取最後一筆會讓回歸對不上）"""

    close_map: Dict[str, Any] = price_api.get_close_map(DAY_1)

    assert close_map["1101"] == 30.0


def test_get_close_map_empty_date(price_api: StockPriceAPI) -> None:
    """查無資料的日期回傳空 dict，而不是拋例外"""

    assert price_api.get_close_map(datetime.date(2023, 12, 25)) == {}


def test_get_volume_lots_map_converts_shares(price_api: StockPriceAPI) -> None:
    """股數換算為張數（1 張 = 1000 股）"""

    volume_map: Dict[str, int] = price_api.get_volume_lots_map(DAY_1)

    assert volume_map["2330"] == 25_000
    assert volume_map["2317"] == 1_500


def test_get_volume_lots_map_skips_invalid(price_api: StockPriceAPI) -> None:
    """成交股數為 NULL 者不納入，但不影響同一天其他股票"""

    volume_map: Dict[str, int] = price_api.get_volume_lots_map(DAY_1)

    assert "2454" not in volume_map
    assert "2330" in volume_map


def test_get_close_series_sorted_by_date(price_api: StockPriceAPI) -> None:
    """區間收盤序列依日期排序，index 為 date"""

    series: pd.Series = price_api.get_close_series(
        "2330", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert list(series.values) == [590.0, 600.0]
    assert list(series.index) == [DAY_1, DAY_2]


def test_get_close_series_empty_when_no_data(price_api: StockPriceAPI) -> None:
    """查無資料時回傳空 Series，呼叫端以 len() 判斷資料是否足夠"""

    series: pd.Series = price_api.get_close_series(
        "9999", datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
    )

    assert series.empty


def test_get_trust_net_shares_map(chip_api: StockChipAPI) -> None:
    """投信買賣超股數對照表，負值（賣超）同樣要回傳"""

    chip_map: Dict[str, Any] = chip_api.get_trust_net_shares_map(DAY_1)

    assert chip_map["2330"] == 1_200_000
    assert chip_map["2317"] == -800_000
    assert "9999" not in chip_map


# === API 邊界===
def test_sql_params_converts_dates_to_iso() -> None:
    """
    `date`／`datetime` 一律轉 ISO 字串

    不轉的話是靠 Python 3.12 已 deprecated 的 sqlite date adapter，
    那個 adapter 隨時可能被移除，屆時每一支 API 都會在同一天壞掉。
    """

    from core.api.base import BaseDataAPI

    params = BaseDataAPI.sql_params(
        "2330", datetime.date(2024, 1, 2), datetime.datetime(2024, 1, 3, 13, 30), 5
    )

    assert params == ("2330", "2024-01-02", "2024-01-03", 5)


def test_get_net_chip_no_longer_raises(tmp_path) -> None:
    """
    `get_net_chip()` 一被呼叫就 `TypeError`

    舊版寫的是 `self.get(start_date, end_date)`，而 `get()` 只收一個 `date`。
    全專案沒有呼叫端，所以壞了也沒人發現，但 API 門面看起來是可用的。
    """

    import sqlite3

    from core.api.tw.stock_chip_api import StockChipAPI

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        'CREATE TABLE chip ("date" TEXT, stock_id TEXT, "證券名稱" TEXT, '
        '"外資買賣超股數" INTEGER, "投信買賣超股數" INTEGER, "自營商買賣超股數" INTEGER)'
    )
    conn.execute(
        "INSERT INTO chip VALUES ('2024-01-02', '2330', '台積電', 100, 200, 300)"
    )
    conn.commit()

    api: StockChipAPI = StockChipAPI(conn=conn)
    df = api.get_net_chip(datetime.date(2024, 1, 1), datetime.date(2024, 1, 31))

    assert len(df) == 1
    assert list(df.columns) == [
        "date",
        "stock_id",
        "證券名稱",
        "外資買賣超股數",
        "投信買賣超股數",
        "自營商買賣超股數",
    ]
    conn.close()


def test_get_net_chip_returns_empty_without_data(tmp_path) -> None:
    """查無資料時回空表，不可在取欄位時炸掉"""

    import sqlite3

    from core.api.tw.stock_chip_api import StockChipAPI

    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute('CREATE TABLE chip ("date" TEXT, stock_id TEXT)')
    conn.commit()

    api: StockChipAPI = StockChipAPI(conn=conn)

    assert api.get_net_chip(datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)).empty
    conn.close()


def test_financial_statement_table_name_is_whitelisted() -> None:
    """
    表名由呼叫端傳入，只能走白名單

    表名不能參數化、只能拼進 SQL，而拼字串的地方就是注入的入口。
    """

    import pytest

    from core.api.tw.financial_statement_api import FinancialStatementAPI
    from core.config import BALANCE_SHEET_TABLE_NAME

    assert (
        FinancialStatementAPI.check_table_name(BALANCE_SHEET_TABLE_NAME)
        == BALANCE_SHEET_TABLE_NAME
    )

    with pytest.raises(ValueError, match="不支援的財報資料表"):
        FinancialStatementAPI.check_table_name("balance_sheet; DROP TABLE price")
