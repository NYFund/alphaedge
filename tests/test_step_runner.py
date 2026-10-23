from typing import List, Tuple

from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.shared.step_runner import retry_if_blocked, run_isolated

"""
ETL 共用的兩個流程骨架：「疑似被擋就重試」與「逐子項隔離、最後彙總再拋」

原本重試有兩份（期貨行情、期貨籌碼）、隔離有四份（期貨行情、保證金、籌碼、休市日），
判準相同、註解互相指涉。這裡釘住骨架本身；各來源的判準與等待時間由各自的測試覆蓋。
"""


class _Source:
    """前幾次回「被擋」、之後回「拿到」的資料源"""

    def __init__(self, blocked_times: int) -> None:
        self.blocked_times: int = blocked_times
        self.calls: int = 0

    def fetch(self) -> str:
        self.calls += 1
        return "blocked" if self.calls <= self.blocked_times else "ok"


# === retry_if_blocked ===
def test_first_success_returns_without_waiting() -> None:
    """第一次就拿到：不等待、不重試"""

    waits: List[float] = []
    source: _Source = _Source(blocked_times=0)

    result, retries = retry_if_blocked(
        source.fetch, lambda r: r == "blocked", 3, 10, "x", sleep=waits.append
    )

    assert (result, retries, source.calls, waits) == ("ok", 0, 1, [])


def test_backoff_grows_linearly_and_stops_at_the_attempt_cap() -> None:
    """等待 10、20 秒各一次，最多取 `attempts` 次；一直被擋就把最後的結果交回"""

    waits: List[float] = []
    source: _Source = _Source(blocked_times=99)

    result, retries = retry_if_blocked(
        source.fetch, lambda r: r == "blocked", 3, 10, "x", sleep=waits.append
    )

    assert (result, retries, source.calls) == ("blocked", 2, 3)
    assert waits == [10, 20]


def test_retry_recovers_after_a_temporary_block() -> None:
    """第二次拿到就不再等第三次"""

    waits: List[float] = []
    source: _Source = _Source(blocked_times=1)

    result, retries = retry_if_blocked(
        source.fetch, lambda r: r == "blocked", 3, 10, "x", sleep=waits.append
    )

    assert (result, retries, source.calls, waits) == ("ok", 1, 2, [10])


def test_single_attempt_means_no_retry() -> None:
    """`attempts=1`：不重試"""

    source: _Source = _Source(blocked_times=99)

    result, retries = retry_if_blocked(
        source.fetch, lambda r: r == "blocked", 1, 10, "x", sleep=lambda s: None
    )

    assert (result, retries, source.calls) == ("blocked", 0, 1)


def test_stop_request_during_backoff_ends_the_retries() -> None:
    """等待期間收到中止：不再重取，把最後的結果交回"""

    stop: GracefulStop = GracefulStop(label="test")  # 不進 with，不裝訊號 handler
    source: _Source = _Source(blocked_times=99)

    def sleep_then_stop(seconds: float) -> None:
        stop.request("測試")

    result, retries = retry_if_blocked(
        source.fetch,
        lambda r: r == "blocked",
        5,
        1,
        "x",
        stop=stop,
        sleep=sleep_then_stop,
    )

    assert (result, retries, source.calls) == ("blocked", 1, 1)


# === run_isolated ===
def test_exceptions_are_isolated_and_reported_in_order() -> None:
    """一個子項拋出不擋後面的；失敗清單帶例外型別與訊息，依執行順序"""

    ran: List[str] = []

    def ok(name: str) -> None:
        ran.append(name)

    def boom() -> None:
        ran.append("b")
        raise ValueError("bad")

    failures: List[Tuple[str, str]] = run_isolated(
        [("a", lambda: ok("a")), ("b", boom), ("c", lambda: ok("c"))], "x"
    )

    assert ran == ["a", "b", "c"]
    assert failures == [("b", "ValueError: bad")]


def test_reported_failures_are_collected_per_item() -> None:
    """子項回傳的失敗描述逐筆收下，與例外混在同一份清單"""

    failures: List[Tuple[str, str]] = run_isolated(
        [
            ("chip", lambda: ["2026-01~2026-01", "2026-03~2026-03"]),
            ("trader", lambda: []),
            ("pcr", lambda: None),
        ],
        "x",
    )

    assert failures == [("chip", "2026-01~2026-01"), ("chip", "2026-03~2026-03")]


def test_nothing_failed_gives_an_empty_list() -> None:
    """全部成功：空清單，呼叫端不拋"""

    assert run_isolated([("a", lambda: None), ("b", lambda: [])], "x") == []
