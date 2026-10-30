import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.context import StrategyContext
from core.live.factory import build_live_trader
from core.live.trader import LiveTrader
from strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy

from .conftest import FakeBroker

"""
期貨策略的預設標的池要在券商登入後才補得到

沒宣告 `symbols` 的期貨策略，標的池由資料源以券商 `resolver` 查掛牌月份補上；
`resolver` 在登入時才建立，而資料源的 `setup()` 在組裝時就跑了。只在組裝時補的話，
標的池永遠是空的、每個段落都拿到 0 筆報價，鉤子照常被呼叫卻永遠沒有訊號，
而且不會有任何錯誤——2026-09-22 演練起期貨線就是這樣，直到 10/15 一支該建倉的期貨策略
沒有委託才被發現。

這裡的假券商**刻意不在組裝前登入**，與 `ShioajiBroker` 一樣登入後才有 `resolver`。
"""

# 台指期掛牌月份（假資料）
LISTED_EXPIRIES: List[str] = ["202610", "202611", "202612"]


class LateResolverBroker(FakeBroker):
    """登入後才有 `resolver` 的假券商，與 `ShioajiBroker` 的生命週期相同"""

    def __init__(self, expiries: Optional[List[str]] = None) -> None:
        super().__init__()
        self.resolver: Optional[Any] = None
        self.expiries: List[str] = list(
            LISTED_EXPIRIES if expiries is None else expiries
        )

    def connect(self) -> None:
        """登入：此時才建立 resolver"""

        super().connect()
        self.resolver = SimpleNamespace(
            list_index_futures_expiries=lambda product: list(self.expiries)
        )


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """資料源建構時不碰正式的歷史資料庫，一律改連 in-memory SQLite"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    # 回測與實盤的連線都經 `BaseDataFeed.connect_db()`，只要換掉基底那一個連線函式
    monkeypatch.setattr("core.datafeed.base.connect_sqlite", connect_in_memory)


def build_trader(dao: LiveTradeDAO, broker: LateResolverBroker) -> LiveTrader:
    """與排程相同的期貨行程；**組裝時券商尚未登入**"""

    return build_live_trader(
        [MomentumFuturesStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261016132800",
        now_provider=lambda: datetime.datetime(2026, 10, 16, 13, 28),
    )


def test_assembly_before_login_leaves_the_universe_empty(dao: LiveTradeDAO) -> None:
    """前提：組裝時券商還沒登入，標的池補不到（這正是要在登入後再補的原因）"""

    trader: LiveTrader = build_trader(dao, LateResolverBroker())

    assert [context.symbols for context in trader.contexts] == [[]]


def test_universe_is_filled_after_login(dao: LiveTradeDAO) -> None:
    """登入後補填：期貨策略的 `context.symbols` 是全部掛牌月份"""

    broker: LateResolverBroker = LateResolverBroker()
    trader: LiveTrader = build_trader(dao, broker)

    broker.connect()
    trader.fill_default_symbols()

    expected: List[str] = [f"TX{expiry}" for expiry in LISTED_EXPIRIES]
    for context in trader.contexts:
        assert context.symbols == expected
        assert context.strategy.symbols == expected


def test_prepare_fills_the_universe_right_after_login(dao: LiveTradeDAO) -> None:
    """`prepare()` 登入後緊接著補填，排在讀模式、對帳等任何會用到標的的步驟之前"""

    trader: LiveTrader = build_trader(dao, LateResolverBroker())
    calls: List[str] = []
    original_connect: Any = trader.broker.connect

    def connect() -> None:
        calls.append("connect")
        original_connect()

    class Stop(Exception):
        """補填之後就中止，不必跑完整個 prepare"""

    def stop() -> List[str]:
        raise Stop()

    trader.broker.connect = connect  # type: ignore[method-assign]
    trader.mark_previous_crash = stop  # type: ignore[method-assign]

    with pytest.raises(Stop):
        trader.prepare()

    assert calls == ["connect"]
    assert all(context.symbols for context in trader.contexts)


def test_universe_still_empty_after_login_writes_a_critical_event(
    dao: LiveTradeDAO,
) -> None:
    """登入後仍查不到掛牌月份：寫 `UNIVERSE_EMPTY`（CRITICAL），不再靜默"""

    broker: LateResolverBroker = LateResolverBroker(expiries=[])
    trader: LiveTrader = build_trader(dao, broker)

    broker.connect()
    trader.fill_default_symbols()

    rows: List[Any] = dao.conn.execute(
        "SELECT strategy_name, severity, category FROM live_risk_event "
        "WHERE category = 'UNIVERSE_EMPTY' ORDER BY rowid"
    ).fetchall()
    assert [row[0] for row in rows] == ["MomentumFuturesStrategy"]
    assert {row[1] for row in rows} == {"CRITICAL"}


def test_declared_symbols_are_left_alone(dao: LiveTradeDAO) -> None:
    """策略自己宣告的標的池不被登入後的補填覆寫"""

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.symbols = ["TX202611"]
    broker: LateResolverBroker = LateResolverBroker()
    trader: LiveTrader = build_live_trader(
        [strategy],
        broker=broker,
        dao=dao,
        run_id="20261016132800",
        now_provider=lambda: datetime.datetime(2026, 10, 16, 13, 28),
    )

    broker.connect()
    trader.fill_default_symbols()

    context: StrategyContext = trader.contexts[0]
    assert context.symbols == ["TX202611"]
