import os
import warnings
from pathlib import Path

"""
前端的路徑與檔名設定

**結果根目錄的預設值必須與後端一致**：`core/config/paths.py` 的
`RESULTS_DIR_PATH` 早在 2026-08「執行期產物移出 `core/`」時就改成
`PROJECT_ROOT / "results"`，前端卻還指著已經不存在的 `core/backtest/results`，
於是本機直接 `streamlit run frontend/app.py` 整頁都是「找不到任何回測結果資料夾」。

**這裡刻意不 `from core.config import RESULTS_DIR_PATH`**：前端映像只 COPY
`frontend/`（見 `frontend/Dockerfile`），import `core` 會讓映像非帶整個後端不可。
兩邊各自算出同一個路徑，由 `tests/test_frontend_config.py` 盯住不會漂開。
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "results"

# 與後端 `core/config/paths.py` 同名，後端寫哪、前端就讀哪，不必各設一次
RESULTS_ENV_VAR = "ALPHAEDGE_RESULTS_DIR"

# 舊名保留一版相容。**只在新名沒設時才生效**，並且會發出 DeprecationWarning
LEGACY_RESULTS_ENV_VAR = "ALPHAEDGE_BACKTEST_RESULTS"


def resolve_results_root(env: dict[str, str] | None = None) -> Path:
    """
    - Description:
        決定結果根目錄：新環境變數 → 舊環境變數（警告）→ 預設值
    - Parameters:
        - env: Optional[dict]
            環境變數來源；預設讀 `os.environ`（測試可傳入替身）
    - Return:
        - Path
            結果根目錄
    """

    source = os.environ if env is None else env

    configured = source.get(RESULTS_ENV_VAR)
    if configured:
        return Path(configured).expanduser()

    legacy = source.get(LEGACY_RESULTS_ENV_VAR)
    if legacy:
        warnings.warn(
            f"環境變數 `{LEGACY_RESULTS_ENV_VAR}` 已更名為 `{RESULTS_ENV_VAR}`"
            "（與後端 `core/config` 統一），本版仍相容但下一版將移除。",
            DeprecationWarning,
            stacklevel=2,
        )
        return Path(legacy).expanduser()

    return DEFAULT_RESULTS_ROOT


RESULTS_ROOT = resolve_results_root()

# 允許舊檔名與新版輸出並存，避免前端因命名差異讀不到
CHART_FILE_CANDIDATES = {
    "資產曲線": ["*balance_curve.png", "balance_curve.png"],
    "資產與基準比較": [
        "*balance_and_benchmark_curve.png",
        "*networth.png",
        "balance_and_benchmark_curve.png",
        "networth.png",
    ],
    "最大回撤": ["*balance_mdd.png", "*mdd.png", "balance_mdd.png", "mdd.png"],
    "每日損益": ["*everyday_profit.png", "everyday_profit.png"],
    # 盯市口徑的每日權益變化（reporter 早就在畫，前端一直沒讀）
    "每日權益變化": [
        "*everyday_equity_change.png",
        "everyday_equity_change.png",
    ],
}

CSV_FILE_CANDIDATES = ["*trading_report.csv", "trading_report.csv"]

# reporter 另外落地的三份 CSV。前端**只讀不算**，指標一律以這些為準：
# - daily_equity：逐日盯市權益，資產曲線／每日損益／MDD 的唯一來源
# - direction_summary：多空分開的績效統計，總覽的勝率／損益／平均 ROI 取自此
# - event_report：強制回補、拒單等尾部事件計數
DAILY_EQUITY_FILE_CANDIDATES = ["*daily_equity.csv", "daily_equity.csv"]
DIRECTION_SUMMARY_FILE_CANDIDATES = [
    "*direction_summary.csv",
    "direction_summary.csv",
]
EVENT_REPORT_FILE_CANDIDATES = ["*event_report.csv", "event_report.csv"]
METRICS_SUMMARY_FILE_CANDIDATES = [
    "*metrics_summary.csv",
    "metrics_summary.csv",
]
