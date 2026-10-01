import ast
from pathlib import Path
from typing import List

"""
測試與腳本以子行程呼叫 `uv run` 時一律帶 `--no-sync`

**不帶的話，跑一次測試就會改掉開發環境**：`uv run` 會先把 venv 同步成預設相依，
順手移除 `uv sync --extra frontend --extra lab` 裝的 extras。2026-10-02 在主目錄跑完整測試後，
`streamlit` 等套件被移除，前端冒煙測試從此改為略過；跑到一半被移除的那一次還有兩條測試失敗。
主目錄又是排程直接執行的環境，測試不可以有這種副作用。
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_SCANNED_DIRS: List[str] = ["tests", "scripts"]


def find_uv_run_without_no_sync(path: Path) -> List[int]:
    """回傳檔案中「以 `uv`、`run` 開頭卻沒有 `--no-sync`」的串列字面值所在行號"""

    tree: ast.AST = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
            continue
        values: List[object] = [
            elt.value if isinstance(elt, ast.Constant) else None for elt in node.elts
        ]
        if values[:2] == ["uv", "run"] and "--no-sync" not in values:
            lines.append(node.lineno)
    return lines


def test_subprocess_uv_run_always_passes_no_sync() -> None:
    """掃 `tests/`、`scripts/` 的每一個 `["uv", "run", ...]`"""

    offenders: List[str] = []
    for directory in _SCANNED_DIRS:
        for path in sorted((_PROJECT_ROOT / directory).rglob("*.py")):
            # 本檔自己的比對字面值 `["uv", "run"]` 不是子行程呼叫
            if path == Path(__file__).resolve():
                continue
            for line in find_uv_run_without_no_sync(path):
                offenders.append(f"{path.relative_to(_PROJECT_ROOT)}:{line}")

    assert offenders == [], f"子行程的 `uv run` 沒帶 --no-sync：{offenders}"


def test_scanner_catches_a_missing_flag(tmp_path: Path) -> None:
    """對照組：沒帶旗標的寫法要被抓到，否則上一條可能只是掃描失效"""

    sample: Path = tmp_path / "sample.py"
    sample.write_text(
        'import subprocess\nsubprocess.run(["uv", "run", "ruff", "check"])\n'
        'subprocess.run(["uv", "run", "--no-sync", "ruff"])\n',
        encoding="utf-8",
    )

    assert find_uv_run_without_no_sync(sample) == [2]
