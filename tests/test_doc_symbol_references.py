import importlib.util
import sys
import types
from pathlib import Path
from typing import Dict, List, Set, Tuple

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""
`check_doc_paths.py` 的符號解析掃描要真的抓得到東西

這道掃描本身就是一道護欄，而護欄最常見的死法是**悄悄不再命中任何東西**：
索引抽不到類別、正規表達式改壞、或掃描範圍縮掉，結果全部變成「0 處」，
看起來比修好還漂亮。

所以這裡驗的是三件事，缺一不可：

1. **索引抽得到**：全庫的類別數與成員數在合理量級。
2. **判準會命中**：對合成的類別與合成的文件，寫錯的成員名一定被抓到。
3. **判準不誤報**：寫對的名字、繼承來的成員、變數當接收者的呼叫都不可被抓。

第 3 條與第 2 條一樣重要。誤報會讓人開始在 `# noqa` 心態下繞過閘門，
最後這道檢查的下場與完全失效相同。
"""


def _load_checker() -> types.ModuleType:
    """以檔案路徑載入閘門腳本（`scripts/` 不是套件，無法 import）"""

    path: Path = _PROJECT_ROOT / "scripts" / "check_doc_paths.py"
    spec = importlib.util.spec_from_file_location("check_doc_paths_under_test", path)
    module: types.ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_class_index_actually_finds_classes() -> None:
    """
    先確認索引抽得到類別與成員

    抽不到的話 `owner not in members` 對每一個引用都成立，整道掃描會靜默地
    永遠回報 0 處——而那正是它要防的那種失效。
    """

    members, bases = _load_checker()._index_classes()

    assert len(members) >= 300, f"只索引到 {len(members)} 個類別，AST 走訪可能已失效"
    assert "BaseDataUpdater" in members
    assert "clean_one" in members["BaseDataUpdater"]
    # 繼承關係也要抽得到，否則子類別引用基底的方法會被誤判成不存在
    assert bases.get("DailyTwoMarketUpdater") == {"BaseDataUpdater"}


def test_a_renamed_member_is_detected(tmp_path: Path) -> None:
    """
    寫錯的成員名一定要被抓到

    以合成的類別與合成的文件驗判準本身，不依賴全庫現況——全庫修乾淨之後，
    「0 處」就再也證明不了這道掃描還活著。
    """

    checker: types.ModuleType = _load_checker()
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def clean_one(self) -> None:\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "guide.md").write_text(
        "呼叫 `Widget.clean_one_day()` 即可。\n", encoding="utf-8"
    )

    checker._PROJECT_ROOT = tmp_path
    checker._SCAN_DIRS = (".",)

    findings: List[Tuple[str, str, str]] = checker.check_symbol_references()

    assert [item[1] for item in findings] == ["Widget.clean_one_day()"], (
        f"寫錯的成員名沒被抓到：{findings}"
    )
    # 提示要指出真正的名字，否則修的人還得自己 grep 一次
    assert "Widget.clean_one()" in findings[0][2]


def test_valid_references_are_not_reported(tmp_path: Path) -> None:
    """
    寫對的、繼承來的、接收者是變數的，一律不可被抓

    誤報比漏報更傷：一旦閘門開始報假的，下一步就是有人把它關掉。
    """

    checker: types.ModuleType = _load_checker()
    (tmp_path / "widget.py").write_text(
        "class Base:\n"
        "    def shared(self) -> None:\n"
        "        pass\n"
        "\n"
        "\n"
        "class Widget(Base):\n"
        "    def own(self) -> None:\n"
        "        self.later = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "guide.md").write_text(
        "自己的 `Widget.own()`、繼承的 `Widget.shared()`、"
        "`__init__` 才長出來的 `Widget.later()`、"
        "接收者是變數的 `widget.whatever()`、"
        "第三方的 `pd.DataFrame()`、"
        "還沒實作的 `FutureThing.plan()`。\n",
        encoding="utf-8",
    )

    checker._PROJECT_ROOT = tmp_path
    checker._SCAN_DIRS = (".",)

    assert checker.check_symbol_references() == []


def test_planning_docs_are_exempt(tmp_path: Path) -> None:
    """
    規劃文件目錄不掃

    規劃文件一律引用**還不存在**的符號，稽核紀錄還會刻意引述「原本寫錯的名字」
    當對照——把那些當漂移會讓閘門被噪音鎖死，理由與刪除檢查跳過同一個目錄相同。

    **驗的是行為而不是常數的字面值**：斷言「豁免清單等於某個 tuple」只是把常數
    抄一遍，改壞了照樣綠。這裡真的建一份規劃文件，確認它沒有被掃到。
    """

    checker: types.ModuleType = _load_checker()

    (tmp_path / "widget.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
    (tmp_path / "backlog").mkdir()
    (tmp_path / "backlog" / "plan.md").write_text(
        "未來會有 `Widget.not_yet()`。\n", encoding="utf-8"
    )

    checker._PROJECT_ROOT = tmp_path
    checker._SCAN_DIRS = (".", "backlog")

    assert checker.check_symbol_references() == []


def test_inheritance_cycle_does_not_hang(tmp_path: Path) -> None:
    """
    繼承環不可讓它無限遞迴

    基底只以名字比對，於是兩個不同模組的同名類別互為基底時就成環。
    真實程式碼不會這樣寫，但索引是跨檔案取聯集的，環是索引造出來的而不是原始碼裡的。
    """

    checker: types.ModuleType = _load_checker()
    members: Dict[str, Set[str]] = {"A": {"a"}, "B": {"b"}}
    bases: Dict[str, Set[str]] = {"A": {"B"}, "B": {"A"}}

    assert checker._resolve_members("A", members, bases, set()) == {"a", "b"}
