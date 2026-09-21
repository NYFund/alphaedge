import ast
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

import pytest

from core.config import settings
from core.config.paths import PROJECT_ROOT
from core.config.settings import NUM_API

"""
設定檔要跟程式對得上：`.env.example`、pyproject、uv.lock

這三份檔案都不會被執行，漂移了也不會有任何錯誤：

- `.env.example` 少列鍵：新人照範本設定後，那個功能安靜地拿到 None。
- per-file-ignores 指到搬走的路徑：豁免形同失效，設定卻看起來還在
  （`stock_tick_loader.py` 從 `core/pipeline/loaders/` 搬進 `tw/` 之後就是這樣）。
- 改了 pyproject 的相依卻沒重新 `uv lock`：本機 `uv sync` 會悄悄重新解析，
  裝到的是沒驗證過的版本；Docker 映像以 `--frozen` 照舊 lock 裝，根本沒裝到新相依。
  CI 的 `uv sync --locked` 也擋得下，但要推上去才知道，本檔讓 pytest 當場就紅。

本檔不連網路、不需要資料庫。
"""


# 掃描範圍：執行期會讀環境變數的程式碼。tests/ 刻意不掃——測試會自行設定環境變數
ENV_SCAN_PATHS: List[str] = ["core", "tasks", "frontend", "scripts", "run.py"]

# 以字面值當第一個參數讀環境變數的函式：標準函式庫與專案內的兩個包裝
ENV_READER_NAMES: Set[str] = {"getenv", "get_env_path", "get_int_env"}

# 範本有列、但程式不直接讀的鍵（鍵 → 理由）
ENV_EXAMPLE_ONLY_KEYS: Dict[str, str] = {}

# 範本裡「鍵=值」的行，選填鍵以 `# KEY=` 的註解形式列出，一併計入
ENV_EXAMPLE_KEY_PATTERN: re.Pattern = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


def iter_scanned_python_files() -> Iterator[Path]:
    """掃描範圍內的所有 `.py` 檔"""

    for entry in ENV_SCAN_PATHS:
        path: Path = PROJECT_ROOT / entry
        if path.is_file():
            yield path
        else:
            yield from path.rglob("*.py")


def is_str_constant(node: ast.AST) -> bool:
    """節點是否為字串字面值"""

    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def collect_env_keys_read_by_code() -> Set[str]:
    """
    - Description:
        以 AST 找出程式會讀的環境變數鍵名

        認得四種寫法：
        1. `os.getenv("X")`、`get_env_path("X", ...)`、`get_int_env("X")`
        2. `os.environ.get("X")`
        3. `os.environ["X"]`
        4. 名稱以 `_ENV_VAR` 結尾的字串常數（例如前端的 `RESULTS_ENV_VAR`），
           這類鍵名是先存成常數、再由別處讀取

        多帳號金鑰以 f-string 組出鍵名，AST 取不到字面值，故依 `NUM_API` 展開。
    - Return:
        - Set[str]
            鍵名集合
    """

    keys: Set[str] = set()

    for file_path in iter_scanned_python_files():
        tree: ast.Module = ast.parse(file_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and node.args
                and is_str_constant(node.args[0])
            ):
                func: ast.AST = node.func
                name: Any = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                is_environ_get: bool = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
                if name in ENV_READER_NAMES or is_environ_get:
                    keys.add(node.args[0].value)

            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and is_str_constant(node.slice)
            ):
                keys.add(node.slice.value)

            elif (
                isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            ):
                targets: List[ast.AST] = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if is_str_constant(node.value) and any(
                    isinstance(target, ast.Name) and target.id.endswith("_ENV_VAR")
                    for target in targets
                ):
                    keys.add(node.value.value)

    for index in range(1, NUM_API + 1):
        keys |= {f"API_KEY_{index}", f"API_SECRET_KEY_{index}"}

    return keys


