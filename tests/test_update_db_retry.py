import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import apps.update_db as update_db
from scripts.launchd.rehearsal_schedule import _CHAINED_JOBS

"""
`update_db` 只重跑失敗的 target

2026-10-16 06:00 證交所封鎖來源 IP，整批更新只有 `price` 失敗，股票開盤段卻因前一交易日
股價缺漏被新鮮度檢查擋下；當時沒有任何重試，要等人工補跑。重試只補失敗的那幾個 target，
成功的不重跑，全部補上就提前結束。
"""


class _Recorder:
    """記下每個 target 被呼叫的次數與起日，並依設定讓前幾次失敗"""

    def __init__(self, fail_times: Dict[str, int]) -> None:
        self.fail_times: Dict[str, int] = dict(fail_times)
        self.calls: List[str] = []
        self.start_dates: List[Tuple[str, datetime.date]] = []
        self.sleeps: List[float] = []

    def make(self, name: str) -> type:
        """建一個名為 `name` 的 updater 替身類別，呼叫結果記到本 recorder"""

        recorder: _Recorder = self

        class Fake:
            """依 recorder 設定決定這次更新要不要失敗的 updater 替身"""

            def update(self, **kwargs) -> None:
                recorder.calls.append(name)
                if "start_date" in kwargs:
                    recorder.start_dates.append((name, kwargs["start_date"]))
                if recorder.fail_times.get(name, 0) > 0:
                    recorder.fail_times[name] -= 1
                    raise ConnectionError(f"{name} 被封鎖")

            def close(self) -> None:
                pass

        return Fake


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    以 `price`、`chip` 兩個替身 target 執行 `main()`；`time.sleep` 只記錄不真的等

    第一輪失敗清單寫在暫存目錄，不碰專案的 `logs/`
    """

    monkeypatch.setattr(
        update_db, "DEFERRED_FAILURES_PATH", tmp_path / "update_db_deferred.json"
    )

    def setup(
        fail_times: Dict[str, int],
        retry_failed: int,
        retry_wait: int = 60,
        from_date: Optional[datetime.date] = datetime.date(2026, 10, 1),
        defer_failed: bool = False,
        retry_deferred: bool = False,
        rec: Optional[_Recorder] = None,
    ) -> _Recorder:
        rec = rec if rec is not None else _Recorder(fail_times)
        monkeypatch.setattr(update_db, "StockPriceUpdater", rec.make("price"))
        monkeypatch.setattr(update_db, "StockChipUpdater", rec.make("chip"))
        monkeypatch.setattr(update_db, "cleanup_api_logs", lambda: None)
        monkeypatch.setattr(update_db.time, "sleep", rec.sleeps.append)
        monkeypatch.setattr(
            update_db,
            "parse_arguments",
            lambda: argparse.Namespace(
                target=["price", "chip"],
                from_date=from_date,
                retry_failed=retry_failed,
                retry_wait=retry_wait,
                defer_failed=defer_failed,
                retry_deferred=retry_deferred,
            ),
        )
        return rec

    return setup


def test_only_failed_targets_are_retried_until_they_succeed(recorder) -> None:
    """`price` 前兩次失敗：第 2 輪補上就結束，`chip` 不重跑、不再等第 3 輪，結束碼 0"""

    rec: _Recorder = recorder({"price": 2}, retry_failed=4, retry_wait=60)

    update_db.main()

    assert rec.calls == ["price", "chip", "price", "price"]
    assert rec.sleeps == [3600, 3600]


def test_still_failing_after_all_rounds_exits_non_zero(recorder) -> None:
    """重試用完仍失敗：結束碼 1，輪數不超過設定"""

    rec: _Recorder = recorder({"price": 99}, retry_failed=3, retry_wait=5)

    with pytest.raises(SystemExit) as exc_info:
        update_db.main()

    assert exc_info.value.code == 1
    assert rec.calls == ["price", "chip", "price", "price", "price"]
    assert rec.sleeps == [300, 300, 300]


def test_no_retry_by_default_keeps_the_old_behavior(recorder) -> None:
    """`--retry-failed 0`：失敗就直接以 1 結束，不等待也不重跑"""

    rec: _Recorder = recorder({"price": 1}, retry_failed=0)

    with pytest.raises(SystemExit):
        update_db.main()

    assert rec.calls == ["price", "chip"]
    assert rec.sleeps == []


def test_nothing_failed_means_no_waiting(recorder) -> None:
    """第一次全部成功：不進入重試、不等待"""

    rec: _Recorder = recorder({}, retry_failed=4)

    update_db.main()

    assert rec.calls == ["price", "chip"]
    assert rec.sleeps == []


def test_retries_keep_the_from_override(recorder) -> None:
    """重跑沿用 `--from` 的覆寫起日，不會退回預設起日"""

    start: datetime.date = datetime.date(2026, 9, 1)
    rec: _Recorder = recorder({"price": 1}, retry_failed=1, from_date=start)

    update_db.main()

    assert [d for name, d in rec.start_dates if name == "price"] == [start, start]


def test_arguments_default_to_no_retry_and_reject_negatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """預設不重試、等待 60 分鐘；負數在解析時就拒絕"""

    monkeypatch.setattr(sys, "argv", ["update_db"])
    args: argparse.Namespace = update_db.parse_arguments()
    assert (args.retry_failed, args.retry_wait) == (0, 60)

    monkeypatch.setattr(sys, "argv", ["update_db", "--retry-failed", "-1"])
    with pytest.raises(SystemExit):
        update_db.parse_arguments()


def test_rehearsal_schedule_retries_failed_targets_hourly() -> None:
    """
    演練排程的資料更新帶重試：每 60 分鐘、最多 4 輪，06:0x 失敗時開盤段前還有兩輪

    重試拆在補比之後：第一輪只記下失敗的，重試只補記下的那幾個
    """

    job = _CHAINED_JOBS["update-db"]

    assert job.first_pass == ["-m", "apps.update_db", "--defer-failed"]
    assert "--retry-deferred" in job.retry
    assert job.retry[job.retry.index("--retry-failed") + 1] == "4"
    assert job.retry[job.retry.index("--retry-wait") + 1] == "60"


def test_defer_records_the_failures_and_exits_zero_without_waiting(
    recorder,
) -> None:
    """
    `--defer-failed`：只跑一輪、不等不重跑，失敗的 target 記下來，以 0 結束

    2026-10-28 `futures_chip` 4 輪重試都失敗，排在後面的三條補比從 06:0x 被拖到 10:26；
    第一輪結束就把重試交出去，後面的工作才不必等。
    """

    rec: _Recorder = recorder({"price": 1}, retry_failed=0, defer_failed=True)

    update_db.main()

    assert rec.calls == ["price", "chip"]
    assert rec.sleeps == []
    payload = json.loads(update_db.DEFERRED_FAILURES_PATH.read_text(encoding="utf-8"))
    assert payload["targets"] == ["price"]
    assert payload["date"] == datetime.date.today().isoformat()


def test_retry_deferred_only_reruns_the_recorded_targets(recorder) -> None:
    """
    `--retry-deferred`：只重跑第一輪記下的 target，沿用第一輪的 `--from`；清單用完就刪

    `chip` 第一輪成功，不能再跑一次。
    """

    start: datetime.date = datetime.date(2026, 9, 1)
    rec: _Recorder = recorder(
        {"price": 2}, retry_failed=0, defer_failed=True, from_date=start
    )
    update_db.main()

    recorder({}, retry_failed=4, retry_deferred=True, from_date=None, rec=rec)
    update_db.main()

    assert rec.calls == ["price", "chip", "price", "price"]
    assert rec.sleeps == [3600, 3600]
    assert [d for name, d in rec.start_dates if name == "price"] == [start] * 3
    assert not update_db.DEFERRED_FAILURES_PATH.exists()


def test_retry_deferred_still_failing_exits_non_zero(recorder) -> None:
    """重試用完仍失敗：以 1 結束，失敗才看得到"""

    rec: _Recorder = recorder({"price": 99}, retry_failed=0, defer_failed=True)
    update_db.main()

    recorder({}, retry_failed=2, retry_deferred=True, rec=rec)
    with pytest.raises(SystemExit) as exc_info:
        update_db.main()

    assert exc_info.value.code == 1
    assert rec.calls == ["price", "chip", "price", "price"]


def test_retry_deferred_without_failures_does_nothing(recorder) -> None:
    """第一輪全部成功：重試那一步什麼都不跑、不等待，以 0 結束"""

    rec: _Recorder = recorder({}, retry_failed=0, defer_failed=True)
    update_db.main()

    recorder({}, retry_failed=4, retry_deferred=True, rec=rec)
    update_db.main()

    assert rec.calls == ["price", "chip"]
    assert rec.sleeps == []


def test_retry_deferred_ignores_a_list_from_another_day(recorder) -> None:
    """不是今天的清單（第一輪那天沒寫成、上次重試沒刪成）：不重跑，而且刪掉"""

    rec: _Recorder = recorder({}, retry_failed=4, retry_deferred=True)
    update_db.DEFERRED_FAILURES_PATH.write_text(
        json.dumps({"date": "2026-10-27", "targets": ["price"], "from_date": None}),
        encoding="utf-8",
    )

    update_db.main()

    assert rec.calls == []
    assert not update_db.DEFERRED_FAILURES_PATH.exists()


def test_defer_and_in_process_retry_cannot_be_combined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--defer-failed` 不在本次重試：同時給 `--retry-failed` 或 `--retry-deferred` 在解析時就拒絕"""

    for argv in (
        ["update_db", "--defer-failed", "--retry-failed", "4"],
        ["update_db", "--defer-failed", "--retry-deferred"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            update_db.parse_arguments()
