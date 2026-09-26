import pathlib
import re
from typing import List, Tuple

_PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

"""
兩份 README 的結構要保持對應

`README.md`（權威）與 `README_en.md` 是**逐節對應的兩份**，不是各自獨立的文件：
只改一邊的結果不是「翻譯落後」，而是英文版少了一整節而沒有任何人會發現。

**驗結構而不驗字數**：譯文的換行本來就與中文不同（實測有四處 ±1 行的偏移，
全是段落折行造成的），硬要求行數相同只會逼人把散文重新折行來湊數字，
下一次編輯又會破掉——那是假精確。

真正會出事的是**結構分岔**：一邊多一節、多一張表、多一段程式碼範例。
故判準是標題／表格／程式碼圍籬的**序列**要一一對應，並且兩份都不許出現
連續空行（那是 2026-09-26 實際修掉的問題：英文版多出六行空白，
使它比中文版長八行，比對兩份時行號一路錯開）。
"""

_HEADING: re.Pattern = re.compile(r"^(#{1,6})\s")
_TABLE_SEPARATOR: re.Pattern = re.compile(r"^\|\s*-{2,}")


def _structure(path: pathlib.Path) -> List[str]:
    """
    抽出結構標記序列

    只認語法層面的標記，不看內容——內容本來就是兩種語言。
    """

    markers: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped: str = line.strip()
        heading: re.Match = _HEADING.match(stripped)
        if heading:
            markers.append(f"H{len(heading.group(1))}")
        elif stripped.startswith("```"):
            markers.append("FENCE")
        elif _TABLE_SEPARATOR.match(stripped):
            markers.append("TABLE")
        elif stripped == "---":
            markers.append("RULE")
    return markers


def _blank_runs(path: pathlib.Path) -> List[int]:
    """回傳所有「連續兩行以上空白」的起始行號（1-indexed）"""

    lines: List[str] = path.read_text(encoding="utf-8").splitlines()
    runs: List[int] = []
    index: int = 0
    while index < len(lines):
        if lines[index].strip():
            index += 1
            continue
        end: int = index
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end - index >= 2:
            runs.append(index + 1)
        index = end
    return runs


def test_the_extractor_actually_finds_structure() -> None:
    """
    先確認抽取抓得到標記

    抓不到的話「兩份的空清單相等」恆成立，整檔變成假綠燈。
    """

    zh: List[str] = _structure(_PROJECT_ROOT / "README.md")

    assert len(zh) >= 40, f"只抽到 {len(zh)} 個結構標記，抽取樣式可能已失效"
    assert zh.count("H2") >= 5
    assert "TABLE" in zh and "FENCE" in zh


def test_both_readmes_have_the_same_structure() -> None:
    """
    章節、表格與程式碼區塊要一一對應

    只改一邊時，少的那一邊不會有任何徵兆——這條是唯一會出聲的地方。
    """

    zh: List[str] = _structure(_PROJECT_ROOT / "README.md")
    en: List[str] = _structure(_PROJECT_ROOT / "README_en.md")

    if zh != en:
        divergence: int = next(
            (i for i, (a, b) in enumerate(zip(zh, en)) if a != b), min(len(zh), len(en))
        )
        context: Tuple[List[str], List[str]] = (
            zh[max(0, divergence - 3) : divergence + 3],
            en[max(0, divergence - 3) : divergence + 3],
        )
        raise AssertionError(
            f"兩份 README 的結構在第 {divergence + 1} 個標記起分岔"
            f"（中文 {len(zh)} 個、英文 {len(en)} 個）：\n"
            f"  README.md    …{context[0]}…\n"
            f"  README_en.md …{context[1]}…"
        )


def test_neither_readme_has_stray_blank_runs() -> None:
    """
    兩份都不許出現連續空行

    多出來的空行讓兩份的行號一路錯開，之後每次比對都得先重新對齊。
    """

    for name in ("README.md", "README_en.md"):
        runs: List[int] = _blank_runs(_PROJECT_ROOT / name)
        assert not runs, f"{name} 在第 {runs} 行出現連續空行"
