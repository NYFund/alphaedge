import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import build_live_trader
from core.live.risk.margin_gate import MarginGate
from core.live.trader import LiveTrader, StrategyContext
from core.models import BaseOrder, FuturesOrder
from core.utils import Action, FuturesPriceType, PositionType

from .conftest import FakeBroker
from .test_live_factory_and_entry import LiveFuturesStrategy

"""
送單前的保證金檢查：策略層與帳戶層兩道

- **策略層**：該策略自己的帳戶可動用餘額，口徑與回測 `open_position()` 相同，
  模擬環境也有效。
- **帳戶層**：券商回報的可用保證金。模擬環境一律回 0（2026-09-22 實測），
  「全部為 0」視為沒有資料——**模擬環境略過、正式環境擋單**。
- 同一批送單逐張累計，不各自拿同一份可用保證金比。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 22, 13, 35)


def snapshot(available: float, equity: float) -> Any:
    return SimpleNamespace(available_margin=available, total_equity=equity)


# === 策略層 ===
def test_strategy_balance_short_blocks() -> None:
    gate: MarginGate = MarginGate(lambda: snapshot(1e9, 1e9), simulation=False)

    reason: Optional[str] = gate.check("Alpha", 300_000.0, 200_000.0)

    assert reason is not None and "策略可動用餘額不足" in reason


def test_strategy_budget_accumulates_within_a_batch() -> None:
    """兩張各自過得了、合計超過：第二張要擋"""

    gate: MarginGate = MarginGate(lambda: snapshot(1e9, 1e9), simulation=False)

    assert gate.check("Alpha", 150_000.0, 200_000.0) is None
    assert gate.check("Alpha", 150_000.0, 200_000.0) is not None
    # 另一支策略有自己的額度
    assert gate.check("Beta", 150_000.0, 200_000.0) is None


# === 帳戶層 ===
def test_broker_margin_short_blocks_and_accumulates() -> None:
    """多支策略共用一個帳戶：各自額度都夠，合計超過券商可用保證金時擋下"""

    gate: MarginGate = MarginGate(lambda: snapshot(250_000.0, 500_000.0), False)

    assert gate.check("Alpha", 150_000.0, 1e9) is None
    reason: Optional[str] = gate.check("Beta", 150_000.0, 1e9)

    assert reason is not None and "帳戶可用保證金不足" in reason


def test_broker_is_queried_once_per_batch() -> None:
    """帳務查詢有額度，一批只查一次"""

    calls: List[int] = []

    def query() -> Any:
        calls.append(1)
        return snapshot(1e9, 1e9)

    gate: MarginGate = MarginGate(query, simulation=False)
    gate.check("Alpha", 1.0, 1e9)
    gate.check("Alpha", 1.0, 1e9)

    assert calls == [1]


@pytest.mark.parametrize(
    "query",
    [
        lambda: snapshot(0.0, 0.0),
        None,
        lambda: (_ for _ in ()).throw(ConnectionError("斷線")),
    ],
    ids=["all-zero", "no-query", "query-raises"],
)
def test_unavailable_broker_margin_is_skipped_in_simulation(query: Any) -> None:
    """模擬環境的券商保證金一律回 0，略過帳戶層、只做策略層"""

    gate: MarginGate = MarginGate(query, simulation=True)

    assert gate.check("Alpha", 100_000.0, 200_000.0) is None
    assert gate.check("Alpha", 150_000.0, 200_000.0) is not None


@pytest.mark.parametrize(
    "query",
    [
        lambda: snapshot(0.0, 0.0),
        None,
        lambda: (_ for _ in ()).throw(ConnectionError("斷線")),
    ],
    ids=["all-zero", "no-query", "query-raises"],
)
def test_unavailable_broker_margin_blocks_in_production(query: Any) -> None:
    """正式環境查不到保證金就不送：那等於把檢查交給券商退單"""

    gate: MarginGate = MarginGate(query, simulation=False)

    reason: Optional[str] = gate.check("Alpha", 100_000.0, 200_000.0)

    assert reason is not None and "正式環境" in reason


# === 引擎：dispatch 內的檢查 ===
@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """歷史資料庫換成空的 in-memory 連線；本檔不需要任何歷史資料"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


