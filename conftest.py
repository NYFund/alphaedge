import importlib.util
import os
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Tuple

import pytest

"""
測試期間的產物隔離：測試不得寫進正式的實盤紀錄庫、`results/` 與 `logs/`

放在**專案根目錄的 `conftest.py`**，是因為 pytest 保證它在 `tests/conftest.py` 之前載入，
也就是**在任何 `core` 模組被 import 之前**：`core/config/paths.py` 在 import 時就把環境變數
算成路徑常數，許多模組再以 `from core.config import X` 各自複製一份，等 fixture 執行時
早已來不及改。不做成 `-p tests.xxx` plugin：`kaleido` 1.2.0 把自己的 `tests/` 裝成頂層套件，
在 site-packages 裡搶先被找到。

隔離三件事：
1. `results/`、`logs/`、kill switch 導到本次 session 的暫存目錄（環境變數覆寫）。
2. 實盤紀錄庫的預設路徑（`LiveTradeDAO.DEFAULT_DB_PATH`）改指暫存目錄。它是從資料根
   推出來的，而資料根**不能**整個導走——`slow` 測試要讀真的 `tw_stock.db`。
3. 絆線：session 開始時記下正式實盤紀錄庫與 `results/` 的狀態，結束時比對，
   有變動就讓整個 pytest 失敗並列出檔案。前兩項漏網時，這裡會當場抓到，
   而不是等到實盤讀到測試留下的 `REDUCE_ONLY`。

曾經發生過的事故：一條測試以子行程跑 `run.py --mode live`，用本機 `.env` 的金鑰
登入模擬環境，每跑一次完整測試就在正式紀錄庫留下一筆 `live_run`，累積出 19 筆
`REDUCE_ONLY`——實盤啟動時會讀回上一次的交易模式。
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent

# 產物根的環境變數；`pristine_config_paths` 會把它們全部清成「未設定」
_ARTIFACT_ENV_KEYS: Tuple[str, ...] = (
    "ALPHAEDGE_DATA_DIR",
    "ALPHAEDGE_RESULTS_DIR",
    "ALPHAEDGE_LOGS_DIR",
    "ALPHAEDGE_LIVE_KILL_SWITCH_PATH",
)


def _configured_root(env_key: str, default: Path) -> Path:
    """覆寫之前，使用者環境實際指向的產物根（可能刻意設在別處）"""

    value: str = os.getenv(env_key, "")
    return Path(value).resolve() if value else default


# 正式位置：一律在覆寫環境變數之前算好
_REAL_TRADING_DB: Path = (
    _configured_root("ALPHAEDGE_DATA_DIR", _PROJECT_ROOT / "data")
    / "db"
    / "tw_trading.db"
)
_REAL_RESULTS_DIR: Path = _configured_root(
    "ALPHAEDGE_RESULTS_DIR", _PROJECT_ROOT / "results"
)

SESSION_ROOT: Path = Path(tempfile.mkdtemp(prefix="alphaedge-tests-"))
SESSION_TRADING_DB: Path = SESSION_ROOT / "db" / "tw_trading.db"

os.environ["ALPHAEDGE_RESULTS_DIR"] = str(SESSION_ROOT / "results")
os.environ["ALPHAEDGE_LOGS_DIR"] = str(SESSION_ROOT / "logs")
os.environ["ALPHAEDGE_LIVE_KILL_SWITCH_PATH"] = str(
    SESSION_ROOT / "live" / "KILL_SWITCH"
)


def _snapshot() -> Dict[str, Tuple[int, int]]:
    """
    正式位置目前的檔案狀態（路徑 → (mtime_ns, size)）

    實盤紀錄庫只看主檔與 `-wal`：唯讀開啟也會更新 `-shm` 的時間，那不是寫入。
    """

    files: List[Path] = [
        path
        for path in (_REAL_TRADING_DB, Path(f"{_REAL_TRADING_DB}-wal"))
        if path.is_file()
    ]
    if _REAL_RESULTS_DIR.is_dir():
        files.extend(path for path in _REAL_RESULTS_DIR.rglob("*") if path.is_file())

    return {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in files}


_BEFORE: Dict[str, Tuple[int, int]] = {}

# 替換前的 `LiveTradeDAO.DEFAULT_DB_PATH`；驗「正式環境用哪個庫」的測試要讀原值
_PRODUCTION_LIVE_DB_PATH: List[Path] = []


def pytest_configure(config: pytest.Config) -> None:
    """改寫實盤紀錄庫的預設路徑，並記下正式位置的初始狀態"""

    from core.dao.tw.live_trade_dao import LiveTradeDAO

    SESSION_TRADING_DB.parent.mkdir(parents=True, exist_ok=True)
    _PRODUCTION_LIVE_DB_PATH[:] = [LiveTradeDAO.DEFAULT_DB_PATH]
    LiveTradeDAO.DEFAULT_DB_PATH = SESSION_TRADING_DB

    _BEFORE.clear()
    _BEFORE.update(_snapshot())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """正式位置有任何新增、刪除或修改就讓整個 pytest 失敗"""

    after: Dict[str, Tuple[int, int]] = _snapshot()
    changed: List[str] = sorted(
        path
        for path in set(_BEFORE) | set(after)
        if _BEFORE.get(path) != after.get(path)
    )
    shutil.rmtree(SESSION_ROOT, ignore_errors=True)

    if not changed:
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    lines: List[str] = [
        "測試寫到了正式位置（實盤紀錄庫或 results/）；請找出沒有隔離的測試：",
        *(f"  {path}" for path in changed),
        "若同一時間有真正的實盤或回測在跑，這裡也會誤報，請確認後重跑。",
    ]
    for line in lines:
        if reporter is not None:
            reporter.write_line(line, red=True)
        else:
            print(line)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def production_live_db_path() -> Path:
    """正式環境下 `LiveTradeDAO` 的預設紀錄庫路徑（測試期間已被換成暫存路徑）"""

    return _PRODUCTION_LIVE_DB_PATH[0]


@pytest.fixture
def pristine_config_paths(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    - Description:
        以「沒有任何產物根覆寫」的環境，重新載入一份獨立的 `core/config/paths.py`

        給驗證預設路徑的測試用：本 plugin 已把 `results/`、`logs/` 導到暫存目錄，
        直接讀 `core.config` 的常數量到的是覆寫後的值。**不 reload 原模組**——
        其他模組早已複製了它的常數，reload 只會讓兩邊不一致。
        環境變數設成空字串而不是刪除：`get_env_path()` 把空字串當成未設定，
        而 `load_dotenv()` 不會覆寫已存在的鍵，`.env` 就補不回來。
    - Parameters:
        - monkeypatch: pytest.MonkeyPatch
            用來暫時清空產物根的環境變數（測試結束自動還原）
    - Return:
        - ModuleType
            全新載入、未受覆寫影響的 paths 模組
    """

    for key in _ARTIFACT_ENV_KEYS:
        monkeypatch.setenv(key, "")

    spec = importlib.util.spec_from_file_location(
        "_pristine_core_config_paths", _PROJECT_ROOT / "core" / "config" / "paths.py"
    )
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
