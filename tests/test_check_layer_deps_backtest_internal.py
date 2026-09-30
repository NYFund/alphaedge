import importlib.util
from pathlib import Path
from types import ModuleType
from typing import List

import pytest

"""`scripts/check_layer_deps.py` 的「回測以外不得 import 回測內部零件」檢查"""

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


def test_outside_imports_of_backtest_internals_are_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    部位管理層、實盤、策略、入口 import 回測的 models／datafeed／report 都要被抓到

    分層等級擋不住：部位管理層與 `core.backtest.models` 同在第 4 層，只算同層邊。
    """

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(
            tmp_path,
            "core/managers/stock/position_manager.py",
            "from core.backtest.models.fill_model import TwStockFillModel\n",
        ),
        _write(
            tmp_path,
            "core/live/factory.py",
            "import core.backtest.datafeed.tw.stock_datafeed\n",
        ),
        _write(
            tmp_path,
            "apps/report_tool.py",
            "from core.backtest.report import reporter\n",
        ),
    ]

    hits: List[str] = checker.check_backtest_internal_imports(files)

    assert hits == [
        "core/managers/stock/position_manager.py:1: "
        "import core.backtest.models.fill_model",
        "core/live/factory.py:1: import core.backtest.datafeed.tw.stock_datafeed",
        "apps/report_tool.py:1: import core.backtest.report",
    ]


def test_backtest_itself_tests_and_assembly_imports_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    回測自己、測試可以 import 內部零件；別處 import 回測的組裝層與引擎本體是合法的

    實盤 parity 比對要真的跑一場回測，`core.backtest.factory`／`backtester` 必須放行。
    """

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", tmp_path)
    files: List[Path] = [
        _write(
            tmp_path,
            "core/backtest/factory.py",
            "from core.backtest.models.fill_model import TwStockFillModel\n",
        ),
        _write(
            tmp_path,
            "tests/backtest/test_fill.py",
            "from core.backtest.models.fill_model import TwStockFillModel\n",
        ),
        _write(
            tmp_path,
            "core/live/factory.py",
            "from core.backtest.factory import build_backtester\n"
            "from core.backtest.backtester import Backtester\n",
        ),
        _write(
            tmp_path,
            "core/models/fill_config.py",
            '"""放在 `core.backtest.models` 的話策略基底就得 import 回測套件"""\n',
        ),
    ]

    assert checker.check_backtest_internal_imports(files) == []
