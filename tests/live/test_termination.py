import argparse
import os
import signal
from typing import Any, Callable, List

import pytest

from core.live.termination import LiveTerminated, raise_on_sigterm
from core.models import OrderTicket
from core.utils import ExecutionTiming

from .test_live_trader_day import (
    Harness,
    ScriptedStrategy,
    make_order,
    run_row,
    start_run,
)

"""
收到 SIGTERM 時先撤單、寫結束紀錄，再結束

容器停止、排程逾時、手動 `kill` 送的都是 SIGTERM；預設處理會直接結束行程，
場上的委託沒人撤，`live_run` 也沒有結束紀錄（要等下次啟動才被標成崩潰）。
"""


def send_sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def test_sigterm_raises_inside_the_block() -> None:
    with pytest.raises(LiveTerminated):
        with raise_on_sigterm():
            send_sigterm()


def test_second_sigterm_during_cleanup_is_ignored() -> None:
    """收尾（撤單要等券商回覆）期間再收到的 SIGTERM 不可把撤單打斷在一半"""

    cleaned: List[str] = []
    with pytest.raises(LiveTerminated):
        with raise_on_sigterm():
            try:
                send_sigterm()
            finally:
                send_sigterm()
                cleaned.append("done")

    assert cleaned == ["done"]


def test_previous_handler_is_restored() -> None:
    before: Any = signal.getsignal(signal.SIGTERM)

    with raise_on_sigterm():
        pass

    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_mid_segment_cancels_open_orders_and_records_the_end() -> None:
    """
    段落送單途中收到 SIGTERM：已送出的委託要撤掉，結束原因寫進 `live_run`

    存活監控只看結束原因決定要不要推播：被停掉的段落不可被當成正常結束。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__(
                "Alpha", [make_order(symbol="2330"), make_order(symbol="2317")]
            )

    harness: Harness = Harness([Alpha()])
    harness.broker.fill_ratio = 0.0
    start_run(harness)
    original: Callable[[OrderTicket], OrderTicket] = harness.broker.place_order

    def terminate_on_second(ticket: OrderTicket) -> OrderTicket:
        if harness.broker.placed_count == 1:
            send_sigterm()
        return original(ticket)

    harness.broker.place_order = terminate_on_second  # type: ignore[assignment]

    with pytest.raises(LiveTerminated):
        with raise_on_sigterm():
            harness.trader.run(ExecutionTiming.AT_CLOSE)

    # 已送出的第一張要撤；第二張在呼叫券商前就被中止（停在 PENDING_SUBMIT、
    # 不確定是否送達），收尾一併送撤單是保守的做法。收到訊號之後不再送出新單
    assert "run1-0001" in harness.broker.cancel_requests
    assert harness.broker.placed_count == 1
    end_reason: str = run_row(harness)[3]
    assert "LiveTerminated" in end_reason


def test_run_entry_exits_with_143_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出碼 143（128 ＋ 15）是 shell 與容器對「被訊號結束」的慣例值"""

    import run as run_module

    class Terminated:
        def run(self, timing: Any) -> None:
            send_sigterm()

    monkeypatch.setattr(
        "core.live.factory.build_live_trader", lambda *args, **kwargs: Terminated()
    )
    args: argparse.Namespace = argparse.Namespace(
        phase="close",
        simulation=True,
        confirm_production=False,
        broker="fake",
        strategy="Alpha",
        dry_run=False,
        resync_from_broker=False,
        confirm_resync=False,
        resume_trading=None,
    )

    assert run_module.run_live(args, {"Alpha": object}) == run_module.EXIT_TERMINATED
    assert run_module.EXIT_TERMINATED == 143
