import ast
import re
from pathlib import Path
from typing import List

from core.models import PreOpenStockQuote, PriceLimits

"""
漲跌停的回傳契約：三處 `get_price_limits()` 一律回 `PriceLimits`，呼叫端只能用欄位名取值

原本規格層回 `(跌停, 漲停)`、實盤資料源與執行層回 `(漲停, 跌停)`，三個同名方法的 tuple
順序不同；呼叫端解包時只要把順序寫反就是靜默錯價，不會有任何錯誤訊息。
"""

CORE_DIR: Path = Path(__file__).resolve().parents[1] / "core"

# 解包或索引取漲跌停的寫法：行首的 `limit_down, limit_up = …`、`limits[0]`
# （`PriceLimits(limit_up=limit_up, limit_down=limit_down)` 這種具名建構不算）
POSITIONAL_ACCESS: re.Pattern = re.compile(
    r"^\s*(limit_down|limit_up)\s*,\s*(limit_up|limit_down)\s*=[^=]|\blimits\[[01]\]"
)


def _core_sources() -> List[Path]:
    """`core/` 底下所有 Python 檔"""

    return sorted(CORE_DIR.rglob("*.py"))


def test_every_get_price_limits_returns_the_named_result() -> None:
    """`core/` 內所有 `get_price_limits()` 的回傳型別標註都是 `PriceLimits`，不可再回 tuple"""

    offenders: List[str] = []
    seen: int = 0
    for path in _core_sources():
        tree: ast.Module = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_price_limits":
                seen += 1
                returns: str = ast.unparse(node.returns) if node.returns else ""
                if returns != "PriceLimits":
                    offenders.append(f"{path.relative_to(CORE_DIR)}:{node.lineno}")

    assert seen >= 3, "規格層、實盤資料源、執行層三處定義至少要掃到"
    assert offenders == [], f"回傳型別不是 PriceLimits：{offenders}"


def test_no_caller_unpacks_or_indexes_the_limits() -> None:
    """呼叫端不可解包或用索引取漲跌停：順序一寫反就是靜默錯價"""

    offenders: List[str] = []
    for path in _core_sources():
        source: str = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            if POSITIONAL_ACCESS.search(line):
                offenders.append(f"{path.relative_to(CORE_DIR)}:{lineno}")

        tree: ast.Module = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value: ast.expr = node.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get_price_limits"
            ):
                continue
            if any(isinstance(target, ast.Tuple) for target in node.targets):
                offenders.append(f"{path.relative_to(CORE_DIR)}:{node.lineno}")

    assert offenders == [], f"以位置取漲跌停：{offenders}"


def test_none_means_both_sides_are_unknown() -> None:
    """`none()` 兩側都是 None；期貨與取不到合約時都回它"""

    limits: PriceLimits = PriceLimits.none()

    assert limits.limit_up is None and limits.limit_down is None
    assert limits == PriceLimits(limit_up=None, limit_down=None)


def test_field_names_match_the_pre_open_quote() -> None:
    """欄位名與盤前報價的 `limit_up`／`limit_down` 相同，兩邊的值可以直接對照"""

    quote: PreOpenStockQuote = PreOpenStockQuote(
        stock_id="2330",
        date=None,
        reference_price=100.0,
        limit_up=110.0,
        limit_down=90.0,
    )

    assert PriceLimits._fields == ("limit_up", "limit_down")
    assert all(hasattr(quote, field) for field in PriceLimits._fields)
