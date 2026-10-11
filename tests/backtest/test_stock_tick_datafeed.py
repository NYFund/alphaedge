import datetime
from typing import Any, List, Optional, Tuple

import pandas as pd
import pytest

from core.backtest.datafeed.tw.stock_tick_datafeed import StockTickDataFeed
from core.models import LiveDataUnavailableError, PreOpenStockQuote, StockQuote
from core.utils import Scale

"""
台股逐筆資料源 `StockTickDataFeed`：逐筆順序、累計量、一般股過濾與段落快照

全部用手寫 tick 與假的 tick API，不連資料庫；資料庫那一側的欄位契約
（含 `cum_volume`）由 `tests/test_stock_tick_timescale.py` 守。
"""

DAY: datetime.date = datetime.date(2024, 5, 8)


def _tick(
    stock_id: str, t: str, close: float, volume: int, cum_volume: int
) -> Tuple[Any, ...]:
    """一列 `StockTickAPI` 格式的 tick"""

    return (
        stock_id,
        pd.Timestamp(f"2024-05-08 {t}"),
        close,
        volume,
        close - 0.5,
        10,
        close + 0.5,
        20,
        1,
        cum_volume,
    )


COLUMNS: List[str] = [
    "stock_id",
    "time",
    "close",
    "volume",
    "bid_price",
    "bid_volume",
    "ask_price",
    "ask_volume",
    "tick_type",
    "cum_volume",
]

# 依 (time, stock_id, seq) 排好的一天：兩檔普通股交錯，另有一檔 ETF（0050）
DAY_TICKS: pd.DataFrame = pd.DataFrame(
    [
        _tick("0050", "09:00:00", 150.0, 9, 9),
        _tick("1101", "09:00:00", 40.0, 5, 5),
        _tick("2330", "09:00:00", 800.0, 7, 7),
        _tick("2330", "09:00:00", 801.0, 2, 9),
        _tick("1101", "10:00:00", 40.5, 3, 8),
        _tick("2330", "13:24:59", 805.0, 4, 13),
        _tick("2330", "13:30:00", 806.0, 6, 19),
    ],
    columns=COLUMNS,
)


class _FakeTickAPI:
    """記下查詢參數、依代號過濾的假 tick API"""

    def __init__(self, ticks: pd.DataFrame) -> None:
        self.ticks: pd.DataFrame = ticks
        self.calls: List[Optional[List[str]]] = []

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        stock_ids: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        self.calls.append(stock_ids)
        if stock_ids is None:
            return self.ticks.copy()
        return self.ticks[self.ticks["stock_id"].isin(stock_ids)].reset_index(drop=True)


def _feed(ticks: pd.DataFrame = DAY_TICKS) -> Tuple[StockTickDataFeed, _FakeTickAPI]:
    """注入假 API 的資料源（不跑 `setup()`，不連資料庫）"""

    feed: StockTickDataFeed = StockTickDataFeed()
    api: _FakeTickAPI = _FakeTickAPI(ticks)
    feed.tick = api
    return feed, api


# === 逐筆 ===
def test_iter_day_ticks_follows_time_then_stock_order() -> None:
    """逐筆依 (time, stock_id) 交錯產生，ETF 被濾掉"""

    feed, _ = _feed()

    count: int = feed.setup_day(DAY, None)
    quotes: List[StockQuote] = list(feed.iter_day_ticks())

    assert count == 6
    assert [(q.stock_id, q.close) for q in quotes] == [
        ("1101", 40.0),
        ("2330", 800.0),
        ("2330", 801.0),
        ("1101", 40.5),
        ("2330", 805.0),
        ("2330", 806.0),
    ]
    assert all(q.scale == Scale.TICK and q.date == DAY for q in quotes)


