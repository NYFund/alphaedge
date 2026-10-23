import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List

import pytest

"""
掃描型檢查一律自驗：掃描範圍不存在或掃到 0 筆，結束碼必須非零

`scripts/check_*.py` 都是「掃一批檔案、找到違規才回非零」的形狀，於是有一個共同的盲點：
**掃描範圍本身消失時，什麼都找不到，結果就是通過**。目錄改名、拆檔、搬家都會讓某一塊
檢查靜默失效，而 CI 一直是綠的。這裡把每一支腳本複製到空的假專案（腳本的專案根目錄
由 `__file__` 推得），等於讓它掃一個什麼都沒有的 repo——必須以 1 結束。

**新寫的掃描腳本自動被納入**：清單取自 `scripts/check_*.py` 的 glob，不靠人登記。
需要額外參數才能指向「不存在的目標」的腳本才登記在 `_EMPTY_TARGET_ARGS`。
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_SCRIPTS: List[Path] = sorted((_PROJECT_ROOT / "scripts").glob("check_*.py"))

# 掃描目標不是專案目錄、而是由參數指定的腳本：給它一個不存在的目標。
# `{root}` 會換成假專案的根目錄
_EMPTY_TARGET_ARGS: Dict[str, List[str]] = {
    "check_overnight_positions.py": ["--db", "{root}/missing.db"],
}


def _load(name: str) -> ModuleType:
    """以檔案路徑載入檢查腳本（`scripts/` 不是套件）"""

    spec = importlib.util.spec_from_file_location(
        name, _PROJECT_ROOT / "scripts" / f"{name}.py"
    )
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_guard_itself_found_the_scripts() -> None:
    """護欄本身也是掃描：glob 落空的話下面的參數化測試一條都不會跑，照樣全綠"""

    assert len(_SCRIPTS) >= 4
    for name in _EMPTY_TARGET_ARGS:
        assert (_PROJECT_ROOT / "scripts" / name).is_file(), name


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda path: path.name)
def test_every_check_script_fails_on_an_empty_project(
    script: Path, tmp_path: Path
) -> None:
    """
    每一支 `scripts/check_*.py` 掃空專案都要以 1 結束

    **只接受 1**：2 是 argparse 的用法錯誤（新腳本要參數卻沒登記），
    traceback 是崩潰而不是判定——兩者都不代表腳本「知道自己沒掃到東西」。
    """

    copied: Path = tmp_path / "scripts" / script.name
    copied.parent.mkdir()
    shutil.copy(script, copied)
    args: List[str] = [
        arg.format(root=tmp_path) for arg in _EMPTY_TARGET_ARGS.get(script.name, [])
    ]

    result: subprocess.CompletedProcess = subprocess.run(
        [sys.executable, str(copied), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, result.stdout + result.stderr


def test_missing_engine_file_is_a_violation() -> None:
    """
    `_ENGINE_FILES` 列了不存在的檔案要算違規

    拆 `core/live/trader.py` 之後，原本的引擎檔不在了；只略過的話市場語意洩漏檢查
    從此不掃實盤引擎，而結束碼照樣是 0。
    """

    checker: ModuleType = _load("check_layer_deps")
    checker._ENGINE_FILES = checker._ENGINE_FILES + ("core/live/no_such_engine.py",)

    problems: List[str] = checker.check_scan_targets(checker.collect_files())

    assert any("core/live/no_such_engine.py" in problem for problem in problems)


def test_real_scan_targets_are_complete() -> None:
    """現況的宣告都指得到東西：上一條的違規來自假檔，不是本來就紅"""

    checker: ModuleType = _load("check_layer_deps")

    assert checker.check_scan_targets(checker.collect_files()) == []


def test_missing_api_dir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`core/api/` 搬家後，無主介面檢查不可掃到 0 個方法還回 0"""

    checker: ModuleType = _load("check_api_orphan_methods")
    monkeypatch.setattr(checker, "_API_DIR", tmp_path / "no_such_api")
    monkeypatch.setattr(sys, "argv", ["check_api_orphan_methods.py"])

    assert checker.main() == 1
    assert "no_such_api" in capsys.readouterr().out
