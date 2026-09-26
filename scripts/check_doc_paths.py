import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

"""
文件路徑漂移檢查：找出 `.md` 裡指不到檔案、但同名檔存在於別處的路徑引用

檔案搬家時文件不會自己跟上。**只回報「同名檔確實存在於別處」的引用**——
那才是漂移；指不到又找不到同名檔的多半是規劃中的未來檔案（`backlog/` 常見），
不是錯誤。

另一類是**檔案被刪除**。搬家看得到（有同名檔可比對），刪除看不到——
沒有同名檔就落進「多半是規劃中的未來檔案」那一堆，README 指向一支已刪除的
策略檔也不會有人察覺。故另以 git 歷史判斷：指不到、全 repo 無同名檔、
**但歷史上存在過**，那就是刪除造成的懸空引用。

第三類是**符號層面的漂移**：檔案還在、路徑還對，但裡面的類別或方法已改名或搬走。
路徑檢查對這一類完全失明，而它比路徑漂移更常見——重構改方法名不必動任何檔名。
判準與路徑那邊同一套邏輯：只在**有正面證據**時回報（類別找得到、成員找不到）。

- Features:
    1. 抓行內程式碼（`` `core/xxx/yyy.py` ``）與 Markdown 連結中的帶目錄路徑
    2. 指得到就跳過；指不到但全 repo 有同名檔即回報，並列出實際位置
    3. 指不到、無同名檔、但 git 歷史存在過 → 回報為「指向已刪除的檔案」
    4. 只寫檔名不寫目錄的簡稱（`` `factory.py` ``）不算——那是行文，不是連結
    5. 反引號內的 `` `Class.method()` ``：類別在全庫有定義而成員（含繼承）
       找不到時回報，`.md` 與 `.py` 註解一起掃
- 使用場景:
    python scripts/check_doc_paths.py           # 有漂移時以非零狀態碼結束
    python scripts/check_doc_paths.py --list-unknown  # 另列指不到且無同名檔者
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 掃描哪些 `.md`。
# `.claude/`、`.cursor/`、`.github/` 是 AI 工具與 CI 讀規則的入口，那裡的路徑指錯
# **會讓規則靜默失效**——沒有任何錯誤訊息，只是那條規則從此不生效，
# 正是這支腳本存在的理由。`.github/` 目前無 `.md`，先納入以防日後新增
_SCAN_DIRS: Tuple[str, ...] = (
    ".claude",
    ".cursor",
    ".github",
    "docs",
    "backlog",
    "core",
    "tasks",
    "frontend",
    "strategy_lab",
    "scripts",
    "tests",
)
_SCAN_FILES: Tuple[str, ...] = ("CLAUDE.md", "README.md", "README_en.md")

# 排除的目錄
_EXCLUDE_PARTS: Set[str] = {".venv", "__pycache__", "node_modules", ".git"}

# 路徑引用（行內程式碼）只認這些副檔名。`.md` 之間的相對連結由
# `check_markdown_links()` 另外處理——兩者的失效方式不同：前者是重構搬檔，
# 後者多半是文件結案搬出 `backlog/` 時忘了改指向
_EXTENSIONS: Tuple[str, ...] = (".py", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".json")

# 行內程式碼與 Markdown 連結目標
_INLINE_CODE: re.Pattern = re.compile(r"`([^`\n]+?)`")

# 符號引用只認 `Class.method()`：類別名開頭大寫，才有辦法與「變數.方法()」分開。
# 接收者是變數時（`risk.check()`）無從得知它的型別，判不了真假
_SYMBOL_CALL: re.Pattern = re.compile(
    r"([A-Z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\(\)"
)

# 符號檢查不掃的目錄：`backlog/` 一律引用**還不存在**的規劃符號，
# 並且會刻意引述「原本寫錯的名字」當對照。理由與 `_DELETION_EXEMPT_DIRS` 相同
_SYMBOL_EXEMPT_DIRS: Tuple[str, ...] = ("backlog/",)

# 歷史紀錄檔：當時的路徑就是那樣，改成現在的路徑反而讓紀錄失真
_HISTORICAL_DOCS: Set[str] = set()

# 敘述搬家這件事本身的句子：舊路徑是主詞，改掉會讓句子不成立
# （例如「`core/config.py` 已拆為套件」）
_NARRATIVE: Set[Tuple[str, str]] = {
    # `core/utils/order.py`（舊的 `OrderUtils`）已刪除；同名的
    # `core/utils/constant/order.py` 是常數拆分後的新檔，兩者無關
    ("backlog/實盤下單架構規劃.md", "core/utils/order.py"),
    ("backlog/PostgreSQL遷移計畫.md", "core/config.py"),
    ("backlog/index.md", "core/config.py"),
}

# 規劃中的搬家目標：`backlog/` 寫的是**搬完之後**的新路徑，現在當然指不到，
# 而 `base.py`、`__init__.py` 這種通用檔名在別處必定有同名檔，於是一律被誤判成漂移。
# **搬完之後要連同條目一起刪掉**——留著不刪，這份檢查就會對那條路徑永久失明。
_PLANNED: Set[Tuple[str, str]] = set()

# 已知待修但暫時擋住的檔案。**解除封鎖後要連同條目一起刪掉**——
# 留著不刪，這份檢查就會對那個檔案永久失明。
_PENDING: Dict[str, str] = {}

# 刪除檢查不掃的目錄：`backlog/` 大量引用規劃中與已淘汰的檔案，
# 且完成紀錄本來就會提到「刪掉了什麼」——把那些當成漂移會讓閘門被噪音鎖死
_DELETION_EXEMPT_DIRS: Tuple[str, ...] = ("backlog/",)

# 刻意敘述「這個檔案已被刪除」的句子：路徑是主詞，改掉句子就不成立
# （例如「這個主題的成品策略已於 2026-09-17 刪除（`core/…/x.py`）」）
_DELETION_NARRATIVE: Set[Tuple[str, str]] = {
    (
        "strategy_lab/strategies/tsmc_overnight_signal/README.md",
        "core/strategies/stock/overnight_lead_event_strategy.py",
    ),
}


def _deleted_paths() -> Set[str]:
    """
    - Description:
        git 歷史中曾存在、現在已不存在的檔案路徑

        判斷「刪除」只能問版本歷史：工作目錄裡沒有任何痕跡可比對。
        **拿不到歷史時回空集合而不是拋出**——這是一道輔助檢查，
        不該讓整支腳本在沒有 git 的環境（例如只解壓原始碼的映像）當掉。
    - Return:
        - Set[str]
            曾被刪除且目前仍不存在的相對路徑
    """

    try:
        result: subprocess.CompletedProcess = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--diff-filter=D",
                "--name-only",
                "--pretty=format:",
            ],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return set()

    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
        and line.strip().endswith(_EXTENSIONS)
        and not (_PROJECT_ROOT / line.strip()).exists()
    }


def _iter_markdown_files() -> List[Path]:
    """取得掃描範圍內所有 `.md`"""

    files: List[Path] = []
    for directory in _SCAN_DIRS:
        root: Path = _PROJECT_ROOT / directory
        if not root.exists():
            continue
        files.extend(
            path for path in root.rglob("*.md") if not _EXCLUDE_PARTS & set(path.parts)
        )
    files.extend(
        _PROJECT_ROOT / name for name in _SCAN_FILES if (_PROJECT_ROOT / name).exists()
    )
    return sorted(set(files))


def _iter_python_files() -> List[Path]:
    """取得掃描範圍內所有 `.py`（含 repo 根目錄那幾支入口）"""

    files: List[Path] = []
    for directory in _SCAN_DIRS:
        root: Path = _PROJECT_ROOT / directory
        if not root.exists():
            continue
        files.extend(
            path for path in root.rglob("*.py") if not _EXCLUDE_PARTS & set(path.parts)
        )
    files.extend(_PROJECT_ROOT.glob("*.py"))
    return sorted(set(files))


def _collect_real_paths() -> List[str]:
    """全 repo 中所有原始碼／設定檔的相對路徑"""

    return sorted(
        str(path.relative_to(_PROJECT_ROOT))
        for path in _PROJECT_ROOT.rglob("*")
        if path.is_file()
        and not _EXCLUDE_PARTS & set(path.parts)
        and path.suffix in _EXTENSIONS
    )


def _is_shorthand(reference: str, real_paths: List[str]) -> bool:
    """
    - Description:
        判斷這個引用是不是「真實路徑的尾段簡稱」

        文件常以 `` `report/reporter.py` `` 稱呼
        `core/backtest/report/reporter.py`——那是行文，不是壞掉的連結，
        與只寫檔名的簡稱同性質。比對必須落在**目錄邊界**上，
        否則 `ker.py` 也會被當成 `broker.py` 的簡稱。
    - Parameters:
        - reference: str
            文件裡寫的路徑
        - real_paths: List[str]
            全 repo 的真實路徑
    - Return:
        - bool
            是尾段簡稱即為 True
    """

    suffix: str = "/" + reference
    return any(real.endswith(suffix) for real in real_paths)


def _moved_to(reference: str, real_paths: List[str]) -> List[str]:
    """
    - Description:
        找出這個引用「搬到哪裡去了」

        判準是**檔名相同，且兩條路徑的父目錄集合有一邊包含另一邊**。

        只比對檔名太寬：`base.py` 全 repo 有七個，規劃文件裡的
        `providers/base.py` 會被任何一個 `base.py` 認領成「搬過去了」。
        只比對最後兩段又太窄：`core/api/futures_chip_api.py` 搬成
        `core/api/tw/futures_chip_api.py` 時，最後兩段已經不同。

        **兩個方向都要比**。只比「引用的父目錄 ⊆ 新路徑」時，僅抓得到搬得更深
        （`core/api/x.py` → `core/api/tw/x.py`）；檔案**往上搬**時引用會多出一段
        已不存在的目錄，那一段永遠不可能出現在新路徑裡，於是整條被歸進
        「指不到又無同名檔」而靜默放過——而重構把套件攤平時，往上搬才是多數。
    - Parameters:
        - reference: str
            文件裡寫的路徑
        - real_paths: List[str]
            全 repo 的真實路徑
    - Return:
        - List[str]
            實際位置；找不到時為空 list
    """

    parts: List[str] = reference.split("/")
    name: str = parts[-1]
    ancestors: Set[str] = set(parts[:-1])

    matched: List[str] = []
    for real in real_paths:
        if Path(real).name != name:
            continue
        real_ancestors: Set[str] = set(Path(real).parts[:-1])
        # 真實路徑在 repo 根目錄時 `real_ancestors` 是空集合，而空集合是任何集合的
        # 子集——不擋掉的話，根目錄放一支 `base.py` 就會被每一條 `*/base.py` 認領
        if ancestors <= real_ancestors or (
            real_ancestors and real_ancestors <= ancestors
        ):
            matched.append(real)
    return matched


def _split_into_package(reference: str) -> bool:
    """`core/config.py` → `core/config/` 這類「單檔拆成套件」的漂移"""

    candidate: Path = _PROJECT_ROOT / reference[: -len(".py")]
    return reference.endswith(".py") and candidate.is_dir()


def _extract_paths(text: str) -> Set[str]:
    """抓出一份文件裡所有「帶目錄的路徑引用」"""

    found: Set[str] = set()
    for raw in _INLINE_CODE.findall(text):
        candidate: str = raw.strip().strip("`")
        # 只寫檔名不寫目錄的簡稱不算——那是行文，不是連結
        if "/" not in candidate:
            continue
        # 命令列、含空白或萬用字元的樣式不是單一檔案引用
        if any(ch in candidate for ch in " *?{}()[]<>|"):
            continue
        if not candidate.endswith(_EXTENSIONS):
            continue
        found.add(candidate[2:] if candidate.startswith("./") else candidate)
    return found


def check_markdown_links() -> List[str]:
    """
    - Description:
        檢查 `.md` 之間的相對連結指得到檔案

        **與路徑漂移是兩回事**：漂移是「行內程式碼寫的原始碼路徑」搬過家，
        這裡是「Markdown 連結」指向的檔案不存在。後者最常見的成因是
        **文件結案後搬出 `backlog/`，而引用它的人沒改指向**——斷掉的連結
        不會有人主動發現，只能靠這道檢查。

        錨點（`#section`）只取檔案部分比對，外部網址略過。
    - Return:
        - List[str]
            `檔案: 連結` 清單；全部指得到時為空
    """

    link_pattern: re.Pattern = re.compile(r"\]\((?!https?://|#)([^)#]+)(?:#[^)]*)?\)")
    broken: List[str] = []

    for doc in _iter_markdown_files():
        rel_doc: str = str(doc.relative_to(_PROJECT_ROOT))
        for match in link_pattern.finditer(doc.read_text(encoding="utf-8")):
            target: Path = (doc.parent / match.group(1)).resolve()
            if not target.exists():
                broken.append(f"{rel_doc}: {match.group(1)}")

    return broken


def _index_classes() -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    - Description:
        以 AST 索引全庫的類別成員與基底類別

        **同名類別的成員取聯集**：同一個名字在不同檔案各有一份時（測試替身最常見），
        分不出文件講的是哪一份，取聯集才不會把存在的成員誤判成不存在。
        基底只記名字不記模組——這裡只需要「往上找得到這個成員嗎」。
    - Return:
        - Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]
            （類別名 → 自身成員名）、（類別名 → 基底類別名）
    """

    members: Dict[str, Set[str]] = {}
    bases: Dict[str, Set[str]] = {}

    for path in _iter_python_files():
        try:
            tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            own: Set[str] = members.setdefault(node.name, set())
            parents: Set[str] = bases.setdefault(node.name, set())

            for base in node.bases:
                if isinstance(base, ast.Name):
                    parents.add(base.id)
                elif isinstance(base, ast.Attribute):
                    parents.add(base.attr)

            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    own.add(sub.name)
                elif isinstance(sub, ast.AnnAssign) and isinstance(
                    sub.target, ast.Name
                ):
                    own.add(sub.target.id)
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            own.add(target.id)
                        elif isinstance(target, ast.Attribute):
                            # `self.xxx = ...`：實例屬性也算成員，否則文件提到
                            # 在 `__init__` 裡才長出來的屬性會被誤判
                            own.add(target.attr)

    return members, bases


