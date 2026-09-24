import argparse
import datetime
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.config import TW_TRADING_DB_PATH
from core.config.settings import (
    LIVE_NOTIFY_CHANNEL,
    LIVE_NOTIFY_TARGET,
    LIVE_NOTIFY_TOKEN,
    now_live,
)
from core.dao.connection import DBError, connect_live_trading
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.notify.base import BaseNotifier, NotifyLevel
from core.live.notify.factory import build_notifier

"""
存活監控：實盤最常見的失效是**該跑的沒跑、沒人發現**

`live_risk_event` 與 log 都是 pull 型：人要先去看才知道。程式在 09:05 因為登入
失敗退出，到收盤都不會有人知道。

**本檔必須是獨立行程，不可以放進 `core/live/`。** 和 LiveTrader 同一個行程的心跳
會跟著行程一起死——那等於沒有監控。

它只做三件事，而且**只以唯讀開啟紀錄庫、不連券商**：
1. 比對「當日應執行的段落表」與 `live_run` 的實際紀錄。
2. 找出「該跑而沒有紀錄」與「有開始紀錄但停在未正常結束」。
3. 推播。

**任何例外都轉成推播，不靜靜退出**——連紀錄庫都開不了也一樣：
**監控自己死掉而沒人知道，和沒有監控是同一件事。**

用法（一律在專案根目錄以 `-m` 執行）：

    .venv/bin/python -m scripts.live_watchdog
    .venv/bin/python -m scripts.live_watchdog --expect open=08:30 close=13:20
"""

# 預設的段落表：段落名 → 應該開始執行的時刻。
#
# 與部署文件的排程對齊；**容忍延遲**是因為排程器本身就有秒級誤差，
# 而且段落要先做完對帳才會寫紀錄
DEFAULT_EXPECTED_PHASES: Dict[str, datetime.time] = {
    "open": datetime.time(8, 30),
    "close": datetime.time(13, 20),
    "after_close": datetime.time(14, 30),
}

# 超過應執行時刻這麼久還沒有紀錄，就判定「沒跑」
GRACE_MINUTES: int = 15

# 有開始紀錄但超過這麼久還沒有結束時間，就判定「跑到一半死掉」
STALE_RUN_MINUTES: int = 90

# 退出碼：0 正常、1 有異常（已推播）、2 連紀錄庫都開不了
EXIT_OK: int = 0
EXIT_ALERTED: int = 1
EXIT_UNREACHABLE: int = 2


@dataclass(frozen=True)
class PhaseStatus:
    """單一段落的檢查結果"""

    phase: str
    expected_at: datetime.time
    started_at: Optional[str]
    ended_at: Optional[str]
    problem: Optional[str]

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
        metavar="段落=HH:MM",
        help="應執行的段落與時刻；未指定時用預設表",
    )
    parser.add_argument(
        "--db-path", default=str(TW_TRADING_DB_PATH), help="實盤紀錄庫路徑"
    )
    parser.add_argument(
        "--grace-minutes",
        type=int,
        default=GRACE_MINUTES,
        help="超過應執行時刻多久仍無紀錄就判定沒跑",
    )
    return parser.parse_args()


def parse_expected(values: Optional[Sequence[str]]) -> Dict[str, datetime.time]:
    """
    - Description:
        解析 `段落=HH:MM` 格式的段落表
    - Parameters:
        - values: Optional[Sequence[str]]
            命令列傳入的段落表；None 或空時回預設表
    - Return:
        - Dict[str, datetime.time]
            段落 → 應執行時刻
    - Raise:
        - ValueError
            格式錯誤
    """

    if not values:
        return dict(DEFAULT_EXPECTED_PHASES)

    expected: Dict[str, datetime.time] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"段落表格式應為 `段落=HH:MM`，收到 {value!r}")
        phase, raw_time = value.split("=", 1)
        hour, minute = raw_time.split(":")
        expected[phase.strip()] = datetime.time(int(hour), int(minute))
    return expected


