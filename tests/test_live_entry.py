import argparse
import datetime
import importlib
import os
import signal
import subprocess
from typing import Any, Dict, List, Optional

import pytest

import apps.live as live_entry
from core.live.datafeed.base import DataFreshnessError
from core.live.risk.trading_mode import TradingMode
from core.utils import ExecutionTiming
from tests.entry_sandbox import run_isolated

"""
`python -m apps.live` 的防呆與退出碼契約

**子行程只用在「參數檢查就會退出」的案例**：一旦走過檢查就會組裝引擎、登入券商。
那些路徑一律在行程內以替身引擎驗，並把 `apps.live.build_live_trader` 換掉——
入口是在模組頂層 import 它的，patch `core.live.factory` 那一份不會生效。
"""


def run_live_entry(*args: str) -> subprocess.CompletedProcess:
    """以子行程跑實盤入口（沙箱環境、金鑰清空）"""

    return run_isolated(["-m", "apps.live", *args])


def make_live_args() -> argparse.Namespace:
    """一組最小可用的實盤參數（模擬環境、fake 券商）"""

    return argparse.Namespace(
        phase="close",
        simulation=True,
        confirm_production=False,
        broker="fake",
        strategy="Alpha",
        dry_run=False,
        resync_from_broker=False,
        confirm_resync=False,
        resume_trading=None,
        date=None,
    )


# === 參數檢查（子行程） ===
def test_production_without_confirmation_is_refused() -> None:
    """
    `--production` 沒帶 `--confirm-production` 時不可能連到正式環境

    **防呆在建立任何連線之前**：打錯的代價是真的下單。
    """

    result: subprocess.CompletedProcess = run_live_entry(
        "--strategy", "MomentumStrategy1", "--phase", "close", "--production"
    )

    assert result.returncode == live_entry.EXIT_USAGE
    assert "confirm-production" in result.stderr


def test_fake_broker_is_refused_in_production() -> None:
    """正式環境不可用假券商：一個打錯的參數不該讓真單變成假單，反過來更不行"""

    result: subprocess.CompletedProcess = run_live_entry(
        "--strategy",
        "MomentumStrategy1",
        "--phase",
        "close",
        "--production",
        "--confirm-production",
        "--broker",
        "fake",
    )

    assert result.returncode == live_entry.EXIT_USAGE


def test_phase_is_required() -> None:
    """沒有段落就不知道要跑什麼；靜默跑預設段落會在錯的時點送單"""

    result: subprocess.CompletedProcess = run_live_entry(
        "--strategy", "MomentumStrategy1"
    )

    assert result.returncode == live_entry.EXIT_USAGE
    assert "--phase" in result.stderr


def test_unknown_strategy_is_reported() -> None:
    """策略名打錯要列出可用的，訊息走 stderr"""

    result: subprocess.CompletedProcess = run_live_entry(
        "--strategy", "NoSuchStrategy", "--phase", "close"
    )

    assert result.returncode == live_entry.EXIT_STRATEGY_NOT_FOUND
    assert "NoSuchStrategy" in result.stderr
    assert "Available strategies" in result.stderr


def test_backtest_flags_are_rejected_by_the_parser() -> None:
    """實盤入口不認得回測旗標，`--mode` 也已經不存在"""

    for flags in (["--show"], ["--mode", "live"]):
        result: subprocess.CompletedProcess = run_live_entry(
            "--strategy", "MomentumStrategy1", "--phase", "close", *flags
        )
        assert result.returncode == live_entry.EXIT_USAGE, flags


def test_help_describes_the_production_safety_flags() -> None:
    """`--help` 要讓人看得出「連正式環境需要兩個旗標」"""

    result: subprocess.CompletedProcess = run_live_entry("--help")

    assert result.returncode == 0
    assert "--production" in result.stdout
    assert "--confirm-production" in result.stdout
    assert "--dry-run" in result.stdout


