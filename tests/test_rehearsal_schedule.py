import datetime
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from scripts.launchd.rehearsal_schedule import (
    _CHAINED_JOBS,
    _DAILY_JOBS,
    _ONE_OFF_JOBS,
    all_plists,
    build_chain_command,
)

"""
演練排程：資料更新與次日 parity 補比的串接

擋的是**排程看起來有跑、實際上沒比到東西**：補比排在資料更新之前、或資料更新
失敗就整串中斷，parity 都會安靜地缺一天，而排程端只看得到一個結束碼。
"""


def test_update_db_runs_at_taipei_six_and_is_followed_by_parity() -> None:
    """
    資料更新在台北 06:00，跑完接著補比股票、盤中與期貨

    補比要用前一交易日的日 K，它在資料更新之後才入庫；
    段落時窗不同的策略不可混在同一個行程，所以各跑一次。
    """

    hour, minute, steps = _CHAINED_JOBS["update-db"]

    assert (hour, minute) == (6, 0)
    assert steps[0][:2] == ["-m", "apps.update_db"]
    parity_strategies: List[str] = [
        step[step.index("--strategy") + 1]
        for step in steps[1:]
        if step[step.index("--phase") + 1] == "parity"
    ]
    assert parity_strategies == [
        "VolumeBreakoutMomentumStrategy",
        "IntradayMomentumStrategy",
        "MomentumFuturesStrategy",
    ]
    # 不可同時留著舊的單步資料更新：兩個排程會在不同時刻各跑一次
    assert "update-db" not in _DAILY_JOBS


def test_futures_segments_load_the_rollover_rehearsal_strategy() -> None:
    """
    期貨三個段落都載入換月演練策略，且與示範策略在同一個行程

    少了任一段，演練策略就缺那個段落的鉤子；分成兩個行程則兩邊各以為
    帳戶是自己的。parity 補比不含演練策略（見上一條）。
    """

    for suffix in ("futures-open", "futures-close", "futures-after-close"):
        args: List[str] = _DAILY_JOBS[suffix][2]
        strategies: List[str] = args[args.index("--strategy") + 1].split(",")
        assert strategies == [
            "MomentumFuturesStrategy",
            "FuturesRolloverRehearsalStrategy",
        ], suffix


def test_chain_runs_every_step_and_reports_the_first_failure(tmp_path: Path) -> None:
    """
    前一步失敗不中斷後面，結束碼取第一個失敗的

    資料更新失敗時補比仍要跑，由它的新鮮度檢查留下「為什麼沒比」的紀錄；
    中斷的話排程只剩一個結束碼，看不出補比根本沒跑。
    """

    record: Path = tmp_path / "ran.txt"
    fake_uv: Path = tmp_path / "uv"
    # 假的 uv：記下最後一個參數（步驟代號），再以它當結束碼離開
    fake_uv.write_text(
        '#!/bin/sh\nfor last; do :; done\necho "$last" >> "' + str(record) + '"\n'
        'exit "$last"\n'
    )
    fake_uv.chmod(0o755)

    command: List[str] = build_chain_command(str(fake_uv), [["0"], ["3"], ["5"]])
    result: subprocess.CompletedProcess = subprocess.run(command, check=False)

    assert record.read_text().split() == ["0", "3", "5"]
    assert result.returncode == 3


def test_chain_exits_zero_when_every_step_succeeds(tmp_path: Path) -> None:
    fake_uv: Path = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)

    result: subprocess.CompletedProcess = subprocess.run(
        build_chain_command(str(fake_uv), [["a"], ["b"]]), check=False
    )

    assert result.returncode == 0


def test_one_off_jobs_are_not_left_in_the_past() -> None:
    """
    一次性排程跑完就要刪掉

    launchd 的「月／日」排程每年重複觸發；過期的留在清單裡，每次重裝都會把它裝回去。
    2026-10-08 重裝時 9/23 的 `sim-short-sell` 就被裝了回來。
    """

    today: datetime.date = datetime.date.today()
    expired: List[str] = [
        suffix
        for suffix, (month, day, *_) in _ONE_OFF_JOBS.items()
        if datetime.date(today.year, month, day) < today
    ]

    assert expired == []


def _uv_invocations(arguments: List[str], uv_bin: str) -> List[List[str]]:
    """
    一條 `ProgramArguments` 裡每一次呼叫 uv 的參數（含 uv 本身）

    排程有三種形狀：直接 `[uv, run, ...]`；`/bin/sh -c` 串多步，每行一個 uv；
    `/bin/sh <腳本>`，uv 寫在腳本裡（以 `$UV_BIN` 取用）。
    """

    if arguments[0] == uv_bin:
        return [arguments]
    if arguments[:2] == ["/bin/sh", "-c"]:
        return [
            shlex.split(line.split(";", 1)[0])
            for line in arguments[2].splitlines()
            if line.startswith(shlex.quote(uv_bin)) or line.startswith(uv_bin)
        ]
    script: str = Path(arguments[1]).read_text(encoding="utf-8")
    return [
        ["$UV_BIN", *match.split()]
        for match in re.findall(r'"\$UV_BIN"\s+([^\\\n]*)', script)
    ]


def test_every_scheduled_uv_run_passes_no_sync() -> None:
    """
    排程裡每一次 `uv run` 都帶 `--no-sync`

    `tests/test_subprocess_uv_no_sync.py` 只認 `["uv", "run", ...]` 字面值，排程是
    `[uv_bin, *_UV_RUN_ARGS, ...]` 組出來的，拿掉 `--no-sync` 那邊照樣綠。不帶的話每次排程
    觸發都會把主目錄的 venv 同步回預設相依、移除 extras——主目錄正是排程執行的環境。
    **每個 plist 至少要找到一次 uv 呼叫**：形狀判讀落空時不可以靜默通過。
    """

    uv_bin: str = "/opt/homebrew/bin/uv"
    plists: Dict[str, Dict[str, Any]] = all_plists(uv_bin)
    missing: List[str] = []
    for suffix, plist in plists.items():
        calls: List[List[str]] = _uv_invocations(plist["ProgramArguments"], uv_bin)
        assert calls, f"{suffix} 找不到任何 uv 呼叫"
        for call in calls:
            if call[1:3] != ["run", "--no-sync"]:
                missing.append(f"{suffix}: {shlex.join(call)}")

    assert missing == [], "排程的 uv run 沒帶 --no-sync：\n" + "\n".join(missing)


def test_every_scheduled_process_runs_in_taipei_time() -> None:
    """
    排程啟動的行程都帶 `TZ=Asia/Taipei`

    ETL 以主機時區的 `date.today()` 取「今天」，實盤以台北時間；主機在別的時區時，
    兩邊在台北 00:00 到時差之間對「今天」的認定差一天，資料更新會抓錯日期。
    """

    plists: Dict[str, Dict[str, Any]] = all_plists("/opt/homebrew/bin/uv")

    assert plists
    for suffix, plist in plists.items():
        assert plist["EnvironmentVariables"].get("TZ") == "Asia/Taipei", suffix
