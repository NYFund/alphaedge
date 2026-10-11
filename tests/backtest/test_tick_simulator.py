import datetime
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides
from core.models import PreOpenStockQuote, StockAccount, StockOrder, StockQuote
from core.models.fill_config import FillConfig
from core.strategies.stock import BaseStockStrategy
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    PositionType,
    Scale,
    TickUniverse,
)
from tests.backtest.conftest import ScriptedStrategy, run_bar
from tests.conftest import build_stock_quote

"""
逐筆回測 `TickSimulator` 的端到端測試：手寫 tick、腳本策略，完整跑 `Backtester.run()`

守的是逐筆回測存在的理由：
- 沒有前視：委託只會用下單之後、同一檔的下一筆成交。
- 成交規則：市價吃對手價（IOC）、限價要成交價穿越（ROD）、對手方為 0 不成交。
- 段落：盤中鉤子只在實盤的時間窗內、每次一筆；開盤與收盤集合競價照實盤的時點。
- 不重複出場、在途委託佔持倉名額、同一份資料跑兩次結果相同。
"""

DAY: datetime.date = datetime.date(2024, 5, 8)

IMMEDIATE_ALL: Dict[str, ExecutionTiming] = {
    "open": ExecutionTiming.IMMEDIATE,
    "close": ExecutionTiming.IMMEDIATE,
    "stop_loss": ExecutionTiming.IMMEDIATE,
}

Rule = Callable[[StockQuote, "_TickStrategy"], List[StockOrder]]


# === 手寫資料 ===
def _tick(
    stock_id: str,
    clock: str,
    close: float,
    bid: float,
    ask: float,
    volume: int = 1,
    bid_volume: int = 10,
    ask_volume: int = 10,
) -> Dict:
    """一筆 tick（欄位同 `StockTickAPI`，`cum_volume` 由 `_frame()` 補）"""

    return {
        "stock_id": stock_id,
        "time": pd.Timestamp(f"{DAY.isoformat()} {clock}"),
        "close": close,
        "volume": volume,
        "bid_price": bid,
        "bid_volume": bid_volume,
        "ask_price": ask,
        "ask_volume": ask_volume,
        "tick_type": 1,
    }


def _frame(rows: List[Dict]) -> pd.DataFrame:
    """依 (time, stock_id) 排序並補上當日累計量"""

    frame: pd.DataFrame = (
        pd.DataFrame(rows)
        .sort_values(["time", "stock_id"], kind="stable")
        .reset_index(drop=True)
    )
    frame["cum_volume"] = frame.groupby("stock_id")["volume"].cumsum()
    return frame


class _FakeTickAPI:
    """依代號過濾的假 tick API"""

    def __init__(self, ticks: pd.DataFrame) -> None:
        self.ticks: pd.DataFrame = ticks

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        stock_ids: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        if stock_ids is None:
            return self.ticks.copy()
        return self.ticks[self.ticks["stock_id"].isin(stock_ids)].reset_index(drop=True)


# === 腳本策略 ===
def _order(stock_id: str, action: Action, price: float, volume: int = 1) -> StockOrder:
    """一張做多的委託（買為開倉、賣為平倉）"""

    return StockOrder(
        stock_id=stock_id,
        date=DAY,
        action=action,
        position_type=PositionType.LONG,
        price=price,
        volume=volume,
    )


class _TickStrategy(BaseStockStrategy):
    """逐筆觸發的腳本策略：每個鉤子記下收到的報價，依規則回傳委託"""

    def __init__(
        self,
        schedule: Dict[str, ExecutionTiming],
        open_rule: Optional[Rule] = None,
        close_rule: Optional[Rule] = None,
        stop_rule: Optional[Rule] = None,
        live_execution: ExecutionStyle = ExecutionStyle.MARKET,
    ) -> None:
        super().__init__()

        self.strategy_name: str = "TickScripted"
        self.init_capital: float = 10_000_000.0
        self.max_holdings: int = 10
        self.scale: Scale = Scale.TICK
        self.is_tick_triggered: bool = True
        self.start_date: datetime.date = DAY
        self.end_date: datetime.date = DAY
        self.execution_schedule = schedule
        self.live_execution = live_execution

        self.open_rule: Optional[Rule] = open_rule
        self.close_rule: Optional[Rule] = close_rule
        self.stop_rule: Optional[Rule] = stop_rule
        # (鉤子, 收到的報價)
        self.calls: List[Tuple[str, List[StockQuote]]] = []

    def setup_account(self, account: StockAccount) -> None:
        self.account: StockAccount = account

    def setup_apis(self, feed=None) -> None:
        pass

    def _apply(
        self, hook: str, rule: Optional[Rule], quotes: List[StockQuote]
    ) -> List[StockOrder]:
        self.calls.append((hook, list(quotes)))
        if rule is None:
            return []
        return [order for quote in quotes for order in rule(quote, self)]

    def check_open_signal(self, stock_quotes: List[StockQuote]) -> List[StockOrder]:
        return self._apply("open", self.open_rule, stock_quotes)

    def check_close_signal(self, stock_quotes: List[StockQuote]) -> List[StockOrder]:
        return self._apply("close", self.close_rule, stock_quotes)

    def check_stop_loss_signal(
        self, stock_quotes: List[StockQuote]
    ) -> List[StockOrder]:
        return self._apply("stop_loss", self.stop_rule, stock_quotes)