def build_futures_trader(dao: LiveTradeDAO, simulation: bool) -> LiveTrader:
    """
    組一個只有期貨策略的引擎

    資金額度分配直接放行：本檔只驗保證金這一關，額度要跑過 `prepare()`
    向券商取帳務才有值，與這裡要驗的事無關。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    trader: LiveTrader = build_live_trader(
        [LiveFuturesStrategy()],
        broker=broker,
        dao=dao,
        simulation=simulation,
        run_id="20260922133500",
        now_provider=lambda: NOW,
    )
    trader.allocator.reserve = lambda name, amount: True  # type: ignore[assignment]
    return trader


def futures_order(action: Action) -> FuturesOrder:
    return FuturesOrder(
        product="TX",
        expiry="202610",
        date=NOW,
        action=action,
        position_type=PositionType.LONG,
        volume=1,
        price=48000.0,
        price_type=FuturesPriceType.LMT,
    )


def margin_events(dao: LiveTradeDAO) -> List[Tuple[Any, ...]]:
    return dao.conn.execute(
        "SELECT strategy_name, category FROM live_risk_event "
        "WHERE category = 'MARGIN_INSUFFICIENT'"
    ).fetchall()


def test_factory_wires_the_requirement_and_the_margin_table(dao: LiveTradeDAO) -> None:
    """
    期貨策略的 context 帶保證金需求計算，保證金表由實盤資料源注入

    不注入時查表模式會**靜默退回「契約價值 × 10%」**，送單前與成交後的判斷
    都跟回測對不上。
    """

    trader: LiveTrader = build_futures_trader(dao, simulation=True)
    context: StrategyContext = trader.contexts[0]

    assert context.calculate_opening_requirement is not None
    assert context.strategy.margin_config.api is not None


def test_insufficient_margin_order_is_not_sent(dao: LiveTradeDAO) -> None:
    """需要的資金超過策略可動用餘額：不送出、寫事件"""

    trader: LiveTrader = build_futures_trader(dao, simulation=True)
    context: StrategyContext = trader.contexts[0]
    context.calculate_opening_requirement = lambda order: 10_000_000.0

    unsent: List[Tuple[StrategyContext, BaseOrder]] = trader.dispatch(
        [(context, futures_order(Action.BUY))], None
    )

    assert len(unsent) == 1
    assert trader.broker.placed_count == 0
    assert margin_events(dao) == [("LiveFuturesStrategy", "MARGIN_INSUFFICIENT")]


def test_sufficient_margin_order_is_sent(dao: LiveTradeDAO) -> None:
    trader: LiveTrader = build_futures_trader(dao, simulation=True)
    context: StrategyContext = trader.contexts[0]
    context.calculate_opening_requirement = lambda order: 100_000.0

    unsent: List[Tuple[StrategyContext, BaseOrder]] = trader.dispatch(
        [(context, futures_order(Action.BUY))], None
    )

    assert unsent == []
    assert trader.broker.placed_count == 1


def test_closing_order_skips_the_margin_check(dao: LiveTradeDAO) -> None:
    """平倉釋放保證金，擋下它只會讓部位留在場上"""

    trader: LiveTrader = build_futures_trader(dao, simulation=False)
    context: StrategyContext = trader.contexts[0]
    context.calculate_opening_requirement = lambda order: 10_000_000.0

    trader.dispatch([(context, futures_order(Action.SELL))], None)

    assert trader.broker.placed_count == 1
    assert margin_events(dao) == []


def test_unknown_requirement_blocks_the_order(dao: LiveTradeDAO) -> None:
    """算不出需要的保證金（例如保證金表沒有這個商品）時擋單，不猜"""

    trader: LiveTrader = build_futures_trader(dao, simulation=True)
    context: StrategyContext = trader.contexts[0]

    def missing(order: BaseOrder) -> float:
        raise ValueError("查無 TX 在 2026-09-22 生效的保證金")

    context.calculate_opening_requirement = missing

    trader.dispatch([(context, futures_order(Action.BUY))], None)

    assert trader.broker.placed_count == 0
    assert len(margin_events(dao)) == 1


def test_production_without_broker_margin_blocks(dao: LiveTradeDAO) -> None:
    """正式環境查不到券商保證金：策略層過得了也不送"""

    trader: LiveTrader = build_futures_trader(dao, simulation=False)
    context: StrategyContext = trader.contexts[0]
    context.calculate_opening_requirement = lambda order: 100_000.0
    trader.margin_query = lambda: snapshot(0.0, 0.0)

    trader.dispatch([(context, futures_order(Action.BUY))], None)

    assert trader.broker.placed_count == 0
    assert len(margin_events(dao)) == 1
