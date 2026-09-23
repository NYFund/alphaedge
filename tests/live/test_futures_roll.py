import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.base import RollPlan
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.factory import build_live_trader, to_live_roll_config
from core.live.risk.trading_mode import TradingMode
from core.live.trader import LiveTrader, StrategyContext
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import ExecutionReport, FuturesOrder, FuturesQuote
from core.utils import (
    Action,
    ExecutionTiming,
    FuturesPriceType,
    FuturesRollRule,
    OrderType,
    PositionType,
)

from .conftest import FakeBroker
from .test_live_factory_and_entry import LiveFuturesStrategy

"""
期貨換月：先平舊月、再開新月

實盤最晚要在最後交易日的**前一個交易日**換（2026-09-22 使用者裁示）：台指期最後交易日
13:30 收盤，期貨尾盤段從 13:30 起，撐到那天舊月就只能被交易所結算。

兩腿有順序相依：平倉腿沒成交就不送開倉腿（否則兩個月份同時在場上、曝險翻倍）；
平倉腿成交而開倉腿送不出去時曝險消失，要記 CRITICAL。
"""

# 2026-10 台指期的最後交易日是 10/21（第三個星期三）
LAST_TRADING_DAY: datetime.date = datetime.date(2026, 10, 21)
DAY_BEFORE: datetime.date = datetime.date(2026, 10, 20)


# === 換月規則的實盤轉換 ===
@pytest.mark.parametrize(
    ("rule", "days", "expected_days"),
    [
        (FuturesRollRule.LAST_TRADING_DAY, 1, 1),
        (FuturesRollRule.DAYS_BEFORE_EXPIRY, 0, 1),
        (FuturesRollRule.DAYS_BEFORE_EXPIRY, 3, 3),
    ],
)
def test_live_roll_is_at_least_one_trading_day_early(
    rule: FuturesRollRule, days: int, expected_days: int
) -> None:
    """撐到最後交易日（或提前 0 日）在實盤一律改成提前 1 個交易日；更早的不動"""

    original: FuturesRollConfig = FuturesRollConfig(rule=rule, days_before_expiry=days)

    live: FuturesRollConfig = to_live_roll_config(original)

    assert live.rule is FuturesRollRule.DAYS_BEFORE_EXPIRY
    assert live.days_before_expiry == expected_days
    # 原物件不動：回測（parity 用的單日回測）仍照策略宣告的規則跑
    assert (original.rule, original.days_before_expiry) == (rule, days)


def test_open_interest_rule_is_refused() -> None:
    """未沖銷量交叉要當日的未沖銷量，實盤盤中取不到"""

    with pytest.raises(ValueError, match="OPEN_INTEREST"):
        to_live_roll_config(FuturesRollConfig(rule=FuturesRollRule.OPEN_INTEREST))


# === 資料源：今天該不該換、換去哪 ===
def weekdays(start: datetime.date, end: datetime.date) -> List[datetime.date]:
    days: List[datetime.date] = []
    current: datetime.date = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += datetime.timedelta(days=1)
    return days


class FakeResolver:
    """列出掛牌月份、以（商品, 月份）取合約"""

    def __init__(self, expiries: List[str]) -> None:
        self.expiries: List[str] = expiries

    def list_index_futures_expiries(self, product: str) -> List[str]:
        return list(self.expiries)

    def resolve_index_futures(self, product: str, expiry: str) -> Any:
        return SimpleNamespace(product=product, expiry=expiry)


class RollBroker:
    """只提供換月判定要用的兩件事：合約解析與期貨快照"""

    def __init__(self, prices: Dict[str, float], expiries: List[str]) -> None:
        self.resolver: FakeResolver = FakeResolver(expiries)
        self.prices: Dict[str, float] = prices

    def get_futures_snapshots(self, contracts: List[Any]) -> List[FuturesQuote]:
        quotes: List[FuturesQuote] = []
        for contract in contracts:
            price: Optional[float] = self.prices.get(
                f"{contract.product}{contract.expiry}"
            )
            if price is not None:
                quotes.append(
                    FuturesQuote(
                        product=contract.product,
                        expiry=contract.expiry,
                        cur_price=price,
                        close=price,
                    )
                )
        return quotes


def make_feed(
    today: datetime.date,
    prices: Optional[Dict[str, float]] = None,
    expiries: Optional[List[str]] = None,
) -> TwFuturesLiveDataFeed:
    config: FuturesRollConfig = to_live_roll_config(FuturesRollConfig())
    config.calendar = FuturesCalendar(
        weekdays(datetime.date(2026, 9, 1), datetime.date(2026, 12, 31))
    )
    broker: RollBroker = RollBroker(
        prices if prices is not None else {"TX202610": 48000.0, "TX202611": 48100.0},
        expiries if expiries is not None else ["202610", "202611", "202612"],
    )
    return TwFuturesLiveDataFeed(
        broker,
        now_provider=lambda: datetime.datetime.combine(today, datetime.time(13, 30)),
        roll_config=config,
    )


