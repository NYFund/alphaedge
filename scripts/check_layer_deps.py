import argparse
import ast
import re
import sys
import tokenize
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

"""
分層相依檢查：以 AST 掃全專案 import，驗證 `_LAYER_RULES` 宣告的相依方向

- Features:
    1. 反向相依：低層 import 高層（例如 core/api → core/backtest）、`core/` import 到
       `tasks/`／`frontend/`／`strategy_lab/`／`tests/`／`scripts/`
    2. 循環 import：檔案層級的強連通分量（含套件 `__init__.py` 的 re-export 邊）
    3. 市場語意洩漏：`_ENGINE_FILES` 列出的引擎本體（回測與實盤）不得出現
       Stock／Futures／Tw 字樣，
       `if market ==` 只允許出現在 `factory.py`
    4. 跨軸目錄污染：每層目錄只承載一條軸（市場 `tw/`／`us/` 或商品類別 `stock/`／`futures/`）
    5. `sys.path` 注入：專案已以 editable 方式安裝，逐處列出以便複查
    6. 資料庫驅動外洩：`core/`、`tasks/` 內 `core/dao/` 以外的檔案不得 `import sqlite3`
- 使用場景:
    python scripts/check_layer_deps.py            # 只印報告，違規時以非零狀態碼結束
    python scripts/check_layer_deps.py --edges     # 另外把所有跨套件的 import 邊倒出來
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 掃描範圍：頂層套件與單檔入口；tests/ 也掃，但它可以 import 任何東西（只當 import 端）
_SCAN_DIRS: Tuple[str, ...] = (
    "core",
    "tasks",
    "frontend",
    "strategy_lab",
    "scripts",
    "tests",
)
_SCAN_FILES: Tuple[str, ...] = ("run.py",)

# 分層等級：數字越大越上層；import 只能由高往低（等級相同且套件不同者另外列為「同層互相 import」）
# 第四欄 exact=True 表示只比對「模組名完全相同」，不含其子模組——
# 用在策略套件門面（`core/strategies/stock/__init__.py` 只 re-export 基底類別）
_LAYER_RULES: Tuple[Tuple[str, int, str, bool], ...] = (
    ("core.config", 0, "共用層／設定", False),
    ("core.utils", 1, "共用層", False),
    ("core.models", 2, "領域層", False),
    # DAO 只 import core.config；與領域層同級但互不相依（同層互相 import 會被列出）
    ("core.dao", 2, "資料存取層（DAO）", False),
    ("core.api", 3, "資料層", False),
    ("core.adapters", 3, "資料層", False),
    ("core.pipeline", 3, "資料層（ETL）", False),
    # 市場結構（交易日曆、結算日、換月規則）：ETL、回測、實盤、策略都要用同一份。
    # 與 `core.api` 同層——日曆的建構子收 API 物件，放更低會變成反向相依
    ("core.market", 3, "市場結構", False),
    # 純公式檔，**只相依 `math` 與 `typing`**（見 `analysis/__init__.py` 的說明）：
    # 它比 `core.utils` 還低，任何人都可以 import 它而不會拉進任何相依。
    # 報表與（日後的）策略都要呼叫同一份公式，故必須擺在所有呼叫端之下
    ("core.backtest.analysis", 1, "共用層／績效公式", False),
    # 部位建構層：回測與實盤共用，**不是**回測概念。只可 import `core.models`／
    # `core.utils`／`core.config`／`core.api`；需要 `FuturesMarginConfig` 時會 import
    # 同層的 `core.managers`，那會列在 F 區（同層互相 import，僅供人工判讀），是刻意的
    ("core.portfolio", 4, "部位建構層", False),
    # 引擎 ↔ 資料源的共用契約：回測與實盤共用，**不是**回測概念
    # （與 `core.portfolio`、`core.execution` 同一個理由）
    ("core.datafeed", 4, "資料源契約", False),
    ("core.backtest.models", 4, "引擎層／可插拔 model", False),
    ("core.backtest.datafeed", 4, "引擎層／資料載入", False),
    ("core.backtest.report", 4, "引擎層／報表", False),
    ("core.managers", 4, "引擎層／部位與帳務", False),
    ("core.backtest.backtester", 5, "引擎層／引擎本體", False),
    ("core.backtest.factory", 6, "組裝層", False),
    ("core.backtest", 5, "引擎層（套件本身）", False),
    # 策略「契約」（抽象基底與其套件門面）是引擎、factory、報表都要認得的介面，
    # 與可插拔 model 同層；具體策略（momentum_strategy_1 等）才是策略層。
    # 引擎若 import 到任何具體策略，仍會被列為反向相依
    # === 實盤（core/broker、core/execution、core/live）===
    # 券商閘道層：只可 import `core.config`／`core.utils`／`core.models`。
    # **不可 import `core.api`**——券商層不讀歷史資料，它只負責「把委託送出去、
    # 把回報收回來」。一旦它開始查價格庫，實盤與回測就會各有一套取數路徑
    ("core.broker", 3, "券商閘道層", False),
    # 回測與實盤共用的委託前處理（方向白名單、max_holdings、排序）。
    # 刻意只 import `core.utils`／`core.models`，由呼叫端傳入純參數而不收策略物件——
    # 收了策略就會多一條 `core.execution` → `core.strategies.base` 的同層邊
    ("core.execution", 4, "共用委託前處理", False),
    # 實盤引擎的元件層，與 `core.backtest.models` 等可插拔 model 同級
    ("core.live.oms", 4, "實盤／委託管理", False),
    ("core.live.risk", 4, "實盤／風控", False),
    ("core.live.datafeed", 4, "實盤／資料載入", False),
    ("core.live.intraday", 4, "實盤／盤中迴圈", False),
    ("core.live.report", 4, "實盤／報表", False),
    ("core.live.attribution", 4, "實盤／多策略歸屬", False),
    ("core.live.account_sync", 4, "實盤／帳戶同步", False),
    ("core.live.reconciler", 4, "實盤／對帳", False),
    ("core.live.capital_allocator", 4, "實盤／資金額度分配", False),
    # 通知是旁路，**只可 import `core.config`／`core.utils`**：不碰模型也不碰 DAO，
    # 這樣它壞掉也不可能拖垮交易主流程
    ("core.live.notify", 4, "實盤／事件通知", False),
    # 套件本身也要登記。沒有規則命中的模組分層為 -1，**它 import 任何東西都會被
    # 判成反向相依**；`core.backtest` 同樣登記了套件本身。
    # 另外沿用 `core/backtest/__init__.py` 的作法：套件層不 eager import
    ("core.live", 5, "實盤引擎層（套件本身）", False),
    ("core.live.trader", 5, "實盤引擎本體", False),
    # 盤後作業：與送單段落沒有共用狀態，但同樣是「組裝一堆元件層跑一條流程」，
    # 故與 `trader` 同級。放 4 會讓它對 oms／report／reconciler 產生一整排同層邊
    ("core.live.after_close", 5, "實盤／盤後作業", False),
    ("core.live.factory", 6, "組裝層", False),
    ("core.strategies.base", 4, "策略契約", False),
    ("core.strategies.stock.base", 4, "策略契約", False),
    ("core.strategies.futures.base", 4, "策略契約", False),
    ("core.strategies.stock", 4, "策略契約（套件門面）", True),
    ("core.strategies.futures", 4, "策略契約（套件門面）", True),
    ("core.strategies", 7, "策略層", False),
    ("run", 8, "入口層", False),
    ("tasks", 8, "入口層", False),
    ("frontend", 8, "應用層", False),
    ("strategy_lab", 8, "研究層", False),
    ("scripts", 8, "工具", False),
    ("tests", 9, "測試（可 import 任何層）", False),
)

# 已登錄、尚未修的反向相依。
# 這是 ratchet：清單內的只列為「已知」，不影響結束碼；新出現的任何一條都會讓檢查失敗。
# **修掉之後要把對應那條從本清單移除**，不要讓它長期留著
# 空的是**目標狀態**：反向相依全部清掉了。再出現一條就是新欠的債，
# 登記進來之前先想清楚能不能用搬家或刪死碼解掉——這三種手段在 2026-09-22
# 把原本的 4 條清成 0 條，沒有一條是靠登記放行的
_KNOWN_REVERSE: Dict[Tuple[str, str], str] = {}

# 非 core 的頂層套件：core/ 內任何一處 import 到它們都是反向相依
_NON_CORE_TOPS: Set[str] = {
    "tasks",
    "frontend",
    "strategy_lab",
    "scripts",
    "tests",
    "run",
}

# 跨軸目錄規則：每層目錄只承載一條軸
_MARKET_AXIS_DIRS: Set[str] = {"tw", "us"}
_INSTRUMENT_AXIS_DIRS: Set[str] = {"stock", "futures", "option", "options"}
_MARKET_AXIS_PACKAGES: Tuple[str, ...] = (
    "core/api",
    "core/broker",
    "core/live/datafeed",
    "core/adapters",
    "core/dao",
    "core/backtest/datafeed",
    "core/market",
    "core/pipeline",
)
# 引擎本體：市場語意一律由注入物件承載，檔案內不得出現 Stock／Futures／Tw 字樣。
# 回測與實盤各一支，兩邊用同一條規則——實盤引擎若分出 `TwStockLiveTrader` 子類，
# 就會重演回測引擎當初的分裂
_ENGINE_FILES: Tuple[str, ...] = (
    "core/backtest/backtester.py",
    "core/live/trader.py",
)
_INSTRUMENT_AXIS_PACKAGES: Tuple[str, ...] = (
    "core/models",
    "core/strategies",
    "core/managers",
)


def module_name_of(path: Path) -> str:
    """把檔案路徑轉成模組名（`core/backtest/factory.py` → `core.backtest.factory`）"""

    rel: Path = path.relative_to(_PROJECT_ROOT)
    parts: List[str] = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def collect_files() -> List[Path]:
    """列出掃描範圍內的所有 .py 檔"""

    files: List[Path] = []
    for name in _SCAN_DIRS:
        base: Path = _PROJECT_ROOT / name
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for name in _SCAN_FILES:
        p: Path = _PROJECT_ROOT / name
        if p.exists():
            files.append(p)
    return sorted(files)


def resolve_import(module: str, node: ast.AST, known_modules: Set[str]) -> List[str]:
    """
    把一個 import 節點解析成專案內的目標模組清單

    `from pkg import name` 時，若 `pkg.name` 本身是模組就指向它，否則指向 `pkg`
    （套件 `__init__.py` 的 re-export 邊由此產生）。
    """

    targets: List[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            targets.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            # 相對匯入：以目前模組所在套件為基準往上推
            base_parts: List[str] = module.split(".")
            is_package: bool = f"{module}.__init__" in known_modules
            # `module` 若本身是 __init__（套件），level=1 指向自己；否則指向上一層
            drop: int = node.level - (1 if is_package else 0)
            if drop > 0:
                base_parts = base_parts[:-drop]
            base: str = ".".join(base_parts)
            if node.module:
                base = f"{base}.{node.module}" if base else node.module
        else:
            base = node.module or ""
        if not base:
            return targets
        for alias in node.names:
            candidate: str = f"{base}.{alias.name}"
            targets.append(candidate if candidate in known_modules else base)
    return targets


def to_known(target: str, known_modules: Set[str]) -> Optional[str]:
    """把 import 目標收斂到專案內最長的已知模組前綴；不是專案內的模組回傳 None"""

    parts: List[str] = target.split(".")
    while parts:
        cand: str = ".".join(parts)
        if cand in known_modules:
            return cand
        parts.pop()
    return None


def layer_of(module: str) -> Tuple[int, str]:
    """取得模組所屬分層（取最長前綴規則）"""

    best: Optional[Tuple[str, int, str]] = None
    for prefix, rank, label, exact in _LAYER_RULES:
        matched: bool = module == prefix or (
            not exact and module.startswith(prefix + ".")
        )
        if matched and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rank, label)
    if best is None:
        return (-1, "未分類")
    return (best[1], best[2])


def top_package(module: str) -> str:
    """模組的頂層套件名（`core.api.tw.x` → `core`）"""

    return module.split(".")[0]


def package_key(module: str) -> str:
    """分層比對用的套件鍵：取 `_LAYER_RULES` 中命中的前綴"""

    best: str = ""
    for prefix, _, _, exact in _LAYER_RULES:
        matched: bool = module == prefix or (
            not exact and module.startswith(prefix + ".")
        )
        if matched and len(prefix) > len(best):
            best = prefix
    return best or top_package(module)


def build_graph(
    files: List[Path],
) -> Tuple[Dict[str, Set[str]], Dict[str, Path], Dict[str, List[Tuple[str, int]]]]:
    """建立檔案層級 import 圖；回傳 (adjacency, module→path, module→[(target, lineno)])"""

    paths: Dict[str, Path] = {module_name_of(p): p for p in files}
    # 讓 `pkg.__init__` 也能被辨識為套件
    known: Set[str] = set(paths)
    for p in files:
        if p.name == "__init__.py":
            known.add(f"{module_name_of(p)}.__init__")

    graph: Dict[str, Set[str]] = defaultdict(set)
    detail: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for module, path in paths.items():
        try:
            tree: ast.AST = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"[SKIP] {path}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for raw in resolve_import(module, node, known):
                target: Optional[str] = to_known(raw, known)
                if target is None or target == module:
                    continue
                graph[module].add(target)
                detail[module].append((target, node.lineno))
    return graph, paths, detail


def strongly_connected(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Tarjan：回傳所有大小 > 1 的強連通分量（即循環）"""

    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    result: List[List[str]] = []
    counter: List[int] = [0]

    sys.setrecursionlimit(10000)

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                result.append(sorted(comp))

    for v in list(graph):
        if v not in index:
            visit(v)
    return result