def test_quote_volume_is_cumulative_and_tick_volume_is_single() -> None:
    """報價的 volume 是當日累計量（與實盤同語意），這一筆的量在 tick_quote.volume"""

    feed, _ = _feed()
    feed.setup_day(DAY, None)

    quotes: List[StockQuote] = list(feed.iter_day_ticks())

    assert [(q.volume, q.tick_quote.volume) for q in quotes] == [
        (5, 5),
        (7, 7),
        (9, 2),
        (8, 3),
        (13, 4),
        (19, 6),
    ]


# === 標的範圍 ===
def test_symbols_are_filtered_to_common_stocks_before_querying() -> None:
    """指定清單時先去重、排序、濾掉非一般股再查；清單就是當天的標的範圍"""

    feed, api = _feed()

    feed.setup_day(DAY, ["2330", "0050", "2330", "1101"])

    assert api.calls == [["1101", "2330"]]
    assert feed.day_symbols == ["1101", "2330"]


def test_whole_market_symbols_come_from_the_day_ticks() -> None:
    """全市場時查詢不帶清單，標的範圍是當天有成交的一般股"""

    feed, api = _feed()

    feed.setup_day(DAY, None)

    assert api.calls == [None]
    assert feed.day_symbols == ["1101", "2330"]


# === 段落快照 ===
def test_open_snapshot_and_close_quotes_per_stock() -> None:
    """開盤取第一筆、快照取時點之前（不含）的最後一筆、收盤取最後一筆，都依代號排序"""

    feed, _ = _feed()
    feed.setup_day(DAY, None)

    opens: List[StockQuote] = feed.get_open_quotes()
    # 13:30:00 整有一筆：不含時點本身，快照停在它前一筆
    snapshot: List[StockQuote] = feed.get_snapshot_quotes(datetime.time(13, 30))
    closes: List[StockQuote] = feed.get_close_quotes()

    assert [(q.stock_id, q.close) for q in opens] == [("1101", 40.0), ("2330", 800.0)]
    assert [(q.stock_id, q.close) for q in snapshot] == [
        ("1101", 40.5),
        ("2330", 805.0),
    ]
    assert [(q.stock_id, q.close, q.volume) for q in closes] == [
        ("1101", 40.5, 8),
        ("2330", 806.0, 19),
    ]


def test_snapshot_skips_stocks_without_trades_before_cutoff() -> None:
    """時點之前沒有成交的股票不會出現在快照裡"""

    feed, _ = _feed()
    feed.setup_day(DAY, None)

    snapshot: List[StockQuote] = feed.get_snapshot_quotes(datetime.time(9, 30))

    assert [(q.stock_id, q.close) for q in snapshot] == [
        ("1101", 40.0),
        ("2330", 801.0),
    ]


def test_reference_quotes_use_given_prices_and_hide_ohlc() -> None:
    """
    盤前報價用傳入的參考價算漲跌停；沒有參考價的標的略過；讀 OHLC 會拋出

    參考價不在資料源另算：引擎的成交模型已經持有（含除權息日的公告基準），兩份來源遲早不一致。
    """

    feed, _ = _feed()
    feed.setup_day(DAY, ["1101", "2330"])

    quotes: List[PreOpenStockQuote] = feed.get_reference_quotes({"2330": 800.0})

    assert [q.stock_id for q in quotes] == ["2330"]
    assert quotes[0].reference_price == 800.0
    assert (quotes[0].limit_down, quotes[0].limit_up) == (720.0, 880.0)
    with pytest.raises(LiveDataUnavailableError, match="close"):
        _ = quotes[0].close


def test_empty_day_returns_nothing() -> None:
    """當天沒有任何成交：筆數 0，逐筆與所有快照都是空的"""

    empty: pd.DataFrame = DAY_TICKS.iloc[0:0]
    feed, _ = _feed(empty)

    assert feed.setup_day(DAY, None) == 0
    assert list(feed.iter_day_ticks()) == []
    assert feed.get_open_quotes() == []
    assert feed.get_snapshot_quotes(datetime.time(13, 25)) == []
    assert feed.get_close_quotes() == []
    assert feed.get_reference_quotes({"2330": 800.0}) == []
