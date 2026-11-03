import datetime
import os
import subprocess
from typing import Callable, Optional, Tuple

"""
行程存活判定：pid 存不存在，加上行程啟動時刻的比對

- Features:
    1. `process_alive()`：`os.kill(pid, 0)` 的存在性檢查
    2. `process_started_at()`：以 `ps -o lstart=` 查行程的啟動時刻
    3. `run_process_alive()`：紀錄裡的行程是否還在——pid 存在、而且啟動時刻不晚於紀錄的開始時刻
- 使用場景:
    段落啟動時判定上一段是否崩潰（`LiveTradeDAO.mark_crashed_runs()`）與存活監控
    （`scripts/live_watchdog.py`）共用同一套。**只看 pid 存不存在不夠**：主機重開機後
    pid 從頭編，2026-10-28 盤中段的 pid 7768 在重開後不到兩小時就可能被別的行程拿到，
    `kill(pid, 0)` 會回「還活著」，已死的段落就不會被標成崩潰、也不以券商重建部位。
    兩邊各寫一份的話，一邊認得 pid 重用、一邊不認得，而分岔不會報錯。
"""

# 行程啟動時刻比紀錄的開始時刻晚這麼多，就是 pid 被別的行程重用了。
# 實盤行程先啟動、做完初始化才寫紀錄，正常情況行程只會比紀錄早
PID_REUSE_TOLERANCE_SECONDS: int = 60

# `pid → (查詢是否成功, 啟動時刻)`；測試替換用
StartedAtQuery = Callable[[int], Tuple[bool, Optional[datetime.datetime]]]


def process_alive(pid: int) -> bool:
    """
    這個 pid 是否還在執行

    `os.kill(pid, 0)` 不送訊號、只做存在性與權限檢查。`PermissionError`
    代表行程存在但屬於別的使用者——那仍然是「活著」。
    """

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # 判不出來時當成「還活著」：誤放過一筆崩潰紀錄，下一次啟動還會再檢查一次；
        # 誤標一個還在送單的段落則會當場推 CRITICAL 並觸發一次不必要的重建
        return True
    return True


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


def run_process_alive(
    pid: int, started_at: str, started_at_of: Optional[StartedAtQuery] = None
) -> Optional[bool]:
    """
    - Description:
        紀錄裡的行程是否還在：pid 存在，而且啟動時刻不比紀錄的開始時刻晚

        比紀錄開始還晚啟動的，是重用同一個 pid 的另一個行程。
    - Parameters:
        - pid: int
            紀錄裡的行程編號（`live_run.pid`）
        - started_at: str
            紀錄的開始時刻（`live_run.started_at`，ISO 格式）
        - started_at_of: Optional[StartedAtQuery]
            查啟動時刻的方式；None 時以 `ps` 查詢
    - Return:
        - Optional[bool]
            還在為 True、不在為 False；判不出來為 None（呼叫端自行退回別的判定）
    """

    query: StartedAtQuery = started_at_of or process_started_at
    ok, process_start = query(pid)
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
