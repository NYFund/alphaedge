import argparse
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
_RUN_PY: Path = _PROJECT_ROOT / "run.py"

"""
`run.py` 的退出碼契約

舊版**兩種失敗都回 0**：策略名找不到只 `print` 後 `return`，`--mode live` 是
`pass`。目前 `run.py` 只有人手動跑所以還沒出事，但一旦接進批次（例如每晚
重跑策略），「策略名打錯」與「回測跑完」在退出碼上長得一模一樣——
那是最典型的假綠燈，與回歸腳本曾把 skip 當通過是同一類問題。

**一定要用 subprocess 驗**：退出碼是**行程**的性質，直接呼叫 `main()` 只驗得到
有沒有拋例外，驗不到 `sys.exit()` 實際交給呼叫端的那個數字，也驗不到
訊息去了 stdout 還是 stderr。

本檔的每一條都**不會真的跑回測**——三種情況都在建 Backtester 之前就結束，
所以不需要 `data/db/*.db`，也不標 `slow`。
"""


# 用法錯誤：與 argparse 自己的用法錯誤同碼（缺必填參數時它就回 2）
EXIT_USAGE_ERROR: int = 2

# 未實作：`raise NotImplementedError` 的預設退出碼
EXIT_UNHANDLED_EXCEPTION: int = 1


def run_entry(*args: str) -> subprocess.CompletedProcess:
    """以子行程跑 `run.py`，回傳完整結果（退出碼、stdout、stderr）"""

    return subprocess.run(
        [sys.executable, str(_RUN_PY), *args],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        timeout=180,
    )


def test_unknown_strategy_exits_with_usage_error() -> None:
    """
    策略名找不到 → 退出碼 2，**不是 0**

    這是本步驟的主要目的：讓呼叫端看得出失敗。
    """

    result: subprocess.CompletedProcess = run_entry("--strategy", "NoSuchStrategy")

    assert result.returncode == EXIT_USAGE_ERROR


def test_unknown_strategy_writes_to_stderr_not_stdout() -> None:
    """
    錯誤訊息走 stderr

    退出碼與輸出流向要一起改才有意義：訊息印在 stdout 會混進正常輸出，
    批次作業把 stdout 收去當報表時就看不見這行了。
    """

    result: subprocess.CompletedProcess = run_entry("--strategy", "NoSuchStrategy")

    assert "NoSuchStrategy" in result.stderr
    assert "not found" in result.stderr
    assert "NoSuchStrategy" not in result.stdout


def test_unknown_strategy_lists_available_strategies() -> None:
    """
    要印出可用清單，否則使用者只知道打錯、不知道該打什麼

    清單本身由 `StrategyLoader` 掃出來，不是寫死的——新增策略會自動出現。
    """

    result: subprocess.CompletedProcess = run_entry("--strategy", "NoSuchStrategy")

    assert "Available strategies:" in result.stderr
    # 專案內既有的策略至少要出現一個；寫死一個名字才驗得到「清單真的有內容」
    assert "MomentumStrategy1" in result.stderr


def test_missing_strategy_argument_is_still_a_usage_error() -> None:
    """
    缺 `--strategy` 維持 argparse 既有的退出碼 2

    這條不是新行為，是釘住「策略找不到」刻意與它同碼——對呼叫端來說
    兩者是同一類問題（用法錯誤），不必再多記一個號碼。
    """

    result: subprocess.CompletedProcess = run_entry()

    assert result.returncode == EXIT_USAGE_ERROR


def test_live_mode_without_phase_fails_loudly() -> None:
    """
    `--mode live` 少了 `--phase` → 非 0 退出並說明原因

    **本條的前身是「實盤尚未實作」**。實盤做出來之後，要守的變成「參數不全時
    不可靜默跑一個預設段落」——那會在錯的時點送單，而且看起來完全正常。
    退出碼 0、零輸出的失敗仍然是最不能接受的那種。
    """

    result: subprocess.CompletedProcess = run_entry(
        "--mode", "live", "--strategy", "MomentumStrategy1"
    )

    assert result.returncode != 0
    assert "--phase" in result.stderr


def test_help_describes_the_production_safety_flags() -> None:
    """
    `--help` 要讓人看得出「連正式環境需要兩個旗標」

    前身是「不可讓 live 看起來已經支援」。實盤支援之後，`--help` 的責任變成
    **講清楚怎樣才會真的下單**——看不出這件事的人，遲早會在正式環境按下 enter。
    """

    result: subprocess.CompletedProcess = run_entry("--help")

    assert result.returncode == 0
    assert "--production" in result.stdout
    assert "--confirm-production" in result.stdout
    assert "--dry-run" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--strategy", "NoSuchStrategy"],
        ["--mode", "live", "--strategy", "MomentumStrategy1"],
    ],
    ids=["unknown-strategy", "live-mode"],
)
def test_failure_paths_never_exit_zero(args: List[str]) -> None:
    """
    所有失敗路徑都不得回 0

    參數化是為了讓日後新增的失敗路徑直接加進這張表，而不是各寫一條。
    """

    assert run_entry(*args).returncode != 0


# === 實盤啟動檢查對應的退出碼 ===
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
        resume_trading=None,
    )


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
    """
    啟動檢查擋下來時要回結束碼 3，**不是 0**

    接上呼叫端之前，`DataFreshnessError` 沒有任何地方會拋出——`run.py` 特地接住它
    回 3 的那條路徑因此是死的，ETL 掛掉三天實盤照樣啟動。
    """

    import importlib

    import run as run_module

    module_name, _, class_name = exception_path.rpartition(".")
    error_type = getattr(importlib.import_module(module_name), class_name)

    class ExplodingTrader:
        def run(self, timing: object) -> None:
            raise error_type(message)

    monkeypatch.setattr(
        "core.live.factory.build_live_trader",
        lambda *args, **kwargs: ExplodingTrader(),
    )

    code: int = run_module.run_live(make_live_args(), {"Alpha": object})

    assert code == run_module.EXIT_STALE_DATA
