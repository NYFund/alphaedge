import ast
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.live.report.parity_checker import RISK_EVENT_TO_CATEGORY
from core.live.risk import event_categories

"""
風控事件類別名的單一來源：寫入端的字面值與 parity 的對照表都要對得上登錄檔

parity 的對照表曾經寫著引擎從未寫出的名字（`CAPITAL_RESERVE_FAILED`、
`CROSS_STRATEGY_CONFLICT`），而引擎實際寫的 `CAPITAL_EXHAUSTED`、`MARGIN_INSUFFICIENT`、
`PRICE_LIMIT` 等一個都不在表上——被擋下的單一律被歸成快照口徑。字串各抄一份不會有任何
錯誤，所以用這裡的掃描把兩邊釘在同一份登錄檔上。
"""

LIVE_ROOT: Path = Path(__file__).resolve().parents[2] / "core" / "live"

# 會寫出事件類別的呼叫：方法名 → 類別參數的位置（關鍵字名或位置索引）
CATEGORY_KEYWORD: str = "category"
POSITIONAL_CATEGORY_ARG: dict = {
    "reject": 0,
    "_write_event": 0,
    "_write_risk_event": 0,
    "_write_roll_event": 3,
}


def module_constants(path: Path) -> Dict[str, str]:
    """檔案最外層的字串常數：`NAME: str = "..."` → `{NAME: 值}`"""

    tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
    constants: Dict[str, str] = {}
    for node in tree.body:
        target: Optional[ast.expr] = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value: Optional[ast.expr] = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def all_live_constants() -> Dict[str, str]:
    """`core/live/` 各檔最外層字串常數的聯集：解析跨檔 import 進來的常數名用"""

    constants: Dict[str, str] = {}
    for path in sorted(LIVE_ROOT.rglob("*.py")):
        constants.update(module_constants(path))
    return constants


def resolve_category(
    value: ast.expr, local: Dict[str, str], shared: Dict[str, str]
) -> Optional[str]:
    """
    把類別參數解析成字串；解析不了（函式參數轉傳的變數）時回 None

    **常數寫法也要解析**：只收字面值的話，以模組常數寫出的類別整條被略過——
    重建歸屬帳的事件類別就曾因此沒登錄，而測試照樣綠。順序是本檔常數、登錄檔、
    其他檔案的常數：登錄檔的名字最可能被 import 進來直接用。
    轉傳變數（`category=category`）的實際值由呼叫端傳入，呼叫端那裡會被掃到。
    """

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        registered: object = getattr(event_categories, value.id, None)
        if value.id in local:
            return local[value.id]
        if isinstance(registered, str):
            return registered
        return shared.get(value.id)
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "event_categories"
    ):
        attribute: object = getattr(event_categories, value.attr, None)
        return attribute if isinstance(attribute, str) else None
    return None


def category_literals(
    path: Path, shared: Optional[Dict[str, str]] = None
) -> List[Tuple[int, str]]:
    """找出檔案裡所有寫出的事件類別（字面值或常數）：`(行號, 類別名)`"""

    shared = all_live_constants() if shared is None else shared
    local: Dict[str, str] = module_constants(path)
    tree: ast.AST = ast.parse(path.read_text(encoding="utf-8"))
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func: ast.expr = node.func
        name: str = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )

        values: List[ast.expr] = []
        if name == "write" or name == "_write_roll_event":
            values.extend(
                keyword.value
                for keyword in node.keywords
                if keyword.arg == CATEGORY_KEYWORD
            )
        index: int = POSITIONAL_CATEGORY_ARG.get(name, -1)
        if index >= 0 and len(node.args) > index:
            values.append(node.args[index])

        for value in values:
            category: Optional[str] = resolve_category(value, local, shared)
            if category is not None:
                found.append((value.lineno, category))
    return found


def test_the_scan_actually_finds_the_writers() -> None:
    """
    掃描本身不得落空

    下一條只斷言「沒有未登錄的類別」，掃到 0 筆時同樣成立：`LIVE_ROOT` 指錯、
    寫入端改用別的方法名，或類別從字面值改成常數，都會讓它靜默變成恆真。
    兩支寫最多事件的檔案必須在命中裡——拆檔或改名時這裡會紅，提醒同步更新。
    """

    hits: Dict[str, int] = {}
    for path in sorted(LIVE_ROOT.rglob("*.py")):
        count: int = len(category_literals(path))
        if count:
            hits[path.name] = hits.get(path.name, 0) + count

    assert sum(hits.values()) >= 30, hits
    assert "risk_manager.py" in hits, hits
    assert "trader.py" in hits, hits


def test_every_category_written_by_the_engine_is_registered() -> None:
    """`core/live/` 寫出的每一個類別字面值都要在登錄檔；新類別要先決定它屬於哪一組"""

    unknown: List[str] = []
    for path in sorted(LIVE_ROOT.rglob("*.py")):
        for lineno, category in category_literals(path):
            if category not in event_categories.ALL_EVENT_CATEGORIES:
                unknown.append(
                    f"{path.relative_to(LIVE_ROOT.parent.parent)}:{lineno} {category}"
                )

    assert unknown == [], "未登錄的事件類別：\n" + "\n".join(unknown)


def test_parity_maps_exactly_the_order_block_categories() -> None:
    """對照表的鍵 ＝ 逐單擋下的類別全集：少一個就歸不到、多一個就是對照著不存在的名字"""

    assert set(RISK_EVENT_TO_CATEGORY) == set(event_categories.ORDER_BLOCK_CATEGORIES)


def test_the_two_groups_do_not_overlap() -> None:
    """一個類別只能屬於一組：逐單擋下或非逐單"""

    assert (
        event_categories.ORDER_BLOCK_CATEGORIES
        & event_categories.OTHER_EVENT_CATEGORIES
    ) == frozenset()
