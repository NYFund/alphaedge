import pathlib
import re
import subprocess
from typing import Dict, List, Tuple

import pytest

"""
品質 ratchet 的註記必須與實況一致

`pyproject.toml` 的 ignore 清單以行尾註解記錄每條規則的**現存處數**，
而清單自己就寫著「不可因為『讓 CI 變綠』就當成永久豁免」——數字是那句話的憑據。

**但沒有任何東西在驗它。** 2026-09-24 實查時六條全部過時，而且每一條都往
「低報」的方向漂——`UP045` 註記 391 處、實際 1,690 處；`BLE001` 註記 70 處、
實際 110 處。低報的後果不是數字難看，是**讓人以為問題比實際小**，
於是那條規則永遠排不進待辦。

本測試讓註記自我驗證：對不上就紅，逼人現查。
"""

PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

# 行尾註解形如 `"BLE001",  # blind-except，92 處`；數字可帶千分位逗號
ANNOTATION: re.Pattern = re.compile(
    r'^\s*"(?P<rule>[A-Z]+\d+)",\s*#[^\n]*?(?P<count>[\d,]+)\s*處'
)


def declared_counts() -> Dict[str, int]:
    """從 `pyproject.toml` 的 ignore 清單讀出各規則的註記處數"""

    counts: Dict[str, int] = {}
    for line in (PROJECT_ROOT / "pyproject.toml").read_text().splitlines():
        matched: re.Match = ANNOTATION.match(line)
        if matched:
            counts[matched.group("rule")] = int(matched.group("count").replace(",", ""))
    return counts


def actual_counts(rules: List[str]) -> Dict[str, int]:
    """現查各規則的實際處數"""

    result: subprocess.CompletedProcess = subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            ".",
            "--select",
            ",".join(rules),
            "--statistics",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    counts: Dict[str, int] = dict.fromkeys(rules, 0)
    for line in result.stdout.splitlines():
        parts: List[str] = line.split()
        if len(parts) >= 2 and parts[1] in counts:
            counts[parts[1]] = int(parts[0])
    return counts


# **刻意的風格選擇，不是債務**：CLAUDE.md §2.4 要求 `Optional[T]` 而非 `T | None`，
# 所以 `UP045` 的數量會隨每個新檔案增加，而那正是預期的結果。
# 把它當 ratchet 是搞錯對象——它的註記只是規模的說明，不是「不可以再變多」。
STYLE_CHOICES: Tuple[str, ...] = ("UP045",)


def test_ignore_annotations_match_reality() -> None:
    """
    ignore 清單的處數註記要與 `--statistics` 一致

    **允許往下、不允許往上**：修掉一些之後註記暫時高報，只代表還沒回寫；
    而實際數量超過註記代表**又長出新的**，那是 ratchet 失效，必須當場擋下。

    `STYLE_CHOICES` 裡的規則不受上限約束（理由見該常數）。
    """

    declared: Dict[str, int] = declared_counts()
    assert declared, "pyproject.toml 的 ignore 清單讀不到任何處數註記"

    actual: Dict[str, int] = actual_counts(sorted(declared))

    drifted: List[Tuple[str, int, int]] = [
        (rule, declared[rule], actual[rule])
        for rule in sorted(declared)
        if rule not in STYLE_CHOICES and actual[rule] > declared[rule]
    ]

    assert not drifted, "以下規則的實際處數超過註記（又長出新的）：\n" + "\n".join(
        f"  {rule}：註記 {noted} 處、實際 {real} 處（+{real - noted}）"
        for rule, noted, real in drifted
    )


def test_annotations_are_not_wildly_stale() -> None:
    """
    註記也不可以**高報**太多

    高報代表已經修掉一批卻沒回寫，而那正是「數字沒人信」的開始——
    一旦沒人信，ratchet 就只是一行裝飾。容許少量落差（修修改改的過程中難免），
    超過一成就要回寫。
    """

    declared: Dict[str, int] = declared_counts()
    actual: Dict[str, int] = actual_counts(sorted(declared))

    stale: List[Tuple[str, int, int]] = [
        (rule, declared[rule], actual[rule])
        for rule in sorted(declared)
        if rule not in STYLE_CHOICES and actual[rule] < declared[rule] * 0.9
    ]

    assert not stale, "以下規則已修掉一批但註記沒回寫：\n" + "\n".join(
        f"  {rule}：註記 {noted} 處、實際只剩 {real} 處" for rule, noted, real in stale
    )


def test_removed_rules_stay_removed() -> None:
    """
    歸零後移除的規則不可以悄悄回來

    `E722`（裸 `except:`）歸零之後整條移出了 ignore 清單。
    它若再次出現在清單裡，代表有人為了讓 CI 變綠而把它加回來——
    而豁免一條規則等於對那類問題永久失明。
    """

    content: str = (PROJECT_ROOT / "pyproject.toml").read_text()
    ignore_block: str = content.split("[tool.ruff.lint.per-file-ignores]")[0]

    assert '"E722"' not in ignore_block, (
        "E722 已歸零並移除，不該再出現在 ignore 清單；"
        "若真的又出現裸 except，請修掉它而不是豁免它"
    )


@pytest.mark.parametrize("rule", ["BLE001", "UP045"])
def test_headline_rules_are_still_tracked(rule: str) -> None:
    """
    數量最大的幾條要確實帶著處數註記，不可以只留規則名

    `E501` 不在此列：它的處數寫在上方的整段說明裡而不是行尾，
    格式不同故不納入自動比對。
    """

    assert rule in declared_counts(), f"{rule} 的處數註記不見了"