def check_phases(
    rows: Sequence[Tuple[Any, ...]],
    expected: Dict[str, datetime.time],
    now: datetime.datetime,
    grace_minutes: int,
) -> List[PhaseStatus]:
    """
    - Description:
        比對段落表與實際紀錄

        **只檢查「應該已經開始」的段落**：還沒到時間的段落沒有紀錄是正常的，
        對它告警會讓人在每天早上收到三則假警報，然後學會忽略這個監控。
    - Parameters:
        - rows: Sequence[Tuple[Any, ...]]
            `live_run` 的當日紀錄 `(phase, started_at, ended_at, end_reason)`
        - expected: Dict[str, datetime.time]
            應執行的段落表
        - now: datetime.datetime
            目前時刻
        - grace_minutes: int
            容忍延遲
    - Return:
        - List[PhaseStatus]
            逐段落的檢查結果
    """

    by_phase: Dict[str, Tuple[Any, ...]] = {}
    for row in rows:
        phase: str = str(row[0] or "")
        # 同一段落若有多筆，取最後一筆——重跑過的那次才是最終狀態
        by_phase[phase] = row

    results: List[PhaseStatus] = []
    for phase, expected_at in sorted(expected.items(), key=lambda item: item[1]):
        # 截止時刻沿用 `now` 的時區：`now_live()` 是 aware，不帶時區地組出來的
        # naive 時刻一比就拋 TypeError，watchdog 會在第一個段落到期時自己崩潰
        deadline: datetime.datetime = datetime.datetime.combine(
            now.date(), expected_at, tzinfo=now.tzinfo
        ) + datetime.timedelta(minutes=grace_minutes)
        if now < deadline:
            continue

        row = by_phase.get(phase)
        if row is None:
            results.append(
                PhaseStatus(
                    phase,
                    expected_at,
                    None,
                    None,
                    f"{phase} 應於 {expected_at} 執行，但 live_run 沒有任何紀錄",
                )
            )
            continue

        started_at: Optional[str] = row[1]
        ended_at: Optional[str] = row[2]
        problem: Optional[str] = None

        if not ended_at:
            elapsed: float = _minutes_since(started_at, now)
            if elapsed > STALE_RUN_MINUTES:
                problem = (
                    f"{phase} 於 {started_at} 開始，已經 {elapsed:.0f} 分鐘沒有結束紀錄"
                    "（跑到一半死掉，或卡在等待回報）"
                )
        elif row[3] and str(row[3]) != LiveTradeDAO.END_REASON_NORMAL:
            problem = f"{phase} 以非正常原因結束：{row[3]}"

        results.append(PhaseStatus(phase, expected_at, started_at, ended_at, problem))

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


def fetch_runs(db_path: str, today: datetime.date) -> List[Tuple[Any, ...]]:
    """
    - Description:
        唯讀讀取當日的啟動紀錄

        **只以唯讀開啟、不連券商**：實盤行程是唯一寫入者，而 watchdog 不該
        佔用同帳號的連線額度或任何一類限流。

        以 `substr(started_at, 1, 10)` 取日期而不是 SQLite 的 `date()`：
        後者會把帶時區的時間換算成 UTC，台北早上 08:00 以前開始的段落會被算到前一天。
    - Parameters:
        - db_path: str
            紀錄庫路徑
        - today: datetime.date
            交易日
    - Return:
        - List[Tuple[Any, ...]]
            `(phase, started_at, ended_at, end_reason)`
    - Raise:
        - DBError
            開不了紀錄庫（由呼叫端轉成 CRITICAL 推播）
    """

    connection = connect_live_trading(db_path, read_only=True)
    try:
        return connection.execute(
            "SELECT phase, started_at, ended_at, end_reason FROM live_run "
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

    statuses: List[PhaseStatus] = check_phases(rows, expected, now, args.grace_minutes)
    problems: List[str] = [
        status.problem for status in statuses if status.problem is not None
    ]

    if not problems:
        logger.info(f"watchdog 檢查通過（已檢查 {len(statuses)} 個段落）")
        return EXIT_OK

    body: str = "\n".join(problems)
    logger.error(body)
    notifier.send(NotifyLevel.CRITICAL, "實盤段落異常", body)
    return EXIT_ALERTED


if __name__ == "__main__":
    sys.exit(main())
