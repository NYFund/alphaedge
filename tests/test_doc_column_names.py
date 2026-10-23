import ast
import importlib.util
import json
import re
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterator, List, Optional, Set, Tuple

"""
文件提到的資料表欄名，必須是那張表真的有的欄位

`check_doc_paths.py` 驗檔案路徑與符號，**不驗欄名**；而欄名寫錯的文件照抄就是
`KeyError`，或更糟——策略走進 `continue` 分支安靜地不開倉。同一類錯誤已經出現兩次
（`strategy_lab/README.md` 與 `strategies/README.md` 都寫過 `成交量`，股票表實際是
`成交股數`），所以改由這裡機器檢查。

- 欄名的來源：`core/dao/` 的 `CREATE TABLE` 欄位定義，加上清洗器
  `core/pipeline/**/*_cleaned_columns.json`（月營收與財報的欄位由清洗結果決定，
  DAO 裡沒有寫死）。**不連資料庫**，CI 也能跑。
- 文件裡被檢查的寫法：「回傳 DataFrame，包含欄位：」後的清單，以及 python 程式碼區塊
  與行內程式碼裡的 `x["中文欄名"]`（賦值建立新欄的不算）。只查含中文的欄名。
- **按段落對到表**：標題含 API 類別名（`### StockPriceAPI`）時，該段落的欄名只拿那支
  API 讀的表比對；其他段落拿全庫欄名的聯集。不分表的話 `成交量` 會因為期貨日 K 有這欄
  而被放過，正好漏掉最常見的那個錯。
- 待辦規劃目錄（`_PLANNING_DIR`）不掃：規劃與審查紀錄會刻意引述「原本寫錯的名字」當對照。
"""

_REPO: Path = Path(__file__).resolve().parent.parent

_CJK: re.Pattern = re.compile(r"[一-鿿]")
# `CREATE TABLE` 內一行一欄的定義：`"欄名" TYPE`
_COLUMN_DEF: re.Pattern = re.compile(
    r'^\s*"([^"]+)"\s+(?:TEXT|INT|INTEGER|REAL|FLOAT|NUMERIC|BIGINT|DATE)\b', re.M
)
_TABLE_CONST: re.Pattern = re.compile(r"\b([A-Z][A-Z0-9_]*_TABLE_NAME)\b")
_DAO_CLASS: re.Pattern = re.compile(r"^class ([A-Z]\w*DAO)\b", re.M)
_DAO_REF: re.Pattern = re.compile(r"\b([A-Z]\w*DAO)\b")
_API_CLASS: re.Pattern = re.compile(r"^class ([A-Z]\w*API)\b", re.M)
_HEADING: re.Pattern = re.compile(r"^#{1,6} ")
_SUBSCRIPT: re.Pattern = re.compile(
    r"[A-Za-z_]\w*\[\s*[\"']([^\"'\]]*[一-鿿][^\"'\]]*)[\"']\s*\]"
)
_INLINE_CODE: re.Pattern = re.compile(r"`([^`]+)`")
_COLUMN_LIST_MARK: str = "包含欄位："
_CLEANED_SUFFIX: str = "_cleaned_columns.json"
_PYTHON_FENCES: Set[str] = {"python", "py"}
# 待辦規劃文件的目錄：裡面引用的欄名是對照用的錯誤範例，不是用法
_PLANNING_DIR: str = "backlog"

# 一筆文件引用：(行號, 段落所屬的 API 或 None, 欄名)
Reference = Tuple[int, Optional[str], str]


def _table_names() -> Dict[str, str]:
    """`core/config/schema.py` 的 `*_TABLE_NAME` 常數 → 表名"""

    tree: ast.Module = ast.parse(
        (_REPO / "core" / "config" / "schema.py").read_text(encoding="utf-8")
    )
    names: Dict[str, str] = {}
    for node in tree.body:
        target: Optional[ast.expr] = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value: Optional[ast.expr] = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id.endswith("_TABLE_NAME")
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            names[target.id] = value.value
    return names


