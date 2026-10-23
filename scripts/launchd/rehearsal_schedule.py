import argparse
import datetime
import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

"""
模擬環境演練的 launchd 排程：產生、載入、移除

每個段落一個 plist，放在 `~/Library/LaunchAgents/`，標籤一律以 `com.alphaedge.live.`
開頭，移除時依前綴整批卸載，不會碰到別的排程。**全部連模擬環境**：
正式環境的旗標不會出現在任何一個排程裡。

**時間以本機時區寫**：launchd 依系統時區觸發，無法逐排程指定時區。
本檔的時刻表以台北時間撰寫，安裝時換算成本機時間，換算用的時差記在每個 plist 的
`ALPHAEDGE_TAIPEI_OFFSET_HOURS`。**時差會變**：夏令時間切換、主機改時區，而且 launchd
可能沿用開機當時的時區、與 shell 的 `date +%z` 不同（2026-10 演練就因此裝錯過一次）。
所以另有一個 `tz-guard` 排程每天在 launchd 本機時間 03:07 觸發，以「被觸發的這一刻」
反推 launchd 實際用的時差，與安裝時記下的不同就自動以量到的時差重裝（見 `ensure_offset()`）。

台北早上的段落落在美東前一天晚上，星期要跟著錯開一天；換算由
`to_local_slots()` 統一處理，不在時刻表裡手寫本機時間。

用法（專案根目錄）：
    uv run python -m scripts.launchd.rehearsal_schedule --install
    uv run python -m scripts.launchd.rehearsal_schedule --uninstall
    uv run python -m scripts.launchd.rehearsal_schedule --status
    uv run python -m scripts.launchd.rehearsal_schedule --ensure-offset   # tz-guard 排程呼叫
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_LAUNCH_AGENTS: Path = Path.home() / "Library" / "LaunchAgents"
_LOG_DIR: Path = _PROJECT_ROOT / "logs" / "launchd"
_LABEL_PREFIX: str = "com.alphaedge.live."

# 台北時間的交易日（週一～五）
_TAIPEI_WEEKDAYS: Tuple[int, ...] = (1, 2, 3, 4, 5)

# 股票開盤段與盤後段的策略：`IntradayMomentumStrategy` 隔天開盤出場在開盤段，
# 盤後對帳與快照也要涵蓋它；盤中進場與停損由 `stock-intraday` 另一個行程負責
_STOCK_STRATEGIES: str = "VolumeBreakoutMomentumStrategy,IntradayMomentumStrategy"

# 期貨三個段落載入的策略（同一個行程、共用帳戶）。
# `FuturesRolloverRehearsalStrategy` 只為 2026-10-20 的換月演練而設：示範策略很少出訊號，
# 換月日沒有部位就驗不到換月兩腿。演練結束移除排程時一併拿掉；
# 盤後 parity 補比只比示範策略（演練策略沒有回測意義）
_FUTURES_STRATEGIES: str = "MomentumFuturesStrategy,FuturesRolloverRehearsalStrategy"

# 標籤後綴 → (台北時, 台北分, `python` 之後的參數)
_DAILY_JOBS: Dict[str, Tuple[int, int, List[str]]] = {
    "stock-open": (
        8,
        30,
        [
            "-m",
            "apps.live",
            "--strategy",
            _STOCK_STRATEGIES,
            "--phase",
            "open",
        ],
    ),
    # 盤中逐筆：開盤段 09:05 收線後才啟動，13:19 收線、早於 13:20 的尾盤段——
    # 同一個帳號的委託回報會推給每一條連線，不讓兩個行程同時在場上
    "stock-intraday": (
        9,
        6,
        [
            "-m",
            "apps.live",
            "--strategy",
            "IntradayMomentumStrategy",
            "--phase",
            "intraday",
        ],
    ),
    "futures-open": (
        8,
        40,
        [
            "-m",
            "apps.live",
            "--strategy",
            _FUTURES_STRATEGIES,
            "--phase",
            "open",
        ],
    ),
    "stock-close": (
        13,
        20,
        [
            "-m",
            "apps.live",
            "--strategy",
            "VolumeBreakoutMomentumStrategy",
            "--phase",
            "close",
        ],
    ),
    "futures-close": (
        13,
        28,
        [
            "-m",
            "apps.live",
            "--strategy",
            _FUTURES_STRATEGIES,
            "--phase",
            "close",
        ],
    ),
    "stock-after-close": (
        14,
        30,
        [
            "-m",
            "apps.live",
            "--strategy",
            _STOCK_STRATEGIES,
            "--phase",
            "after_close",
        ],
    ),
    "futures-after-close": (
        14,
        35,
        [
            "-m",
            "apps.live",
            "--strategy",
            _FUTURES_STRATEGIES,
            "--phase",
            "after_close",
        ],
    ),
}

# 依序執行的多步排程：標籤後綴 → (台北時, 台北分, [各步 `python` 之後的參數])
#
# 資料更新排在台北 06:00，跑完**接著**補比前一交易日的訊號 parity：
# - 補比要用前一交易日的日 K，它在資料更新後才入庫；盤後當下比，回測一張委託都產生不出來。
# - 接在後面而不排固定時刻：資料更新實測 8 分鐘到近 3 小時不等，固定時刻可能比到還沒入庫的資料。
#   資料更新失敗時補比照樣執行，由它自己的新鮮度檢查拒絕（退出碼 3），失敗原因才看得到。
# - 06:00 而不是 08:00：留兩個多小時給 08:30 的開盤段，資料更新拖長時也不會撞上。
# - 失敗的 target 每 60 分鐘只重跑失敗的那幾個、最多 4 輪：交易所封鎖來源 IP 時整批更新
#   只差那一兩個 target，股票段落卻全被新鮮度檢查擋下。06:0x 失敗的話 07:xx、08:xx 兩輪
#   趕得上開盤段，之後兩輪趕收盤段；補比因此可能延後，但它本來就不急。
# 股票與期貨各跑一次補比：兩條線的策略不可混在同一個行程（段落時窗不同）。
# 盤中動能策略另跑一次：它的段落（逐筆＋開盤段）與尾盤策略不同，混在同一行程會因時窗無交集而拒絕。
# 它的當日回測是日 K 近似、實盤是逐筆，**差異是預期中的**（使用者裁示先直接比，有不一樣再檢討原因）
_CHAINED_JOBS: Dict[str, Tuple[int, int, List[List[str]]]] = {
    "update-db": (
        6,
        0,
        [
            ["-m", "apps.update_db", "--retry-failed", "4", "--retry-wait", "60"],
            [
                "-m",
                "apps.live",
                "--strategy",
                "VolumeBreakoutMomentumStrategy",
                "--phase",
                "parity",
            ],
            [
                "-m",
                "apps.live",
                "--strategy",
                "IntradayMomentumStrategy",
                "--phase",
                "parity",
            ],
            [
                "-m",
                "apps.live",
                "--strategy",
                "MomentumFuturesStrategy",
                "--phase",
                "parity",
            ],
        ],
    ),
}

# 只跑一次的盤中實測：標籤後綴 → 台北 (月, 日, 時, 分, `python` 之後的參數)
#
# **跑完就要從這裡刪掉**：launchd 的「月／日」排程每年都會重複觸發，留著的話
# 每次重裝（例如夏令時間切換）都會把它裝回去，明年同一天再跑一次。
# 2026-09-23 的 `sim-short-sell`（盤中放空實測）已執行完畢並移除。
# 護欄見 `tests/test_rehearsal_schedule.py`
_ONE_OFF_JOBS: Dict[str, Tuple[int, int, int, int, List[str]]] = {}

# 排程一律帶 `--no-sync`：`uv run` 預設會先同步環境，`pyproject.toml` 一變動就要重建
# 套件並連 PyPI 下載 build 相依；排程時段的網路不保證可用，DNS 一失敗段落就在
# 進入口之前結束。改完相依後手動 `uv sync` 一次即可
_UV_RUN_ARGS: Tuple[str, ...] = ("run", "--no-sync", "python")

# 存活監控的觸發間隔（秒）；時段判斷在 watchdog_in_window.sh 裡
_WATCHDOG_INTERVAL_SECONDS: int = 300

# 時差守衛：固定在 launchd 本機時間觸發、**不換算**，被觸發的時刻本身就是量測。
# 03:07 換成台北是 15:07～17:07（本機 UTC−4～−6），所有段落都已結束，重裝不會撞上段落
_TZ_GUARD_SUFFIX: str = "tz-guard"
_TZ_GUARD_LOCAL_HOUR: int = 3
_TZ_GUARD_LOCAL_MINUTE: int = 7
# 實際觸發晚於排定時刻這麼多分鐘以上時不採信量測（主機睡眠或負載造成的延遲會讓小時算錯）
_TZ_GUARD_MAX_DELAY_MINUTES: int = 8
# 記在每個 plist 的安裝時差（台北比 launchd 本機快幾小時）
_OFFSET_ENV: str = "ALPHAEDGE_TAIPEI_OFFSET_HOURS"


def taipei_offset_hours() -> int:
    """台北比本機快幾小時（依安裝當下的時差）"""

    local: str = subprocess.run(
        ["date", "+%z"], capture_output=True, text=True, check=True
    ).stdout.strip()
    sign: int = -1 if local.startswith("-") else 1
    local_hours: int = sign * int(local[1:3])
    return 8 - local_hours


def to_local_slots(
    hour: int, minute: int, weekdays: Tuple[int, ...], offset: Optional[int] = None
) -> List[Dict[str, int]]:
    """
    台北的 (時, 分, 星期) → 本機的 `StartCalendarInterval` 項目；跨日時星期一起平移

    `offset` 是台北比本機快幾小時；未給時取 `taipei_offset_hours()`
    """

    offset = taipei_offset_hours() if offset is None else offset
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


def to_local_once(
    month: int, day: int, hour: int, minute: int, offset: Optional[int] = None
) -> Dict[str, int]:
    """台北的某一刻 → 本機的月、日、時、分（只跑一次的排程用）；`offset` 同 `to_local_slots()`"""

    offset = taipei_offset_hours() if offset is None else offset
    taipei: datetime.datetime = datetime.datetime(
        datetime.date.today().year, month, day, hour, minute
    )
    local: datetime.datetime = taipei - datetime.timedelta(hours=offset)
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
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    """組一個 plist 的內容；`offset` 是換算用的時差，記進環境變數供 `ensure_offset()` 比對"""

    plist: Dict[str, Any] = {
        "Label": f"{_LABEL_PREFIX}{suffix}",
        "ProgramArguments": arguments,
        "WorkingDirectory": str(_PROJECT_ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path(uv_bin).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "UV_BIN": uv_bin,
            # 被排程的行程一律以台北時間取「今天」：ETL 用 `datetime.date.today()`（主機時區），
            # 實盤用 `now_live()`（台北）。主機不在台北時區時，兩者在台北 00:00～時差之間
            # 會差一天。**不影響觸發時刻**：`StartCalendarInterval` 由 launchd 以主機時區解讀，
            # 已在安裝時以 `to_local_slots()` 換算
            "TZ": "Asia/Taipei",
        },
        "StandardOutPath": str(_LOG_DIR / f"{suffix}.log"),
        "StandardErrorPath": str(_LOG_DIR / f"{suffix}.err.log"),
    }
    if offset is not None:
        plist["EnvironmentVariables"][_OFFSET_ENV] = str(offset)
    if calendar is not None:
        plist["StartCalendarInterval"] = calendar
    if interval is not None:
        plist["StartInterval"] = interval
    return plist


def all_plists(uv_bin: str, offset: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """全部排程：標籤後綴 → plist 內容；`offset` 未給時取 `taipei_offset_hours()`"""

    offset = taipei_offset_hours() if offset is None else offset
    plists: Dict[str, Dict[str, Any]] = {}
    for suffix, (hour, minute, args) in _DAILY_JOBS.items():
        plists[suffix] = build_plist(
            suffix,
            [uv_bin, *_UV_RUN_ARGS, *args],
            calendar=to_local_slots(hour, minute, _TAIPEI_WEEKDAYS, offset),
            uv_bin=uv_bin,
            offset=offset,
        )
    for suffix, (hour, minute, steps) in _CHAINED_JOBS.items():
        plists[suffix] = build_plist(
            suffix,
            build_chain_command(uv_bin, steps),
            calendar=to_local_slots(hour, minute, _TAIPEI_WEEKDAYS, offset),
            uv_bin=uv_bin,
            offset=offset,
        )
    for suffix, (month, day, hour, minute, args) in _ONE_OFF_JOBS.items():
        plists[suffix] = build_plist(
            suffix,
            [uv_bin, *_UV_RUN_ARGS, *args],
            calendar=to_local_once(month, day, hour, minute, offset),
            uv_bin=uv_bin,
            offset=offset,
        )
    plists["watchdog"] = build_plist(
        "watchdog",
        ["/bin/sh", str(Path(__file__).with_name("watchdog_in_window.sh"))],
        interval=_WATCHDOG_INTERVAL_SECONDS,
        uv_bin=uv_bin,
        offset=offset,
    )
    # 守衛自己不換算、也不記時差：它的觸發時刻要固定在 launchd 本機時間，才量得出時差
    plists[_TZ_GUARD_SUFFIX] = build_plist(
        _TZ_GUARD_SUFFIX,
        [
            uv_bin,
            *_UV_RUN_ARGS,
            "-m",
            "scripts.launchd.rehearsal_schedule",
            "--ensure-offset",
        ],
        calendar={"Hour": _TZ_GUARD_LOCAL_HOUR, "Minute": _TZ_GUARD_LOCAL_MINUTE},
        uv_bin=uv_bin,
    )
    return plists


def measure_launchd_offset(now_utc: datetime.datetime) -> Optional[int]:
    """
    - Description:
        由守衛被觸發的時刻反推 launchd 實際使用的時差（台北比 launchd 本機快幾小時）

        守衛排在 launchd 本機 03:07；被觸發時的 UTC 時刻減掉 03:07 就是 launchd 的 UTC 時差。
        **量的是 launchd 怎麼解讀排程，不是 shell 的 `date +%z`**：兩者可能不同
        （launchd 沿用開機當時的時區）。觸發延遲太久時小時可能算錯，那時回 None、不採信。
    - Parameters:
        - now_utc: datetime.datetime
            被觸發當下的 UTC 時刻（aware）
    - Return:
        - Optional[int]
            台北比 launchd 本機快幾小時；觸發延遲超過 `_TZ_GUARD_MAX_DELAY_MINUTES` 時為 None
    """

    delay: int = now_utc.minute - _TZ_GUARD_LOCAL_MINUTE
    if not 0 <= delay < _TZ_GUARD_MAX_DELAY_MINUTES:
        return None
    launchd_utc_offset: int = (_TZ_GUARD_LOCAL_HOUR - now_utc.hour) % 24
    # 正規化到 −12～+14（世界時區的範圍）
    if launchd_utc_offset > 14:
        launchd_utc_offset -= 24
    return 8 - launchd_utc_offset


def installed_offset() -> Optional[int]:
    """已安裝排程記下的時差；沒有安裝、或是加這個欄位之前裝的，回 None"""

    for path in sorted(_LAUNCH_AGENTS.glob(f"{_LABEL_PREFIX}*.plist")):
        with path.open("rb") as handle:
            content: Dict[str, Any] = plistlib.load(handle)
        raw: Optional[str] = content.get("EnvironmentVariables", {}).get(_OFFSET_ENV)
        if raw is not None:
            return int(raw)
    return None


def ensure_offset(now_utc: Optional[datetime.datetime] = None) -> Optional[int]:
    """
    - Description:
        守衛排程的本體：launchd 實際時差與安裝時記下的不同，就以量到的時差重裝

        **重裝時跳過守衛自己**：卸載自己的標籤會在重裝到一半時把這個行程殺掉；
        守衛的 plist 不隨時差變動，本來就不必重裝。
    - Parameters:
        - now_utc: Optional[datetime.datetime]
            被觸發的 UTC 時刻；None 時取現在
    - Return:
        - Optional[int]
            重裝時採用的時差；不需重裝或量測不可信時為 None
    """

    now_utc = datetime.datetime.now(datetime.UTC) if now_utc is None else now_utc
    measured: Optional[int] = measure_launchd_offset(now_utc)
    if measured is None:
        print(f"守衛於 {now_utc.isoformat()} 觸發，延遲過久，本次不量測時差")
        return None

    recorded: Optional[int] = installed_offset()
    if recorded == measured:
        print(f"launchd 時差仍為台北快 {measured} 小時，排程不變")
        return None

    print(f"launchd 時差由 {recorded} 變為 {measured} 小時，以新時差重裝排程")
    install(offset=measured, skip={_TZ_GUARD_SUFFIX})
    return measured


def build_chain_command(uv_bin: str, steps: List[List[str]]) -> List[str]:
    """
    - Description:
        把多步指令組成一個 `/bin/sh -c` 排程：**每一步都執行**，結束碼取第一個失敗的

        前一步失敗不中斷後面：資料更新失敗時補比仍要跑，由它的新鮮度檢查留下
        「為什麼沒比」的紀錄；中斷的話就只剩一個結束碼，看不出補比沒跑。
    - Parameters:
        - uv_bin: str
            uv 執行檔路徑
        - steps: List[List[str]]
            各步 `python` 之後的參數
    - Return:
        - List[str]
            `ProgramArguments`
    """

    lines: List[str] = ["status=0"]
    for step in steps:
        command: str = shlex.join([uv_bin, *_UV_RUN_ARGS, *step])
        lines.append(f'{command}; code=$?; [ "$status" -eq 0 ] && status=$code')
    lines.append('exit "$status"')
    return ["/bin/sh", "-c", "\n".join(lines)]


def domain() -> str:
    """launchctl 的目標網域（目前登入者的 GUI session）"""

    return f"gui/{os.getuid()}"


def install(offset: Optional[int] = None, skip: Optional[Set[str]] = None) -> None:
    """
    產生 plist、寫入 LaunchAgents 並逐一載入（已載入的先卸載再載）

    `offset` 未給時以 `date +%z` 換算；與 launchd 實際時差不同時，`tz-guard`
    下一次觸發會以量到的時差改正。`skip` 是不重裝的標籤後綴。
    """

    uv_bin: Optional[str] = shutil.which("uv")
    if uv_bin is None:
        raise SystemExit("找不到 uv，無法安裝排程")

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    offset = taipei_offset_hours() if offset is None else offset
    print(f"台北比本機快 {offset} 小時；排程以此換算")

    for suffix, content in all_plists(uv_bin, offset).items():
        if skip and suffix in skip:
            continue
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
    group.add_argument(
        "--ensure-offset",
        action="store_true",
        help="tz-guard 排程呼叫：launchd 實際時差與安裝時不同就自動重裝",
    )
    args: argparse.Namespace = parser.parse_args()

    if args.install:
        install()
    elif args.uninstall:
        uninstall()
    elif args.ensure_offset:
        ensure_offset()
    else:
        status()


if __name__ == "__main__":
    main()
