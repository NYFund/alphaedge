import pathlib
import re
from typing import Dict, List, Set

"""
文件寫的報表檔名要與實際產出一致

2026-09-25 實查時四份文件全部對不上：`core/backtest/README.md` 列了四個圖檔名，
**沒有一個正確**——少了策略名前綴，`networth` 被寫成 `balance_and_benchmark_curve`、
`mdd` 被寫成 `balance_mdd`，還漏了第五張 `everyday_equity_change`。
`strategy_lab/` 兩份也沿用了同一批舊名。

這種錯誤的代價是使用者照著文件去找檔案而找不到，而現有的
`scripts/check_doc_paths.py` 抓不到——它驗的是**檔案路徑**，
而這些是還沒產生出來的產出檔名，不是版控中的路徑。

本測試把兩邊綁在一起：檔名的權威來源是 `core/backtest/` 的 f-string，
文件只要漏列或寫錯就紅。
"""

PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

# 產出檔名的權威來源：`reporter`／`plotting`／`backtester` 裡的 f-string。
# 形如 `{self.strategy.strategy_name}_trading_report.csv`
OUTPUT_PATTERN: re.Pattern = re.compile(
    r"\{[a-z_.]*strategy_name\}_([a-z_]+\.(?:png|csv))"
)

# 會列出報表檔名的文件。**不含 `docs/`**：那裡描述的是模組關係而不是產出清單
DOCS: List[str] = [
    "core/backtest/README.md",
    "core/strategies/README.md",
]


def actual_outputs() -> Set[str]:
    """從 `core/backtest/` 的原始碼抽出實際會落地的檔名（去掉策略名前綴）"""

    names: Set[str] = set()
    for path in (PROJECT_ROOT / "core" / "backtest").rglob("*.py"):
        names.update(OUTPUT_PATTERN.findall(path.read_text(encoding="utf-8")))
    return names


def test_the_extractor_actually_finds_something() -> None:
    """
    先確認抽取樣式抓得到東西

    抽不到的話 `actual_outputs()` 會是空集合，而「空集合是任何集合的子集」——
    下面兩條斷言會無條件通過，護欄就在沒人察覺的情況下死掉。
    """

    outputs: Set[str] = actual_outputs()

    assert len(outputs) >= 8, f"只抽到 {len(outputs)} 個產出檔名，抽取樣式可能已失效"
    assert "trading_report.csv" in outputs
    assert "balance_curve.png" in outputs


def test_documented_filenames_all_exist_in_code() -> None:
    """
    文件列的每個報表檔名都要真的會被產出

    寫錯的代價是使用者照著去找檔案而找不到——`mdd.png` 被寫成 `balance_mdd.png`
    就是這樣，而那個錯誤在文件裡待了很久都沒人發現。
    """

    outputs: Set[str] = actual_outputs()
    documented: re.Pattern = re.compile(r"`<策略>_([a-z_]+\.(?:png|csv))`")

    unknown: Dict[str, List[str]] = {}
    for doc in DOCS:
        text: str = (PROJECT_ROOT / doc).read_text(encoding="utf-8")
        stale: List[str] = sorted(
            {name for name in documented.findall(text) if name not in outputs}
        )
        if stale:
            unknown[doc] = stale

    assert not unknown, "以下文件列的報表檔名不會被產出：\n" + "\n".join(
        f"  {doc}：{names}" for doc, names in unknown.items()
    )


def test_strategies_readme_lists_every_output() -> None:
    """
    `core/strategies/README.md` 要列出**全部**產出

    它是策略作者的入口，漏列等於那份產出不存在。
    `core/backtest/README.md` 不納入本條——它以表格分 CSV 與圖表兩節，
    格式不同，由上一條保證沒有寫錯的即可。
    """

    outputs: Set[str] = actual_outputs()
    text: str = (PROJECT_ROOT / "core/strategies/README.md").read_text(encoding="utf-8")
    documented: Set[str] = set(re.findall(r"`<策略>_([a-z_]+\.(?:png|csv))`", text))

    missing: List[str] = sorted(outputs - documented)

    assert not missing, f"`core/strategies/README.md` 漏列了這些產出：{missing}"
