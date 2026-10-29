import argparse
import datetime
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from loguru import logger

from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.config import LOGS_DIR_PATH, TW_STOCK_DB_PATH, TW_TRADING_DB_PATH
from core.config.settings import (
    LIVE_NOTIFY_CHANNEL,
    LIVE_NOTIFY_TARGET,
    LIVE_NOTIFY_TOKEN,
    now_live,
)
from core.dao.connection import (
    DBConnection,
    DBError,
    connect_live_trading,
    connect_sqlite,
)
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.calendar import (
    OfficialHolidayCalendarSource,
    TradingCalendarSource,
    TradingCalendarUnavailableError,
    WeekendCalendarSource,
    resolve_trading_day,
)
from core.live.notify.base import BaseNotifier, NotifyLevel
from core.live.notify.factory import build_notifier
from core.utils.constant.market import InstrumentType

"""
存活監控：實盤最常見的失效是**該跑的沒跑、沒人發現**

`live_risk_event` 與 log 都是 pull 型：人要先去看才知道。程式在 09:05 因為登入
失敗退出，到收盤都不會有人知道。

**本檔必須是獨立行程，不可以放進 `core/live/`。** 和 LiveTrader 同一個行程的心跳
會跟著行程一起死——那等於沒有監控。

它只做三件事，而且**只以唯讀開啟紀錄庫、不連券商**：
1. 先判斷今天是不是交易日（週末＋官方開休市日曆，與實盤段落同一套判定），
   休市日直接結束；再比對「當日應執行的段落表」與 `live_run` 的實際紀錄。
2. 找出「該跑而沒有紀錄」與「有開始紀錄但停在未正常結束」（行程已不在，或超過時間上限）。
3. 推播；同一個異常當天只推一次（已推過的記在 `logs/` 底下的小檔案，這是它唯一寫入的東西）。

**任何例外都轉成推播，不靜靜退出**——連紀錄庫都開不了也一樣：
**監控自己死掉而沒人知道，和沒有監控是同一件事。**

用法（一律在專案根目錄以 `-m` 執行）：

    .venv/bin/python -m scripts.live_watchdog
    .venv/bin/python -m scripts.live_watchdog --expect open=08:30 close=13:20
    .venv/bin/python -m scripts.live_watchdog --expect close:stock=13:20 close:futures=13:28

段落鍵可以帶市場（`段落:市場`）：股票與期貨是兩個行程、段落名相同，只看段落名的話
股票尾盤的紀錄會把「期貨尾盤沒跑」遮住。不帶市場的鍵維持舊語意——該段落任一市場有跑就算。
"""

# 段落鍵裡的市場字樣 → `live_run.instrument_type` 的值
MARKET_TOKENS: Dict[str, str] = {
    "stock": InstrumentType.STOCK.value,
    "futures": InstrumentType.FUTURE.value,
    "future": InstrumentType.FUTURE.value,
}

# 預設的段落表：段落鍵 → 應該開始執行的時刻。
#
# 與部署文件的排程對齊；**容忍延遲**是因為排程器本身就有秒級誤差，
# 而且段落要先做完對帳才會寫紀錄。
# 預設不帶市場：只跑一條線的部署不該因為另一條線沒排而每天告警；
# 兩條線都跑時由排程以 `--expect` 逐市場指定（見 scripts/launchd/watchdog_in_window.sh）
DEFAULT_EXPECTED_PHASES: Dict[str, datetime.time] = {
    # 次日資料更新後的訊號 parity 補比，排在 06:00 的資料更新之後
    "parity": datetime.time(6, 0),
    "open": datetime.time(8, 30),
    "intraday": datetime.time(9, 6),
    "close": datetime.time(13, 20),
    "after_close": datetime.time(14, 30),
}

# 超過應執行時刻這麼久還沒有紀錄，就判定「沒跑」
GRACE_MINUTES: int = 15

# 有開始紀錄但超過這麼久還沒有結束時間，就判定「跑到一半死掉」
STALE_RUN_MINUTES: int = 90

# 本來就要跑很久的段落另訂上限。盤中段落 09:06 開始、13:19 收線（約 253 分鐘）：
# 沿用 90 分鐘的話，它每天 10:36 起都會被誤報成「跑到一半死掉」，
# 然後這個監控就會被靜音
PHASE_STALE_MINUTES: Dict[str, int] = {"intraday": 270}

