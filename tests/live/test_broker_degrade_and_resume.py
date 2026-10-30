import datetime
import threading
from typing import Any, Dict, List

import pytest

import apps.live as live_entry
from core.broker.base import (
    BROKER_CLOCK_SKEW,
    BROKER_RECONNECT_EXHAUSTED,
    BROKER_SESSION_DOWN,
)
from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.factory import build_live_trader
from core.live.risk import event_categories
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.trader import LiveTrader

from .conftest import FakeBroker
from .test_live_factory_and_entry import (  # noqa: F401  in_memory_history_db 是 autouse fixture
    AnotherLiveStockStrategy,
    LiveStockStrategy,
    in_memory_history_db,
)
from .test_shioaji_broker import FakeApi, FakeSession

"""
券商連線降級的分流，以及 `--resume-trading` 指名恢復

連線中斷、重連次數用完、時鐘偏差三種偵測都跑在券商的執行緒上，原本只記 log、不降級。
裁示的分流：
- 連線中斷 → **不降級**，寫 WARN 事件並推播；重連成功、恢復流程（含對帳）跑完再寫一筆。
  降級會落地並延續到之後的段落，短暫斷線不該停掉之後的開倉；有漏接由重連後的對帳降級。
- 重連次數用完、時鐘偏差 → 帳戶層降 `REDUCE_ONLY` 並推 CRITICAL（不降 `HALTED`，停損要送得出去）。
- 降級要寫紀錄庫，而 SQLite 連線不可跨執行緒：券商執行緒只入列，由引擎主執行緒取出處理。

`--resume-trading`：不給策略名維持全部恢復；給策略名時只恢復那幾支、帳戶層不動。
"""

NOW: datetime.datetime = datetime.datetime(2026, 10, 19, 13, 25)
RUN_DATE: datetime.date = NOW.date()