def long_position(expiry: str = "202610", volume: int = 2) -> Any:
    return SimpleNamespace(
        product="TX",
        expiry=expiry,
        symbol=f"TX{expiry}",
        volume=volume,
        position_type=PositionType.LONG,
        is_closed=False,
    )


def test_rolls_on_the_trading_day_before_the_last_trading_day() -> None:
    """最後交易日的前一個交易日：平 10 月、以相同方向與口數開 11 月"""

    plans: List[RollPlan] = make_feed(DAY_BEFORE).plan_rolls(
        [long_position()], DAY_BEFORE
    )

    assert len(plans) == 1
    close: FuturesOrder = plans[0].close_order
    opening: FuturesOrder = plans[0].open_order
    assert (close.symbol, close.action, close.volume) == ("TX202610", Action.SELL, 2)
    assert (opening.symbol, opening.action, opening.volume) == (
        "TX202611",
        Action.BUY,
        2,
    )
    # 範圍市價 ＋ IOC：換月要的是換過去；沒立即成交就由券商取消，開倉腿隨之放棄
    for leg in (close, opening):
        assert leg.position_type is PositionType.LONG
        assert leg.price_type is FuturesPriceType.MKP
        assert leg.order_type is OrderType.IOC


def test_short_position_rolls_with_buy_then_sell() -> None:
    short: Any = long_position()
    short.position_type = PositionType.SHORT

    plan: RollPlan = make_feed(DAY_BEFORE).plan_rolls([short], DAY_BEFORE)[0]

    assert (plan.close_order.action, plan.open_order.action) == (
        Action.BUY,
        Action.SELL,
    )


def test_no_roll_well_before_expiry() -> None:
    today: datetime.date = datetime.date(2026, 10, 15)

    assert make_feed(today).plan_rolls([long_position()], today) == []


def test_position_already_in_the_active_month_is_not_rolled_back() -> None:
    """
    只往遠月換：部位已在當家契約或更遠的月份時不動（回測同一條規則）

    12 月的部位在換月日遇到當家契約 11 月，往回換就是憑空付兩次手續費與價差。
    """

    # 三個月份都有報價，擋下的一定是「只往遠月換」這條，不是缺價
    feed: TwFuturesLiveDataFeed = make_feed(
        DAY_BEFORE,
        prices={"TX202610": 48000.0, "TX202611": 48100.0, "TX202612": 48200.0},
    )

    assert feed.plan_rolls([long_position("202611")], DAY_BEFORE) == []
    assert feed.plan_rolls([long_position("202612")], DAY_BEFORE) == []


def test_weekly_contract_is_not_rolled() -> None:
    """週契約是不同的商品，換成月契約不是同一條曝險的延續"""

    assert (
        make_feed(DAY_BEFORE).plan_rolls([long_position("202610W4")], DAY_BEFORE) == []
    )


def test_missing_snapshot_skips_the_roll() -> None:
    """沒有參考價就不換：風控與保證金檢查都做不了"""

    feed: TwFuturesLiveDataFeed = make_feed(DAY_BEFORE, prices={"TX202610": 48000.0})

    assert feed.plan_rolls([long_position()], DAY_BEFORE) == []


