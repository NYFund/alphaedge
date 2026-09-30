import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Type

import pytest
from loguru import logger

import core.strategies.strategy_loader as loader_module
from core.strategies.base import BaseStrategy
from core.strategies.strategy_loader import StrategyLoader

"""
`StrategyLoader.load()`：只載入指定的策略

全掃描會 import 每一個策略模組，正式環境的實盤行程也就會執行研究中策略的
module-level 程式碼。這裡用一個「一 import 就拋錯」的模組當哨兵：
指定其他策略時它不可以被碰到。
"""

# 一旦被 import 就先寫標記檔、再拋錯的模組內容。
# **不能只靠「拋錯」判斷有沒有被 import**：import 失敗的模組不會留在 `sys.modules`，
# 以它為斷言的話，就算 loader 真的 import 了它，測試照樣通過
_EXPLODING_SOURCE: str = (
    "from pathlib import Path\n"
    "\n"
    "Path(__file__).with_name('research.imported').touch()\n"
    'raise RuntimeError("研究中的策略不可被實盤入口 import")\n'
)

# 繼承既有具體策略：不必在測試裡重寫一整組抽象方法，類別仍「定義在該模組內」
_GOOD_SOURCE: str = (
    "from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1\n"
    "\n"
    "\n"
    "class GoodStrategy(MomentumStrategy1):\n"
    '    """可實例化的策略"""\n'
)


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    在暫存目錄建一個假的策略套件，並讓 loader 改掃它

    套件名帶上暫存目錄名，避免不同測試之間共用 `sys.modules` 的快取。
    """

    name: str = f"fake_strategies_{tmp_path.name}"
    root: Path = tmp_path / name
    (root / "stock").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "stock" / "__init__.py").write_text("", encoding="utf-8")
    (root / "stock" / "good.py").write_text(_GOOD_SOURCE, encoding="utf-8")
    (root / "stock" / "research.py").write_text(_EXPLODING_SOURCE, encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    package: ModuleType = importlib.import_module(name)
    monkeypatch.setattr(loader_module, "strategies_pkg", package)
    yield root
    for module_name in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[module_name]


def test_named_load_does_not_import_other_modules(fake_package: Path) -> None:
    """指定 `GoodStrategy` 時，會拋錯的研究模組不可以被 import"""

    found: Dict[str, Type[BaseStrategy]] = StrategyLoader.load(["GoodStrategy"])

    assert list(found) == ["GoodStrategy"]
    assert not (fake_package / "stock" / "research.imported").exists()


def test_full_scan_still_imports_everything(fake_package: Path) -> None:
    """
    對照組：全掃描確實會碰到研究模組（逐模組隔離，不會整個崩潰）

    少了這條，上一條可能只是因為哨兵根本沒被掃到才通過。
    """

    messages: List[str] = []
    sink_id: int = logger.add(lambda message: messages.append(message), level="ERROR")
    try:
        found: Dict[str, Type[BaseStrategy]] = StrategyLoader.load_strategies()
    finally:
        logger.remove(sink_id)

    assert "GoodStrategy" in found
    assert (fake_package / "stock" / "research.imported").exists()
    assert any(f"{fake_package.name}.stock.research" in text for text in messages)


def test_unknown_name_is_omitted(fake_package: Path) -> None:
    """找不到的名稱不出現在回傳值，由入口比對後回報"""

    assert StrategyLoader.load(["NoSuchStrategy"]) == {}


def test_abstract_class_is_not_a_strategy(fake_package: Path) -> None:
    """同名但不可實例化（抽象基底）視同找不到"""

    (fake_package / "stock" / "base_like.py").write_text(
        "from core.strategies.stock.base import BaseStockStrategy\n"
        "\n"
        "\n"
        "class AbstractOnly(BaseStockStrategy):\n"
        '    """沒有實作任何抽象方法"""\n',
        encoding="utf-8",
    )

    assert StrategyLoader.load(["AbstractOnly"]) == {}


def test_duplicate_class_name_is_refused(fake_package: Path) -> None:
    """同名類別出現在兩個模組：類別名是 `--strategy` 的識別名稱，必須唯一"""

    (fake_package / "stock" / "good_copy.py").write_text(_GOOD_SOURCE, encoding="utf-8")

    with pytest.raises(ValueError, match="GoodStrategy"):
        StrategyLoader.load(["GoodStrategy"])


def test_unparsable_module_is_skipped(fake_package: Path) -> None:
    """語法錯誤的檔案記 error 並略過，不影響其他策略"""

    (fake_package / "stock" / "broken.py").write_text(
        "class Broken(:\n", encoding="utf-8"
    )

    assert list(StrategyLoader.load(["GoodStrategy"])) == ["GoodStrategy"]


def test_real_strategies_load_by_name() -> None:
    """真實目錄：每一支全掃描找得到的策略，依名稱也找得到同一個類別"""

    registry: Dict[str, Type[BaseStrategy]] = StrategyLoader.load_strategies()

    assert StrategyLoader.load(sorted(registry)) == registry