def code_lines(path: Path) -> Dict[int, str]:
    """逐行取出「去掉字串常數與註解」後的程式碼，讓 docstring 裡的字樣不被誤判"""

    lines: Dict[int, str] = defaultdict(str)
    with path.open("rb") as handle:
        try:
            for tok in tokenize.tokenize(handle.readline):
                if tok.type in (tokenize.STRING, tokenize.COMMENT):
                    continue
                if tok.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER):
                    lines[tok.start[0]] += tok.string + " "
        except tokenize.TokenError:
            pass
    return lines


def check_market_leakage() -> List[str]:
    """引擎本體不得出現市場字樣；`if market ==` 只准出現在 factory.py"""

    problems: List[str] = []
    for engine_rel in _ENGINE_FILES:
        engine: Path = _PROJECT_ROOT / engine_rel
        if not engine.exists():
            continue
        for lineno, code in sorted(code_lines(engine).items()):
            if re.search(r"\b(Stock|Futures|Tw)[A-Za-z]*\b", code):
                problems.append(
                    f"{engine_rel}:{lineno}: 引擎出現市場字樣：{code.strip()}"
                )
    for path in (_PROJECT_ROOT / "core").rglob("*.py"):
        if path.name == "factory.py":
            continue
        for lineno, code in sorted(code_lines(path).items()):
            if re.search(r"\bmarket\s*==", code) or re.search(
                r"==\s*Market\s*\.", code
            ):
                rel: str = str(path.relative_to(_PROJECT_ROOT))
                problems.append(
                    f"{rel}:{lineno}: factory 以外的市場分派：{code.strip()}"
                )
    return problems


