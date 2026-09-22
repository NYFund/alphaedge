import datetime
import sqlite3
from typing import Callable, List, Tuple

import pytest

from core.api.tw.stock_dividend_api import StockDividendAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.dao.base import BaseDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.dao.tw.stock_price_dao import StockPriceDAO

"""
還原價（後復權）查詢測試

以合成的 price ＋ dividend 資料驗證還原邏輯（建表走 DAO 的正式 schema），不連網路、不碰正式 DB。

核心性質（後復權的定義）：
1. 除權息**之前**的價格不變 —— 這正是選後復權而非前復權的理由：歷史價格穩定，
   LONG baseline 不會因為新的除權息而自動失效
2. 除權息**當日起**的價格往上還原，使跨越除權息日的報酬率反映真實漲跌
3. 多次除權息時係數**累乘**

測試資料以台積電 2024 年的真實除息事件為藍本：
- 2024-06-13 除息：前收 909.0 → 參考價 905.5（配息 3.5）
- 2024-09-12 除息：前收 901.0 → 參考價 896.99（配息 4.0）
"""

STOCK_ID: str = "2330"

# (日期, 收盤價) —— 原始成交價
PRICE_ROWS: List[Tuple[str, float]] = [
    ("2024-06-12", 909.0),  # 除息前一日
    ("2024-06-13", 900.0),  # 除息當日（參考價 905.5，實際收 900）
    ("2024-09-11", 901.0),  # 第二次除息前一日
    ("2024-09-12", 890.0),  # 第二次除息當日（參考價 896.99）
]

# (除權息日, 前收盤價, 參考價)
DIVIDEND_ROWS: List[Tuple[str, float, float]] = [
    ("2024-06-13", 909.0, 905.5),
    ("2024-09-12", 901.0, 896.99),
]

FIRST_FACTOR: float = 909.0 / 905.5
SECOND_FACTOR: float = 901.0 / 896.99


@pytest.fixture
def price_api(
    dao_factory: Callable[..., BaseDAO], memory_conn: sqlite3.Connection
) -> StockPriceAPI:
    """以正式 schema 建出 price 與 dividend 兩張表，並回傳共用連線的 API"""

    dao_factory(
        StockPriceDAO,
        records=[
            # OHLC 以收盤價代入即可，本測試只驗證還原價的掛載與退回行為
            {
                "date": date,
                "stock_id": STOCK_ID,
                "證券名稱": "台積電",
                "開盤價": close,
                "最高價": close,
                "最低價": close,
                "收盤價": close,
                "成交股數": 1_000_000,
            }
            for date, close in PRICE_ROWS
        ],
    )
    dao_factory(
        StockDividendDAO,
        records=[
            {
                "date": date,
                "stock_id": STOCK_ID,
                "除權息前收盤價": before,
                "除權息參考價": reference,
                "還原係數": reference / before,
            }
            for date, before, reference in DIVIDEND_ROWS
        ],
    )

    return StockPriceAPI(conn=memory_conn)


def test_price_before_ex_date_is_unchanged(price_api: StockPriceAPI) -> None:
    """後復權：除權息之前的價格不得變動（歷史穩定，baseline 才不會自動失效）"""

    date: datetime.date = datetime.date(2024, 6, 12)

    assert price_api.get_adjusted_close_map(date)[STOCK_ID] == pytest.approx(909.0)
    assert price_api.get_close_map(date)[STOCK_ID] == pytest.approx(909.0)


def test_price_on_ex_date_is_adjusted_upward(price_api: StockPriceAPI) -> None:
    """除權息當日起套用係數，還原價高於原始價"""

    date: datetime.date = datetime.date(2024, 6, 13)

    assert price_api.get_adjusted_close_map(date)[STOCK_ID] == pytest.approx(
        900.0 * FIRST_FACTOR
    )
    # 原始價不受影響：成交與成本仍走這一條
    assert price_api.get_close_map(date)[STOCK_ID] == pytest.approx(900.0)


def test_return_across_ex_date_excludes_dividend(price_api: StockPriceAPI) -> None:
    """
    跨除權息日的報酬率必須是真實漲跌，不含配息造成的跳空

    原始價算出 900/909 − 1 = −0.99%，其中 0.385% 純粹來自配息 3.5 元；
    還原後應等於 900/905.5 − 1 = −0.61%（相對除權息參考價的真實跌幅）
    """

    before: float = price_api.get_adjusted_close_map(datetime.date(2024, 6, 12))[
        STOCK_ID
    ]
    after: float = price_api.get_adjusted_close_map(datetime.date(2024, 6, 13))[
        STOCK_ID
    ]

    adjusted_return: float = after / before - 1
    true_return: float = 900.0 / 905.5 - 1
    raw_return: float = 900.0 / 909.0 - 1

    assert adjusted_return == pytest.approx(true_return)
    assert adjusted_return > raw_return