# 退出碼：0 正常、1 有異常（已推播或先前推播過）、2 連紀錄庫都開不了
EXIT_OK: int = 0
EXIT_ALERTED: int = 1
EXIT_UNREACHABLE: int = 2

# 已推播過的異常記在這裡，同一個異常當天只推一次。
# watchdog 每 5 分鐘一個新行程，記憶只能放在檔案；檔案遺失只會多推一次，不會漏推
DEFAULT_STATE_PATH: Path = LOGS_DIR_PATH / "watchdog_alerted.json"

# 這些段落排在資料更新之後、由同一條排程串起來：串接還在跑（資料更新失敗重試中）時，
# 沒有紀錄是「還沒輪到」而不是「沒跑」。重試最長可達數小時，照常告警的話
# 那段時間每 5 分鐘一則假 CRITICAL
CHAINED_AFTER_UPDATE_DB: FrozenSet[str] = frozenset({"parity"})

# 資料更新串接的行程特徵：launchd 以 `/bin/sh -c` 依序跑資料更新與三條補比，
# 整條串接跑完之前那個 sh 行程的命令列都帶著這一段
UPDATE_DB_CHAIN_PATTERN: str = "apps[.]update_db"

# 行程啟動時刻比段落紀錄的開始時刻晚這麼多，就是 pid 被別的行程重用了。
# 實盤行程先啟動、做完初始化才寫紀錄，正常情況行程只會比紀錄早
PID_REUSE_TOLERANCE_SECONDS: int = 60


@dataclass(frozen=True)
class PhaseStatus:
    """單一段落的檢查結果"""

    phase: str
    expected_at: datetime.time
    started_at: Optional[str]
    ended_at: Optional[str]
    problem: Optional[str]
    market: Optional[str] = None  # `live_run.instrument_type` 的值；不分市場時為 None
    # 逐筆異常的（去重鍵, 訊息）；`problem` 是這些訊息串起來的結果
    alerts: Tuple[Tuple[str, str], ...] = ()

    @property
    def is_healthy(self) -> bool:
        """沒有問題才算健康"""

        return self.problem is None


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="實盤存活監控（唯讀、不連券商）"
    )
    parser.add_argument(
        "--expect",
        nargs="*",
        metavar="段落[:市場]=HH:MM",
        help=(
            "應執行的段落與時刻；未指定時用預設表。"
            "市場為 stock／futures，不帶市場時該段落任一市場有跑就算"
        ),
    )
    parser.add_argument(
        "--db-path", default=str(TW_TRADING_DB_PATH), help="實盤紀錄庫路徑"
    )
    parser.add_argument(
        "--stock-db-path",
        default=str(TW_STOCK_DB_PATH),
        help="研究庫路徑（讀官方開休市日曆判斷今天是否為交易日）",
    )
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=GRACE_MINUTES,
        help="超過應執行時刻多久仍無紀錄就判定沒跑",
    )
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="已推播異常的紀錄檔（同一個異常當天只推一次）",
    )
    return parser.parse_args()


def split_phase_key(key: str) -> Tuple[str, Optional[str]]:
    """
    - Description:
        把段落鍵拆成（段落名, 市場）

        市場字樣只認 `MARKET_TOKENS` 裡的幾個：打錯字（`future s`、`stocks`）要當場拋出，
        否則那個鍵永遠比不到任何紀錄、每天都告警，然後被靜音。
    - Parameters:
        - key: str
            `段落` 或 `段落:市場`
    - Return:
        - Tuple[str, Optional[str]]
            段落名與 `live_run.instrument_type` 的值；不帶市場時為 None
    - Raise:
        - ValueError
            市場字樣不認得
    """

    phase, _, raw_market = key.partition(":")
    market: str = raw_market.strip().lower()
    if not market:
        return phase.strip(), None
    if market not in MARKET_TOKENS:
        raise ValueError(
            f"段落鍵的市場只能是 {sorted(MARKET_TOKENS)}，收到 {raw_market!r}"
        )
    return phase.strip(), MARKET_TOKENS[market]