def check_axis_dirs() -> List[str]:
    """每層目錄只承載一條軸"""

    problems: List[str] = []
    for pkg in _MARKET_AXIS_PACKAGES:
        base: Path = _PROJECT_ROOT / pkg
        if not base.exists():
            continue
        for sub in base.rglob("*"):
            if sub.is_dir() and sub.name in _INSTRUMENT_AXIS_DIRS:
                problems.append(
                    f"{sub.relative_to(_PROJECT_ROOT)}: 市場軸目錄底下出現商品軸目錄"
                )
    for pkg in _INSTRUMENT_AXIS_PACKAGES:
        base = _PROJECT_ROOT / pkg
        if not base.exists():
            continue
        for sub in base.rglob("*"):
            if sub.is_dir() and sub.name in _MARKET_AXIS_DIRS:
                problems.append(
                    f"{sub.relative_to(_PROJECT_ROOT)}: 商品軸目錄底下出現市場軸目錄"
                )
    return problems


def check_strategy_facades(graph: Dict[str, Set[str]]) -> List[str]:
    """策略套件門面（stock/futures 的 __init__）只准 import 自己的 base，不得拉進具體策略"""

    problems: List[str] = []
    for facade in (
        "core.strategies",
        "core.strategies.stock",
        "core.strategies.futures",
    ):
        for dst in sorted(graph.get(facade, ())):
            if dst not in {f"{facade}.base", "core.strategies.base"}:
                problems.append(
                    f"{facade}/__init__.py import 了 {dst}：門面一 eager import 具體策略"
                    "就會造成循環 import"
                )
    return problems


