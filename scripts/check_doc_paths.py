import argparse
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
沒有同名檔就落進「多半是規劃中的未來檔案」那一堆。實測曾有兩份 README 指向
一支已刪除的策略檔而無人察覺，故另以 git 歷史判斷：指不到、全 repo 無同名檔、
**但歷史上存在過**，那就是刪除造成的懸空引用。

- Features:
    1. 抓行內程式碼（`` `core/xxx/yyy.py` ``）與 Markdown 連結中的帶目錄路徑
    2. 指得到就跳過；指不到但全 repo 有同名檔即回報，並列出實際位置
    3. 指不到、無同名檔、但 git 歷史存在過 → 回報為「指向已刪除的檔案」
    4. 只寫檔名不寫目錄的簡稱（`` `factory.py` ``）不算——那是行文，不是連結
- 使用場景:
    python scripts/check_doc_paths.py           # 有漂移時以非零狀態碼結束
    python scripts/check_doc_paths.py --list-unknown  # 另列指不到且無同名檔者
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 掃描哪些 `.md`
_SCAN_DIRS: Tuple[str, ...] = (
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

# 歷史紀錄檔：當時的路徑就是那樣，改成現在的路徑反而讓紀錄失真
_HISTORICAL_DOCS: Set[str] = set()

# 敘述搬家這件事本身的句子：舊路徑是主詞，改掉會讓句子不成立
# （例如「`core/config.py` 已拆為套件」）
_NARRATIVE: Set[Tuple[str, str]] = {
    ("backlog/PostgreSQL遷移計畫.md", "core/config.py"),
    ("backlog/index.md", "core/config.py"),
    ("backlog/架構重構與冗餘收斂.md", "core/config.py"),
}

# 規劃中的搬家目標：`backlog/` 寫的是**搬完之後**的新路徑，現在當然指不到，
# 而 `base.py`、`__init__.py` 這種通用檔名在別處必定有同名檔，於是一律被誤判成漂移。
# **搬完之後要連同條目一起刪掉**——留著不刪，這份檢查就會對那條路徑永久失明。
_PLANNED: Set[Tuple[str, str]] = {
    ("backlog/架構重構與冗餘收斂.md", "core/datafeed/__init__.py"),
    ("backlog/架構重構與冗餘收斂.md", "core/datafeed/base.py"),
}

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

        判準是**最後兩段相同**（父目錄 ＋ 檔名）：`core/api/futures_chip_api.py`
        搬成 `core/api/tw/futures_chip_api.py`，兩者的最後兩段是
        `api/futures_chip_api.py` 與 `tw/futures_chip_api.py`——不相同，
        故改以「檔名相同且引用的父目錄仍是新路徑的一段」收斂。

        只比對檔名太寬：`base.py` 全 repo 有七個，
        `backlog/美股ETL與回測架構規劃.md` 的 `providers/base.py`
        是規劃中的未來檔案，不該被判成漂移。
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

    return [
        real
        for real in real_paths
        if Path(real).name == name and ancestors <= set(Path(real).parts[:-1])
    ]


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
        **文件結案後搬出 `backlog/`，而引用它的人沒改指向**——實測曾有
        一份文件結案刪除後，引用它的連結斷了九天沒人發現。

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
