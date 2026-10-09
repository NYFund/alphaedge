import importlib.util
from pathlib import Path
from types import ModuleType
from typing import List

import pytest

"""`scripts/check_layer_deps.py` 的資料庫驅動 import 檢查"""

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


def test_timescale_drivers_outside_dao_are_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    tick 的 TimescaleDB 驅動和 sqlite3 一樣只能出現在 `core/dao/`

    不擋的話 loader 或 API 會順手自己開 psycopg 連線、自己寫 SQL，
    「SQL 只在 DAO」又回到只靠慣例。
    """

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(
            tmp_path,
            "core/pipeline/tw/loaders/stock_tick_loader.py",
            "import psycopg\n",
        ),
        _write(
            tmp_path,
            "core/api/tw/stock_tick_api.py",
            "import connectorx as cx\n",
        ),
        _write(tmp_path, "apps/update_db.py", "from psycopg import sql\n"),
    ]

    hits: List[str] = checker.check_db_driver_imports(files)

    assert hits == [
        "core/pipeline/tw/loaders/stock_tick_loader.py:1: import psycopg",
        "core/api/tw/stock_tick_api.py:1: import connectorx",
        "apps/update_db.py:1: import psycopg",
    ]


def test_timescale_drivers_inside_dao_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """連線入口與 tick DAO 本來就要 import 驅動"""

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(tmp_path, "core/dao/timescale.py", "import psycopg\n"),
        _write(tmp_path, "core/dao/tw/stock_tick_dao.py", "import connectorx\n"),
    ]

    assert checker.check_db_driver_imports(files) == []
