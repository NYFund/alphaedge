import subprocess
from typing import List

import pytest

from tests.entry_sandbox import run_isolated

"""
`python -m apps.backtest` 的退出碼契約

**一定要用 subprocess 驗**：退出碼是**行程**的性質，直接呼叫 `main()` 只驗得到
有沒有拋例外，驗不到 `sys.exit()` 實際交給呼叫端的那個數字，也驗不到
訊息去了 stdout 還是 stderr。

本檔的每一條都**不會真的跑回測**——這些情況都在建 Backtester 之前就結束，
所以不需要 `data/db/*.db`，也不標 `slow`。
"""


# 用法錯誤：與 argparse 自己的用法錯誤同碼（缺必填參數時它就回 2）
EXIT_USAGE_ERROR: int = 2


def run_backtest_entry(*args: str) -> subprocess.CompletedProcess:
    """以子行程跑回測入口"""

    return run_isolated(["-m", "apps.backtest", *args])


def test_unknown_strategy_exits_with_usage_error() -> None:
    """策略名打錯與回測跑完在排程端不可長得一樣"""

    result: subprocess.CompletedProcess = run_backtest_entry(
        "--strategy", "NoSuchStrategy"
    )

    assert result.returncode == EXIT_USAGE_ERROR


def test_unknown_strategy_reports_on_stderr_with_available_list() -> None:
    """錯誤訊息走 stderr（不混進正常輸出），並列出可用策略"""

    result: subprocess.CompletedProcess = run_backtest_entry(
        "--strategy", "NoSuchStrategy"
    )

    assert "not found" in result.stderr
    assert "not found" not in result.stdout
    assert "Available strategies:" in result.stderr
    assert "MomentumStrategy1" in result.stderr


def test_missing_strategy_argument_is_a_usage_error() -> None:
    """缺必填參數由 argparse 拒絕，同樣是用法錯誤"""

    assert run_backtest_entry().returncode == EXIT_USAGE_ERROR


@pytest.mark.parametrize(
    "flags",
    [
        ["--production", "--confirm-production"],
        ["--dry-run"],
        ["--phase", "open"],
        ["--broker", "fake"],
        ["--resync-from-broker"],
        ["--resume-trading"],
        ["--mode", "live"],
    ],
    ids=["production", "dry-run", "phase", "broker", "resync", "resume", "mode"],
)
def test_live_flags_are_rejected_by_the_parser(flags: List[str]) -> None:
    """
    回測入口不認得任何實盤旗標：帶了就是用法錯誤，**不可以靜靜跑一場回測**

    以為自己在連正式環境下單、其實只跑了回測，會讓人以為「今天沒有訊號」。
    舊的共用 parser 靠一份手寫清單擋；分開之後由 argparse 在結構上擋掉，
    新增實盤旗標也不必回來補這份清單。
    """

    result: subprocess.CompletedProcess = run_backtest_entry(
        "--strategy", "MomentumStrategy1", *flags
    )

    assert result.returncode == EXIT_USAGE_ERROR
    assert flags[0] in result.stderr


def test_show_flags_are_accepted() -> None:
    """回測自己的旗標要照常解析；停在「策略名找不到」代表 `--no-show` 沒被拒絕"""

    result: subprocess.CompletedProcess = run_backtest_entry(
        "--strategy", "NoSuchStrategy", "--no-show"
    )

    assert "not found" in result.stderr


def test_show_and_no_show_are_mutually_exclusive() -> None:
    """兩個互斥旗標同時出現是用法錯誤"""

    result: subprocess.CompletedProcess = run_backtest_entry(
        "--strategy", "MomentumStrategy1", "--show", "--no-show"
    )

    assert result.returncode == EXIT_USAGE_ERROR


def test_help_lists_only_backtest_flags() -> None:
    """`--help` 不再出現任何實盤旗標：回測入口的使用者看不到、也用不到它們"""

    result: subprocess.CompletedProcess = run_backtest_entry("--help")

    assert result.returncode == 0
    assert "--strategy" in result.stdout
    assert "--no-show" in result.stdout
    assert "--production" not in result.stdout
    assert "--phase" not in result.stdout
