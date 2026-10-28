import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import scripts.live_watchdog as watchdog
from core.dao.tw.live_trade_dao import LiveTradeDAO
from scripts.live_watchdog import PhaseStatus, check_phases, fetch_runs

"""
存活監控：行程已死卻沒有結束紀錄

2026-10-28 11:32 主機重開機，盤中段行程消失、`live_run` 沒有 `ended_at`。watchdog 只看
「開始多久」，盤中段的上限 270 分鐘要到 13:36 才到，結果 11:38～13:19 每 5 分鐘都回
「檢查通過」，直到收盤段開段把它標成 `CRASHED` 才發現。

規則：沒有結束紀錄時先看行程還在不在（pid 加上啟動時刻，防 pid 被重用）；判不出來才退回時間上限。
"""

TAIPEI: datetime.timezone = datetime.timezone(datetime.timedelta(hours=8))
DAY: datetime.date = datetime.date(2026, 10, 28)
STARTED: str = "2026-10-28T09:06:05+08:00"
AT_1140: datetime.datetime = datetime.datetime(2026, 10, 28, 11, 40, tzinfo=TAIPEI)
INTRADAY: Dict[str, datetime.time] = {"intraday": datetime.time(9, 6)}


def unfinished_intraday(pid: Optional[int] = 7768) -> Tuple[Any, ...]:
    """還沒有結束紀錄的盤中段"""

    return ("intraday", STARTED, None, None, "Stock", None, pid)


def test_dead_process_is_reported_before_the_time_limit() -> None:
    """行程已不在：不必等 270 分鐘的上限，當輪就告警"""

    statuses: List[PhaseStatus] = check_phases(
        [unfinished_intraday()],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
        run_alive=lambda pid, started_at: False,
    )

    assert statuses[0].problem is not None
    assert "pid=7768" in statuses[0].problem
    assert "已經不在" in statuses[0].problem


def test_live_process_within_the_time_limit_is_healthy() -> None:
    """行程還在、也沒超過上限：正常的盤中段"""

    statuses: List[PhaseStatus] = check_phases(
        [unfinished_intraday()],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
        run_alive=lambda pid, started_at: True,
    )

    assert statuses[0].is_healthy is True


@pytest.mark.parametrize("pid", [None, 7768])
def test_unknown_liveness_falls_back_to_the_time_limit(pid: Optional[int]) -> None:
    """沒有 pid、或查詢判不出來：維持舊行為，只看時間上限（11:40 還沒到）"""

    statuses: List[PhaseStatus] = check_phases(
        [unfinished_intraday(pid)],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
        run_alive=lambda pid, started_at: None,
    )

    assert statuses[0].is_healthy is True


def test_old_row_shape_is_not_checked_for_liveness() -> None:
    """6 欄的舊形狀沒有 pid：不查行程，也不拋例外"""

    def must_not_be_called(pid: int, started_at: str) -> Optional[bool]:
        raise AssertionError("沒有 pid 不該查行程")

    statuses: List[PhaseStatus] = check_phases(
        [unfinished_intraday()[:6]],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
        run_alive=must_not_be_called,
    )

    assert statuses[0].is_healthy is True


def test_dead_and_later_crashed_share_one_alert_key() -> None:
    """
    同一次執行先被判成「行程已不在」、後來被收盤段標成 `CRASHED`：去重鍵相同

    不然同一件事會推兩次。
    """

    dead: List[PhaseStatus] = check_phases(
        [unfinished_intraday()],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
        run_alive=lambda pid, started_at: False,
    )
    crashed: List[PhaseStatus] = check_phases(
        [
            (
                "intraday",
                STARTED,
                "2026-10-28T13:20:10+08:00",
                "CRASHED",
                "Stock",
                None,
                7768,
            )
        ],
        INTRADAY,
        AT_1140,
        grace_minutes=15,
    )

    assert [key for key, _ in dead[0].alerts] == [key for key, _ in crashed[0].alerts]


