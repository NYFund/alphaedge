import importlib.util
from pathlib import Path
from types import ModuleType
from typing import List

"""`scripts/check_layer_deps.py` 的「`core/strategies/` 只放契約」檢查"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_checker() -> ModuleType:
    """以檔案路徑載入檢查腳本（`scripts/` 不是套件）"""

    spec = importlib.util.spec_from_file_location(
        "check_layer_deps", _PROJECT_ROOT / "scripts" / "check_layer_deps.py"
    )
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str) -> None:
    """在假專案根目錄下寫一個空檔"""

    path: Path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_concrete_strategy_inside_core_is_flagged(tmp_path: Path) -> None:
    """
    具體策略放回 `core/strategies/` 要被抓到

    分層等級擋不住：它 import 的契約、部位建構都與 `core.strategies` 同層或更低。
    放回去之後策略載入器也掃不到它，`--strategy` 清單裡會憑空少一支。
    """

    checker: ModuleType = _load_checker()
    for rel in checker._STRATEGY_CONTRACT_FILES:
        _write(tmp_path, rel)
    _write(tmp_path, "core/strategies/stock/my_breakout_strategy.py")

    problems: List[str] = checker.check_strategy_contract_only(tmp_path)

    assert len(problems) == 1
    assert "core/strategies/stock/my_breakout_strategy.py" in problems[0]


def test_contract_files_alone_pass(tmp_path: Path) -> None:
    """只有契約與門面時沒有違規；`__pycache__` 不算"""

    checker: ModuleType = _load_checker()
    for rel in checker._STRATEGY_CONTRACT_FILES:
        _write(tmp_path, rel)
    _write(tmp_path, "core/strategies/__pycache__/stale.py")

    assert checker.check_strategy_contract_only(tmp_path) == []


def test_the_real_tree_holds_only_contracts() -> None:
    """專案本身的 `core/strategies/` 現在只剩契約"""

    assert _load_checker().check_strategy_contract_only() == []