def _run(
    strategy: _TickStrategy,
    rows: List[Dict],
    monkeypatch: pytest.MonkeyPatch,
    reference_prices: Optional[Dict[str, float]] = None,
) -> Backtester:
    """以手寫 tick 跑完一天（全市場模式，不連任何資料庫）"""

    monkeypatch.setattr(Backtester, "setup", lambda self: None)
    backtester: Backtester = build_backtester(
        strategy,
        write_artifacts=False,
        overrides=BacktestOverrides(tick_universe=TickUniverse.ALL),
    )
    backtester.data_feed.tick = _FakeTickAPI(_frame(rows))
    monkeypatch.setattr(backtester.data_feed, "is_market_open", lambda date: True)
    monkeypatch.setattr(backtester.data_feed, "close", lambda: None)
    if reference_prices:
        backtester.fill_model.reference_prices.update(reference_prices)

    backtester.run()
    return backtester


def _buy_once_at(clock: str, stock_id: str = "2330") -> Rule:
    """在指定時間的那一筆（只一次）送出市價買單"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if (
            quote.stock_id == stock_id
            and quote.tick_quote.time.strftime("%H:%M:%S") == clock
        ):
            return [_order(stock_id, Action.BUY, quote.close)]
        return []

    return rule


def _entry_prices(backtester: Backtester) -> List[float]:
    """依開倉順序的成交價"""

    return [position.price for position in backtester.account.positions]


# === 段落與餵入單位 ===
def test_immediate_hooks_get_one_quote_per_call_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盤中鉤子每次只拿到一筆，依時間順序；09:05 之前與 13:18 之後的 tick 不交給鉤子"""

    strategy: _TickStrategy = _TickStrategy(IMMEDIATE_ALL)
    rows: List[Dict] = [
        _tick("2330", "09:00:00", 100.0, 99.5, 100.0),
        _tick("1101", "09:05:00", 40.0, 39.9, 40.0),
        _tick("2330", "09:05:00", 100.5, 100.0, 100.5),
        _tick("1101", "10:00:00", 40.1, 40.0, 40.1),
        _tick("2330", "13:18:30", 101.0, 100.5, 101.0),
    ]

    _run(strategy, rows, monkeypatch)

    opens: List[List[StockQuote]] = [q for hook, q in strategy.calls if hook == "open"]
    assert all(len(quotes) == 1 for quotes in opens)
    assert [(q[0].stock_id, q[0].close) for q in opens] == [
        ("1101", 40.0),
        ("2330", 100.5),
        ("1101", 40.1),
    ]


def test_hooks_never_see_a_later_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """策略在第 N 筆時看不到第 N+1 筆：每次呼叫時引擎還沒讀到之後的資料"""

    seen: List[pd.Timestamp] = []

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        seen.append(quote.tick_quote.time)
        return []

    strategy: _TickStrategy = _TickStrategy(IMMEDIATE_ALL, open_rule=rule)
    rows: List[Dict] = [
        _tick("2330", f"10:00:{second:02d}", 100.0 + second, 99.0, 101.0)
        for second in range(5)
    ]

    _run(strategy, rows, monkeypatch)

    assert seen == sorted(seen)
    assert len(seen) == 5


