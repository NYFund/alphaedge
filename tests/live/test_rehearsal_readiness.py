import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, List, Optional

import pandas as pd
import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.factory import build_live_trader
from core.live.strategy_guard import inspect_strategy
from core.live.trader import LiveTrader
from core.market.tw.market_calendar import MarketCalendar
from core.models.stock.trading_list import DayTradeListSnapshot
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
    """資料源建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


# === 策略本身 ===
@pytest.mark.parametrize("strategy_class", [MomentumStrategy1, MomentumFuturesStrategy])
def test_rehearsal_strategies_pass_the_live_readiness_check(
    strategy_class: Any,
) -> None:
    """演練用的兩支策略都通過實盤前檢查：`inspect_strategy()` 回空清單（沒有任何問題）"""

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

    monkeypatch.setattr(
        MarketCalendar, "previous_trading_day_from_api", staticmethod(from_database)
    )

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
    strategy: Any = SimpleNamespace(symbols=[], get_live_symbols=lambda latest: None)

    feed.fill_default_universe(strategy)

    assert strategy.symbols == ["2317", "2330"]


def test_stock_feed_uses_the_strategy_screen_with_the_latest_data_date() -> None:
    """
    策略有盤前篩選時用它的結果，並把資料最新日傳進去

    盤中策略受逐筆訂閱上限所限，不能沿用全市場；日期由資料源給而不是策略自己取今天
    （美東主機的台北早上，`date.today()` 是前一天）。
    """

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())
    feed.get_latest_data_date = lambda: datetime.date(2026, 10, 8)  # type: ignore
    feed.price = SimpleNamespace(get=lambda date: pd.DataFrame({"stock_id": ["2330"]}))
    received: List[datetime.date] = []

    def screen(latest: datetime.date) -> List[str]:
        received.append(latest)
        return ["2305", "2340"]

    strategy: Any = SimpleNamespace(symbols=[], get_live_symbols=screen)

    feed.fill_default_universe(strategy)

    assert strategy.symbols == ["2305", "2340"]
    assert received == [datetime.date(2026, 10, 8)]


def test_stock_feed_does_not_swallow_a_broken_strategy_screen() -> None:
    """篩選寫錯要在啟動時當場失敗；吞成警告的話，盤中整天沒有標的而沒人發現"""

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())
    feed.get_latest_data_date = lambda: datetime.date(2026, 10, 8)  # type: ignore
    feed.price = SimpleNamespace(get=lambda date: pd.DataFrame({"stock_id": ["2330"]}))

    def broken(latest: datetime.date) -> List[str]:
        raise KeyError("成交量")

    strategy: Any = SimpleNamespace(symbols=[], get_live_symbols=broken)

    with pytest.raises(KeyError):
        feed.fill_default_universe(strategy)


class FakeDayTradeListAPI:
    """假的當沖名單 API：只有 10/7 入庫；記錄查詢次數"""

    def __init__(self) -> None:
        self.calls: int = 0

    def get_snapshot(self, date: datetime.date) -> Optional[DayTradeListSnapshot]:
        self.calls += 1
        if date == datetime.date(2026, 10, 7):
            return DayTradeListSnapshot(day_tradable=frozenset({"2330"}))
        return None

    def get_covered_dates(
        self, start: datetime.date, end: datetime.date
    ) -> List[datetime.date]:
        return [datetime.date(2026, 10, 6), datetime.date(2026, 10, 7)]


def test_live_day_trade_list_falls_back_to_the_latest_list() -> None:
    """
    當日名單收盤後才入庫，盤中沿用最近一份；同一天只查一次

    回 None 的話，依名單決定進場的策略會整天不進場而沒有任何錯誤。
    """

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker=SimpleNamespace())
    api: FakeDayTradeListAPI = FakeDayTradeListAPI()
    feed.day_trade_list = api  # type: ignore

    first: Optional[DayTradeListSnapshot] = feed.get_day_trade_list(
        datetime.date(2026, 10, 8)
    )
    calls_after_first: int = api.calls
    second: Optional[DayTradeListSnapshot] = feed.get_day_trade_list(
        datetime.date(2026, 10, 8)
    )

    assert first is not None and first.day_tradable == frozenset({"2330"})
    assert second is first
    assert api.calls == calls_after_first


def test_stock_feed_keeps_a_declared_universe() -> None:
    """策略自己宣告了標的池就不動它，預設標的池不得覆蓋過去"""

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