def _table_columns() -> Dict[str, Set[str]]:
    """表名 → 欄名集合（DAO 的建表定義＋清洗器的欄位清單）"""

    names: Dict[str, str] = _table_names()
    columns: Dict[str, Set[str]] = defaultdict(set)

    for path in sorted((_REPO / "core" / "dao").rglob("*.py")):
        text: str = path.read_text(encoding="utf-8")
        defined: Set[str] = set(_COLUMN_DEF.findall(text))
        if not defined:
            continue
        for const in set(_TABLE_CONST.findall(text)):
            if const in names:
                columns[names[const]] |= defined

    tables: List[str] = sorted(set(names.values()), key=len, reverse=True)
    for path in sorted((_REPO / "core" / "pipeline").rglob(f"*{_CLEANED_SUFFIX}")):
        stem: str = path.name[: -len(_CLEANED_SUFFIX)]
        # 清洗器的檔名是表名或表名加後綴（`monthly_revenue_report` → `monthly_revenue`）
        table: Optional[str] = next(
            (t for t in tables if stem == t or stem.startswith(t + "_")), None
        )
        if table is not None:
            columns[table] |= set(json.loads(path.read_text(encoding="utf-8")))

    return dict(columns)


def _api_tables() -> Dict[str, Set[str]]:
    """API 類別 → 它經由 DAO 讀的表"""

    names: Dict[str, str] = _table_names()
    dao_tables: Dict[str, Set[str]] = {}
    for path in sorted((_REPO / "core" / "dao").rglob("*.py")):
        text: str = path.read_text(encoding="utf-8")
        tables: Set[str] = {
            names[const] for const in _TABLE_CONST.findall(text) if const in names
        }
        for dao in _DAO_CLASS.findall(text):
            dao_tables[dao] = tables

    api_tables: Dict[str, Set[str]] = {}
    for path in sorted((_REPO / "core" / "api").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tables = set()
        for dao in set(_DAO_REF.findall(text)):
            tables |= dao_tables.get(dao, set())
        for api in _API_CLASS.findall(text):
            api_tables[api] = tables
    return api_tables


def _column_list(line: str) -> List[str]:
    """「包含欄位：」之後的中文欄名；尾端的「等」不是欄名的一部分"""

    rest: str = line.split(_COLUMN_LIST_MARK, 1)[1]
    found: List[str] = []
    for token in re.split(r"[,，、]", rest):
        name: str = token.strip().removesuffix("等").strip()
        if _CJK.search(name):
            found.append(name)
    return found


def _subscripts(segment: str) -> List[str]:
    """`x["中文"]` 形式的欄名；後面接 `=`（建立新欄）的不算"""

    found: List[str] = []
    for match in _SUBSCRIPT.finditer(segment):
        if re.match(r"\s*=(?!=)", segment[match.end() :]):
            continue
        found.append(match.group(1))
    return found


def _references(text: str, api_names: Set[str]) -> Iterator[Reference]:
    """逐行找出文件裡的欄名引用，並標上所在段落的 API"""

    api: Optional[str] = None
    fence: Optional[str] = None  # 目前所在程式碼區塊的語言；None 表示不在區塊內

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped: str = line.strip()
        if stripped.startswith("```"):
            fence = None if fence is not None else stripped[3:].strip().lower()
            continue

        if fence is None and _HEADING.match(line):
            api = next(
                (name for name in api_names if re.search(rf"\b{name}\b", line)), None
            )
            continue

        if _COLUMN_LIST_MARK in line and (fence is None or fence in _PYTHON_FENCES):
            for name in _column_list(line):
                yield lineno, api, name

        if fence is None:
            segments: List[str] = _INLINE_CODE.findall(line)
        elif fence in _PYTHON_FENCES:
            segments = [line]
        else:
            segments = []  # mermaid 等圖表的節點標籤也是 `X["…"]`，不是欄名
        for segment in segments:
            for name in _subscripts(segment):
                yield lineno, api, name


def _violations(
    rel_doc: str,
    text: str,
    columns: Dict[str, Set[str]],
    api_tables: Dict[str, Set[str]],
) -> List[str]:
    """一份文件裡指不到欄位的引用"""

    known: Set[str] = set().union(*columns.values())
    problems: List[str] = []
    for lineno, api, name in _references(text, set(api_tables)):
        if api is None:
            if name not in known:
                problems.append(f"{rel_doc}:{lineno} {name}（全庫沒有這個欄位）")
            continue

        tables: Set[str] = api_tables[api]
        allowed: Set[str] = set().union(*(columns.get(t, set()) for t in tables))
        if not allowed:
            problems.append(
                f"{rel_doc}:{lineno} {name}（{api} 讀的表 {sorted(tables)} "
                "找不到欄位定義，無從比對）"
            )
        elif name not in allowed:
            problems.append(f"{rel_doc}:{lineno} {name}（{api} 讀的表沒有這個欄位）")
    return problems


def _documents() -> List[Path]:
    """與 `check_doc_paths.py` 同一份掃描範圍，扣掉待辦規劃目錄"""

    spec = importlib.util.spec_from_file_location(
        "check_doc_paths", _REPO / "scripts" / "check_doc_paths.py"
    )
    checker: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return [
        path
        for path in checker._iter_markdown_files()
        if path.relative_to(_REPO).parts[0] != _PLANNING_DIR
    ]


def test_column_index_covers_the_documented_tables() -> None:
    """欄名索引本身不得落空：建表定義或清洗器搬家時，下面的比對會變成什麼都放過"""

    columns: Dict[str, Set[str]] = _table_columns()
    api_tables: Dict[str, Set[str]] = _api_tables()

    assert "成交股數" in columns["price"]
    assert "外資買賣超股數" in columns["chip"]
    assert "當月營收" in columns["monthly_revenue"]
    assert api_tables["StockPriceAPI"] == {"price"}
    assert api_tables["StockChipAPI"] == {"chip"}
    assert api_tables["MonthlyRevenueReportAPI"] == {"monthly_revenue"}


def test_scan_finds_the_api_sections() -> None:
    """掃描本身不得落空：三個 API 段落的「包含欄位」清單都要被找到"""

    api_tables: Dict[str, Set[str]] = _api_tables()
    found: Set[Optional[str]] = set()
    for path in _documents():
        for _, api, _ in _references(path.read_text(encoding="utf-8"), set(api_tables)):
            found.add(api)

    assert {"StockPriceAPI", "StockChipAPI", "MonthlyRevenueReportAPI"} <= found


def test_documented_columns_exist() -> None:
    """文件提到的中文欄名都指得到真實欄位"""

    columns: Dict[str, Set[str]] = _table_columns()
    api_tables: Dict[str, Set[str]] = _api_tables()
    problems: List[str] = []
    for path in _documents():
        problems.extend(
            _violations(
                str(path.relative_to(_REPO)),
                path.read_text(encoding="utf-8"),
                columns,
                api_tables,
            )
        )

    assert problems == [], "文件寫了不存在的欄名：\n" + "\n".join(problems)


def test_wrong_column_in_an_api_section_is_caught() -> None:
    """
    股票價格段落寫 `成交量` 要紅

    `成交量` 是期貨日 K 的欄位，拿全庫聯集比對會被放過；要按段落對到 `price` 表才抓得到。
    """

    columns: Dict[str, Set[str]] = _table_columns()
    api_tables: Dict[str, Set[str]] = _api_tables()
    doc: str = "\n".join(
        [
            "### StockPriceAPI - 日線價格資料",
            "```python",
            'volume = prices["成交量"]',
            'shares = prices["成交股數"]',
            "# 回傳 DataFrame，包含欄位：stock_id, 收盤價, 成交量等",
            'prices["漲幅"] = prices["收盤價"].pct_change()',
            "```",
            "```mermaid",
            'A["實盤引擎"] --> B["回測引擎"]',
            "```",
        ]
    )

    problems: List[str] = _violations("fake.md", doc, columns, api_tables)

    assert len(problems) == 2, problems
    assert all("成交量" in problem for problem in problems)