# === 成交規則 ===
def test_market_buy_fills_at_next_tick_ask_of_same_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    沒有前視：10:00 觸發的市價買，跳過其他股票，用 2330 下一筆（10:02）的委賣價成交，
    不是觸發那一筆的價格
    """

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00")
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("1101", "10:01:00", 40.0, 39.9, 40.0),
        _tick("2330", "10:02:00", 101.0, 100.5, 101.5),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert _entry_prices(backtester) == [101.5]


def test_market_sell_fills_at_next_tick_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    """平倉的市價賣吃下一筆的委買價；成交前持倉一直在（盤中即時入帳）"""

    def close_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:05:00":
            return [_order("2330", Action.SELL, quote.close)]
        return []

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00"), close_rule=close_rule
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:05:00", 102.0, 101.5, 102.0),
        _tick("2330", "10:06:00", 103.0, 102.5, 103.0),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    records = [r for r in backtester.account.trade_records if r.is_closed]
    assert [(r.buy_price, r.sell_price) for r in records] == [(100.0, 102.5)]


def test_market_order_expires_when_next_tick_is_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下一筆沒有委賣（鎖漲停）：市價買不成交、立即作廢，不會等到後面的 tick 才成交"""

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00")
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 110.0, 109.5, 110.0),
        _tick("2330", "10:01:00", 110.0, 110.0, 0.0, ask_volume=0),
        _tick("2330", "10:02:00", 109.5, 109.0, 109.5),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert _entry_prices(backtester) == []
    assert backtester.event_counts["expired_ioc"] == 1


def _limit_buy_at_ten(limit: float) -> Rule:
    """10:00 那一筆送出一張限價買單"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:00:00":
            return [_order("2330", Action.BUY, limit)]
        return []

    return rule


def test_limit_order_does_not_fill_on_a_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    """限價買 100：之後的成交價只碰到 100、沒有跌破，不成交（同價位前面還有人排隊）"""

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL,
        open_rule=_limit_buy_at_ten(100.0),
        live_execution=ExecutionStyle.LIMIT,
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 101.0, 100.5, 101.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:02:00", 100.5, 100.0, 100.5),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert _entry_prices(backtester) == []
    assert backtester.event_counts["expired_at_close"] == 1


def test_limit_order_fills_at_limit_when_trade_goes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限價買 100：成交價跌破到 99.5 時，以限價 100 成交"""

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL,
        open_rule=_limit_buy_at_ten(100.0),
        live_execution=ExecutionStyle.LIMIT,
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 101.0, 100.5, 101.0),
        _tick("2330", "10:01:00", 99.5, 99.0, 99.5),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert _entry_prices(backtester) == [100.0]