def parse_expected(values: Optional[Sequence[str]]) -> Dict[str, datetime.time]:
    """
    - Description:
        解析 `段落[:市場]=HH:MM` 格式的段落表
    - Parameters:
        - values: Optional[Sequence[str]]
            命令列傳入的段落表；None 或空時回預設表
    - Return:
        - Dict[str, datetime.time]
            段落鍵（`段落` 或 `段落:市場`，市場字樣轉小寫）→ 應執行時刻
    - Raise:
        - ValueError
            格式錯誤或市場字樣不認得
    """

    if not values:
        return dict(DEFAULT_EXPECTED_PHASES)

    expected: Dict[str, datetime.time] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"段落表格式應為 `段落[:市場]=HH:MM`，收到 {value!r}")
        raw_key, raw_time = value.split("=", 1)
        phase, _, raw_market = raw_key.partition(":")
        split_phase_key(raw_key)  # 只為了驗證市場字樣
        key: str = phase.strip()
        if raw_market.strip():
            key = f"{key}:{raw_market.strip().lower()}"
        hour, minute = raw_time.split(":")
        expected[key] = datetime.time(int(hour), int(minute))
    return expected


def _row_matches(row: Tuple[Any, ...], phase: str, market: Optional[str]) -> bool:
    """
    這筆 `live_run` 紀錄是不是指定段落（與市場）的

    紀錄沒有市場（欄位後加之前的舊紀錄，或 4 欄的舊形狀）時視為任一市場都算：
    舊紀錄補不出內容，拿它們告警只會製造假警報。
    """

    if str(row[0] or "") != phase:
        return False
    if market is None:
        return True
    recorded: Optional[str] = row[4] if len(row) > 4 else None
    return recorded is None or str(recorded) == market


def _row_strategies(row: Tuple[Any, ...]) -> FrozenSet[str]:
    """
    這筆紀錄載入的策略集合；欄位缺漏或格式不對時為空集合

    第六欄是 `strategy_params_json`（`{"strategies": [...]}`）；較早的紀錄沒有這欄或為 NULL，
    一律歸成同一組，維持「同段落取最後一筆」的舊語意。
    """

    raw: Any = row[5] if len(row) > 5 else None
    if not raw:
        return frozenset()
    try:
        return frozenset(
            str(name) for name in json.loads(str(raw)).get("strategies", [])
        )
    except (ValueError, AttributeError):
        return frozenset()


def _row_problem(
    label: str,
    row: Tuple[Any, ...],
    phase: str,
    now: datetime.datetime,
    run_alive: Optional[Callable[[int, str], Optional[bool]]] = None,
) -> Optional[str]:
    """
    - Description:
        一筆紀錄的問題：行程已不在卻沒有結束紀錄、停在未結束太久，或以非正常原因結束

        **沒有結束紀錄時先看行程還在不在**：只靠「開始多久」的話，盤中段落的上限是
        270 分鐘，09:06 開始、10 點就死掉的行程要到 13:36 才判得出來——中間那幾個小時
        的部位沒有人在看。行程判不出來（沒有 pid、查詢失敗）時退回時間上限。
    - Parameters:
        - label: str
            訊息裡的段落名稱
        - row: Tuple[Any, ...]
            `live_run` 紀錄；第七欄是 `pid`，可省略
        - phase: str
            段落名（決定時間上限）
        - now: datetime.datetime
            目前時刻
        - run_alive: Optional[Callable[[int, str], Optional[bool]]]
            `(pid, 開始時刻) → 是否還在`，判不出來回 None；None 時不檢查行程
    - Return:
        - Optional[str]
            問題描述；健康時為 None
    """

    started_at: Optional[str] = row[1]
    ended_at: Optional[str] = row[2]
    if not ended_at:
        pid: Optional[Any] = row[6] if len(row) > 6 else None
        if (
            pid is not None
            and run_alive is not None
            and run_alive(int(pid), str(started_at)) is False
        ):
            return (
                f"{label} 於 {started_at} 開始，行程（pid={pid}）已經不在卻沒有結束紀錄"
                "（被中止，或主機重開機）"
            )
        elapsed: float = _minutes_since(started_at, now)
        if elapsed > PHASE_STALE_MINUTES.get(phase, STALE_RUN_MINUTES):
            return (
                f"{label} 於 {started_at} 開始，已經 {elapsed:.0f} 分鐘沒有結束紀錄"
                "（跑到一半死掉，或卡在等待回報）"
            )
        return None
    if row[3] and str(row[3]) != LiveTradeDAO.END_REASON_NORMAL:
        return f"{label} 以非正常原因結束：{row[3]}"
    return None