def test_factors_compound_across_multiple_ex_dates(price_api: StockPriceAPI) -> None:
    """兩次除權息之後，係數為兩次的連乘"""

    dividend_api: StockDividendAPI = price_api.get_dividend_api()

    assert dividend_api.get_cumulative_factor(
        STOCK_ID, datetime.date(2024, 6, 12)
    ) == pytest.approx(1.0)
    assert dividend_api.get_cumulative_factor(
        STOCK_ID, datetime.date(2024, 6, 13)
    ) == pytest.approx(FIRST_FACTOR)
    # 兩次除息之間維持第一次的係數
    assert dividend_api.get_cumulative_factor(
        STOCK_ID, datetime.date(2024, 9, 11)
    ) == pytest.approx(FIRST_FACTOR)
    assert dividend_api.get_cumulative_factor(
        STOCK_ID, datetime.date(2024, 9, 12)
    ) == pytest.approx(FIRST_FACTOR * SECOND_FACTOR)


def test_stock_without_dividend_keeps_raw_price(price_api: StockPriceAPI) -> None:
    """未曾除權息的股票：係數為 1，還原價等於原始價"""

    dividend_api: StockDividendAPI = price_api.get_dividend_api()

    assert dividend_api.get_cumulative_factor(
        "9999", datetime.date(2024, 6, 13)
    ) == pytest.approx(1.0)
    assert "9999" not in dividend_api.get_cumulative_factor_map(
        datetime.date(2024, 6, 13)
    )


def test_adjusted_series_is_continuous(price_api: StockPriceAPI) -> None:
    """區間序列跨過除權息日時，還原後不再出現虛假跳空"""

    series = price_api.get_adjusted_close_series(
        STOCK_ID, datetime.date(2024, 6, 12), datetime.date(2024, 6, 13)
    )

    assert len(series) == 2
    assert series.iloc[0] == pytest.approx(909.0)
    assert series.iloc[1] == pytest.approx(900.0 * FIRST_FACTOR)


def test_single_and_cumulative_factor_maps_differ(price_api: StockPriceAPI) -> None:
    """
    單次係數與累乘係數是兩件事，不可混用

    - `get_adjust_factor_map()`：只有**當日**除權息的股票才有值（單次落差比例，< 1）
    - `get_cumulative_factor_map()`：所有**曾經**除權息過的股票都有值（歷史累乘，> 1）
    """

    dividend_api: StockDividendAPI = price_api.get_dividend_api()
    date: datetime.date = datetime.date(2024, 9, 12)

    single: float = dividend_api.get_adjust_factor_map(date)[STOCK_ID]
    cumulative: float = dividend_api.get_cumulative_factor_map(date)[STOCK_ID]

    assert single == pytest.approx(896.99 / 901.0)
    assert single < 1
    assert cumulative == pytest.approx(FIRST_FACTOR * SECOND_FACTOR)
    assert cumulative > 1

    # 非除權息日：單次為空，累乘仍有值
    non_ex_date: datetime.date = datetime.date(2024, 9, 11)
    assert dividend_api.get_adjust_factor_map(non_ex_date) == {}
    assert dividend_api.get_cumulative_factor_map(non_ex_date)[STOCK_ID] == (
        pytest.approx(FIRST_FACTOR)
    )


def test_factor_cache_is_reset_after_update(
    price_api: StockPriceAPI, dao_factory: Callable[..., BaseDAO]
) -> None:
    """快取在整個 process 內有效；更新 dividend 表後須以 reset 讓新資料生效"""

    dividend_api: StockDividendAPI = price_api.get_dividend_api()
    date: datetime.date = datetime.date(2024, 12, 31)

    assert dividend_api.get_cumulative_factor(STOCK_ID, date) == pytest.approx(
        FIRST_FACTOR * SECOND_FACTOR
    )

    dao_factory(
        StockDividendDAO,
        records=[
            {
                "date": "2024-12-12",
                "stock_id": STOCK_ID,
                "除權息前收盤價": 1045.0,
                "除權息參考價": 1041.0,
                "還原係數": 1041.0 / 1045.0,
            }
        ],
        conn=dividend_api.conn,
    )

    # 尚未 reset：仍讀到快取值
    assert dividend_api.get_cumulative_factor(STOCK_ID, date) == pytest.approx(
        FIRST_FACTOR * SECOND_FACTOR
    )

    dividend_api.reset_factor_cache()
    assert dividend_api.get_cumulative_factor(STOCK_ID, date) == pytest.approx(
        FIRST_FACTOR * SECOND_FACTOR * (1045.0 / 1041.0)
    )