def collect_env_example_keys() -> Set[str]:
    """`.env.example` 列出的鍵名（含以註解形式列出的選填鍵）"""

    lines: List[str] = (
        (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    )
    return {
        match.group(1)
        for line in lines
        if (match := ENV_EXAMPLE_KEY_PATTERN.match(line.strip())) is not None
    }


def load_pyproject() -> Dict[str, Any]:
    """讀取 pyproject.toml"""

    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def canonical_name(requirement: str) -> str:
    """套件名正規化（PEP 503）：大小寫與 `-`／`_`／`.` 視為相同，去掉版本與 extras"""

    name: str = re.split(r"[<>=!~;\[\s]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


# === .env.example ===
def test_env_scanner_recognizes_every_reader_form() -> None:
    """
    掃描規則要真的抓得到專案現有的每一種讀法

    規則寫錯而永遠不命中時，下面兩條雙向核對會一起安靜地通過——護欄就是空殼。
    """

    keys: Set[str] = collect_env_keys_read_by_code()

    assert {
        "FINMIND_API_TOKEN",  # os.getenv
        "DDB_PORT",  # get_int_env
        "ALPHAEDGE_DATA_DIR",  # get_env_path
        "ALPHAEDGE_SHOW_FIGURES",  # 帶型別標註的 *_ENV_VAR 常數
        "ALPHAEDGE_BACKTEST_RESULTS",  # 不帶型別標註的 *_ENV_VAR 常數
    } <= keys


def test_env_example_covers_every_key_the_code_reads() -> None:
    """程式會讀的鍵都要列在範本裡，否則照範本設定的人會安靜地拿到 None"""

    missing: List[str] = sorted(
        collect_env_keys_read_by_code() - collect_env_example_keys()
    )

    assert missing == [], f".env.example 缺少程式會讀的鍵：{missing}"


def test_env_example_has_no_key_the_code_never_reads() -> None:
    """範本不留程式已經不讀的鍵；例外必須列在 `ENV_EXAMPLE_ONLY_KEYS` 並註明理由"""

    code_keys: Set[str] = collect_env_keys_read_by_code()
    stale: List[str] = sorted(
        collect_env_example_keys() - code_keys - set(ENV_EXAMPLE_ONLY_KEYS)
    )

    assert stale == [], f".env.example 列了程式不讀的鍵：{stale}"
    # 例外清單本身也要誠實：程式開始讀之後就該從清單移除
    assert set(ENV_EXAMPLE_ONLY_KEYS) & code_keys == set()


# === pyproject.toml ===
def test_per_file_ignores_point_to_existing_paths() -> None:
    """
    per-file-ignores 的每條路徑都要存在

    不含 `/` 的條目（例如 `__init__.py`）是任意目錄都適用的檔名樣式，不檢查。
    """

    patterns: Dict[str, Any] = load_pyproject()["tool"]["ruff"]["lint"][
        "per-file-ignores"
    ]
    missing: List[str] = [
        pattern
        for pattern in patterns
        if "/" in pattern and not any(PROJECT_ROOT.glob(pattern))
    ]

    assert missing == [], f"per-file-ignores 指到不存在的路徑：{missing}"


# === uv.lock ===
def normalize_requirement(requirement: str) -> str:
    """把相依字串正規化成「名稱＋版本條件」，去掉空白，便於與 uv.lock 比對"""

    name: str = canonical_name(requirement)
    specifier: str = re.split(r"[<>=!~]", requirement, maxsplit=1)[0]
    return name + requirement[len(specifier) :].split(";")[0].replace(" ", "")


def test_uv_lock_matches_pyproject_dependencies() -> None:
    """
    uv.lock 記錄的本專案相依，必須與 pyproject 的主相依與各 extra 完全一致

    uv.lock 的本專案條目（`[package.metadata]` 的 `requires-dist`）是鎖定當下
    pyproject 宣告的相依；兩邊對不上，代表改了 pyproject 卻沒重新 `uv lock`。
    """

    pyproject: Dict[str, Any] = load_pyproject()["project"]
    declared: Set[str] = {
        normalize_requirement(requirement) for requirement in pyproject["dependencies"]
    }
    for extra, requirements in pyproject["optional-dependencies"].items():
        declared |= {
            f"{normalize_requirement(requirement)}[{extra}]"
            for requirement in requirements
        }

    lock: Dict[str, Any] = tomllib.loads(
        (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    )
    root: Dict[str, Any] = next(
        package for package in lock["package"] if package["name"] == pyproject["name"]
    )
    locked: Set[str] = set()
    for entry in root["metadata"]["requires-dist"]:
        requirement: str = canonical_name(entry["name"]) + entry.get("specifier", "")
        extra_match: Optional[re.Match] = re.fullmatch(
            r"extra == '(.+)'", entry.get("marker", "")
        )
        locked.add(
            f"{requirement}[{extra_match.group(1)}]" if extra_match else requirement
        )

    assert declared - locked == set(), (
        f"pyproject 有、uv.lock 沒有（請執行 `uv lock`）：{sorted(declared - locked)}"
    )
    assert locked - declared == set(), (
        f"uv.lock 有、pyproject 沒有（請執行 `uv lock`）：{sorted(locked - declared)}"
    )


# === 實盤設定 ===
def test_no_environment_variable_can_select_production_trading() -> None:
    """
    **正式環境不可由環境變數開啟**，只能由命令列 `--confirm-production`

    `.env` 的設定會留在機器上。一旦有一個「連正式環境」的鍵存在，
    某天排程就會默默連上去下真單，而那一刻沒有任何人在看。
    """

    suspicious: List[str] = sorted(
        key
        for key in collect_env_keys_read_by_code() | collect_env_example_keys()
        if "PRODUCTION" in key and "RESULTS" not in key
    )

    assert suspicious == [], f"出現可由環境變數切到正式環境的鍵：{suspicious}"


def test_require_shioaji_ca_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """憑證缺值時當場拋出，不回傳 None 讓 `activate_ca()` 拿到空路徑"""

    monkeypatch.setattr(settings, "SHIOAJI_CA_PATH", None)
    monkeypatch.setattr(settings, "SHIOAJI_CA_PASSWORD", None)

    with pytest.raises(RuntimeError, match="SHIOAJI_CA_PATH"):
        settings.require_shioaji_ca()


def test_require_shioaji_ca_raises_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """路徑有設定但檔案不在時也要拋出：錯字造成的缺檔要在下單前就現形"""

    monkeypatch.setattr(settings, "SHIOAJI_CA_PATH", str(tmp_path / "nope.pfx"))
    monkeypatch.setattr(settings, "SHIOAJI_CA_PASSWORD", "x")

    with pytest.raises(RuntimeError, match="不存在"):
        settings.require_shioaji_ca()


def test_live_timezone_is_taipei_and_aware() -> None:
    """
    實盤時間一律是台北時區的 aware datetime

    主機時區是 UTC 而程式讀 naive 的 `datetime.now()` 時，尾盤段會整段跑在
    錯的時刻，而且不會有任何錯誤訊息。
    """

    now = settings.now_live()

    assert settings.LIVE_TIMEZONE_NAME == "Asia/Taipei"
    assert now.tzinfo is not None
    assert now.utcoffset() is not None