def check_phases(
    rows: Sequence[Tuple[Any, ...]],
    expected: Dict[str, datetime.time],
    now: datetime.datetime,
    grace_minutes: int,
    run_alive: Optional[Callable[[int, str], Optional[bool]]] = None,
    pending_phases: FrozenSet[str] = frozenset(),
) -> List[PhaseStatus]:
    """
    - Description:
        比對段落表與實際紀錄

        **只檢查「應該已經開始」的段落**：還沒到時間的段落沒有紀錄是正常的，
        對它告警會讓人在每天早上收到三則假警報，然後學會忽略這個監控。

        **段落鍵帶市場時只認該市場的紀錄**：股票與期貨是兩個行程、段落名相同，
        只看段落名會讓股票尾盤的紀錄遮住「期貨尾盤沒跑」。

        **同一段落鍵有多筆紀錄時依策略集合分組、每組取最後一筆，任一組不健康就告警**：
        同市場同段落也可能是不同行程（parity 補比股票線就分兩個行程跑），只取最後一筆的話，
        先跑的那個死掉、後跑的正常結束，就回報通過。同一組策略重跑過時以最後一次為準。

        **每個異常帶一個去重鍵**：段落鍵＋開始時刻（同一次執行不論先被判成卡住、
        後來又被標成崩潰，都是同一件事），沒有紀錄的段落是段落鍵＋`missing`。
    - Parameters:
        - rows: Sequence[Tuple[Any, ...]]
            `live_run` 的當日紀錄
            `(phase, started_at, ended_at, end_reason, instrument_type, strategy_params_json, pid)`；
            後三欄可省略（舊形狀），視為不分市場、不分策略、不檢查行程
        - expected: Dict[str, datetime.time]
            應執行的段落表，鍵為 `段落` 或 `段落:市場`
        - now: datetime.datetime
            目前時刻
        - grace_minutes: int
            容忍延遲
        - run_alive: Optional[Callable[[int, str], Optional[bool]]]
            `(pid, 開始時刻) → 是否還在`；None 時不檢查行程（只看時間上限）
        - pending_phases: FrozenSet[str]
            上游還在跑的段落名：沒有紀錄時不算沒跑
    - Return:
        - List[PhaseStatus]
            逐段落的檢查結果
    """

    results: List[PhaseStatus] = []
    for key, expected_at in sorted(expected.items(), key=lambda item: item[1]):
        phase, market = split_phase_key(key)
        # 截止時刻沿用 `now` 的時區：`now_live()` 是 aware，不帶時區地組出來的
        # naive 時刻一比就拋 TypeError，watchdog 會在第一個段落到期時自己崩潰
        deadline: datetime.datetime = datetime.datetime.combine(
            now.date(), expected_at, tzinfo=now.tzinfo
        ) + datetime.timedelta(minutes=grace_minutes)
        if now < deadline:
            continue

        matched: List[Tuple[Any, ...]] = [
            candidate for candidate in rows if _row_matches(candidate, phase, market)
        ]

        if not matched:
            if phase in pending_phases:
                logger.info(f"{key} 還沒有紀錄，上游的資料更新仍在執行，先不判定")
                results.append(
                    PhaseStatus(phase, expected_at, None, None, None, market)
                )
                continue
            message: str = f"{key} 應於 {expected_at} 執行，但 live_run 沒有任何紀錄"
            results.append(
                PhaseStatus(
                    phase,
                    expected_at,
                    None,
                    None,
                    message,
                    market,
                    ((f"{key}|missing", message),),
                )
            )
            continue

        # 每組策略只留最後一筆（dict 覆寫保留先出現的位置，輸出順序穩定）
        latest: Dict[FrozenSet[str], Tuple[Any, ...]] = {}
        for candidate in matched:
            latest[_row_strategies(candidate)] = candidate

        alerts: List[Tuple[str, str]] = []
        for strategies, row in latest.items():
            label: str = (
                f"{key}（{'、'.join(sorted(strategies))}）" if strategies else key
            )
            row_problem: Optional[str] = _row_problem(label, row, phase, now, run_alive)
            if row_problem is not None:
                alerts.append((f"{label}|{row[1]}", row_problem))

        last: Tuple[Any, ...] = matched[-1]
        results.append(
            PhaseStatus(
                phase,
                expected_at,
                last[1],
                last[2],
                "；".join(message for _, message in alerts) if alerts else None,
                market,
                tuple(alerts),
            )
        )

    return results


