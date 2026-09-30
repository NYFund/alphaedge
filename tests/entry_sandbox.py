import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

"""
以子行程跑入口時的環境沙箱

根目錄 `conftest.py` 對 `LiveTradeDAO.DEFAULT_DB_PATH` 的 patch 只在行程內有效，
子行程會從 `ALPHAEDGE_DATA_DIR` 重算，而資料根**刻意不整個導走**（`slow` 測試要讀
真的 `tw_stock.db`）。直接繼承 `os.environ` 等於把本機 `.env` 的真實金鑰與真實資料根
交給子行程——`conftest.py` 記載的那起事故就是這個形狀：一條測試以子行程跑實盤入口、
用本機金鑰登入模擬環境，累積出 19 筆 `REDUCE_ONLY`。

所有以子行程跑入口的測試都走這裡，隔離規則只維護一份。
"""

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]


def run_isolated(
    command: List[str], timeout: Optional[float] = 180
) -> subprocess.CompletedProcess:
    """
    - Description:
        在沙箱環境以目前的 Python 跑一個入口，回傳完整結果（退出碼、stdout、stderr）

        產物根指到暫存目錄、金鑰清空。金鑰清成**空字串而不是刪除**：
        `load_dotenv()` 不覆寫已存在的鍵，刪掉反而會被 `.env` 補回來。
    - Parameters:
        - command: List[str]
            `python` 之後的參數，例如 `["-m", "apps.backtest", "--help"]`
        - timeout: Optional[float]
            逾時秒數
    - Return:
        - subprocess.CompletedProcess
            執行結果
    """

    sandbox: Path = Path(tempfile.mkdtemp(prefix="alphaedge-entry-"))
    (sandbox / "data" / "db").mkdir(parents=True)
    env: Dict[str, str] = {
        **os.environ,
        "ALPHAEDGE_DATA_DIR": str(sandbox / "data"),
        "ALPHAEDGE_RESULTS_DIR": str(sandbox / "results"),
        "ALPHAEDGE_LOGS_DIR": str(sandbox / "logs"),
        "ALPHAEDGE_LIVE_KILL_SWITCH_PATH": str(sandbox / "KILL_SWITCH"),
        "API_KEY": "",
        "API_SECRET_KEY": "",
    }
    try:
        return subprocess.run(
            [sys.executable, *command],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=timeout,
            env=env,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
