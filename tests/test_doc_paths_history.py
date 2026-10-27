import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Set

import pytest

"""
文件路徑檢查的「已刪除檔案」判定：拿不到完整 git 歷史時要明講，不可當成沒有刪除

shallow clone 裡 `git log --diff-filter=D` 照樣成功，只是只看得到最後幾個 commit，
於是刪除檢查查到 0 筆、結束碼 0——看起來跑過，其實沒跑。CI 的 `actions/checkout`
預設就是 depth 1。
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_checker() -> ModuleType:
    """以檔案路徑載入檢查腳本（`scripts/` 不是套件）"""

    spec = importlib.util.spec_from_file_location(
        "check_doc_paths", _PROJECT_ROOT / "scripts" / "check_doc_paths.py"
    )
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> None:
    """在指定目錄執行 git；失敗就讓測試直接炸"""

    command: List[str] = [
        "git",
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        *args,
    ]
    subprocess.run(command, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_deletion(tmp_path: Path) -> Path:
    """兩個 commit 的 repo：第一個加入 `old.py`，第二個刪掉它"""

    origin: Path = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    (origin / "old.py").write_text("x = 1\n", encoding="utf-8")
    (origin / "keep.py").write_text("y = 1\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "add")
    _git(origin, "rm", "-q", "old.py")
    _git(origin, "commit", "-q", "-m", "delete")
    return origin


def _deleted_in(clone: Path, monkeypatch: pytest.MonkeyPatch) -> Optional[Set[str]]:
    """以 `clone` 當專案根目錄呼叫 `_deleted_paths()`"""

    checker: ModuleType = _load_checker()
    monkeypatch.setattr(checker, "_PROJECT_ROOT", clone)
    return checker._deleted_paths()


def test_full_history_finds_the_deleted_file(
    repo_with_deletion: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整歷史查得到被刪掉的檔案：對照組，證明下一條的 None 不是因為根本查不到"""

    clone: Path = tmp_path / "full"
    _git(tmp_path, "clone", "-q", f"file://{repo_with_deletion}", str(clone))

    assert _deleted_in(clone, monkeypatch) == {"old.py"}


def test_shallow_clone_is_reported_not_treated_as_no_deletion(
    repo_with_deletion: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shallow clone 回 None，由 `main()` 判紅；回空集合就是假綠燈"""

    clone: Path = tmp_path / "shallow"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        f"file://{repo_with_deletion}",
        str(clone),
    )

    assert _deleted_in(clone, monkeypatch) is None


def test_no_git_keeps_the_lenient_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不是 git repo（只解壓原始碼的映像）時維持回空集合，不讓整支腳本當掉"""

    assert _deleted_in(tmp_path, monkeypatch) == set()
