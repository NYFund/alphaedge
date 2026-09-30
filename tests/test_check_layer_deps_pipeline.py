import importlib.util
from pathlib import Path
from types import ModuleType
from typing import List

import pytest

"""`scripts/check_layer_deps.py` 的框架→資料管線 import 檢查"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_checker() -> ModuleType:
    """以檔案路徑載入檢查腳本（`scripts/` 不是套件）"""

    spec = importlib.util.spec_from_file_location(
        "check_layer_deps", _PROJECT_ROOT / "scripts" / "check_layer_deps.py"
    )
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str, source: str) -> Path:
    """在假專案根目錄下寫一個檔案"""

    path: Path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_framework_import_of_pipeline_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `core/` 內、`core/pipeline/` 以外的檔案 import 資料管線要被抓到

    分層等級擋不住這條：`core.pipeline` 與 `core.api` 同級，引擎層往下 import
    它屬於合法的向下相依，只有這道專屬檢查看得到。
    """

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(
            tmp_path,
            "core/backtest/feed.py",
            "from core.pipeline.shared.date_planner import DatePlanner\n",
        ),
        _write(tmp_path, "core/live/runner.py", "import core.pipeline\n"),
    ]

    hits: List[str] = checker.check_framework_pipeline_imports(files)

    assert hits == [
        "core/backtest/feed.py:1: import core.pipeline.shared.date_planner",
        "core/live/runner.py:1: import core.pipeline",
    ]


def test_pipeline_itself_and_entry_points_are_not_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`core/pipeline/` 自身、`tasks/` 可以 import 它；說明文字裡的字樣不算"""

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(
            tmp_path,
            "core/pipeline/tw/updater.py",
            "from core.pipeline.shared.date_planner import DatePlanner\n",
        ),
        _write(
            tmp_path,
            "tasks/update_db.py",
            "from core.pipeline.tw.updaters import x\n",
        ),
        _write(
            tmp_path,
            "core/dao/__init__.py",
            '"""位於 `core.api`／`core.pipeline` 之下"""\n',
        ),
        _write(tmp_path, "core/pipelines_lookalike.py", "import core.pipelines\n"),
    ]

    assert checker.check_framework_pipeline_imports(files) == []
