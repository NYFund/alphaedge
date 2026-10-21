import datetime
import sqlite3
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import build_live_trader
from core.live.risk.risk_manager import RiskDecision
from core.live.trader import LiveTrader, StrategyContext
from core.models import StockOrder
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
from core.strategies.stock.stock_round_trip_rehearsal_strategy import (
    StockRoundTripRehearsalStrategy,
)
from core.utils import Action, PositionType

from .conftest import FakeBroker

"""
股票下單路徑演練策略在實盤引擎裡：與示範策略同一個行程、1 張 0050 過得了風控

擋的是**排程跑了、單卻一張都送不出去**：同一個行程的段落時窗合併失敗會在啟動時
整批結束；額度切不出張數或金額上限擋下，兩者都要等演練當天才會發現。
"""

# 2026-10 實盤報價量級（0050 收盤約 109 元）
PRICE: float = 109.0


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """資料源與策略建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)
    # 示範策略組裝時會預建回測區間的交易日清單，in-memory 庫沒有價格表可查；
    # 這裡驗的是演練策略，示範策略只要組得起來
    monkeypatch.setattr(MomentumStrategy1, "build_trading_days", lambda self: [])


def build_trader(dao: LiveTradeDAO) -> LiveTrader:
    """與演練排程相同的組合：示範策略與演練策略同一個股票行程"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    return build_live_trader(
        [MomentumStrategy1(), StockRoundTripRehearsalStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261022132000",
        now_provider=lambda: datetime.datetime(2026, 10, 22, 13, 20),
    )


def rehearsal_context(trader: LiveTrader) -> StrategyContext:
    """取出演練策略的 context"""

    contexts: Dict[str, StrategyContext] = {
        context.name: context for context in trader.contexts
    }
    return contexts["StockRoundTripRehearsalStrategy"]


def test_both_stock_strategies_share_one_process(dao: LiveTradeDAO) -> None:
    """兩支策略的段落時窗合併得起來（同為股票尾盤段），且演練策略只看 0050"""

    trader: LiveTrader = build_trader(dao)

    names: List[str] = [context.name for context in trader.contexts]
    assert names == ["MomentumStrategy1", "StockRoundTripRehearsalStrategy"]
    assert rehearsal_context(trader).symbols == ["0050"]


def test_live_account_uses_the_rehearsal_quota(dao: LiveTradeDAO) -> None:
    """實盤帳戶以 `live_capital` 建立：張數是由 45 萬切出來的，不是 `init_capital` 的 100 萬"""

    context: StrategyContext = rehearsal_context(build_trader(dao))

    assert context.account.init_capital == 450000.0


@pytest.mark.parametrize("action", [Action.BUY, Action.SELL])
def test_one_lot_of_0050_clears_the_risk_checks(
    dao: LiveTradeDAO, action: Action
) -> None:
    """
    1 張 0050 的開倉與平倉都通過實盤逐單風控（金額走 context 的換算器，與送單時相同）

    單筆金額上限若改以 `live_capital` 為基準（45 萬的兩成只有 9 萬），開倉這條會先紅
    ——提醒額度要跟著調。
    """

    trader: LiveTrader = build_trader(dao)
    context: StrategyContext = rehearsal_context(trader)
    order: StockOrder = StockOrder(
        stock_id="0050",
        date=datetime.date(2026, 10, 22),
        action=action,
        position_type=PositionType.LONG,
        price=PRICE,
        volume=1,
    )

    decision: RiskDecision = trader.risk_manager.check(
        order,
        context.name,
        context.strategy.init_capital,
        context.notional(order),
        reference_price=PRICE,
    )

    assert decision.passed, decision.reason
