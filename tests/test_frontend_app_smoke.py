import shutil
import sys
from pathlib import Path

import pytest

"""
前端冒煙測試：用 Streamlit 的 `AppTest` 真的把 `frontend/app.py` 跑一遍

`frontend/app.py` 本身沒有其他測試：Streamlit 的呼叫簽章錯了（例如參數被移除、
名字打錯），只有實際開頁才會炸。以測試用的回測報表跑一次，
任何未處理的例外都會出現在 `app.exception` 裡。

需要 `--extra frontend`（streamlit）；沒裝時略過並在 `-rs` 列出原因，
CI 以 `--extra frontend` 安裝，所以在 CI 一定會跑。
"""

streamlit_testing = pytest.importorskip(
    "streamlit.testing.v1", reason="需要 `uv sync --extra frontend`"
)

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
_APP_PATH: Path = _PROJECT_ROOT / "frontend" / "app.py"
_FIXTURE_DIR: Path = Path(__file__).resolve().parent / "fixtures" / "frontend_report"


@pytest.fixture
def results_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一個只含測試報表的結果根目錄，並讓前端重新讀取設定"""

    root: Path = tmp_path / "results"
    shutil.copytree(_FIXTURE_DIR, root / "Foreign-Sell-Short-Day-Trade")
    monkeypatch.setenv("ALPHAEDGE_RESULTS_DIR", str(root))
    # 設定在 import 時就讀環境變數；清掉快取才會以上面的路徑重新載入
    for name in ("config", "frontend.config"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return root


def test_app_renders_a_report_without_exceptions(results_root: Path) -> None:
    """有報表時整頁跑完、沒有任何例外，也沒有走到「找不到報表」的錯誤訊息"""

    app = streamlit_testing.AppTest.from_file(str(_APP_PATH), default_timeout=60)
    app.run()

    assert not app.exception
    assert [error.value for error in app.error] == []


def test_app_reports_missing_results_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """結果目錄是空的：顯示錯誤訊息並停下，而不是拋例外"""

    monkeypatch.setenv("ALPHAEDGE_RESULTS_DIR", str(tmp_path / "empty"))
    for name in ("config", "frontend.config"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    app = streamlit_testing.AppTest.from_file(str(_APP_PATH), default_timeout=60)
    app.run()

    assert not app.exception
    assert any("找不到任何回測結果資料夾" in error.value for error in app.error)
