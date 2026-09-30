import sys
from typing import Dict, List, Optional, Tuple

from loguru import logger

"""
舊入口的轉發 shim：`python run.py --mode backtest|live …` → `apps.backtest`／`apps.live`

回測與實盤已拆成兩個入口（`python -m apps.backtest`、`python -m apps.live`）。
本檔只在過渡期讓舊指令繼續可用：抽出 `--mode`（預設 `backtest`），其餘參數原封不動
交給對應入口，**退出碼原樣往外傳**——launchd 與 compose 依它判讀，吞成 0 或 1
就是假綠燈。呼叫端全部改用新入口之後，本檔刪除。
"""

# `--mode` 的值 → 新入口的模組
_MODE_TO_ENTRY: Dict[str, str] = {"backtest": "apps.backtest", "live": "apps.live"}

# 用法錯誤的退出碼；與 argparse 自己的用法錯誤同碼
_EXIT_USAGE: int = 2


def split_mode(argv: List[str]) -> Tuple[Optional[str], List[str]]:
    """
    - Description:
        從參數列抽出 `--mode`，回傳（模式, 其餘參數）

        支援 `--mode live` 與 `--mode=live` 兩種寫法；沒帶時為 `backtest`，
        與舊入口的預設相同。`--mode` 後面沒有值時模式為 None，由呼叫端當成用法錯誤。
    - Parameters:
        - argv: List[str]
            不含程式名的參數列
    - Return:
        - Tuple[Optional[str], List[str]]
            模式與其餘參數
    """

    mode: Optional[str] = "backtest"
    rest: List[str] = []
    index: int = 0
    while index < len(argv):
        token: str = argv[index]
        if token == "--mode":
            mode = argv[index + 1] if index + 1 < len(argv) else None
            index += 2
            continue
        if token.startswith("--mode="):
            mode = token.split("=", 1)[1]
        else:
            rest.append(token)
        index += 1
    return mode, rest


def main(argv: Optional[List[str]] = None) -> int:
    """
    - Description:
        依 `--mode` 轉發到新入口，回傳新入口的退出碼
    - Parameters:
        - argv: Optional[List[str]]
            不含程式名的參數列；None 時讀 `sys.argv`
    - Return:
        - int
            退出碼
    """

    mode, rest = split_mode(sys.argv[1:] if argv is None else argv)
    entry: Optional[str] = _MODE_TO_ENTRY.get(mode or "")
    if entry is None:
        print(
            f"--mode 必須是 {' 或 '.join(_MODE_TO_ENTRY)}（收到：{mode}）",
            file=sys.stderr,
        )
        return _EXIT_USAGE

    logger.warning(
        f"`run.py --mode {mode}` 已改為轉發，之後會移除；"
        f"請改用 `python -m {entry}`（參數相同，但不帶 --mode）"
    )

    # 延後 import：回測不該為了實盤那一整串相依（shioaji、券商閘道）付 import 成本
    if mode == "live":
        from apps.live import main as entry_main
    else:
        from apps.backtest import main as entry_main

    return entry_main(rest)


if __name__ == "__main__":
    sys.exit(main())
