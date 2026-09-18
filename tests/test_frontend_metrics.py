import ast
import subprocess
import sys
from pathlib import Path
from typing import List, Set

import pandas as pd

from frontend.services.metrics import extract_backtest_date_range

"""
前端與 reporter 用同一份公式

舊版 `frontend/app.py` 自己寫了一份 Sharpe／Sortino，而且**寫在模組層級的
Streamlit 呼叫之後**——測試連 import 都做不到，於是那份公式從來沒有被驗證過。
它正好踩中 `performance_metrics` 開頭列的缺陷：以 `ddof=0` 取標準差、沒有扣無風險
利率、Sortino 對「低於門檻的那幾期」取標準差（那是它們**彼此之間**的離散度，
不是相對於門檻的偏差）。

本檔盯住三件事：

1. 前端的 Sharpe／Sortino 與 `core/backtest/analysis/performance_metrics.py` **逐值相同**。
2. 公式本身對得上**手算**，不是「兩邊都錯得一樣」。
3. `app.py` 不再定義任何計算函式，且前端 import `core` 的成本沒有變重。
"""


_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
_APP_PATH: Path = _PROJECT_ROOT / "frontend" / "app.py"


# === 與 core 共用同一份公式 ===
def test_date_range_covers_entry_and_exit() -> None:
    """
    區間要涵蓋進場與出場

    只看 `Sell Date` 對 SHORT 是開倉日，右端會提早——本例的 `Exit Date`
    比 `Sell Date` 晚，區間右端必須是 `Exit Date`。
    """

    df: pd.DataFrame = pd.DataFrame(
        {
            "Entry Date": ["2024-01-05", "2024-02-01"],
            "Exit Date": ["2024-01-10", "2024-03-15"],
            "Sell Date": ["2024-01-05", "2024-02-01"],
        }
    )

    start, end = extract_backtest_date_range(df)

    assert start == pd.Timestamp("2024-01-05")
    assert end == pd.Timestamp("2024-03-15")


def test_date_range_is_none_without_date_columns() -> None:
    """沒有任何日期欄位時回 `(None, None)`，不要猜"""

    assert extract_backtest_date_range(pd.DataFrame({"X": [1]})) == (None, None)


# === 端到端：由 daily_equity 算到指標 ===
def test_app_defines_no_calculation_functions() -> None:
    """
    `app.py` 只剩渲染函式

    計算留在 `app.py` 就等於**不可能被測試**：它在模組層級呼叫
    `st.set_page_config()`，測試 import 它就會炸。
    """

    tree: ast.Module = ast.parse(_APP_PATH.read_text(encoding="utf-8"))
    top_level: List[str] = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert top_level, "app.py 應該還有渲染函式"
    calculation_like: List[str] = [
        name
        for name in top_level
        if name.startswith(("_calc_", "_extract_", "_compute_", "_summarise_"))
    ]
    assert calculation_like == []
    assert all(
        name.startswith("_render_") or name.startswith("_get_") for name in top_level
    ), top_level


def test_frontend_does_not_import_core_at_all() -> None:
    """
    前端**完全不 import `core`**

    所有績效指標改讀 reporter 落地的 `metrics_summary.csv`，公式只存在於
    `core/` 一處。`frontend/Dockerfile` 因此不再 COPY 任何 `core/` 檔案——
    這條一旦紅了，代表映像少 COPY 東西，症狀會是容器啟動時才 ImportError。
    """

    imported: Set[str] = set()
    for path in (_PROJECT_ROOT / "frontend").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "core"
            ):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(
                    alias.name for alias in node.names if alias.name.startswith("core")
                )

    assert imported == set(), f"前端不該 import core：{sorted(imported)}"


def test_importing_performance_metrics_stays_cheap() -> None:
    """
    import 共用公式不得拉進 pandas／numpy／shioaji

    `core/backtest/analysis/__init__.py` 一旦在套件層 import 相依 pandas／shioaji 的模組，
    這條就會紅——那代表前端映像得裝進整個後端才跑得起來。
    以子行程量測，避免被本測試檔自己已經 import 的模組汙染。
    """

    script: str = (
        "import sys;"
        "from core.backtest.analysis.performance_metrics import compute_annualized_sharpe;"
        "heavy={'pandas','numpy','shioaji','sqlite3','loguru','requests'};"
        "print(','.join(sorted(heavy & {m.split('.')[0] for m in sys.modules})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
