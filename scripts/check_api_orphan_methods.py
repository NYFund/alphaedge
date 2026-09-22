import argparse
import ast
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

"""
`core/api/` 無主公開介面檢查：以 AST 掃出公開方法，逐一比對全 repo 的呼叫點

`core/api/` 是**策略作者的公開介面**（見 `core/strategies/README.md`），
「`core/` 內部零呼叫」對它們是正常狀態——所以判準不是「有沒有人呼叫」，
而是「有沒有東西盯著它」：**零呼叫且零測試**才算無主。

無主的介面不是壞掉的程式碼，它現在多半能跑；問題是下一次資料表欄位一改，
它會安靜地跟著壞，而沒有任何東西會紅。

- Features:
    1. 以 AST 取得 `core/api/` 每個類別的公開方法（不含 `_` 前綴與 dunder）
    2. 掃全 repo 的 `.py` 找呼叫點，並區分「正式程式碼」與「`tests/`」兩類
    3. 只有 `tests/` 呼叫者視為**已被維護**，不列為違規
- 使用場景:
    python scripts/check_api_orphan_methods.py           # 違規時以非零狀態碼結束
    python scripts/check_api_orphan_methods.py --all     # 另外列出「只有測試在呼叫」的方法
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_API_DIR: Path = _PROJECT_ROOT / "core" / "api"

# 掃描呼叫點的範圍；`.venv`／快取不掃
_SCAN_DIRS: Tuple[str, ...] = (
    "core",
    "tasks",
    "frontend",
    "strategy_lab",
    "scripts",
    "tests",
)
_SCAN_FILES: Tuple[str, ...] = ("run.py",)

# 基底類別的樣板方法：由子類實作、由框架呼叫，不受本檢查管轄
_FRAMEWORK_METHODS: Set[str] = {"get", "setup", "close"}


def _iter_python_files(root: Path) -> List[Path]:
    """取得目錄下所有 `.py`（排除快取與虛擬環境）"""

    return [
        path
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    ]


def collect_public_methods() -> Dict[str, List[Tuple[str, str]]]:
    """
    - Description:
        掃出 `core/api/` 每個公開方法及其所在檔案與類別
    - Return:
        - Dict[str, List[Tuple[str, str]]]
            `{方法名: [(相對路徑, 類別名), ...]}`
    """

    methods: Dict[str, List[Tuple[str, str]]] = {}

    for path in _iter_python_files(_API_DIR):
        tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        rel: str = str(path.relative_to(_PROJECT_ROOT))

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name.startswith("_") or item.name in _FRAMEWORK_METHODS:
                    continue
                methods.setdefault(item.name, []).append((rel, node.name))

    return methods


def collect_call_sites(names: Set[str]) -> Dict[str, Set[str]]:
    """
    - Description:
        掃全 repo 找「取用該方法」的地方

        **看的是屬性取值（`ast.Attribute`），不是只有帶括號的呼叫**：

        ```python
        calculate = (
            self.margin_config.api.calculate_stock_futures_maintenance_margin
            if maintenance
            else self.margin_config.api.calculate_stock_futures_margin
        )
        return calculate(product, date, price, ...)
        ```

        方法被繫結成變數再呼叫，`obj.method(...)` 的形狀從未出現。只認
        `.name(` 的話這種呼叫端完全看不到，該方法會被報成無主介面——
        而那是**誤報**，誤報過的閘門會教人忽略這一關。

        用 AST 而不是正規表達式：`.name` 這個字串也會出現在註解與 docstring 裡，
        比對文字會讓「只在註解被提到」算成有人用，那是反方向的漏。

        **`core/api/` 自己也算呼叫端**：`get_stock_net_chip()` 呼叫
        `get_stock_chip()`，後者就有人在用。定義本身是 `ast.FunctionDef`
        而不是 `ast.Attribute`，不會被誤計。
    - Parameters:
        - names: Set[str]
            要找的方法名集合
    - Return:
        - Dict[str, Set[str]]
            `{方法名: {出現的相對路徑, ...}}`
    """

    call_sites: Dict[str, Set[str]] = {name: set() for name in names}

    targets: List[Path] = []
    for directory in _SCAN_DIRS:
        targets.extend(_iter_python_files(_PROJECT_ROOT / directory))
    targets.extend(_PROJECT_ROOT / name for name in _SCAN_FILES)

    for path in targets:
        if not path.exists():
            continue
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        try:
            tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            # 語法壞掉的檔案跳過並說出來：靜靜略過等於把它的呼叫點一起丟掉，
            # 那會讓被它獨佔使用的方法變成假的無主介面
            print(f"  （略過語法無法解析的檔案：{rel}）", file=sys.stderr)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in call_sites:
                call_sites[node.attr].add(rel)

    return call_sites


def main() -> int:
    """列出零呼叫零測試的公開方法；有違規時回非零狀態碼"""

    parser = argparse.ArgumentParser(description="core/api 無主公開介面檢查")
    parser.add_argument(
        "--all",
        action="store_true",
        help="另外列出「只有測試在呼叫」的方法（屬正常狀態，僅供參考）",
    )
    args = parser.parse_args()

    methods: Dict[str, List[Tuple[str, str]]] = collect_public_methods()
    call_sites: Dict[str, Set[str]] = collect_call_sites(set(methods))

    orphans: List[str] = []
    test_only: List[str] = []

    for name in sorted(methods):
        sites: Set[str] = call_sites[name]
        if not sites:
            orphans.append(name)
        elif all(site.startswith("tests/") for site in sites):
            test_only.append(name)

    print(f"core/api 公開方法：{len(methods)} 個")

    if args.all:
        print(f"\n只有測試在呼叫（{len(test_only)} 個，屬正常狀態）：")
        for name in test_only:
            owners: str = "、".join(f"{cls}" for _, cls in methods[name])
            print(f"  {name}  <- {owners}")

    if orphans:
        print(f"\n零呼叫且零測試（{len(orphans)} 個）：")
        for name in orphans:
            for rel, cls in methods[name]:
                print(f"  {rel}::{cls}.{name}")
        print(
            "\n每個都要二擇一：補一條測試讓它進入「有人維護」那一組，"
            "或確認無人需要即刪除並在該檔 docstring 註記。"
        )
        return 1

    print("\n零呼叫且零測試：0 個")
    return 0


if __name__ == "__main__":
    sys.exit(main())
