import datetime
import sqlite3
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import build_live_trader
from core.live.risk.risk_manager import RiskDecision
from core.live.trader import LiveTrader, StrategyContext
from core.models import FuturesOrder
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


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """資料源建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


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


def test_one_lot_of_tx_clears_the_risk_checks(dao: LiveTradeDAO) -> None:
    """
    演練策略的一口台指期通過實盤逐單風控（金額走 context 的換算器，與送單時相同）

    換月日的兩腿走同一道檢查，平倉腿通過之後開倉腿也要通過（單日累計）。
    金額的口徑若日後改成含契約乘數，這條會先紅——提醒額度要跟著調。
    """

    trader: LiveTrader = build_trader(dao)
    contexts: Dict[str, StrategyContext] = {
        context.name: context for context in trader.contexts
    }
    context: StrategyContext = contexts["FuturesRolloverRehearsalStrategy"]

    decisions: List[RiskDecision] = []
    for expiry, action in (("202610", Action.SELL), ("202611", Action.BUY)):
        order: FuturesOrder = FuturesOrder(
            product="TX",
            expiry=expiry,
            date=datetime.date(2026, 10, 20),
            action=action,
            position_type=PositionType.LONG,
            price=TX_PRICE,
            volume=1,
        )
        amount: float = context.notional(order)
        decisions.append(
            trader.risk_manager.check(
                order,
                context.name,
                context.strategy.init_capital,
                amount,
                reference_price=TX_PRICE,
            )
        )

    assert [decision.passed for decision in decisions] == [True, True]