def _resolve_members(
    class_name: str,
    members: Dict[str, Set[str]],
    bases: Dict[str, Set[str]],
    seen: Set[str],
) -> Set[str]:
    """沿基底類別往上收集所有成員名（`seen` 擋住繼承環，不然遇到環會無限遞迴）"""

    if class_name in seen:
        return set()
    seen.add(class_name)

    resolved: Set[str] = set(members.get(class_name, set()))
    for parent in bases.get(class_name, set()):
        resolved |= _resolve_members(parent, members, bases, seen)
    return resolved


def check_symbol_references() -> List[Tuple[str, str, str]]:
    """
    - Description:
        檢查反引號內的 `` `Class.method()` `` 解析得到

        **只回報「類別找得到、成員找不到」**。這與路徑檢查只回報「同名檔存在於別處」
        是同一個判準：要有正面證據才算漂移。放寬會立刻淹掉——實測全庫有 84 處
        `X()` 或 `x.y()` 形式的引用，其中絕大多數是 builtin（`print()`）、
        第三方（`pd.DataFrame()`）、規劃中還不存在的符號，或接收者是變數而非類別
        （`capital_allocator.reserve()`）。那些一律無從判斷，報出來只會讓閘門被噪音鎖死。

        `.py` 也掃：反引號在 Python 裡只出現在註解與說明字串，而**註解指向已改名的
        方法**正是最常見的一類——改名時 grep 不到反引號裡的舊名字。
    - Return:
        - List[Tuple[str, str, str]]
            （檔案、寫的符號、該類別實際有的相近成員）；全部解析得到時為空
    """

    members, bases = _index_classes()
    findings: List[Tuple[str, str, str]] = []

    for path in _iter_markdown_files() + _iter_python_files():
        rel: str = str(path.relative_to(_PROJECT_ROOT))
        if rel.startswith(_SYMBOL_EXEMPT_DIRS):
            continue

        for raw in sorted(set(_INLINE_CODE.findall(path.read_text(encoding="utf-8")))):
            matched: re.Match = _SYMBOL_CALL.fullmatch(raw.strip())
            if matched is None:
                continue

            owner, member = matched.group(1), matched.group(2)
            if owner not in members:
                continue
            if member in _resolve_members(owner, members, bases, set()):
                continue

            findings.append((rel, f"{owner}.{member}()", _hint(owner, member, members)))

    return findings


