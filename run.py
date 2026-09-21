import argparse
import sys
from typing import Dict, List, Type

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures
from core.strategies.base import BaseStrategy
from core.strategies.strategy_loader import StrategyLoader

"""Main entry point of the trading system: run backtest or live trading from project root"""


# -----------------------------------------------------------------------
# run.py 使用方式說明
# -----------------------------------------------------------------------
# Description: 本檔案為交易系統主程式入口，用於執行指定策略的回測或實盤
# Parameters: --mode (backtest | live), --strategy (策略類別名稱，必填)
# Example: python run.py --strategy MeanReversion
# Notes: Strategy Name 為 Class 名稱
#
# -----------------------------------------------------------------------
# 退出碼
# -----------------------------------------------------------------------
# 0  正常結束
# 2  用法錯誤：策略名找不到、或 --production 沒帶 --confirm-production
#    （與 argparse 自己的用法錯誤同碼，缺必填參數時它就回 2）
# 1  未預期的例外
# 3  資料未更新到前一個交易日
# 4  對帳不一致
# 5  kill switch 生效
# 6  上次結束時**帳戶層**交易模式非 NORMAL，本次未帶 --resume-trading
#
# **`6` 要和 `4`、`5` 分開**：排程看到 4／5 是「今天剛出事」，看到 6 是
# 「昨天出的事還沒有人處理」，兩者的處理急迫性不同。
# **策略層降級不走退出碼**：那會讓一支策略的降級擋掉整個排程，
# 與「一支策略拋例外不可拖垮其他策略」直接矛盾。
#
# **舊版兩者都回 0**：找不到策略只 `print` 後 `return`，`--mode live` 是 `pass`。
# 目前 `run.py` 只有人手動跑所以還沒出事，但一旦接進批次（例如每晚重跑策略），
# 「策略名打錯」與「回測跑完」在退出碼上長得一模一樣——那是最典型的假綠燈。
#


# 用法錯誤的退出碼；與 argparse 自己的用法錯誤同碼（缺必填參數時它就回 2）
EXIT_USAGE: int = 2
EXIT_STRATEGY_NOT_FOUND: int = EXIT_USAGE
EXIT_STALE_DATA: int = 3
EXIT_RECONCILE_MISMATCH: int = 4
EXIT_KILL_SWITCH: int = 5
EXIT_MODE_NOT_NORMAL: int = 6

# 段落名 → 執行段落。`after_close` 的盤後作業由 Phase4-6 接上，
# 這裡先讓它有一個明確的入口，而不是讓使用者打了之後什麼都沒發生
PHASE_TO_TIMING: Dict[str, str] = {
    "open": "AT_OPEN",
    "close": "AT_CLOSE",
    "intraday": "IMMEDIATE",
}


def parse_arguments() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Trading System"
    )

    # `live` 保留在 choices 裡（它是規劃中的模式，`--mode` 這個參數才有意義），
    # 但 help 必須講明尚未實作——否則 `--help` 看起來像已經支援實盤
    parser.add_argument(
        "--mode",
        choices=["backtest", "live"],
        default="backtest",
        help="執行模式：backtest（回測）或 live（實盤）",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help="策略類別名稱；實盤可用逗號分隔多支（多策略共用一個帳戶）",
    )
    # 回測畫完的五張圖要不要在瀏覽器開起來。**預設不開**：
    # 舊版寫死開啟，每跑一次回測就彈出 5 個分頁，批次掃參數時一次開幾十個，
    # 在無頭環境（CI、容器、nohup）更是直接失敗。圖本來就會存成 PNG。
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

    _add_live_arguments(parser)

    return parser.parse_args()


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    """
    實盤專用旗標

    **正式環境只能由命令列開啟，而且要兩個旗標**：刻意沒有對應的環境變數——
    `.env` 的設定會留在機器上，某天排程就會默默連上去下真單。
    """

    group = parser.add_argument_group("實盤（--mode live）")
    group.add_argument(
        "--phase",
        choices=["open", "close", "after_close", "intraday"],
        help="實盤執行的段落",
    )
    group.add_argument(
        "--broker",
        choices=["shioaji", "fake"],
        default="shioaji",
        help="券商閘道；fake 只給測試用，正式環境一律拒絕",
    )
    environment = group.add_mutually_exclusive_group()
    environment.add_argument(
        "--simulation",
        dest="simulation",
        action="store_true",
        default=True,
        help="連模擬環境（預設）",
    )
    environment.add_argument(
        "--production",
        dest="simulation",
        action="store_false",
        help="連正式環境；**必須同時帶 --confirm-production**",
    )
    group.add_argument(
        "--confirm-production",
        action="store_true",
        help="確認要連正式環境（與 --production 併用）",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="走完整流程但不真的送出委託",
    )
    group.add_argument(
        "--resync-from-broker",
        action="store_true",
        help="以券商部位重建本地部位（對帳不一致、人工確認後才用）",
    )
    group.add_argument(
        "--resume-trading",
        nargs="*",
        metavar="策略名",
        help=(
            "人工恢復交易模式；不給策略名時恢復帳戶層。"
            "**不要放進排程指令**——它一旦寫進 crontab 就等於自動恢復"
        ),
    )