def _minutes_since(started_at: Optional[str], now: datetime.datetime) -> float:
    """
    開始到現在經過幾分鐘；時戳解不開或只有日期時回 0（不誤報）

    只有日期的是舊紀錄：當成當天 00:00 的話，盤中常駐的段落一過截止時刻就會被誤報。
    """

    if not started_at or len(str(started_at)) <= len("YYYY-MM-DD"):
        return 0.0
    try:
        started: datetime.datetime = datetime.datetime.fromisoformat(str(started_at))
    except ValueError:
        return 0.0

    if started.tzinfo is not None and now.tzinfo is None:
        started = started.replace(tzinfo=None)
    elif started.tzinfo is None and now.tzinfo is not None:
        started = started.replace(tzinfo=now.tzinfo)
    return (now - started).total_seconds() / 60.0


def process_started_at(pid: int) -> Tuple[bool, Optional[datetime.datetime]]:
    """
    - Description:
        查詢行程的啟動時刻（`ps -o lstart=`）

        `ps` 以本行程的時區（排程帶 `TZ=Asia/Taipei`）印出當地時間，
        `astimezone()` 以同一個時區補上時區資訊，與 `live_run.started_at` 可直接相減。
    - Parameters:
        - pid: int
            行程編號
    - Return:
        - Tuple[bool, Optional[datetime.datetime]]
            `(查詢是否成功, 啟動時刻)`；查詢成功但行程不存在時啟動時刻為 None
    """

    try:
        completed: subprocess.CompletedProcess = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return False, None

    text: str = completed.stdout.strip()
    if completed.returncode != 0 and not text:
        # `ps -p` 找不到行程時結束碼 1、沒有輸出
        return True, None
    try:
        started: datetime.datetime = datetime.datetime.strptime(
            " ".join(text.split()), "%a %b %d %H:%M:%S %Y"
        )
    except ValueError:
        return False, None
    return True, started.astimezone()


def run_process_alive(pid: int, started_at: str) -> Optional[bool]:
    """
    - Description:
        段落紀錄的行程是否還在

        **只看 pid 存不存在不夠**：主機重開機後 pid 從頭編，2026-10-28 盤中段的 pid 7768
        在重開後不到兩小時就可能被別的行程拿到，`kill(pid, 0)` 會回「還活著」。
        所以要比對行程的啟動時刻：比段落開始還晚的，是重用同一個 pid 的另一個行程。
    - Parameters:
        - pid: int
            `live_run.pid`
        - started_at: str
            `live_run.started_at`
    - Return:
        - Optional[bool]
            還在為 True、不在為 False；判不出來為 None（呼叫端退回時間上限）
    """

    ok, process_start = process_started_at(pid)
    if not ok:
        return None
    if process_start is None:
        return False
    try:
        run_start: datetime.datetime = datetime.datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if run_start.tzinfo is None:
        run_start = run_start.astimezone()
    late: float = (process_start - run_start).total_seconds()
    return late <= PID_REUSE_TOLERANCE_SECONDS