# === 報價路徑：adapter 是否正確掛上還原價 ===
def test_quote_without_adjustment_falls_back_to_close() -> None:
    """未啟用還原時 `adj_close` 為 None，`signal_close` 必須等於原始收盤價"""

    from core.models import StockQuote

    quote: StockQuote = StockQuote(stock_id=STOCK_ID, close=900.0)

    assert quote.adj_close is None
    assert quote.signal_close == pytest.approx(900.0)


def test_quote_with_adjustment_uses_adj_close() -> None:
    """啟用還原時 `signal_close` 走還原價，但 `close` 仍是原始價（成交與成本用）"""

    from core.models import StockQuote

    adjusted: float = 900.0 * FIRST_FACTOR
    quote: StockQuote = StockQuote(stock_id=STOCK_ID, close=900.0, adj_close=adjusted)

    assert quote.signal_close == pytest.approx(adjusted)
    assert quote.close == pytest.approx(900.0)


def test_adapter_attaches_adjusted_close_only_when_enabled(
    price_api: StockPriceAPI,
) -> None:
    """adapter 的 `adjusted` 參數決定是否掛還原價；OHLC 一律維持原始價"""

    from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter

    date: datetime.date = datetime.date(2024, 6, 13)

    # 查詢由呼叫端負責（adapter 已是純轉換層）
    price_df = price_api.get(date)
    raw_quotes = StockQuoteAdapter.from_day_rows(price_df, date)
    adjusted_quotes = StockQuoteAdapter.from_day_rows(
        price_df, date, price_api.get_adjusted_close_map(date)
    )

    raw = next(q for q in raw_quotes if q.stock_id == STOCK_ID)
    adj = next(q for q in adjusted_quotes if q.stock_id == STOCK_ID)

    assert raw.adj_close is None
    assert raw.signal_close == pytest.approx(900.0)

    assert adj.adj_close == pytest.approx(900.0 * FIRST_FACTOR)
    assert adj.signal_close == pytest.approx(900.0 * FIRST_FACTOR)
    # 成交價與成本用的原始價不得被還原污染
    assert adj.close == pytest.approx(900.0)
    assert adj.cur_price == pytest.approx(900.0)


# === 除權息日的漲跌停基準 ===
def test_price_limit_basis_overrides_prev_close() -> None:
    """
    除權息日的漲跌停基準改用交易所公告的開盤競價基準

    沿用前一交易日收盤會讓整段區間偏高：以聯發科 2024-01-04 為例，
    前收 953 算出 [858, 1045]，但官方公告為 [836, 1020]
    """

    from core.backtest.models.fill_model import TwStockFillModel
    from core.backtest.models.instrument_spec import TwStockSpec

    fill_model: TwStockFillModel = TwStockFillModel()
    spec: TwStockSpec = TwStockSpec()

    fill_model.on_bar_close([StockQuoteStub(symbol="2454", close=953.0)])
    assert fill_model.prev_close["2454"] == pytest.approx(953.0)

    # 除權息日：以開盤競價基準覆寫
    fill_model.apply_price_limit_basis({"2454": 928.0})

    assert fill_model.prev_close["2454"] == pytest.approx(928.0)
    assert spec.get_price_limits(928.0) == (836.0, 1020.0)


def test_price_limit_basis_only_touches_listed_symbols() -> None:
    """只覆寫有公告的標的，其餘維持前一交易日收盤"""

    from core.backtest.models.fill_model import TwStockFillModel

    fill_model: TwStockFillModel = TwStockFillModel()
    fill_model.on_bar_close(
        [
            StockQuoteStub(symbol="2454", close=953.0),
            StockQuoteStub(symbol="2330", close=600.0),
        ]
    )

    fill_model.apply_price_limit_basis({"2454": 928.0})

    assert fill_model.prev_close["2454"] == pytest.approx(928.0)
    assert fill_model.prev_close["2330"] == pytest.approx(600.0)


def test_empty_basis_is_a_no_op() -> None:
    """非除權息日的基準對照表為空，不得影響既有的前收盤價"""

    from core.backtest.models.fill_model import TwStockFillModel

    fill_model: TwStockFillModel = TwStockFillModel()
    fill_model.on_bar_close([StockQuoteStub(symbol="2330", close=600.0)])

    fill_model.apply_price_limit_basis({})

    assert fill_model.prev_close["2330"] == pytest.approx(600.0)


class StockQuoteStub:
    """`on_bar_close()` 只取 symbol 與 close，故以最小 stub 代替完整報價"""

    def __init__(self, symbol: str, close: float) -> None:
        self.symbol: str = symbol
        self.close: float = close
        self.cur_price: float = close