def _hint(owner: str, member: str, members: Dict[str, Set[str]]) -> str:
    """給出該類別實際有的相近成員，讓修的人不必自己再 grep 一次"""

    keywords: Set[str] = {part for part in member.split("_") if len(part) > 3}
    near: List[str] = sorted(
        name
        for name in members.get(owner, set())
        if not name.startswith("_") and keywords & set(name.split("_"))
    )
    return "、".join(f"{owner}.{name}()" for name in near[:3])


def main() -> int:
    """列出搬過家卻沒更新的路徑引用；有漂移時回非零狀態碼"""

    parser = argparse.ArgumentParser(description="文件路徑漂移檢查")
    parser.add_argument(
        "--list-unknown",
        action="store_true",
        help="另外列出指不到且全 repo 也沒有同名檔者（多為規劃中的未來檔案）",
    )
    args = parser.parse_args()

    real_paths: List[str] = _collect_real_paths()
    deleted: Set[str] = _deleted_paths()

    drifted: List[Tuple[str, str, List[str]]] = []
    unknown: List[Tuple[str, str]] = []
    pending: List[Tuple[str, str, List[str]]] = []
    removed: List[Tuple[str, str]] = []

    for doc in _iter_markdown_files():
        rel_doc: str = str(doc.relative_to(_PROJECT_ROOT))
        if rel_doc in _HISTORICAL_DOCS:
            continue

        for reference in sorted(_extract_paths(doc.read_text(encoding="utf-8"))):
            if (_PROJECT_ROOT / reference).exists():
                continue
            # 真實路徑的尾段簡稱：那是行文，不是壞掉的連結
            if _is_shorthand(reference, real_paths):
                continue
            # 敘述搬家這件事本身的句子：舊路徑是主詞，不算漂移
            if (rel_doc, reference) in _NARRATIVE:
                continue
            # 規劃中的搬家目標：新路徑還不存在，不算漂移
            if (rel_doc, reference) in _PLANNED:
                continue

            if _split_into_package(reference):
                elsewhere: List[str] = [f"{reference[:-3]}/（已拆為套件）"]
            else:
                elsewhere = _moved_to(reference, real_paths)

            if not elsewhere:
                if (
                    reference in deleted
                    and not rel_doc.startswith(_DELETION_EXEMPT_DIRS)
                    and (rel_doc, reference) not in _DELETION_NARRATIVE
                ):
                    removed.append((rel_doc, reference))
                else:
                    unknown.append((rel_doc, reference))
            elif rel_doc in _PENDING:
                pending.append((rel_doc, reference, elsewhere))
            else:
                drifted.append((rel_doc, reference, elsewhere))

    if args.list_unknown:
        print(f"指不到且無同名檔（{len(unknown)} 個，多為規劃中的未來檔案）：")
        for rel_doc, reference in unknown:
            print(f"  {rel_doc}: {reference}")
        print()

    if pending:
        print(f"已知待修、暫時擋住的（{len(pending)} 處，不計入結束碼）：")
        for rel_doc, reference, elsewhere in pending:
            print(f"  {rel_doc}")
            print(f"      寫的是 {reference}")
            print(f"      實際在 {'、'.join(elsewhere)}")
        for rel_doc, reason in _PENDING.items():
            print(f"  擋住的理由（{rel_doc}）：{reason}")
        print()

    if removed:
        print(f"指向已刪除檔案的引用（{len(removed)} 處）：")
        for rel_doc, reference in removed:
            print(f"  {rel_doc}")
            print(f"      寫的是 {reference}（git 歷史有、現在沒有）")
        print(
            "\n改指向現存的檔案，或改寫成不點名檔案。"
            "刻意要敘述『這個檔案已被刪除』時，登記到 `_DELETION_NARRATIVE`。"
        )
        return 1

    if drifted:
        print(f"搬過家卻沒更新的引用（{len(drifted)} 處）：")
        for rel_doc, reference, elsewhere in drifted:
            print(f"  {rel_doc}")
            print(f"      寫的是 {reference}")
            print(f"      實際在 {'、'.join(elsewhere)}")
        return 1

    print("搬過家卻沒更新的引用：0 處")
    print("指向已刪除檔案的引用：0 處")

    broken_links: List[str] = check_markdown_links()
    if broken_links:
        print(f"\n指不到檔案的 Markdown 連結（{len(broken_links)} 條）：")
        for item in broken_links:
            print(f"  {item}")
        print("\n最常見的成因是文件結案搬出 `backlog/` 後，引用它的人沒改指向。")
        return 1

    print("指不到檔案的 Markdown 連結：0 條")

    stale_symbols: List[Tuple[str, str, str]] = check_symbol_references()
    if stale_symbols:
        print(f"\n解析不到的符號引用（{len(stale_symbols)} 處）：")
        for rel_doc, symbol, hint in stale_symbols:
            print(f"  {rel_doc}")
            print(f"      寫的是 {symbol}（類別有定義，這個成員沒有）")
            if hint:
                print(f"      該類別實際有 {hint}")
        print("\n改成現存的名字；類別確實不再有這個成員時，連敘述一起改掉。")
        return 1

    print("解析不到的符號引用：0 處")
    return 0


if __name__ == "__main__":
    sys.exit(main())