def run_live(args: argparse.Namespace, registry: Dict[str, Type[BaseStrategy]]) -> int:
    """
    - Description:
        實盤入口：先防呆、再組裝、最後跑一個段落

        **防呆一律在建立任何連線之前**：`--production` 打錯的代價是真的下單，
        那不是一個可以「先連連看再說」的操作。
    - Parameters:
        - args: argparse.Namespace
            命令列參數
        - registry: Dict[str, Type[BaseStrategy]]
            已註冊的策略
    - Return:
        - int
            退出碼
    """

    # 延後 import：實盤那一整串相依（shioaji、券商閘道、OMS）只有實盤用得到，
    # 回測不該為了它們付 import 成本，也不該因為它們壞掉而跑不動
    from core.live.datafeed.base import DataFreshnessError
    from core.live.datafeed.calendar import TradingCalendarUnavailableError
    from core.live.factory import UnsupportedMarketError, build_live_trader
    from core.live.risk.trading_mode import TradingMode
    from core.utils import ExecutionTiming

    if args.phase is None:
        print("實盤模式必須指定 --phase", file=sys.stderr)
        return EXIT_USAGE

    if not args.simulation and not args.confirm_production:
        # 兩個旗標才開得了正式環境；**刻意沒有對應的環境變數**——
        # `.env` 的設定會留在機器上，某天排程就會默默連上去下真單
        print(
            "--production 必須同時帶 --confirm-production 才會連到正式環境",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.broker == "fake" and not args.simulation:
        print("正式環境不可使用 fake 券商", file=sys.stderr)
        return EXIT_USAGE

    names: List[str] = [
        name.strip() for name in args.strategy.split(",") if name.strip()
    ]
    missing: List[str] = [name for name in names if name not in registry]
    if missing:
        print(f"找不到策略：{missing}", file=sys.stderr)
        print(
            f"Available strategies: {', '.join(sorted(registry)) or '(none)'}",
            file=sys.stderr,
        )
        return EXIT_STRATEGY_NOT_FOUND

    strategies: List[BaseStrategy] = [registry[name]() for name in names]

    try:
        trader = build_live_trader(
            strategies,
            broker_kind=args.broker,
            simulation=args.simulation,
            dry_run=args.dry_run,
            resume_trading=args.resume_trading is not None,
            phase=args.phase,
        )
    except (UnsupportedMarketError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    environment: str = "模擬" if args.simulation else "**正式**"
    print(f"實盤啟動：{environment}環境、段落 {args.phase}、策略 {names}")

    try:
        if args.phase == "after_close":
            # 盤後不送新倉單，走另一條流程：刷新委託、對帳、回填成本、
            # 殘量處理、輸出報表
            summary = trader.run_after_close()
            print(f"盤後作業完成：{summary['pending_actions']} 筆跨日待辦")
        else:
            trader.run(ExecutionTiming[PHASE_TO_TIMING[args.phase]])
    except DataFreshnessError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STALE_DATA
    except TradingCalendarUnavailableError as exc:
        # 判不出今天是不是交易日 → 拒絕啟動。與資料過期同一個結束碼：
        # 對排程而言兩者是同一件事——**資料源給不出可信的答案，今天不要跑**
        print(f"無法判定交易日，拒絕啟動：{exc}", file=sys.stderr)
        return EXIT_STALE_DATA

    return _resolve_live_exit_code(trader, TradingMode)


def _resolve_live_exit_code(trader: object, trading_mode: object) -> int:
    """
    - Description:
        由本次執行的結果決定退出碼

        對帳不一致與 kill switch **都不拋例外**（它們只降級），所以要在這裡
        把狀態翻譯成排程看得懂的號碼。三者的處理急迫性不同：
        5 是有人按下了停止鍵、4 是今天剛發現不一致、6 是昨天出的事還沒人處理。
    - Parameters:
        - trader: object
            跑完的引擎
        - trading_mode: object
            交易模式 Enum
    - Return:
        - int
            退出碼
    """

    if trader.risk_manager.is_kill_switch_on():
        return EXIT_KILL_SWITCH

    reconcile = trader.last_reconcile
    if reconcile is not None and not reconcile.is_consistent:
        return EXIT_RECONCILE_MISMATCH

    # **只看帳戶層**：策略層降級不走退出碼，那會讓一支策略的降級擋掉整個排程
    if trader.mode_state.account_mode is not trading_mode.NORMAL:
        return EXIT_MODE_NOT_NORMAL

    return 0


def main() -> None:
    args: argparse.Namespace = parse_arguments()
    strategy_name: str = args.strategy

    strategies: Dict[str, Type[BaseStrategy]] = StrategyLoader.load_strategies()

    if args.mode == "live":
        sys.exit(run_live(args, strategies))

    if strategy_name not in strategies:
        # 錯誤訊息走 stderr、退出碼非 0：這兩件事缺一不可——訊息印在 stdout
        # 會混進正常輸出，退出碼 0 則讓呼叫端完全看不出失敗
        print(
            f"Strategy '{strategy_name}' not found. "
            "Please check the spelling or ensure it is registered.",
            file=sys.stderr,
        )
        available: str = ", ".join(sorted(strategies)) or "(none)"
        print(f"Available strategies: {available}", file=sys.stderr)
        sys.exit(EXIT_STRATEGY_NOT_FOUND)

    # Initialize strategy
    strategy: BaseStrategy = strategies[strategy_name]()

    # Backtest or Live Trading
    if args.mode == "backtest":
        backtester: Backtester = build_backtester(strategy)
        # 命令列旗標優先於環境變數；兩者都沒給就是不開圖
        backtester.show_figures = (
            resolve_show_figures() if args.show is None else args.show
        )
        backtester.run()


if __name__ == "__main__":
    main()
