import ast
from pathlib import Path
from typing import List, Tuple

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


def category_literals(path: Path) -> List[Tuple[int, str]]:
    """找出檔案裡所有以字面值寫出的事件類別：`(行號, 類別名)`"""

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
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append((value.lineno, value.value))
    return found


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
