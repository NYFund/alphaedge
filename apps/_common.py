import sys
from typing import Dict, List, Type

from core.strategies.base import BaseStrategy

"""回測與實盤入口共用的小工具：退出碼常數與策略名解析"""


# 用法錯誤的退出碼；與 argparse 自己的用法錯誤同碼（缺必填參數時它就回 2）
EXIT_USAGE: int = 2


def _report_unknown_strategies(
    missing: List[str], registry: Dict[str, Type[BaseStrategy]]
) -> None:
    """
    - Description:
        把找不到的策略名與可用策略清單寫到 stderr

        錯誤訊息走 stderr、退出碼非 0：這兩件事缺一不可——訊息印在 stdout
        會混進正常輸出，退出碼 0 則讓呼叫端完全看不出失敗。退出碼由呼叫端決定。
    - Parameters:
        - missing: List[str]
            找不到的策略名
        - registry: Dict[str, Type[BaseStrategy]]
            已註冊的策略
    """

    for name in missing:
        print(
            f"Strategy '{name}' not found. "
            "Please check the spelling or ensure it is registered.",
            file=sys.stderr,
        )
    available: str = ", ".join(sorted(registry)) or "(none)"
    print(f"Available strategies: {available}", file=sys.stderr)


def _resolve_strategy_or_exit(
    name: str, registry: Dict[str, Type[BaseStrategy]]
) -> Type[BaseStrategy]:
    """
    - Description:
        依類別名稱取出策略；找不到時列出可用策略並以用法錯誤結束行程
    - Parameters:
        - name: str
            策略類別名稱
        - registry: Dict[str, Type[BaseStrategy]]
            已註冊的策略
    - Return:
        - Type[BaseStrategy]
            策略類別
    """

    if name in registry:
        return registry[name]

    _report_unknown_strategies([name], registry)
    sys.exit(EXIT_USAGE)
