import datetime
import sqlite3
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.risk.trading_mode import TradingMode, TradingModeState

"""
斷線重連與重啟恢復

常駐程式一定會遇到斷線與崩潰，而這兩件事最危險的失效方式是**沉默**：
- 斷線後程式照跑到收盤，每一次送單都失敗。
- 崩潰後重啟，降級狀態讀不回來，帶著錯誤部位繼續交易。

第二件事正是 `TradingModeState.load()` docstring 說的「實盤最常見的事故型態」，
只是換成程式自己崩潰、而不是有人按重啟鍵。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 9, 5)
LATER: datetime.datetime = datetime.datetime(2026, 9, 21, 13, 40)


@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


def add_run(dao: LiveTradeDAO, run_id: str, started_at: datetime.datetime) -> None:
    dao.insert_run(
        {
            "run_id": run_id,
            "started_at": started_at,
            "phase": "open",
            "simulation": 1,
            "dry_run": 0,
        }
    )
    dao.conn.commit()


def run_row(dao: LiveTradeDAO, run_id: str) -> Dict[str, Any]:
    columns: List[str] = [
        str(row[1]) for row in dao.conn.execute("PRAGMA table_info(live_run)")
    ]
    values = dao.conn.execute(
        "SELECT * FROM live_run WHERE run_id = ?", (run_id,)
    ).fetchone()
    return dict(zip(columns, values))


# === 帳戶層模式要即時落地 ===
def test_account_degrade_is_persisted_immediately(dao: LiveTradeDAO) -> None:
    """
    帳戶層降級當下就要寫進 `live_run`

    只靠 `finish_run()` 寫的話，崩潰時它根本不會被呼叫——那一列會停在插入時的
    `NORMAL`，重啟讀回來就是「什麼事都沒發生」。
    """

    add_run(dao, "run1", NOW)
    state: TradingModeState = TradingModeState(dao, "run1", lambda: NOW)

    state.degrade(TradingMode.HALTED, "對帳不一致")

    assert run_row(dao, "run1")["account_mode"] == TradingMode.HALTED.value


def test_strategy_degrade_does_not_touch_the_account_row(dao: LiveTradeDAO) -> None:
    """策略層降級寫 `live_strategy_mode`，不可波及帳戶層"""

    add_run(dao, "run1", NOW)
    state: TradingModeState = TradingModeState(dao, "run1", lambda: NOW)

    state.degrade(TradingMode.REDUCE_ONLY, "當日虧損", strategy_name="Alpha")

    assert run_row(dao, "run1")["account_mode"] == TradingMode.NORMAL.value
    assert dao.get_strategy_modes()["Alpha"] == TradingMode.REDUCE_ONLY.value


# === 崩潰標記 ===
def test_crashed_run_is_marked_and_keeps_its_mode(dao: LiveTradeDAO) -> None:
    """
    上次崩潰的紀錄要標成 CRASHED，**而且不可覆寫它的模式**

    覆寫等於把崩潰當下的降級狀態擦掉，那比沒有標記還糟。
    """

    add_run(dao, "crashed", NOW)
    TradingModeState(dao, "crashed", lambda: NOW).degrade(
        TradingMode.HALTED, "部位不可信"
    )
    add_run(dao, "current", LATER)

    marked: List[str] = dao.mark_crashed_runs("current", LATER)

    assert marked == ["crashed"]
    row: Dict[str, Any] = run_row(dao, "crashed")
    assert row["end_reason"] == "CRASHED"
    assert row["ended_at"] is not None
    assert row["account_mode"] == TradingMode.HALTED.value


def test_restart_reads_back_the_mode_from_a_crash(dao: LiveTradeDAO) -> None:
    """
    **本步驟的重點**：崩潰重啟不可以靜默解除 halt

    接上之前這條路徑是兩層一起漏——`account_mode` 停在 `NORMAL`（只有
    `finish_run()` 會寫），而 `ended_at IS NULL` 又讓 `get_last_account_mode()`
    直接跳過那一列。按下重啟鍵就帶著錯誤部位繼續交易。
    """

    add_run(dao, "crashed", NOW)
    TradingModeState(dao, "crashed", lambda: NOW).degrade(
        TradingMode.HALTED, "對帳不一致"
    )

    add_run(dao, "current", LATER)
    dao.mark_crashed_runs("current", LATER)

    resumed: TradingModeState = TradingModeState(dao, "current", lambda: LATER)
    resumed.load()

    assert resumed.effective_mode() is TradingMode.HALTED


def test_normal_shutdown_is_not_reported_as_a_crash(dao: LiveTradeDAO) -> None:
    """正常結束的紀錄不可被標成崩潰——每天都告警的話沒有人會再看它"""

    add_run(dao, "yesterday", NOW)
    dao.finish_run("yesterday", LATER, "正常結束", TradingMode.NORMAL.value)
    add_run(dao, "current", LATER)

    assert dao.mark_crashed_runs("current", LATER) == []


def test_current_run_is_never_marked_as_crashed(dao: LiveTradeDAO) -> None:
    """
    正在跑的這一次也是 `ended_at IS NULL`，不可把自己標成崩潰

    標掉的話本次一啟動就會對自己發告警，而且 `finish_run()` 之後會有兩筆結束紀錄。
    """

    add_run(dao, "current", NOW)

    assert dao.mark_crashed_runs("current", NOW) == []
    assert run_row(dao, "current")["ended_at"] is None


# === 重連恢復 ===
def make_harness(**kwargs: Any) -> Any:
    """沿用日頻段落的整組替身，不另外造一套"""

    from tests.live.test_live_trader_day import Harness, ScriptedStrategy, make_order

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    return Harness([Alpha()], **kwargs)


def test_connected_broker_is_left_alone() -> None:
    """連線正常時不該多做任何事——每次送單前都重連一次會把登入額度用光"""

    harness: Any = make_harness()
    harness.broker.connect()
    before: int = harness.broker.placed_count

    assert harness.trader.ensure_connected() is True
    assert harness.broker.placed_count == before


def test_reconnect_resubscribes_and_reconciles_before_resuming() -> None:
    """
    重連成功後要**照順序**走完恢復：重新訂閱 → 接管委託 → 對帳

    少了重新訂閱，盤中迴圈收不到任何報價，然後心跳會判成「行情中斷」——
    症狀看起來像券商的問題，實際上是自己沒訂。
    """

    harness: Any = make_harness()
    harness.broker.connect()
    harness.contexts[0].symbols = ["2330"]
    harness.broker.subscribed.clear()

    harness.trader.recover_after_reconnect()

    assert harness.broker.subscribed == {"2330"}
    assert harness.trader.last_reconcile is not None


def test_disconnected_broker_is_reconnected_then_recovered() -> None:
    """
    斷線時要自己重連並恢復

    `ShioajiSession.reconnect()` 連退避與每日上限都寫好了，但接上呼叫端之前
    正式路徑上**一個呼叫點都沒有**——斷線之後程式會一路跑到收盤，
    每一次送單都失敗。
    """

    harness: Any = make_harness()
    harness.broker.connect()
    harness.contexts[0].symbols = ["2330"]
    harness.broker.close()

    assert harness.trader.ensure_connected() is True
    assert harness.broker.is_connected() is True
    assert harness.broker.subscribed == {"2330"}


def test_failed_reconnect_stops_submitting() -> None:
    """
    重連失敗要回 False 讓呼叫端停手

    **不可以回 True 讓流程繼續**：拿一份不知道對不對的部位去交易，
    比停下來嚴重得多。
    """

    harness: Any = make_harness()
    harness.broker.close()
    harness.broker.fail_connect = True

    assert harness.trader.ensure_connected() is False


# === 單一事件 queue 的接線 ===
def test_routing_sends_executions_to_the_sink_not_the_queue() -> None:
    """
    導向之後 `drain_execution_queue()` 要是空的

    兩邊都撈會把同一筆回報消化兩次，而且順序會亂——事件迴圈存在的理由
    就是讓順序確定。
    """

    harness: Any = make_harness()
    received: List[Any] = []
    harness.broker.connect()
    harness.broker.route_events(lambda quote: None, received.append)

    harness.broker.execution_queue.put("fake-report")

    assert received == ["fake-report"]
    assert harness.broker.drain_execution_queue() == []


def test_routing_failure_does_not_kill_the_callback_thread() -> None:
    """
    轉交端拋例外要吞掉

    這條路徑跑在券商的執行緒上，例外往上拋會讓那條執行緒死掉，
    之後所有回報靜默消失——而策略還在跑。
    """

    harness: Any = make_harness()
    harness.broker.connect()

    def exploding(item: Any) -> None:
        raise ValueError("轉交失敗")

    harness.broker.route_events(lambda quote: None, exploding)
    harness.broker.execution_queue.put("fake-report")
