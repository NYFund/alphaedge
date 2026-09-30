import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

import run as run_module
from tests.entry_sandbox import run_isolated

"""
`run.py` 轉發 shim 的契約

回測與實盤已拆成 `apps.backtest`／`apps.live`，`run.py` 在過渡期只負責抽出 `--mode`
並轉發。這裡只驗轉發本身：轉到對的入口、參數原封不動、**退出碼與新入口相同**。
各入口自己的行為由 `tests/test_backtest_entry.py`、`tests/test_live_entry.py` 驗。

退出碼一律以子行程驗：它是**行程**的性質，直接呼叫 `main()` 驗不到
`sys.exit()` 實際交給呼叫端（launchd、compose）的那個數字。
"""

_RUN_PY: Path = Path(__file__).resolve().parents[1] / "run.py"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ("backtest", [])),
        (["--strategy", "A"], ("backtest", ["--strategy", "A"])),
        (["--mode", "live", "--strategy", "A"], ("live", ["--strategy", "A"])),
        (["--strategy", "A", "--mode=live"], ("live", ["--strategy", "A"])),
        (["--strategy", "A", "--mode"], (None, ["--strategy", "A"])),
    ],
    ids=["default", "backtest", "live", "equals-form", "missing-value"],
)
def test_split_mode(argv: List[str], expected: Tuple[Optional[str], List[str]]) -> None:
    """`--mode` 被抽掉、其餘參數順序不變；沒帶時預設回測，與舊入口相同"""

    assert run_module.split_mode(argv) == expected


@pytest.mark.parametrize(
    ("shim_args", "entry_args"),
    [
        (["--strategy", "NoSuchStrategy"], ["-m", "apps.backtest"]),
        (
            ["--mode", "backtest", "--strategy", "MomentumStrategy1", "--production"],
            ["-m", "apps.backtest"],
        ),
        (["--mode", "live", "--strategy", "MomentumStrategy1"], ["-m", "apps.live"]),
        (
            [
                "--mode",
                "live",
                "--strategy",
                "MomentumStrategy1",
                "--phase",
                "close",
                "--production",
            ],
            ["-m", "apps.live"],
        ),
        (
            ["--mode", "live", "--strategy", "NoSuchStrategy", "--phase", "close"],
            ["-m", "apps.live"],
        ),
    ],
    ids=[
        "backtest-unknown-strategy",
        "backtest-with-live-flag",
        "live-without-phase",
        "live-production-unconfirmed",
        "live-unknown-strategy",
    ],
)
def test_exit_code_matches_the_new_entry(
    shim_args: List[str], entry_args: List[str]
) -> None:
    """
    shim 與新入口的退出碼相同，而且都不是 0

    launchd 與 compose 依退出碼判讀；shim 把它吞成 0 或 1 就是假綠燈。
    `--mode backtest --production` 仍要回 2：以為在下單、其實跑了回測的情況不可復活。
    """

    rest: List[str] = run_module.split_mode(shim_args)[1]
    shim: subprocess.CompletedProcess = run_isolated([str(_RUN_PY), *shim_args])
    entry: subprocess.CompletedProcess = run_isolated([*entry_args, *rest])

    assert shim.returncode == entry.returncode
    assert shim.returncode != 0


def test_unknown_mode_is_a_usage_error() -> None:
    """`--mode` 只收 backtest 與 live"""

    result: subprocess.CompletedProcess = run_isolated(
        [str(_RUN_PY), "--mode", "paper", "--strategy", "MomentumStrategy1"]
    )

    assert result.returncode == 2
    assert "--mode" in result.stderr


def test_shim_warns_with_the_new_command() -> None:
    """轉發時要寫出新指令，呼叫端才知道要改成什麼"""

    result: subprocess.CompletedProcess = run_isolated(
        [str(_RUN_PY), "--mode", "live", "--strategy", "NoSuchStrategy"]
    )

    assert "python -m apps.live" in result.stderr
