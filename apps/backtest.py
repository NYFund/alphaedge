import argparse
import sys
from typing import List, Optional

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures
from core.strategies.base import BaseStrategy

from ._common import _resolve_strategy_or_exit

"""
回測入口：python -m apps.backtest --strategy <策略類別名稱>

- `--strategy` 收的是**策略的類別名稱**，只接一支；可用的策略以
  `core/strategies/{stock,futures}/` 底下的非抽象子類為準
- 本 parser 只認得回測的旗標：帶了實盤旗標（例如 `--production`）時，
  argparse 會直接以用法錯誤拒絕，不可能「以為在下單、其實跑了回測」

退出碼：0 正常結束；2 用法錯誤（策略名找不到、未知旗標、缺必填參數）；
1 未預期的例外
"""


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    - Description:
        解析回測的命令列參數
    - Parameters:
        - argv: Optional[List[str]]
            參數列；None 時讀 `sys.argv`
    - Return:
        - argparse.Namespace
            解析結果
    """

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="python -m apps.backtest", description="回測"
    )
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help="策略類別名稱（只接一支）",
    )
    # 回測畫完的五張圖要不要在瀏覽器開起來。**預設不開**：圖本來就會存成 PNG，
    # 自動開啟在批次掃參數時一次彈出幾十個分頁，在無頭環境（CI、容器、nohup）更是直接失敗
    show_group = parser.add_mutually_exclusive_group()
    show_group.add_argument(
        "--show",
        dest="show",
        action="store_true",
        default=None,
        help="回測結束後在瀏覽器開啟圖表",
    )
    show_group.add_argument(
        "--no-show",
        dest="show",
        action="store_false",
        help=f"不開啟圖表（未指定時依環境變數 {SHOW_FIGURES_ENV_VAR}，預設不開）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """
    - Description:
        解析參數、載入策略並跑一場回測
    - Parameters:
        - argv: Optional[List[str]]
            參數列；None 時讀 `sys.argv`
    - Return:
        - int
            退出碼
    """

    args: argparse.Namespace = parse_arguments(argv)

    strategy: BaseStrategy = _resolve_strategy_or_exit(args.strategy)()

    backtester: Backtester = build_backtester(strategy)
    # 命令列旗標優先於環境變數；兩者都沒給就是不開圖
    backtester.show_figures = resolve_show_figures() if args.show is None else args.show
    backtester.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