def update_db_chain_running() -> bool:
    """
    - Description:
        資料更新與補比的排程串接是否還在跑

        判不出來時回 False：照常告警，寧可多一則也不要漏報。
    - Return:
        - bool
            還在跑為 True
    """

    try:
        completed: subprocess.CompletedProcess = subprocess.run(
            ["pgrep", "-f", UPDATE_DB_CHAIN_PATTERN],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def load_alerted(state_path: Path, today: datetime.date) -> Set[str]:
    """
    - Description:
        讀回今天已推播過的異常去重鍵

        檔案不存在、壞掉或是前一天的，一律當成今天還沒推過：寧可多推一次。
    - Parameters:
        - state_path: Path
            紀錄檔
        - today: datetime.date
            今天
    - Return:
        - Set[str]
            已推播過的去重鍵
    """

    try:
        state: Any = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(state, dict) or state.get("date") != today.isoformat():
        return set()
    return {str(key) for key in state.get("keys", [])}


def save_alerted(state_path: Path, today: datetime.date, keys: Set[str]) -> None:
    """
    - Description:
        寫回今天已推播過的去重鍵；寫不進去只記 log（下一輪會再推一次）
    - Parameters:
        - state_path: Path
            紀錄檔
        - today: datetime.date
            今天
        - keys: Set[str]
            已推播過的去重鍵
    """

    payload: str = json.dumps(
        {"date": today.isoformat(), "keys": sorted(keys)}, ensure_ascii=False
    )
    temp_path: Path = state_path.with_name(state_path.name + ".tmp")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(state_path)
    except OSError as exc:
        logger.warning(f"已推播紀錄寫不進 {state_path}（下一輪會重複推播）：{exc}")


def resolve_today(
    stock_db_path: str, today: datetime.date
) -> Tuple[Optional[bool], Optional[str]]:
    """
    - Description:
        判斷今天是否為交易日：週末來源＋官方開休市日曆，與實盤段落用同一個 `resolve_trading_day()`

        **不放行、也不在休市日告警**：沒有這一步時，週末與國定假日整天每 5 分鐘都會
        推一則「段落沒跑」的 CRITICAL，真正的告警會被淹沒。

        **判斷不出來時回 None，由呼叫端照交易日檢查並另外告警**（寧可誤報，不可漏報）：
        平日遇到官方日曆尚未入庫的年度、或研究庫開不了，就是這種情況。
        研究庫開不了時仍保留週末來源，週六日照樣判得出休市。

        不用券商合約檔來源：watchdog 刻意不連券商，不佔同帳號的連線與限流額度。
    - Parameters:
        - stock_db_path: str
            研究庫路徑（`market_holiday` 表所在）
        - today: datetime.date
            要判斷的日期
    - Return:
        - Tuple[Optional[bool], Optional[str]]
            `(是否為交易日, 判斷不出來的原因)`；判斷得出來時原因為 None
    """

    sources: List[TradingCalendarSource] = [WeekendCalendarSource()]
    conn: Optional[DBConnection] = None
    note: Optional[str] = None
    try:
        conn = connect_sqlite(stock_db_path, read_only=True)
        sources.insert(0, OfficialHolidayCalendarSource(MarketHolidayAPI(conn=conn)))
    except (DBError, OSError) as exc:
        note = f"研究庫開不了（{stock_db_path}）：{exc}"

    try:
        return resolve_trading_day(today, sources), None
    except (TradingCalendarUnavailableError, DBError) as exc:
        reason: str = str(exc) if note is None else f"{note}；{exc}"
        return None, reason
    finally:
        if conn is not None:
            conn.close()


def fetch_runs(db_path: str, today: datetime.date) -> List[Tuple[Any, ...]]:
    """
    - Description:
        唯讀讀取當日的啟動紀錄

        **只以唯讀開啟、不連券商**：實盤行程是唯一寫入者，而 watchdog 不該
        佔用同帳號的連線額度或任何一類限流。

        以 `substr(started_at, 1, 10)` 取日期而不是 SQLite 的 `date()`：
        後者會把帶時區的時間換算成 UTC，台北早上 08:00 以前開始的段落會被算到前一天。

        **`instrument_type` 欄不存在時讀成 NULL**：這欄由實盤行程啟動時補上，唯讀的
        watchdog 補不了。新版部署後到下一個段落啟動前，直接選這欄會讓每一輪都報
        「讀不到紀錄庫」——假警報之外，那段時間也完全沒在檢查段落。
    - Parameters:
        - db_path: str
            紀錄庫路徑
        - today: datetime.date
            交易日
    - Return:
        - List[Tuple[Any, ...]]
            `(phase, started_at, ended_at, end_reason, instrument_type, strategy_params_json, pid)`
    - Raise:
        - DBError
            開不了紀錄庫（由呼叫端轉成 CRITICAL 推播）
    """

    connection = connect_live_trading(db_path, read_only=True)
    try:
        columns: Set[str] = {
            row[1] for row in connection.execute("PRAGMA table_info(live_run)")
        }
        instrument_column: str = (
            "instrument_type" if "instrument_type" in columns else "NULL"
        )
        # 同理：策略集合欄缺的話讀成 NULL，同段落的紀錄退回不分策略
        strategies_column: str = (
            "strategy_params_json" if "strategy_params_json" in columns else "NULL"
        )
        # pid 欄缺的話讀成 NULL，退回只看時間上限
        pid_column: str = "pid" if "pid" in columns else "NULL"
        return connection.execute(
            "SELECT phase, started_at, ended_at, end_reason, "
            f"{instrument_column} AS instrument_type, "
            f"{strategies_column} AS strategy_params_json, "
            f"{pid_column} AS pid "
            "FROM live_run "
            "WHERE substr(started_at, 1, 10) = ? ORDER BY started_at, rowid",
            (today.isoformat(),),
        ).fetchall()
    finally:
        connection.close()


def main() -> int:
    """
    - Description:
        跑一輪檢查並推播；**任何例外都轉成推播，不靜靜退出**
    - Return:
        - int
            退出碼
    """

    args: argparse.Namespace = parse_arguments()
    notifier: BaseNotifier = build_notifier(
        LIVE_NOTIFY_CHANNEL, LIVE_NOTIFY_TOKEN, LIVE_NOTIFY_TARGET, blocking=True
    )
    now: datetime.datetime = now_live()

    try:
        expected: Dict[str, datetime.time] = parse_expected(args.expect)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNREACHABLE

    trading_day, calendar_problem = resolve_today(args.stock_db_path, now.date())
    if trading_day is False:
        logger.info(f"{now.date()} 不是交易日，不檢查段落")
        return EXIT_OK

    try:
        rows: List[Tuple[Any, ...]] = fetch_runs(args.db_path, now.date())
    except (DBError, OSError) as exc:
        # **開不了紀錄庫本身就是 CRITICAL**：WAL 的唯讀連線需要 `-shm`，
        # 交易行程已死時它可能不在——那剛好是最需要 watchdog 的時候
        message: str = (
            f"watchdog 讀不到實盤紀錄庫（{args.db_path}）：{exc}。"
            "無法判斷今天的段落有沒有跑"
        )
        logger.error(message)
        notifier.send(NotifyLevel.CRITICAL, "watchdog 讀不到紀錄庫", message)
        return EXIT_UNREACHABLE

    pending: FrozenSet[str] = (
        CHAINED_AFTER_UPDATE_DB if update_db_chain_running() else frozenset()
    )
    statuses: List[PhaseStatus] = check_phases(
        rows,
        expected,
        now,
        args.grace_minutes,
        run_alive=run_process_alive,
        pending_phases=pending,
    )
    alerts: List[Tuple[str, str]] = [
        alert for status in statuses for alert in status.alerts
    ]
    if calendar_problem is not None:
        alerts.insert(
            0,
            (
                f"calendar|{now.date()}",
                f"無法判定 {now.date()} 是否為交易日，已照交易日檢查：{calendar_problem}",
            ),
        )

    if not alerts:
        logger.info(f"watchdog 檢查通過（已檢查 {len(statuses)} 個段落）")
        return EXIT_OK

    # **同一個異常當天只推一次**：段落死掉後，到收線前每 5 分鐘都還是同一個狀態，
    # 每輪都推的話一個事件會變成幾十則 CRITICAL，推播很快就會被靜音
    state_path: Path = Path(args.state_path)
    alerted: Set[str] = load_alerted(state_path, now.date())
    fresh: List[Tuple[str, str]] = []
    for key, message in alerts:
        if key in alerted:
            logger.info(f"已推播過，不再重複：{message}")
        else:
            fresh.append((key, message))
    if not fresh:
        return EXIT_ALERTED

    body: str = "\n".join(message for _, message in fresh)
    logger.error(body)
    failed_before: int = notifier.failed
    notifier.send(NotifyLevel.CRITICAL, "實盤段落異常", body)
    if notifier.failed == failed_before:
        # 推播失敗時不記：下一輪再推一次
        save_alerted(state_path, now.date(), alerted | {key for key, _ in fresh})
    return EXIT_ALERTED


if __name__ == "__main__":
    sys.exit(main())