def make_trader(dao: LiveTradeDAO, broker: FakeBroker) -> LiveTrader:
    """以假券商組出單一台股策略的引擎"""

    return build_live_trader(
        [LiveStockStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261019132000",
        now_provider=lambda: NOW,
    )


def categories(dao: LiveTradeDAO) -> List[str]:
    """當日寫過的風控事件類別"""

    return [row["category"] for row in dao.get_risk_events_by_date(RUN_DATE)]


# === 降級分流 ===
def test_session_down_records_a_warning_but_does_not_degrade(
    dao: LiveTradeDAO,
) -> None:
    """連線中斷：寫事件、不降級"""

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = make_trader(dao, broker)

    broker.report_degrade(BROKER_SESSION_DOWN, "Shioaji session 中斷")
    trader.connection.apply_broker_degrades()

    assert trader.mode_state.account_mode is TradingMode.NORMAL
    assert event_categories.BROKER_SESSION_DOWN in categories(dao)
    assert event_categories.DEGRADE not in categories(dao)


@pytest.mark.parametrize("kind", [BROKER_RECONNECT_EXHAUSTED, BROKER_CLOCK_SKEW])
def test_persistent_faults_degrade_to_reduce_only(dao: LiveTradeDAO, kind: str) -> None:
    """重連次數用完、時鐘偏差：帳戶層降 REDUCE_ONLY（不是 HALTED）"""

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = make_trader(dao, broker)

    broker.report_degrade(kind, "持續性故障")
    trader.connection.apply_broker_degrades()

    assert trader.mode_state.account_mode is TradingMode.REDUCE_ONLY
    assert event_categories.DEGRADE in categories(dao)


def test_dispatch_applies_pending_degrades_before_sending(dao: LiveTradeDAO) -> None:
    """送單前先套用：時鐘已不可信的這一批不可照樣以 NORMAL 送出"""

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = make_trader(dao, broker)

    broker.report_degrade(BROKER_CLOCK_SKEW, "本機時鐘偏差 60 秒")
    trader.execution.dispatch([], None)

    assert trader.mode_state.account_mode is TradingMode.REDUCE_ONLY


def test_events_reported_from_another_thread_are_applied_on_the_main_thread(
    dao: LiveTradeDAO,
) -> None:
    """券商執行緒只入列；寫紀錄庫發生在主執行緒，不會撞上 SQLite 的跨執行緒限制"""

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = make_trader(dao, broker)

    worker: threading.Thread = threading.Thread(
        target=broker.report_degrade, args=(BROKER_RECONNECT_EXHAUSTED, "次數用完")
    )
    worker.start()
    worker.join()
    trader.connection.apply_broker_degrades()

    assert trader.mode_state.account_mode is TradingMode.REDUCE_ONLY
    assert broker.drain_degrade_events() == []


def test_successful_reconnect_records_a_warning(dao: LiveTradeDAO) -> None:
    """斷線後重連成功、恢復流程跑完：不降級，留一筆重連成功的紀錄"""

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = make_trader(dao, broker)
    broker.connect()
    broker.close()

    assert trader.connection.ensure_connected() is True
    assert trader.mode_state.account_mode is TradingMode.NORMAL
    assert event_categories.BROKER_RECONNECTED in categories(dao)


def test_shioaji_broker_routes_session_degrades_into_its_queue() -> None:
    """閘道建立時把 session 的降級回呼接到自己的佇列（session 本身不碰紀錄庫）"""

    limiter: RateLimiter = RateLimiter(time_source=lambda: 0.0, sleep=lambda s: None)
    session: FakeSession = FakeSession(FakeApi(), limiter)
    broker: ShioajiBroker = ShioajiBroker(session, limiter)

    assert session.degrade_handler is not None
    session.degrade_handler(BROKER_CLOCK_SKEW, "偏差")

    assert broker.drain_degrade_events() == [(BROKER_CLOCK_SKEW, "偏差")]


# === 指名恢復 ===
def _degrade_everything(dao: LiveTradeDAO) -> None:
    """上一次啟動以帳戶層 REDUCE_ONLY 結束，兩支策略也各自降級並落地"""

    dao.insert_run(
        {"run_id": "run0", "started_at": NOW, "phase": "close", "simulation": 1}
    )
    state: TradingModeState = TradingModeState(dao, "run0", lambda: NOW)
    state.degrade(TradingMode.REDUCE_ONLY, "當日虧損", strategy_name="A")
    state.degrade(TradingMode.REDUCE_ONLY, "當日虧損", strategy_name="B")
    dao.finish_run("run0", NOW, "對帳不一致", "REDUCE_ONLY")


def test_resume_without_names_restores_everything(dao: LiveTradeDAO) -> None:
    """不給策略名：帳戶層與所有已記錄的策略一起恢復（既有的恢復流程不變）"""

    _degrade_everything(dao)
    state: TradingModeState = TradingModeState(
        dao=dao, run_id="run1", now_provider=lambda: NOW, resume_trading=True
    )
    state.load()

    assert state.account_mode is TradingMode.NORMAL
    assert state.strategy_modes == {"A": TradingMode.NORMAL, "B": TradingMode.NORMAL}


def test_resume_with_names_restores_only_those_strategies(dao: LiveTradeDAO) -> None:
    """給策略名：只恢復那幾支，帳戶層與其他策略的降級維持"""

    _degrade_everything(dao)
    state: TradingModeState = TradingModeState(
        dao=dao,
        run_id="run1",
        now_provider=lambda: NOW,
        resume_trading=True,
        resume_strategies=["A"],
    )
    state.load()

    assert state.account_mode is TradingMode.REDUCE_ONLY
    assert state.strategy_modes["A"] is TradingMode.NORMAL
    assert state.strategy_modes["B"] is TradingMode.REDUCE_ONLY


def test_named_resume_does_not_unlock_the_account_level(dao: LiveTradeDAO) -> None:
    """指名恢復時，引擎不可把「帳戶層非 NORMAL」當成已恢復而放行送單"""

    named: LiveTrader = build_live_trader(
        [LiveStockStrategy(), AnotherLiveStockStrategy()],
        broker=FakeBroker(),
        dao=dao,
        run_id="20261019132000",
        now_provider=lambda: NOW,
        resume_trading=True,
        resume_strategies=["LiveStockStrategy"],
    )
    everything: LiveTrader = build_live_trader(
        [LiveStockStrategy()],
        broker=FakeBroker(),
        dao=dao,
        run_id="20261019132001",
        now_provider=lambda: NOW,
        resume_trading=True,
    )

    assert named.resume_trading is False
    assert named.mode_state.resume_strategies == ["LiveStockStrategy"]
    assert everything.resume_trading is True


def test_cli_rejects_resume_names_outside_the_loaded_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指名的策略不在 --strategy 之中：用法錯誤（打錯名字的話恢復會靜默落空）"""

    built: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        live_entry, "build_live_trader", lambda *args, **kwargs: built.append(kwargs)
    )

    code: int = live_entry.main(
        [
            "--strategy",
            "VolumeBreakoutMomentumStrategy",
            "--phase",
            "after_close",
            "--resume-trading",
            "IntradayMomentumStrategy",
        ]
    )

    assert code == live_entry.EXIT_USAGE
    assert built == []
