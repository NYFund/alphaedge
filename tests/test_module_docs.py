import ast
import pathlib
from typing import List, Tuple

_PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]
_CORE: pathlib.Path = _PROJECT_ROOT / "core"

"""
`core/` 每個模組都要有模組層級說明

2026-09-26 補齊時有 11 個模組與 14 個 `__init__.py` 完全沒有說明，最大一支 658 行。
補完是一次性的，**維持才是問題**：新增模組時沒有任何東西會提醒，於是覆蓋率
只會往下掉，而「哪幾檔沒有」每次都要重新掃一遍才知道。

本專案的模組說明有兩種合法位置（CLAUDE.md §2.3）：檔首 docstring，或
**import 區塊之後的裸字串**（`core/api/`、`core/models/`、`core/pipeline/` 一律用後者）。
兩種都算，故判準是「模組層級有沒有任何獨立的字串運算式」而不是 `ast.get_docstring()`。

策略檔是明示例外：它們的交易邏輯寫在 class docstring（買進／賣出／停損三個區塊），
再加一份模組說明只會變成兩處各說一次。
"""


def _module_level_string(tree: ast.Module) -> bool:
    """模組層級是否有獨立的字串運算式（檔首 docstring 或 import 之後的裸字串）"""

    return any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for node in tree.body
    )


def _class_docstring_only_files() -> Tuple[str, ...]:
    """
    交易邏輯寫在 class docstring 的策略檔

    **以目錄判斷而不是列檔名**：列檔名的話，新增一支策略就要回來改這份清單，
    而漏改的表現是測試紅——那會讓人乾脆把整條規則放寬。
    """

    return ("core/strategies/stock/", "core/strategies/futures/")


def _iter_modules() -> List[pathlib.Path]:
    """`core/` 下所有 `.py`"""

    return sorted(
        path for path in _CORE.rglob("*.py") if "__pycache__" not in path.parts
    )


def test_the_scan_actually_finds_modules() -> None:
    """
    先確認掃描抓得到檔案

    抓不到的話下面兩條會對空清單成立，無條件通過。
    """

    modules: List[pathlib.Path] = _iter_modules()

    assert len(modules) >= 200, f"只掃到 {len(modules)} 個模組，掃描範圍可能已失效"
    assert any(path.name == "__init__.py" for path in modules)


def test_every_module_has_a_module_level_description() -> None:
    """
    每個非策略模組都要有模組層級說明

    沒有說明的模組，讀的人只能從類別名猜它在整條資料流的哪個位置；
    最大一支 658 行，猜錯的代價是改錯地方。
    """

    exempt: Tuple[str, ...] = _class_docstring_only_files()
    missing: List[str] = []

    for path in _iter_modules():
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        if any(rel.startswith(prefix) for prefix in exempt):
            continue
        if not _module_level_string(ast.parse(path.read_text(encoding="utf-8"))):
            missing.append(rel)

    assert not missing, "以下模組缺少模組層級說明（CLAUDE.md §2.3）：\n" + "\n".join(
        f"  {rel}" for rel in missing
    )


def test_exempt_strategy_files_still_document_themselves() -> None:
    """
    豁免的策略檔要有 class docstring

    否則「豁免」等於完全沒有說明——交易邏輯是最需要寫清楚的那一類。
    """

    exempt: Tuple[str, ...] = _class_docstring_only_files()
    undocumented: List[str] = []

    for path in _iter_modules():
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        if path.name == "__init__.py" or not any(
            rel.startswith(prefix) for prefix in exempt
        ):
            continue
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
