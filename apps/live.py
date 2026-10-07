import argparse
import datetime
import sys
from typing import Any, Dict, List, Optional, Type

from core.live.attribution.resync import ResyncPlan, ResyncRefusedError
from core.live.datafeed.base import DataFreshnessError
from core.live.datafeed.calendar import TradingCalendarUnavailableError
from core.live.factory import UnsupportedMarketError, build_live_trader
from core.live.risk.trading_mode import TradingMode
from core.live.termination import LiveTerminated, raise_on_sigterm
from core.live.trader import LiveTrader
from core.strategies.base import BaseStrategy
from core.strategies.strategy_loader import StrategyLoader
from core.utils import ExecutionTiming

from ._common import EXIT_USAGE, _report_unknown_strategies

"""
實盤入口：python -m apps.live --strategy <策略類別名稱[,…]> --phase <段落>

- `--strategy` 收策略的類別名稱，可用逗號分隔多支（多策略共用一個帳戶）
- 預設連模擬環境；正式環境要同時帶 `--production` 與 `--confirm-production`
- 其餘旗標詳見 `--help`（不在此另開一份會漂移的清單）
"""


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
# 7  --resync-from-broker 只列出重建計畫、沒有寫入（未帶 --confirm-resync）
# 143 收到 SIGTERM（容器停止、排程逾時）：已撤未成交單、寫完結束紀錄才離開
#     （128 ＋ 訊號編號 15，是 shell 與容器對「被訊號結束」的慣例值）
#
# **每一種失敗都要有自己的號碼**：「策略名打錯」與「段落跑完」若都回 0，
# 在排程端長得一模一樣——那是最典型的假綠燈。
# **`7` 不是 0**：只列計畫代表歸屬帳仍與券商不一致，排程不可把它當成已處理。
# 重建被拒絕（歸屬帳已損壞、或仍有未終結的委託）回 `4`：與對帳不一致同一件事，
# 都要人工處理。
# **`6` 要和 `4`、`5` 分開**：排程看到 4／5 是「今天剛出事」，看到 6 是
# 「昨天出的事還沒有人處理」，兩者的處理急迫性不同。
# **策略層降級不走退出碼**：那會讓一支策略的降級擋掉整個排程，
# 與「一支策略拋例外不可拖垮其他策略」直接矛盾。
#
EXIT_STRATEGY_NOT_FOUND: int = EXIT_USAGE
EXIT_STALE_DATA: int = 3
EXIT_RECONCILE_MISMATCH: int = 4
EXIT_KILL_SWITCH: int = 5
EXIT_MODE_NOT_NORMAL: int = 6
EXIT_RESYNC_PLAN_ONLY: int = 7
EXIT_TERMINATED: int = 143

# 以券商部位重建時寫進 `live_run.phase` 的值；它不是交易段落，存活監控不會等它
RESYNC_PHASE: str = "resync"

# 次日補比 parity 的段落名；同樣不是交易段落，存活監控不會等它
PARITY_PHASE: str = "parity"

# `--broker` 的預設值
DEFAULT_BROKER: str = "shioaji"

# 段落名 → 執行段落。`after_close` 不在表內：盤後作業不送新倉單，
# `run_live()` 另走 `run_after_close()` 那條流程
PHASE_TO_TIMING: Dict[str, ExecutionTiming] = {
    "open": ExecutionTiming.AT_OPEN,
    "close": ExecutionTiming.AT_CLOSE,
    "intraday": ExecutionTiming.IMMEDIATE,
}


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """
    - Description:
        解析實盤的命令列參數

        **正式環境只能由命令列開啟，而且要兩個旗標**：刻意沒有對應的環境變數——
        `.env` 的設定會留在機器上，某天排程就會默默連上去下真單。
    - Parameters:
        - argv: Optional[List[str]]
            參數列；None 時讀 `sys.argv`
    - Return:
        - argparse.Namespace
            解析結果
    """

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="python -m apps.live", description="實盤"
    )
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help="策略類別名稱；可用逗號分隔多支（多策略共用一個帳戶）",
    )
    parser.add_argument(
        "--phase",
        choices=["open", "close", "after_close", "intraday", PARITY_PHASE],
        help=(
            "執行的段落（--resync-from-broker 時不可指定）；"
            "parity 為次日資料更新後補比前一交易日，不連券商、不送單"
        ),
    )
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        help="只給 --phase parity 用：要補比的交易日（YYYY-MM-DD），預設為歷史資料最新日",
    )
    parser.add_argument(
        "--broker",
        choices=["shioaji", "fake"],
        default=DEFAULT_BROKER,
        help="券商閘道；fake 只給測試用，正式環境一律拒絕",
    )
    environment = parser.add_mutually_exclusive_group()
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
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="確認要連正式環境（與 --production 併用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="走完整流程但不真的送出委託",
    )
    parser.add_argument(
        "--resync-from-broker",
        action="store_true",
        help=(
            "以券商部位重建歸屬帳（獨立作業，不跑段落、不可與 --phase 併用）；"
            "只帶這個旗標時只列出計畫，不寫入"
        ),
    )
    parser.add_argument(
        "--confirm-resync",
        action="store_true",
        help="確認寫入重建計畫（與 --resync-from-broker 併用）",
    )
    parser.add_argument(
        "--resume-trading",
        nargs="*",
        metavar="策略名",
        help=(
            "人工恢復交易模式；不給策略名時恢復帳戶層。"
            "**不要放進排程指令**——它一旦寫進 crontab 就等於自動恢復"
        ),
    )
    return parser.parse_args(argv)