def test_unfilled_limit_order_expires_at_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """限價一直沒被穿越：收盤作廢並計數"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:00:00":
            return [_order("2330", Action.BUY, 90.0)]
        return []

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=rule, live_execution=ExecutionStyle.LIMIT
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "11:00:00", 99.0, 98.5, 99.0),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert _entry_prices(backtester) == []
    assert backtester.event_counts["expired_at_close"] == 1


def test_volume_cap_uses_next_tick_opposite_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成交量上限的分母是下一筆的委賣量：50% × 4 張 → 委託 5 張縮成 2 張"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:00:00":
            return [_order("2330", Action.BUY, quote.close, volume=5)]
        return []

    strategy: _TickStrategy = _TickStrategy(IMMEDIATE_ALL, open_rule=rule)
    strategy.fill_config = FillConfig(max_volume_share=0.5)
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0, ask_volume=4),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert [p.volume for p in backtester.account.positions] == [2]
    assert backtester.event_counts["truncated_by_volume"] == 1


# === 開盤與收盤集合競價 ===
def test_opening_auction_sees_reference_and_fills_at_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開盤段的鉤子拿到盤前報價（只有參考價），委託以該檔第一筆（開盤集合競價）的成交價成交"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        return [_order(quote.stock_id, Action.BUY, quote.reference_price)]

    strategy: _TickStrategy = _TickStrategy(
        {"open": ExecutionTiming.AT_OPEN}, open_rule=rule
    )
    rows: List[Dict] = [
        _tick("2330", "09:00:00", 103.0, 102.5, 103.0),
        _tick("2330", "09:01:00", 104.0, 103.5, 104.0),
    ]

    backtester: Backtester = _run(
        strategy, rows, monkeypatch, reference_prices={"2330": 100.0}
    )

    opens: List[List[StockQuote]] = [q for hook, q in strategy.calls if hook == "open"]
    assert len(opens) == 1
    assert isinstance(opens[0][0], PreOpenStockQuote)
    assert _entry_prices(backtester) == [103.0]


def test_closing_auction_uses_snapshot_and_fills_at_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    收盤段的鉤子看 13:25 之前的最後一筆，委託以之後的收盤集合競價成交價成交；
    鉤子被呼叫時，13:30 那一筆還沒有被撮合（不會看到未來的成交）
    """

    snapshot_times: List[pd.Timestamp] = []

    def close_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        snapshot_times.append(quote.tick_quote.time)
        return [_order("2330", Action.SELL, quote.close)]

    strategy: _TickStrategy = _TickStrategy(
        {
            "open": ExecutionTiming.IMMEDIATE,
            "close": ExecutionTiming.AT_CLOSE,
            "stop_loss": ExecutionTiming.AT_CLOSE,
        },
        open_rule=_buy_once_at("10:00:00"),
        close_rule=close_rule,
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("2330", "13:24:59", 102.0, 101.5, 102.0),
        _tick("2330", "13:30:00", 105.0, 104.5, 105.5),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    records = [r for r in backtester.account.trade_records if r.is_closed]
    assert snapshot_times == [pd.Timestamp(f"{DAY} 13:24:59")]
    assert [(r.buy_price, r.sell_price) for r in records] == [(100.0, 105.0)]


# === 不重複、名額、決定性 ===
def test_stop_loss_is_submitted_once_per_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    停損條件每一筆都成立：市價停損在下一筆先撮合成交、部位平掉，之後的 tick 不會再送
    （舊版整天的 tick 一次給，會對同一檔送出上千張平倉單）
    """

    def stop_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        return [_order(quote.stock_id, Action.SELL, quote.close)]

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00"), stop_rule=stop_rule
    )
    rows: List[Dict] = [
        _tick("2330", f"10:{minute:02d}:00", 100.0, 99.5, 100.0) for minute in range(6)
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    stages: List[str] = [stage for _, stage, _ in backtester.submitted_orders]
    assert stages.count("stop_loss") == 1
    assert not backtester.account.check_has_position("2330")


def test_resting_exit_is_not_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    限價平倉單掛著沒成交：之後每一筆都不再拿這一檔去問平倉，不會疊出好幾張賣單
    （疊單的話，價格一到會把同一個部位賣好幾次）
    """

    def close_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        return [_order(quote.stock_id, Action.SELL, 200.0)]

    def buy_at_market(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:00:00":
            return [_order("2330", Action.BUY, 101.0)]
        return []

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL,
        open_rule=buy_at_market,
        close_rule=close_rule,
        live_execution=ExecutionStyle.LIMIT,
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 101.0, 100.5, 101.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
    ] + [_tick("2330", f"10:0{i}:00", 100.0, 99.5, 100.0) for i in range(2, 7)]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    stages: List[str] = [stage for _, stage, _ in backtester.submitted_orders]
    assert stages.count("close") == 1
    assert backtester.event_counts["expired_at_close"] == 1


def test_pending_open_orders_take_holding_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在途的開倉委託也佔持倉名額：上限 1 檔時，第一張還沒成交，第二檔的開倉就被擋"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "10:00:00":
            return [_order(quote.stock_id, Action.BUY, quote.close)]
        return []

    strategy: _TickStrategy = _TickStrategy(IMMEDIATE_ALL, open_rule=rule)
    strategy.max_holdings = 1
    rows: List[Dict] = [
        _tick("1101", "10:00:00", 40.0, 39.9, 40.0),
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("1101", "10:01:00", 40.0, 39.9, 40.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert [p.symbol for p in backtester.account.positions] == ["1101"]
    assert backtester.event_counts["rejected_max_holdings"] == 1


def test_duplicate_open_while_pending_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一檔已有在途的限價開倉單：之後的開倉訊號略過並計數，不會疊單"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        return [_order("2330", Action.BUY, 95.0)]

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=rule, live_execution=ExecutionStyle.LIMIT
    )
    rows: List[Dict] = [
        _tick("2330", f"10:0{i}:00", 100.0, 99.5, 100.0) for i in range(3)
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert backtester.event_counts["rejected_duplicate_pending"] == 2
    assert backtester.event_counts["expired_at_close"] == 1


def test_same_data_twice_gives_identical_trades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """決定性：同一份資料跑兩次，成交紀錄逐筆相同"""

    def make_strategy() -> _TickStrategy:
        def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
            if not strategy.account.check_has_position(quote.stock_id):
                return [_order(quote.stock_id, Action.BUY, quote.close)]
            return []

        def stop_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
            return [_order(quote.stock_id, Action.SELL, quote.close)]

        return _TickStrategy(IMMEDIATE_ALL, open_rule=rule, stop_rule=stop_rule)

    rows: List[Dict] = [
        _tick(stock, f"10:{minute:02d}:00", price + minute, price - 1, price + 1)
        for minute in range(4)
        for stock, price in (("1101", 40.0), ("2330", 100.0))
    ]

    def trades(backtester: Backtester) -> List[Tuple]:
        return [
            (r.symbol, r.buy_price, r.sell_price, r.buy_volume)
            for r in backtester.account.trade_records
        ]

    first: List[Tuple] = trades(_run(make_strategy(), rows, monkeypatch))
    second: List[Tuple] = trades(_run(make_strategy(), rows, monkeypatch))

    assert first == second
    assert first


# === 盤中帳務與日終結算 ===
def test_same_day_round_trip_costs_match_day_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    同一天先買後賣：逐筆與日 K 走同一套帳務，同價同量時手續費、證交稅（當沖減半）、損益完全相同
    """

    def close_rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        if quote.tick_quote.time.strftime("%H:%M:%S") == "11:00:00":
            return [_order("2330", Action.SELL, quote.close)]
        return []

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00"), close_rule=close_rule
    )
    strategy.allow_day_trade = True
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 99.5, 99.0, 99.5),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("2330", "11:00:00", 102.0, 101.5, 102.0),
        _tick("2330", "11:01:00", 102.5, 102.0, 102.5),
    ]
    tick_records = [
        r
        for r in _run(strategy, rows, monkeypatch).account.trade_records
        if r.is_closed
    ]

    day_strategy = ScriptedStrategy(
        open_script={DAY: [_order("2330", Action.BUY, 100.0)]},
        close_script={DAY: [_order("2330", Action.SELL, 102.0)]},
    )
    day_strategy.allow_day_trade = True
    day_strategy.start_date = DAY
    day_strategy.end_date = DAY
    day_backtester: Backtester = build_backtester(day_strategy, write_artifacts=False)
    run_bar(
        day_backtester,
        DAY,
        [build_stock_quote("2330", DAY, cur_price=101.0, high=103.0, low=99.0)],
    )
    day_records = [r for r in day_backtester.account.trade_records if r.is_closed]

    def costs(records) -> List[Tuple]:
        return [
            (r.buy_price, r.sell_price, r.commission, r.tax, r.realized_pnl)
            for r in records
        ]

    assert costs(tick_records) == costs(day_records)
    assert len(tick_records) == 1
    # 當沖減半真的有套到：102 元 × 1 張的證交稅全額 0.3% 是 306，減半為 153
    assert tick_records[0].tax == 153


def test_intraday_fill_reduces_cash_for_later_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盤中成交即時入帳：第一筆成交用掉資金後，後面的開倉單因餘額不足被擋"""

    def rule(quote: StockQuote, strategy: _TickStrategy) -> List[StockOrder]:
        clock: str = quote.tick_quote.time.strftime("%H:%M:%S")
        if (quote.stock_id, clock) in {("2330", "10:00:00"), ("1101", "11:00:00")}:
            return [_order(quote.stock_id, Action.BUY, quote.close)]
        return []

    strategy: _TickStrategy = _TickStrategy(IMMEDIATE_ALL, open_rule=rule)
    strategy.init_capital = 150_000.0
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("1101", "11:00:00", 100.0, 99.5, 100.0),
        _tick("1101", "11:01:00", 100.0, 99.5, 100.0),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    assert [p.symbol for p in backtester.account.positions] == ["2330"]
    assert backtester.event_counts["rejected_insufficient_balance"] == 1


def test_end_of_day_settlement_uses_last_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """日終結算與權益快照用每檔最後一筆：持有到收盤的部位以收盤成交價盯市"""

    strategy: _TickStrategy = _TickStrategy(
        IMMEDIATE_ALL, open_rule=_buy_once_at("10:00:00")
    )
    rows: List[Dict] = [
        _tick("2330", "10:00:00", 100.0, 99.5, 100.0),
        _tick("2330", "10:01:00", 100.0, 99.5, 100.0),
        _tick("2330", "13:30:00", 110.0, 109.5, 110.0),
    ]

    backtester: Backtester = _run(strategy, rows, monkeypatch)

    position_value: float = 110.0 * 1000
    assert backtester.daily_equity[-1]["Equity"] == pytest.approx(
        backtester.account.balance + position_value
    )
    assert backtester.fill_model.reference_prices["2330"] == 110.0
