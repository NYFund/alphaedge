import datetime
import sqlite3
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.factory import build_live_trader
from core.live.risk.risk_manager import ExposureItem, RiskDecision
from core.live.trader import LiveTrader, StrategyContext
from core.models import FuturesOrder
from core.models.risk_config import FuturesRiskConfig
from core.utils import Action, PositionType
from strategies.futures.futures_rollover_rehearsal_strategy import (
    FuturesRolloverRehearsalStrategy,
)
from strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy

from .conftest import FakeBroker

"""
期貨換月演練策略在實盤引擎裡：與示範策略同一個行程、一口台指期過得了風控

擋的是**排程跑了、單卻一張都送不出去**：同一個行程的段落時窗合併失敗會在啟動時
整批結束；額度不足則每張委託都被風控擋下，兩者都要等演練當天才會發現。
"""

# 2026-10 實盤報價量級（TXFJ6 參考價約 50,000 點）
TX_PRICE: float = 50000.0
# 台指期每口原始保證金（2026-08-12 起）
TX_MARGIN: float = 701000.0


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """資料源建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    # 回測與實盤的連線都經 `BaseDataFeed.connect_db()`，只要換掉基底那一個連線函式

    monkeypatch.setattr("core.datafeed.base.connect_sqlite", connect_in_memory)


def build_trader(dao: LiveTradeDAO) -> LiveTrader:
    """與演練排程相同的組合：示範策略與演練策略同一個期貨行程"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    return build_live_trader(
        [MomentumFuturesStrategy(), FuturesRolloverRehearsalStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261019132800",
        now_provider=lambda: datetime.datetime(2026, 10, 19, 13, 28),
    )


def test_both_futures_strategies_share_one_process(dao: LiveTradeDAO) -> None:
    """兩支策略的段落時窗合併得起來（同為期貨尾盤段），排程可以放在同一行"""

    trader: LiveTrader = build_trader(dao)

    names: List[str] = [context.name for context in trader.contexts]
    assert names == ["MomentumFuturesStrategy", "FuturesRolloverRehearsalStrategy"]


def stub_tx_margin(context: StrategyContext) -> None:
    """測試庫沒有保證金表：每口原始保證金固定為 2026-08 起的台指期公告值"""

    context.position_manager.margin_model.initial_margin_per_lot = (  # type: ignore[method-assign]
        lambda product, date, price, multiplier: TX_MARGIN
    )


@pytest.mark.parametrize(
    ("strategy_name", "lots"),
    [("FuturesRolloverRehearsalStrategy", 1), ("MomentumFuturesStrategy", 2)],
)
def test_futures_strategies_clear_the_risk_checks(
    dao: LiveTradeDAO, strategy_name: str, lots: int
) -> None:
    """
    兩支期貨策略的正常口數通過實盤風控：逐單以保證金計、批次以名目槓桿計

    換月日的兩腿走同一道逐單檢查，平倉腿通過之後開倉腿也要通過（單日累計）。
    期貨金額口徑曾經不乘契約乘數（一口台指期只算幾萬元），改成保證金與契約價值之後，
    `MomentumFuturesStrategy` 在 300 萬額度下開 2 口會同時超過單筆保證金與槓桿上限——
    額度與口數改了任何一邊，這條會先紅。
    """

    trader: LiveTrader = build_trader(dao)
    contexts: Dict[str, StrategyContext] = {
        context.name: context for context in trader.contexts
    }
    context: StrategyContext = contexts[strategy_name]
    stub_tx_margin(context)

    decisions: List[RiskDecision] = []
    for expiry, action in (("202610", Action.SELL), ("202611", Action.BUY)):
        order: FuturesOrder = FuturesOrder(
            product="TX",
            expiry=expiry,
            date=datetime.date(2026, 10, 20),
            action=action,
            position_type=PositionType.LONG,
            price=TX_PRICE,
            volume=lots,
        )
        decisions.append(
            trader.risk_manager.check(
                order,
                context.name,
                context.capital_limit,
                context.notional(order),
                context.risk_limits,
                reference_price=TX_PRICE,
            )
        )

    opening: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202611",
        date=datetime.date(2026, 10, 20),
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=TX_PRICE,
        volume=lots,
    )
    allowed: List[ExposureItem] = trader.risk_manager.check_batch(
        [ExposureItem(opening, context.exposure(opening))],
        context.name,
        context.capital_limit,
        context.risk_limits,
    )

    assert [decision.passed for decision in decisions] == [True, True]
    assert len(allowed) == 1, "名目槓桿超過上限，開倉單被批次曝險截掉"


def test_futures_amounts_are_margin_and_contract_value(dao: LiveTradeDAO) -> None:
    """
    期貨的資金占用是保證金、曝險是契約價值（點數 × 乘數 × 口數）

    兩者都不能退回「點數 × 口數」：那樣一口台指期只算幾萬元，金額類風控等於沒有作用。
    """

    trader: LiveTrader = build_trader(dao)
    context: StrategyContext = trader.contexts[0]
    stub_tx_margin(context)
    order: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202611",
        date=datetime.date(2026, 10, 20),
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=TX_PRICE,
        volume=2,
    )

    assert context.notional(order) == 2 * TX_MARGIN
    assert context.exposure(order) == TX_PRICE * 200 * 2
    assert isinstance(context.risk_limits, FuturesRiskConfig)