# === 引擎：兩腿依序執行 ===
@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """歷史資料庫換成空的 in-memory 連線；本檔不需要任何歷史資料"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


def build_trader(dao: LiveTradeDAO, fill_ratio: float) -> Tuple[LiveTrader, FakeBroker]:
    """
    組一個期貨引擎，換月計畫固定為「10 月 → 11 月」

    資金額度與保證金直接放行：本檔只驗兩腿的先後與放棄，那兩關各有自己的測試。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = fill_ratio
    trader: LiveTrader = build_live_trader(
        [LiveFuturesStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261020133000",
        now_provider=lambda: datetime.datetime(2026, 10, 20, 13, 30),
    )
    trader.allocator.reserve = lambda name, amount: True  # type: ignore[assignment]
    trader._sleep = lambda seconds: None
    context: StrategyContext = trader.contexts[0]
    context.calculate_opening_requirement = lambda order: 0.0
    # 測試的資料庫沒有保證金表：部位管理改用比率模式，再先開好一口 10 月多單，
    # 平倉腿的成交才會被帳戶同步認成「平多」而不是「開空」
    context.position_manager.margin_config = FuturesMarginConfig.ratio()
    trader.account_sync.apply_fill(
        ExecutionReport(
            broker_seqno="seed",
            broker_trade_id="seed",
            symbol="TX202610",
            action=Action.BUY,
            price=48000.0,
            volume=1,
            ts=datetime.datetime(2026, 10, 19, 13, 30),
        ),
        strategy_name=context.name,
    )

    def leg(expiry: str, action: Action) -> FuturesOrder:
        return FuturesOrder(
            product="TX",
            expiry=expiry,
            action=action,
            position_type=PositionType.LONG,
            volume=1,
            price=48000.0,
            order_type=OrderType.IOC,
            price_type=FuturesPriceType.MKP,
        )

    context.data_feed.plan_rolls = lambda positions, today: [  # type: ignore
        RollPlan(
            close_order=leg("202610", Action.SELL),
            open_order=leg("202611", Action.BUY),
            reason="TX202610 → TX202611（1 口）",
        )
    ]
    return (trader, broker)


def roll_events(dao: LiveTradeDAO) -> List[Tuple[Any, ...]]:
    return dao.conn.execute(
        "SELECT category, severity FROM live_risk_event "
        "WHERE category LIKE 'ROLL_%' ORDER BY event_id"
    ).fetchall()


def placed_symbols(broker: FakeBroker) -> List[str]:
    return [ticket.order.symbol for ticket in broker.tickets.values()]


def test_close_leg_fills_then_open_leg_is_sent(dao: LiveTradeDAO) -> None:
    trader, broker = build_trader(dao, fill_ratio=1.0)

    assert trader.execute_rolls(None) == 1
    assert placed_symbols(broker) == ["TX202610", "TX202611"]
    assert roll_events(dao) == []


def test_unfilled_close_leg_abandons_the_open_leg(dao: LiveTradeDAO) -> None:
    """平倉腿沒成交：不送開倉腿（否則兩個月份同時在場上），寫事件"""

    trader, broker = build_trader(dao, fill_ratio=0.0)

    assert trader.execute_rolls(None) == 0
    assert placed_symbols(broker) == ["TX202610"]
    assert roll_events(dao) == [("ROLL_ABANDONED", "WARN")]


def test_open_leg_failure_after_close_fill_is_critical(dao: LiveTradeDAO) -> None:
    """舊月已平、新月送不出去：曝險消失，要馬上有人知道"""

    trader, broker = build_trader(dao, fill_ratio=1.0)
    calls: List[str] = []

    def reserve_only_once(name: str, amount: float) -> bool:
        calls.append(name)
        return len(calls) == 1

    trader.allocator.reserve = reserve_only_once  # type: ignore[assignment]

    assert trader.execute_rolls(None) == 0
    assert placed_symbols(broker) == ["TX202610"]
    assert roll_events(dao) == [("ROLL_OPEN_FAILED", "CRITICAL")]


def test_reduce_only_mode_skips_the_roll(dao: LiveTradeDAO) -> None:
    """開倉腿是新曝險；不允許開新倉時整筆不動，而不是只平不開"""

    trader, broker = build_trader(dao, fill_ratio=1.0)
    trader.mode_state.degrade(TradingMode.REDUCE_ONLY, "測試")

    assert trader.execute_rolls(None) == 0
    assert placed_symbols(broker) == []
    assert roll_events(dao) == [("ROLL_SKIPPED", "WARN")]


def test_factory_injects_a_live_roll_config_and_calendar(dao: LiveTradeDAO) -> None:
    """策略挑合約與轉倉共用同一份實盤換月設定，且都有日曆可用"""

    trader, _ = build_trader(dao, fill_ratio=1.0)
    context: StrategyContext = trader.contexts[0]

    assert context.strategy.roll_config is context.data_feed.roll_config
    assert context.strategy.roll_config.rule is FuturesRollRule.DAYS_BEFORE_EXPIRY
    assert context.strategy.roll_config.calendar is not None


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        (ExecutionTiming.AT_CLOSE, ["TX202610", "TX202611"]),
        (ExecutionTiming.AT_OPEN, []),
    ],
)
def test_close_segment_rolls_before_collecting_signals(
    dao: LiveTradeDAO, timing: ExecutionTiming, expected: List[str]
) -> None:
    """換月掛在尾盤段開頭；開盤段不換（換月與回測的轉倉同在收盤時點）"""

    trader, broker = build_trader(dao, fill_ratio=1.0)

    trader.submit_segment(timing, None)

    assert placed_symbols(broker)[: len(expected)] == expected
    if not expected:
        assert "TX202611" not in placed_symbols(broker)