# === 重建旗標組合（行程內） ===
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"resync_from_broker": True}, "--phase"),
        ({"phase": None, "confirm_resync": True}, "--confirm-resync"),
        (
            {"phase": None, "resync_from_broker": True, "resume_trading": []},
            "--resume-trading",
        ),
    ],
    ids=["resync-with-phase", "confirm-without-resync", "resync-with-resume"],
)
def test_resync_flag_combinations_are_refused_before_building(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    overrides: Dict[str, Any],
    message: str,
) -> None:
    """
    重建是獨立作業：不跑段落、不恢復交易模式，錯的組合在組裝引擎之前就拒絕

    與 `--phase` 併用，人會以為重建完接著跑了段落；與 `--resume-trading` 併用，
    重建結果還沒人看過，降級就已經解除了。
    """

    def must_not_build(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("旗標組合錯誤時不可以組裝實盤引擎")

    monkeypatch.setattr(live_entry, "build_live_trader", must_not_build)
    args: argparse.Namespace = make_live_args()
    for name, value in overrides.items():
        setattr(args, name, value)

    # 策略要找得到：找不到策略也回用法錯誤，會讓這條在沒擋旗標時照樣通過
    assert live_entry.run_live(args, {"Alpha": object}) == live_entry.EXIT_USAGE
    assert message in capsys.readouterr().err


class ResyncTrader:
    """只回應重建的替身引擎"""

    def __init__(self, plan: Any = None, error: Exception = None) -> None:
        self.plan: Any = plan
        self.error: Exception = error
        self.confirm: Any = None
        self.last_reconcile: Any = None

    def resync_from_broker(self, confirm: bool) -> Any:
        """記下是否確認寫入，回傳預設的計畫或拋出預設的錯誤"""

        self.confirm = confirm
        if self.error is not None:
            raise self.error
        return self.plan


def run_resync(
    monkeypatch: pytest.MonkeyPatch, trader: ResyncTrader, confirm: bool
) -> int:
    """以替身引擎跑一次重建，回傳退出碼"""

    monkeypatch.setattr(live_entry, "build_live_trader", lambda *args, **kwargs: trader)
    args: argparse.Namespace = make_live_args()
    args.phase = None
    args.resync_from_broker = True
    args.confirm_resync = confirm
    return live_entry.run_live(args, {"Alpha": object})


def test_resync_plan_only_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    只列計畫回 7，**不是 0**

    只列計畫代表歸屬帳仍與券商不一致，排程若把它當成已處理，下一個段落照樣帶著
    錯的部位啟動。
    """

    from core.live.attribution.resync import RESYNC_CLOSE, ResyncAction, ResyncPlan

    plan: ResyncPlan = ResyncPlan(
        actions=[ResyncAction(RESYNC_CLOSE, "Alpha", "2330", "LONG", 2, "L1")]
    )
    trader: ResyncTrader = ResyncTrader(plan=plan)

    assert run_resync(monkeypatch, trader, confirm=False) == (
        live_entry.EXIT_RESYNC_PLAN_ONLY
    )
    assert trader.confirm is False


def test_refused_resync_exits_with_reconcile_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """歸屬帳已損壞而拒絕重建：與對帳不一致同一件事，都要人工處理"""

    from core.live.attribution.resync import ResyncRefusedError

    trader: ResyncTrader = ResyncTrader(error=ResyncRefusedError("2330 兩個持有者"))

    assert run_resync(monkeypatch, trader, confirm=True) == (
        live_entry.EXIT_RECONCILE_MISMATCH
    )


# === 段落執行（行程內） ===
@pytest.mark.parametrize(
    ("exception_path", "message"),
    [
        ("core.live.datafeed.base.DataFreshnessError", "資料停在 T-30"),
        (
            "core.live.datafeed.calendar.TradingCalendarUnavailableError",
            "沒有任何來源判定得出",
        ),
    ],
)
def test_startup_check_failures_exit_with_stale_data(
    monkeypatch: pytest.MonkeyPatch, exception_path: str, message: str
) -> None:
    """啟動檢查擋下來時要回結束碼 3，**不是 0**：ETL 掛掉時實盤不可照樣啟動"""

    module_name, _, class_name = exception_path.rpartition(".")
    error_type = getattr(importlib.import_module(module_name), class_name)

    class ExplodingTrader:
        """一跑段落就拋出啟動檢查的錯誤"""

        def run(self, timing: Any) -> None:
            """模擬啟動檢查失敗"""

            raise error_type(message)

    monkeypatch.setattr(
        live_entry, "build_live_trader", lambda *args, **kwargs: ExplodingTrader()
    )

    code: int = live_entry.run_live(make_live_args(), {"Alpha": object})

    assert code == live_entry.EXIT_STALE_DATA


def test_date_is_only_for_the_parity_phase() -> None:
    """`--date` 只給補比用；帶在交易段落上會讓人以為那一段跑的是指定日期"""

    args: argparse.Namespace = make_live_args()
    args.date = datetime.date(2026, 10, 7)

    assert live_entry.run_live(args, {"Alpha": object}) == live_entry.EXIT_USAGE


def test_parity_phase_runs_the_next_day_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--phase parity` 只走補比，不跑任何交易段落；資料沒更新時回 3"""

    class ParityTrader:
        """補比被呼叫時拋出資料過期"""

        def __init__(self) -> None:
            self.dates: List[Optional[datetime.date]] = []

        def run_parity(self, run_date: Optional[datetime.date]) -> int:
            self.dates.append(run_date)
            raise DataFreshnessError("資料停在 T-30")

        def run(self, timing: Any) -> None:
            raise AssertionError("補比不可跑交易段落")

    trader: ParityTrader = ParityTrader()
    monkeypatch.setattr(live_entry, "build_live_trader", lambda *args, **kwargs: trader)
    args: argparse.Namespace = make_live_args()
    args.phase = live_entry.PARITY_PHASE
    args.date = datetime.date(2026, 10, 7)

    assert live_entry.run_live(args, {"Alpha": object}) == live_entry.EXIT_STALE_DATA
    assert trader.dates == [datetime.date(2026, 10, 7)]


class RecordingTrader:
    """只記錄被呼叫的是哪一條流程；其餘屬性供結束碼判定讀取"""

    last_reconcile: Any = None
    risk_manager: Any = type(
        "Risk", (), {"is_kill_switch_on": staticmethod(lambda: False)}
    )()
    mode_state: Any = type("Mode", (), {"account_mode": TradingMode.NORMAL})()

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def run_after_close(self) -> Dict[str, Any]:
        """盤後流程"""

        self.calls.append("after_close")
        return {"pending_actions": 0}

    def run(self, timing: Any) -> None:
        """一般段落"""

        self.calls.append(timing)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("open", ExecutionTiming.AT_OPEN),
        ("close", ExecutionTiming.AT_CLOSE),
        ("intraday", ExecutionTiming.IMMEDIATE),
        ("after_close", "after_close"),
    ],
)
def test_each_phase_runs_its_own_flow(
    monkeypatch: pytest.MonkeyPatch, phase: str, expected: Any
) -> None:
    """
    每個段落走到對的流程，段落名也要傳進 factory

    `after_close` 要真的走盤後流程，不可以被當成用法錯誤擋掉——否則排程會以為
    自己打錯參數，而盤後其實從來沒跑過。段落名寫進 `live_run`，存活監控才比對得到。
    """

    trader: RecordingTrader = RecordingTrader()
    build_kwargs: Dict[str, Any] = {}

    def fake_build(*arguments: Any, **kwargs: Any) -> RecordingTrader:
        build_kwargs.update(kwargs)
        return trader

    monkeypatch.setattr(live_entry, "build_live_trader", fake_build)
    args: argparse.Namespace = live_entry.parse_arguments(
        ["--strategy", "Alpha", "--phase", phase]
    )

    code: int = live_entry.run_live(args, {"Alpha": object})

    assert trader.calls == [expected]
    assert code == 0
    assert build_kwargs["phase"] == phase


def test_sigterm_exits_with_143(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出碼 143（128 ＋ 15）是 shell 與容器對「被訊號結束」的慣例值"""

    class Terminated:
        """段落中收到 SIGTERM"""

        def run(self, timing: Any) -> None:
            """模擬排程逾時送出的 SIGTERM"""

            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(
        live_entry, "build_live_trader", lambda *args, **kwargs: Terminated()
    )

    assert live_entry.run_live(make_live_args(), {"Alpha": object}) == 143


def test_exit_codes_are_distinct() -> None:
    """
    退出碼要分得開，數值也不可改：排程端依賴這些號碼

    排程看到 4／5 是「今天剛出事」，看到 6 是「昨天出的事還沒有人處理」，
    兩者的處理急迫性不同。
    """

    codes: List[int] = [
        live_entry.EXIT_USAGE,
        live_entry.EXIT_STALE_DATA,
        live_entry.EXIT_RECONCILE_MISMATCH,
        live_entry.EXIT_KILL_SWITCH,
        live_entry.EXIT_MODE_NOT_NORMAL,
        live_entry.EXIT_RESYNC_PLAN_ONLY,
        live_entry.EXIT_TERMINATED,
    ]

    assert codes == [2, 3, 4, 5, 6, 7, 143]