def test_pending_phase_without_rows_is_not_missing() -> None:
    """上游還在跑的段落沒有紀錄：不算沒跑；不在名單裡的段落照常告警"""

    statuses: List[PhaseStatus] = check_phases(
        [],
        {"parity:stock": datetime.time(6, 0), "open:stock": datetime.time(8, 30)},
        AT_1140,
        grace_minutes=15,
        pending_phases=frozenset({"parity"}),
    )

    by_phase = {status.phase: status for status in statuses}
    assert by_phase["parity"].is_healthy is True
    assert by_phase["open"].problem is not None


def _fake_ps(
    monkeypatch: pytest.MonkeyPatch,
    result: Tuple[bool, Optional[datetime.datetime]],
) -> None:
    """讓 `process_started_at()` 回固定結果"""

    monkeypatch.setattr(watchdog, "process_started_at", lambda pid: result)


def test_pid_reused_by_a_later_process_counts_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    pid 還在，但那個行程比段落開始還晚啟動：是重開機後重用同一個 pid 的別的行程

    2026-10-28 重開機後兩小時內 pid 已編到 1 萬多，盤中段的 7768 很可能被重用。
    """

    _fake_ps(monkeypatch, (True, datetime.datetime(2026, 10, 28, 12, 1, tzinfo=TAIPEI)))

    assert watchdog.run_process_alive(7768, STARTED) is False


def test_the_original_process_counts_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """行程比段落紀錄早幾秒啟動（先初始化才寫紀錄）：就是那個段落本身"""

    _fake_ps(
        monkeypatch, (True, datetime.datetime(2026, 10, 28, 9, 6, 1, tzinfo=TAIPEI))
    )

    assert watchdog.run_process_alive(7768, STARTED) is True


def test_missing_process_counts_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """查得到結果、但行程不存在"""

    _fake_ps(monkeypatch, (True, None))

    assert watchdog.run_process_alive(7768, STARTED) is False


def test_failed_query_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ps` 跑不起來或輸出看不懂：判不出來，交給時間上限"""

    _fake_ps(monkeypatch, (False, None))

    assert watchdog.run_process_alive(7768, STARTED) is None


def test_process_started_at_reads_the_running_process() -> None:
    """真的查本行程：查得到，而且啟動時刻在現在之前"""

    ok, started = watchdog.process_started_at(os.getpid())

    assert ok is True
    assert started is not None
    assert started <= datetime.datetime.now().astimezone()


def test_process_started_at_reports_a_missing_process() -> None:
    """不存在的 pid：查詢成功、啟動時刻為 None"""

    ok, started = watchdog.process_started_at(99_999_999)

    assert (ok, started) == (True, None)


def test_fetch_runs_returns_the_pid(tmp_path: Path) -> None:
    """watchdog 讀回的紀錄要帶 pid，才判得出行程還在不在"""

    path: Path = tmp_path / "tw_trading.db"
    dao: LiveTradeDAO = LiveTradeDAO(db_path=path)
    dao.ensure_tables()
    dao.insert_run(
        {
            "run_id": "run-intraday",
            "started_at": STARTED,
            "phase": "intraday",
            "simulation": 1,
            "pid": 7768,
        }
    )
    dao.conn.commit()
    dao.close()

    rows: List[Tuple[Any, ...]] = fetch_runs(str(path), DAY)

    assert rows[0][6] == 7768


def test_alert_record_round_trip(tmp_path: Path) -> None:
    """已推播紀錄：同一天讀得回來；換一天、檔案壞掉都當成沒推過"""

    path: Path = tmp_path / "state" / "watchdog_alerted.json"
    watchdog.save_alerted(path, DAY, {"a", "b"})

    assert watchdog.load_alerted(path, DAY) == {"a", "b"}
    assert watchdog.load_alerted(path, DAY + datetime.timedelta(days=1)) == set()

    path.write_text("{not json", encoding="utf-8")
    assert watchdog.load_alerted(path, DAY) == set()
