import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, List

import pandas as pd
import pytest

from core.backtest.datafeed.tw.market_calendar import MarketCalendar
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.factory import build_live_trader
from core.live.strategy_guard import inspect_strategy
from core.live.trader import LiveTrader
from core.strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1

from .conftest import FakeBroker
from .test_live_factory_and_entry import LiveStockStrategy

"""
模擬環境演練前的準備：演練用的兩支策略要真的跑得起來

以前兩個問題會讓演練整天「跑完、不報錯、也不出任何訊號」：

1. 實盤只抓 `context.symbols` 的報價，而全市場掃描型的策略沒有宣告標的池——
   拿不到任何報價，永遠不會有訊號。
2. `MomentumStrategy1` 的「前一交易日」查的是依回測區間預建的清單；實盤日期超出清單時，
   平移會落到清單最後一天（一年多前），拿那天的收盤當「昨收」算漲幅。
"""


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


# === 策略本身 ===
@pytest.mark.parametrize("strategy_class", [MomentumStrategy1, MomentumFuturesStrategy])
def test_rehearsal_strategies_pass_the_live_readiness_check(
    strategy_class: Any,
) -> None:
    assert inspect_strategy(strategy_class()) == []


def test_previous_trading_date_beyond_the_prebuilt_list_asks_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清單依回測區間預建；實盤日期超出清單時要回頭查資料庫，不可平移到清單最後一天"""

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.trading_days = [datetime.date(2025, 5, 29), datetime.date(2025, 5, 30)]
    asked: List[datetime.date] = []

    def from_database(api: Any, date: datetime.date) -> datetime.date:
        asked.append(date)
        return datetime.date(2026, 9, 21)

    monkeypatch.setattr(MarketCalendar, "get_last_trading_date", from_database)

    assert strategy.get_previous_trading_date(datetime.date(2026, 9, 22)) == (
        datetime.date(2026, 9, 21)
    )
    assert asked == [datetime.date(2026, 9, 22)]
    # 清單範圍內照舊查清單（回測的加速不受影響）
    assert strategy.get_previous_trading_date(datetime.date(2025, 5, 30)) == (
        datetime.date(2025, 5, 29)
    )


# === 預設標的池 ===
def test_stock_feed_fills_the_previous_day_universe() -> None:
    """沒宣告標的池的策略：以前一交易日價格表上的每一檔為標的（與回測每天的標的池相同）"""

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())
    feed.get_latest_data_date = lambda: datetime.date(2026, 9, 22)  # type: ignore
    feed.price = SimpleNamespace(
        get=lambda date: pd.DataFrame({"stock_id": ["2330", "2317", "2330"]})
    )
    strategy: Any = SimpleNamespace(symbols=[])

    feed.fill_default_universe(strategy)

    assert strategy.symbols == ["2317", "2330"]


def test_stock_feed_keeps_a_declared_universe() -> None:
    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())
    strategy: Any = SimpleNamespace(symbols=["2454"])

    feed.fill_default_universe(strategy)

    assert strategy.symbols == ["2454"]


def test_stock_feed_without_a_price_table_leaves_no_universe() -> None:
    """查不到價格表只影響「沒有標的」，不讓資料源啟動失敗"""

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())

    def missing() -> datetime.date:
        raise sqlite3.OperationalError("no such table: price")

    feed.get_latest_data_date = missing  # type: ignore
    strategy: Any = SimpleNamespace(symbols=[])

    feed.fill_default_universe(strategy)

    assert strategy.symbols == []


def test_futures_feed_fills_all_listed_months_of_the_products() -> None:
    """期貨給所有掛牌月份：`select_near_month()` 要在全部月份裡挑當家契約"""

    resolver: Any = SimpleNamespace(
        list_index_futures_expiries=lambda product: ["202610", "202611", "202612"]
    )
    feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
        broker=SimpleNamespace(resolver=resolver)
    )
    strategy: Any = SimpleNamespace(symbols=[], products=["TX"])

    feed.fill_default_contracts(strategy)

    assert strategy.symbols == ["TX202610", "TX202611", "TX202612"]


# === factory ===
class SelfDeclaringStrategy(LiveStockStrategy):
    """在 `setup_apis()` 才決定標的池的策略"""

    def __init__(self) -> None:
        super().__init__()
        self.symbols = []

    def setup_apis(self, feed: Any) -> None:
        self.symbols = ["2330", "2317"]


def test_context_reads_the_universe_after_setup() -> None:
    """
    context 的標的池要在資料源 setup 之後才讀

    以前建 context 時就複製了一份，setup 之後才補上的標的池永遠進不了 context。
    """

    dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    dao.ensure_tables()
    trader: LiveTrader = build_live_trader(
        [SelfDeclaringStrategy()],
        broker=FakeBroker(),
        dao=dao,
        run_id="20260922140000",
        now_provider=lambda: datetime.datetime(2026, 9, 22, 14, 0),
    )

    assert trader.contexts[0].symbols == ["2330", "2317"]