def run_live(args: argparse.Namespace, registry: Dict[str, Type[BaseStrategy]]) -> int:
    """
    - Description:
        先防呆、再組裝、最後跑一個段落

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

    usage_error: str = _check_resync_arguments(args)
    if usage_error:
        print(usage_error, file=sys.stderr)
        return EXIT_USAGE

    # `--phase` 不設成 required：`--resync-from-broker` 時反而不可以有它，
    # 手寫檢查的錯誤訊息也比 argparse 的清楚
    if args.phase is None and not args.resync_from_broker:
        print("實盤必須指定 --phase", file=sys.stderr)
        return EXIT_USAGE

    if not args.simulation and not args.confirm_production:
        print(
            "--production 必須同時帶 --confirm-production 才會連到正式環境",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.date is not None and args.phase != PARITY_PHASE:
        print("--date 只能與 --phase parity 併用", file=sys.stderr)
        return EXIT_USAGE
    if args.broker == "fake" and not args.simulation:
        print("正式環境不可使用 fake 券商", file=sys.stderr)
        return EXIT_USAGE

    names: List[str] = _split_strategy_names(args.strategy)
    missing: List[str] = [name for name in names if name not in registry]
    if missing:
        _report_unknown_strategies(missing)
        return EXIT_STRATEGY_NOT_FOUND

    strategies: List[BaseStrategy] = [registry[name]() for name in names]

    try:
        trader: LiveTrader = build_live_trader(
            strategies,
            broker_kind=args.broker,
            simulation=args.simulation,
            dry_run=args.dry_run,
            resume_trading=args.resume_trading is not None,
            phase=RESYNC_PHASE if args.resync_from_broker else args.phase,
        )
    except (UnsupportedMarketError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    environment: str = "模擬" if args.simulation else "**正式**"

    try:
        # SIGTERM 在區塊內改成拋 `LiveTerminated`，沿引擎的 `finally` 撤單、
        # 寫結束紀錄；預設處理會直接結束行程，場上的委託沒人撤
        with raise_on_sigterm():
            return _run_live_phase(trader, args, names, environment)
    except LiveTerminated as exc:
        print(f"{exc}：已撤未成交單並寫入結束紀錄", file=sys.stderr)
        return EXIT_TERMINATED


def _split_strategy_names(value: str) -> List[str]:
    """逗號分隔的策略名 → 名稱清單（去空白、略過空項）"""

    return [name.strip() for name in value.split(",") if name.strip()]


def _run_live_phase(
    trader: LiveTrader, args: argparse.Namespace, names: List[str], environment: str
) -> int:
    """
    - Description:
        跑一個段落（或重建）並把結果翻譯成退出碼
    - Parameters:
        - trader: LiveTrader
            組裝好的引擎
        - args: argparse.Namespace
            命令列參數
        - names: List[str]
            策略名
        - environment: str
            環境說明（模擬／正式）
    - Return:
        - int
            退出碼
    """

    if args.resync_from_broker:
        print(f"以券商部位重建歸屬帳：{environment}環境、策略 {names}")
        return _run_resync(trader, args.confirm_resync)

    print(f"實盤啟動：{environment}環境、段落 {args.phase}、策略 {names}")

    try:
        if args.phase == PARITY_PHASE:
            unexplained: int = trader.run_parity(args.date)
            print(f"訊號 parity 補比完成：未解釋差異 {unexplained} 筆")
            return 0
        if args.phase == "after_close":
            # 盤後不送新倉單，走另一條流程：刷新委託、對帳、回填成本、
            # 殘量處理、輸出報表
            summary: Dict[str, Any] = trader.run_after_close()
            print(f"盤後作業完成：{summary['pending_actions']} 筆跨日待辦")
        else:
            trader.run(PHASE_TO_TIMING[args.phase])
    except DataFreshnessError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STALE_DATA
    except TradingCalendarUnavailableError as exc:
        # 判不出今天是不是交易日 → 拒絕啟動。與資料過期同一個結束碼：
        # 對排程而言兩者是同一件事——**資料源給不出可信的答案，今天不要跑**
        print(f"無法判定交易日，拒絕啟動：{exc}", file=sys.stderr)
        return EXIT_STALE_DATA

    return _resolve_live_exit_code(trader)


def _check_resync_arguments(args: argparse.Namespace) -> str:
    """
    - Description:
        重建相關旗標的組合檢查；合法時回空字串

        重建是**獨立作業**：不跑段落，也不恢復交易模式。與 `--phase` 併用的話，
        人會以為重建完接著跑了段落；與 `--resume-trading` 併用的話，
        重建結果還沒人看過，降級就已經解除了。
    - Parameters:
        - args: argparse.Namespace
            命令列參數
    - Return:
        - str
            錯誤訊息；合法時為空字串
    """

    if args.confirm_resync and not args.resync_from_broker:
        return "--confirm-resync 必須與 --resync-from-broker 併用"
    if not args.resync_from_broker:
        return ""
    if args.phase is not None:
        return "--resync-from-broker 是獨立作業，不可與 --phase 併用"
    if args.resume_trading is not None:
        return (
            "--resync-from-broker 不可與 --resume-trading 併用："
            "請先確認重建結果，再另外以 --resume-trading 恢復交易"
        )
    return ""


def _run_resync(trader: LiveTrader, confirm: bool) -> int:
    """
    - Description:
        執行以券商部位重建歸屬帳，並把結果翻譯成退出碼
    - Parameters:
        - trader: LiveTrader
            組裝好的引擎
        - confirm: bool
            是否寫入
    - Return:
        - int
            退出碼
    """

    try:
        plan: ResyncPlan = trader.resync_from_broker(confirm)
    except ResyncRefusedError as exc:
        print(f"拒絕重建：{exc}", file=sys.stderr)
        return EXIT_RECONCILE_MISMATCH

    for line in plan.describe():
        print(line)

    if plan.actions and not confirm:
        print(
            "以上為重建計畫，尚未寫入；確認後加上 --confirm-resync 再執行一次",
            file=sys.stderr,
        )
        return EXIT_RESYNC_PLAN_ONLY

    code: int = _resolve_live_exit_code(trader)
    if code == EXIT_MODE_NOT_NORMAL:
        print(
            "帳戶層交易模式仍非 NORMAL：確認重建結果無誤後，以 --resume-trading 恢復",
            file=sys.stderr,
        )
    return code


def _resolve_live_exit_code(trader: LiveTrader) -> int:
    """
    - Description:
        由本次執行的結果決定退出碼

        對帳不一致與 kill switch **都不拋例外**（它們只降級），所以要在這裡
        把狀態翻譯成排程看得懂的號碼。三者的處理急迫性不同：
        5 是有人按下了停止鍵、4 是今天剛發現不一致、6 是昨天出的事還沒人處理。
    - Parameters:
        - trader: LiveTrader
            跑完的引擎
    - Return:
        - int
            退出碼
    """

    if trader.risk_manager.is_kill_switch_on():
        return EXIT_KILL_SWITCH

    reconcile: Optional[Any] = trader.last_reconcile
    if reconcile is not None and not reconcile.is_consistent:
        return EXIT_RECONCILE_MISMATCH

    # **只看帳戶層**：策略層降級不走退出碼，那會讓一支策略的降級擋掉整個排程
    if trader.mode_state.account_mode is not TradingMode.NORMAL:
        return EXIT_MODE_NOT_NORMAL

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """
    - Description:
        解析參數、載入策略並跑一個實盤段落
    - Parameters:
        - argv: Optional[List[str]]
            參數列；None 時讀 `sys.argv`
    - Return:
        - int
            退出碼
    """

    args: argparse.Namespace = parse_arguments(argv)
    # 只載入指定的策略：全掃描會執行每一支策略（含研究中的）的 module-level 程式碼
    registry: Dict[str, Type[BaseStrategy]] = StrategyLoader.load(
        _split_strategy_names(args.strategy)
    )
    return run_live(args, registry)


if __name__ == "__main__":
    sys.exit(main())