# 只有 DAO 層可以直接碰資料庫驅動；其餘一律經由 `core.dao`
_DB_DRIVER_MODULES: Set[str] = {"sqlite3"}
_DB_DRIVER_GUARDED_DIRS: Tuple[str, ...] = ("core", "tasks")
_DB_DRIVER_ALLOWED_DIR: str = "core/dao"


def check_db_driver_imports(files: List[Path]) -> List[str]:
    """
    - Description:
        `core/`、`tasks/` 內 `core/dao/` 以外的檔案不得 import 資料庫驅動

        「SQL、連線與交易只寫在 DAO」原本只是慣例：DAO 重構前 `core/` 有 44 處
        `sqlite3.connect`、updater 與 loader 各開一條連線且有些從不關閉。慣例擋不住
        下一個人順手 `import sqlite3`，故改成檢查。型別標註請用 `core.dao.connection`
        的 `DBConnection`，捕捉錯誤用 `DBError`。以 AST 判定，說明文字裡的字樣不算。
    - Parameters:
        - files: List[Path]
            要掃的檔案
    - Return:
        - List[str]
            `檔案:行號: import 敘述` 清單
    """

    hits: List[str] = []
    for path in files:
        rel: str = path.relative_to(_PROJECT_ROOT).as_posix()
        if not rel.startswith(tuple(f"{d}/" for d in _DB_DRIVER_GUARDED_DIRS)):
            continue
        if rel.startswith(f"{_DB_DRIVER_ALLOWED_DIR}/"):
            continue
        try:
            tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            modules: List[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            for module in modules:
                if module.split(".")[0] in _DB_DRIVER_MODULES:
                    hits.append(f"{rel}:{node.lineno}: import {module}")
    return hits


def check_sys_path(files: List[Path]) -> List[str]:
    """
    - Description:
        列出所有 sys.path 注入（**只認真的呼叫，不認文字**）

        以 AST 找 `sys.path.insert(...)`／`sys.path.append(...)` 的呼叫節點。
        舊版用 `"sys.path.insert" in line` 逐行比對字串，於是**說明這件事的
        docstring 也會被算成一處**——`sys.path` 注入全數清乾淨後，唯一剩下的那一筆
        正是解釋「原本靠 sys.path.insert 硬塞」的那行註解。
        護欄把自己的說明文字算成違規，就沒辦法拿它當「應為 0」的判準。
    - Parameters:
        - files: List[Path]
            要掃的檔案
    - Return:
        - List[str]
            `檔案:行號` 清單
    """

    hits: List[str] = []
    self_path: Path = Path(__file__).resolve()

    for path in files:
        if path.resolve() == self_path:
            continue
        try:
            tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func: ast.expr = node.func
            # 比對 `sys.path.insert` / `sys.path.append` 這個屬性鏈
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"insert", "append"}
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "sys"
            ):
                hits.append(f"{path.relative_to(_PROJECT_ROOT)}:{func.lineno}")
    return hits


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="分層相依檢查"
    )
    parser.add_argument("--edges", action="store_true", help="倒出所有跨套件 import 邊")
    args: argparse.Namespace = parser.parse_args()

    files: List[Path] = collect_files()
    graph, paths, detail = build_graph(files)

    reverse: List[str] = []  # 低層 import 高層
    known_reverse: List[str] = []  # 已登錄的反向相依
    same_layer: List[str] = []  # 同層不同套件互相 import
    core_to_outside: List[str] = []  # core/ import 到非 core
    edges_out: List[str] = []

    for src in sorted(graph):
        src_rank, _ = layer_of(src)
        src_key: str = package_key(src)
        for dst, lineno in sorted(set(detail[src])):
            dst_rank, _ = layer_of(dst)
            dst_key: str = package_key(dst)
            if src_key != dst_key:
                edges_out.append(
                    f"{src} -> {dst}  ({paths[src].relative_to(_PROJECT_ROOT)}:{lineno})"
                )
            if top_package(src) == "core" and top_package(dst) in _NON_CORE_TOPS:
                core_to_outside.append(
                    f"{paths[src].relative_to(_PROJECT_ROOT)}:{lineno}: {src} -> {dst}"
                )
                continue
            if src_rank == 9:
                continue  # tests 可 import 任何層
            if src_key == dst_key:
                continue
            if dst.startswith(src + ".") or src.startswith(dst + "."):
                continue  # 套件門面與自己的子模組之間不算跨層
            if dst_rank > src_rank:
                if (src, dst) in _KNOWN_REVERSE:
                    known_reverse.append(
                        f"{paths[src].relative_to(_PROJECT_ROOT)}:{lineno}: "
                        f"{src} -> {dst}  [{_KNOWN_REVERSE[(src, dst)]}]"
                    )
                    continue
                reverse.append(
                    f"{paths[src].relative_to(_PROJECT_ROOT)}:{lineno}: "
                    f"{src}（{layer_of(src)[1]}）-> {dst}（{layer_of(dst)[1]}）"
                )
            elif dst_rank == src_rank:
                same_layer.append(
                    f"{paths[src].relative_to(_PROJECT_ROOT)}:{lineno}: {src} -> {dst}"
                )

    cycles: List[List[str]] = strongly_connected(graph)
    leakage: List[str] = check_market_leakage()
    axis: List[str] = check_axis_dirs()
    facades: List[str] = check_strategy_facades(graph)
    sys_path_hits: List[str] = check_sys_path(files)
    db_driver_hits: List[str] = check_db_driver_imports(files)

    def section(title: str, items: List[str]) -> None:
        print(f"\n=== {title}（{len(items)}）===")
        for item in items:
            print(f"  {item}")

    print(
        f"掃描 {len(files)} 個檔案，{sum(len(v) for v in graph.values())} 條專案內 import 邊"
    )
    section("A. core/ 反向 import 到非 core 套件", core_to_outside)
    section("B. 低層 import 高層（違反單向相依，新出現者）", reverse)
    section("B'. 已登錄的反向相依（附錄 A 追蹤中，不影響結束碼）", known_reverse)
    section("C. 循環 import（強連通分量）", [" <-> ".join(c) for c in cycles])
    section("D. 市場語意洩漏", leakage)
    section("E. 跨軸目錄污染", axis)
    section("E'. 策略套件門面 eager import 具體策略", facades)
    section("E''. DAO 以外 import 資料庫驅動（core／tasks）", db_driver_hits)
    section("F. 同層不同套件互相 import（僅列出，需人工判讀）", same_layer)
    section("G. sys.path 注入（僅列出）", sys_path_hits)
    if args.edges:
        section("H. 全部跨套件 import 邊", edges_out)

    violations: int = (
        len(core_to_outside)
        + len(reverse)
        + len(cycles)
        + len(leakage)
        + len(axis)
        + len(facades)
        + len(db_driver_hits)
    )
    print(f"\n違規總數：{violations}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
