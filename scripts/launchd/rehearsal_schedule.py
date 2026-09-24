import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

"""
模擬環境演練的 launchd 排程：產生、載入、移除

每個段落一個 plist，放在 `~/Library/LaunchAgents/`，標籤一律以 `com.alphaedge.live.`
開頭，移除時依前綴整批卸載，不會碰到別的排程。**全部連模擬環境**：
正式環境的旗標不會出現在任何一個排程裡。

**時間以本機時區寫**：launchd 依系統時區觸發，無法逐排程指定時區。
本檔的時刻表以台北時間撰寫，安裝時換算成本機時間——換算依「安裝當下」的時差，
**夏令時間切換（美東 11/1）後要重新安裝**，否則全部偏一小時。

台北早上的段落落在美東前一天晚上，星期要跟著錯開一天；換算由
`to_local_slots()` 統一處理，不在時刻表裡手寫本機時間。

用法（專案根目錄）：
    uv run python -m scripts.launchd.rehearsal_schedule --install
    uv run python -m scripts.launchd.rehearsal_schedule --uninstall
    uv run python -m scripts.launchd.rehearsal_schedule --status
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_LAUNCH_AGENTS: Path = Path.home() / "Library" / "LaunchAgents"
_LOG_DIR: Path = _PROJECT_ROOT / "logs" / "launchd"
_LABEL_PREFIX: str = "com.alphaedge.live."

# 台北時間的交易日（週一～五）
_TAIPEI_WEEKDAYS: Tuple[int, ...] = (1, 2, 3, 4, 5)

# 標籤後綴 → (台北時, 台北分, run.py 參數)
_DAILY_JOBS: Dict[str, Tuple[int, int, List[str]]] = {
    "update-db": (8, 0, ["-m", "tasks.update_db"]),
    "stock-open": (
        8,
        30,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumStrategy1",
            "--phase",
            "open",
        ],
    ),
    "futures-open": (
        8,
        40,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumFuturesStrategy",
            "--phase",
            "open",
        ],
    ),
    "stock-close": (
        13,
        20,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumStrategy1",
            "--phase",
            "close",
        ],
    ),
    "futures-close": (
        13,
        28,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumFuturesStrategy",
            "--phase",
            "close",
        ],
    ),
    "stock-after-close": (
        14,
        30,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumStrategy1",
            "--phase",
            "after_close",
        ],
    ),
    "futures-after-close": (
        14,
        35,
        [
            "run.py",
            "--mode",
            "live",
            "--strategy",
            "MomentumFuturesStrategy",
            "--phase",
            "after_close",
        ],
    ),
}

# 只跑一次的盤中實測：台北 (月, 日, 時, 分) → pytest 參數
_ONE_OFF_JOBS: Dict[str, Tuple[int, int, int, int, List[str]]] = {
    "sim-short-sell": (
        9,
        23,
        9,
        30,
        [
            "-m",
            "pytest",
            "tests/live/test_shioaji_sim.py",
            "-m",
            "shioaji_sim_order",
            "-k",
            "short_sell",
            "-v",
            "-s",
            "-p",
            "no:randomly",
        ],
    ),
}

# 存活監控的觸發間隔（秒）；時段判斷在 watchdog_in_window.sh 裡
_WATCHDOG_INTERVAL_SECONDS: int = 300


def taipei_offset_hours() -> int:
    """台北比本機快幾小時（依安裝當下的時差）"""

    local: str = subprocess.run(
        ["date", "+%z"], capture_output=True, text=True, check=True
    ).stdout.strip()
    sign: int = -1 if local.startswith("-") else 1
    local_hours: int = sign * int(local[1:3])
    return 8 - local_hours


def to_local_slots(
    hour: int, minute: int, weekdays: Tuple[int, ...]
) -> List[Dict[str, int]]:
    """台北的 (時, 分, 星期) → 本機的 `StartCalendarInterval` 項目；跨日時星期一起平移"""

    offset: int = taipei_offset_hours()
    local_hour: int = hour - offset
    day_shift: int = 0
    while local_hour < 0:
        local_hour += 24
        day_shift -= 1
    while local_hour >= 24:
        local_hour -= 24
        day_shift += 1

    # launchd 的 Weekday：0 與 7 都是週日
    return [
        {"Weekday": (weekday + day_shift) % 7, "Hour": local_hour, "Minute": minute}
        for weekday in weekdays
    ]


def to_local_once(month: int, day: int, hour: int, minute: int) -> Dict[str, int]:
    """台北的某一刻 → 本機的月、日、時、分（只跑一次的排程用）"""

    import datetime

    taipei: datetime.datetime = datetime.datetime(
        datetime.date.today().year, month, day, hour, minute
    )
    local: datetime.datetime = taipei - datetime.timedelta(hours=taipei_offset_hours())
    return {
        "Month": local.month,
        "Day": local.day,
        "Hour": local.hour,
        "Minute": local.minute,
    }


def build_plist(
    suffix: str,
    arguments: List[str],
    calendar: Optional[Any] = None,
    interval: Optional[int] = None,
    uv_bin: str = "",
) -> Dict[str, Any]:
    """組一個 plist 的內容"""

    plist: Dict[str, Any] = {
        "Label": f"{_LABEL_PREFIX}{suffix}",
        "ProgramArguments": arguments,
        "WorkingDirectory": str(_PROJECT_ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path(uv_bin).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "UV_BIN": uv_bin,
        },
        "StandardOutPath": str(_LOG_DIR / f"{suffix}.log"),
        "StandardErrorPath": str(_LOG_DIR / f"{suffix}.err.log"),
    }
    if calendar is not None:
        plist["StartCalendarInterval"] = calendar
    if interval is not None:
        plist["StartInterval"] = interval
    return plist


def all_plists(uv_bin: str) -> Dict[str, Dict[str, Any]]:
    """全部排程：標籤後綴 → plist 內容"""

    plists: Dict[str, Dict[str, Any]] = {}
    for suffix, (hour, minute, args) in _DAILY_JOBS.items():
        plists[suffix] = build_plist(
            suffix,
            [uv_bin, "run", "python", *args],
            calendar=to_local_slots(hour, minute, _TAIPEI_WEEKDAYS),
            uv_bin=uv_bin,
        )
    for suffix, (month, day, hour, minute, args) in _ONE_OFF_JOBS.items():
        plists[suffix] = build_plist(
            suffix,
            [uv_bin, "run", "python", *args],
            calendar=to_local_once(month, day, hour, minute),
            uv_bin=uv_bin,
        )
    plists["watchdog"] = build_plist(
        "watchdog",
        ["/bin/sh", str(Path(__file__).with_name("watchdog_in_window.sh"))],
        interval=_WATCHDOG_INTERVAL_SECONDS,
        uv_bin=uv_bin,
    )
    return plists


def domain() -> str:
    """launchctl 的目標網域（目前登入者的 GUI session）"""

    return f"gui/{os.getuid()}"


def install() -> None:
    """產生 plist、寫入 LaunchAgents 並逐一載入（已載入的先卸載再載）"""

    uv_bin: Optional[str] = shutil.which("uv")
    if uv_bin is None:
        raise SystemExit("找不到 uv，無法安裝排程")

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    print(f"台北比本機快 {taipei_offset_hours()} 小時；排程以此換算")

    for suffix, content in all_plists(uv_bin).items():
        path: Path = _LAUNCH_AGENTS / f"{_LABEL_PREFIX}{suffix}.plist"
        subprocess.run(
            ["launchctl", "bootout", domain(), str(path)], capture_output=True
        )
        with path.open("wb") as handle:
            plistlib.dump(content, handle)
        subprocess.run(["launchctl", "bootstrap", domain(), str(path)], check=True)
        print(f"已載入 {content['Label']}")


def uninstall() -> None:
    """依標籤前綴卸載並刪除全部演練排程"""

    for path in sorted(_LAUNCH_AGENTS.glob(f"{_LABEL_PREFIX}*.plist")):
        subprocess.run(
            ["launchctl", "bootout", domain(), str(path)], capture_output=True
        )
        path.unlink()
        print(f"已移除 {path.stem}")


def status() -> None:
    """列出已安裝的排程與上次結束碼"""

    for path in sorted(_LAUNCH_AGENTS.glob(f"{_LABEL_PREFIX}*.plist")):
        label: str = path.stem
        result: subprocess.CompletedProcess = subprocess.run(
            ["launchctl", "print", f"{domain()}/{label}"],
            capture_output=True,
            text=True,
        )
        loaded: bool = result.returncode == 0
        last_exit: str = next(
            (
                line.split("=", 1)[1].strip()
                for line in result.stdout.splitlines()
                if "last exit code" in line
            ),
            "-",
        )
        print(f"{label:45s} 已載入={loaded} 上次結束碼={last_exit}")


def main() -> None:
    """安裝、移除或列出演練排程"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="模擬環境演練的 launchd 排程"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--status", action="store_true")
    args: argparse.Namespace = parser.parse_args()

    if args.install:
        install()
    elif args.uninstall:
        uninstall()
    else:
        status()


if __name__ == "__main__":
    main()
