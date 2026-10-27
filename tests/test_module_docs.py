import ast
import pathlib
from typing import List, Tuple

from tests.test_entrypoint_and_logging import _GUARDED_PACKAGES

_PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]
# 掃描根與入口／日誌護欄同一份清單，再加 `frontend/`（該護欄不管它：前端不寫 loguru 檔）。
# CLAUDE.md §2 適用 `**/*.py`，只掃 `core/` 時 `frontend/app.py` 一直沒有說明也沒人發現
_SCANNED_PACKAGES: Tuple[str, ...] = (*_GUARDED_PACKAGES, "frontend")
# 具體策略的商品類別目錄（框架只留契約，具體策略在頂層 `strategies/`）
_STRATEGY_DIRS: Tuple[pathlib.Path, ...] = (
    _PROJECT_ROOT / "strategies" / "stock",
    _PROJECT_ROOT / "strategies" / "futures",
)

"""
每個模組都要有模組層級說明（`core/`、`apps/`、`strategies/`、`scripts/`、`strategy_lab/`、`frontend/`）

2026-09-26 補齊時有 11 個模組與 14 個 `__init__.py` 完全沒有說明，最大一支 658 行。
補完是一次性的，**維持才是問題**：新增模組時沒有任何東西會提醒，於是覆蓋率
只會往下掉，而「哪幾檔沒有」每次都要重新掃一遍才知道。

本專案的模組說明有兩種合法位置：檔首 docstring，或
**import 區塊之後的裸字串**（`core/api/`、`core/models/`、`core/pipeline/` 一律用後者）。
兩種都算，故判準是「模組層級有沒有任何獨立的字串運算式」而不是 `ast.get_docstring()`。

具體策略不在 `core/`（在頂層 `strategies/`），規則也不同：交易邏輯寫在 class docstring
（買進／賣出／停損三個區塊），再加一份模組說明只會變成兩處各說一次，故另一條測試只要求
class docstring。
"""


def _module_level_string(tree: ast.Module) -> bool:
    """模組層級是否有獨立的字串運算式（檔首 docstring 或 import 之後的裸字串）"""

    return any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for node in tree.body
    )


def _iter_strategy_files() -> List[pathlib.Path]:
    """
    具體策略檔（交易邏輯寫在 class docstring）

    **以目錄判斷而不是列檔名**：列檔名的話，新增一支策略就要回來改這份清單，
    而漏改的表現是測試紅——那會讓人乾脆把整條規則放寬。
    """

    return sorted(
        path
        for directory in _STRATEGY_DIRS
        for path in directory.glob("*.py")
        if path.name != "__init__.py"
    )


def _iter_modules() -> List[pathlib.Path]:
    """掃描範圍內所有 `.py`，扣掉具體策略檔（它們的規則見 `_iter_strategy_files()`）"""

    strategy_files: set = set(_iter_strategy_files())
    return sorted(
        path
        for package in _SCANNED_PACKAGES
        for path in (_PROJECT_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts and path not in strategy_files
    )


def test_the_scan_actually_finds_modules() -> None:
    """
    先確認掃描抓得到檔案

    抓不到的話下面兩條會對空清單成立，無條件通過。
    """

    modules: List[pathlib.Path] = _iter_modules()

    assert len(modules) >= 300, f"只掃到 {len(modules)} 個模組，掃描範圍可能已失效"
    assert any(path.name == "__init__.py" for path in modules)
    # 每個宣告的目錄都要真的掃到東西：目錄改名時清單會留著舊名字、靜默少掃一塊
    for package in _SCANNED_PACKAGES:
        assert any(
            path.relative_to(_PROJECT_ROOT).parts[0] == package for path in modules
        ), f"{package}/ 一個模組都沒掃到"


def test_every_module_has_a_module_level_description() -> None:
    """
    每個非策略模組都要有模組層級說明

    沒有說明的模組，讀的人只能從類別名猜它在整條資料流的哪個位置；
    最大一支 658 行，猜錯的代價是改錯地方。
    """

    missing: List[str] = []

    for path in _iter_modules():
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        if not _module_level_string(ast.parse(path.read_text(encoding="utf-8"))):
            missing.append(rel)

    assert not missing, "以下模組缺少模組層級說明（CLAUDE.md §2.3）：\n" + "\n".join(
        f"  {rel}" for rel in missing
    )


def test_strategy_files_document_themselves() -> None:
    """
    具體策略檔要有 class docstring

    不要求模組說明，不等於可以完全沒有說明——交易邏輯是最需要寫清楚的那一類。
    **先確認掃得到策略檔**：目錄搬走之後掃到空清單，這條會無條件通過。
    """

    strategy_files: List[pathlib.Path] = _iter_strategy_files()
    assert len(strategy_files) >= 3, (
        f"只掃到 {len(strategy_files)} 支策略，目錄可能已改"
    )

    undocumented: List[str] = []
    for path in strategy_files:
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        classes: List[ast.ClassDef] = [
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ]
        if not classes or not any(ast.get_docstring(node) for node in classes):
            undocumented.append(rel)

    assert not undocumented, (
        "以下策略檔既無模組說明也無 class docstring：\n"
        + "\n".join(f"  {rel}" for rel in undocumented)
    )
