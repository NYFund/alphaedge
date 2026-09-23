import argparse
import sys
from typing import Any, Dict, List, Optional

from loguru import logger

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.broker.tw.shioaji_session import ShioajiSession
from core.live.factory import live_capital
from core.live.risk.risk_config import CAPITAL_SAFETY_RATIO
from core.models import BrokerAccountSnapshot
from core.portfolio.aggregation import check_quota_against_equity
from core.strategies.base import BaseStrategy
from core.strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1

"""
額度檢查會不會通過？——連模擬環境唯讀核對

**動機**：`LiveTrader._account_equity()` 原本算的是「可用餘額 ＋ 各策略持倉占用」，
少了未交割款與不屬於任何策略的持倉，於是帳上有接管部位時總權益被算成 0，
每個段落都在 `prepare()` 拒絕啟動。改成取券商快照的 `total_equity` 之後，
「明天會不會通過」仍然只是推算——本工具實際連一次模擬環境把數字問出來。

**唯讀**：只呼叫 `account_balance()`、`list_settlements()`、`list_positions()`，
不送委託、不寫任何資料庫、不動交易模式。

**不印任何憑證**：登入走 `ShioajiSession`，失敗時只印刮過憑證的單行原因。
"""


CONNECT_FAILURE_HINTS: List[str] = [
    "1. `.env` 是否存在且被讀到。",
    "2. 模擬環境的金鑰與正式環境不同，確認用的是模擬那組。",
    "3. 券商端的模擬帳號是否仍在有效期內。",
]


def parse_arguments() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="連模擬環境核對額度檢查的基準（唯讀）"
    )
    parser.add_argument(
        "--strategy",
        choices=["stock", "futures", "both"],
        default="stock",
        help="以哪些策略的宣告額度核對（預設 stock，對應開盤段）",
    )
    return parser.parse_args()


def build_quotas(which: str) -> Dict[str, float]:
    """組出與實盤啟動時相同的額度表"""

    strategies: List[BaseStrategy] = []
    if which in ("stock", "both"):
        strategies.append(MomentumStrategy1())
    if which in ("futures", "both"):
        strategies.append(MomentumFuturesStrategy())

    return {type(s).__name__: live_capital(s) for s in strategies}


def report_snapshot(snapshot: BrokerAccountSnapshot) -> None:
    """印出券商快照的各個組成"""

    logger.info("--- 券商帳務快照 ---")
    logger.info(
        f"  available_balance（可用餘額）：{snapshot.available_balance:>14,.0f}"
    )
    logger.info(f"  total_equity（總權益）：      {snapshot.total_equity:>14,.0f}")
    logger.info(f"  unrealized_pnl（未實現）：    {snapshot.unrealized_pnl:>14,.0f}")

    raw: Dict[str, Any] = snapshot.raw or {}
    if raw:
        logger.info("  總權益的組成：")
        for key in ("acc_balance", "pending_settlement", "position_cost"):
            if key in raw:
                logger.info(f"    {key:<20} {float(raw[key]):>14,.0f}")


def report_verdict(
    snapshot: BrokerAccountSnapshot, quotas: Dict[str, float], simulation: bool
) -> bool:
    """
    - Description:
        套用 `verify_quota()` 的判準，回答「會不會通過」
    - Parameters:
        - snapshot: BrokerAccountSnapshot
            券商帳務快照
        - quotas: Dict[str, float]
            各策略宣告的實盤額度
        - simulation: bool
            是否為模擬環境（決定要不要走帳務全 0 的退路）
    - Return:
        - bool
            會通過為 True
    """

    equity: float = snapshot.total_equity

    if equity <= 0 and simulation:
        # 與實盤一致：查不到帳務時**略過**額度總量檢查，而不是捏一個數字讓它通過
        logger.warning(
            "模擬環境查不到帳務（欄位整組回 0），實盤會**略過額度總量檢查**"
            "（正式環境不走這條路，那裡的 0 代表真的沒有資金，會拒絕啟動）"
        )
        logger.info(f"  Σ 宣告額度 {sum(quotas.values()):,.0f} 不受此檢查限制")
        logger.info("✅ 不會被額度檢查擋下（因為那道檢查被略過）")
        return True

    logger.info("--- 額度檢查 ---")
    for name, quota in quotas.items():
        logger.info(f"  {name:<28} {quota:>14,.0f}")
    logger.info(f"  Σ 宣告額度                   {sum(quotas.values()):>14,.0f}")
    logger.info(f"  基準（總權益）               {equity:>14,.0f}")
    logger.info(
        f"  門檻 = 基準 × {CAPITAL_SAFETY_RATIO:.0%}          "
        f"{equity * CAPITAL_SAFETY_RATIO:>14,.0f}"
    )

    problem: Optional[str] = check_quota_against_equity(
        quotas, equity, CAPITAL_SAFETY_RATIO
    )

    if problem is None:
        margin: float = equity * CAPITAL_SAFETY_RATIO - sum(quotas.values())
        logger.info(f"✅ 會通過，餘裕 {margin:,.0f}")
        return True

    logger.error(f"❌ 不會通過：{problem}")
    return False


def compare_with_old_formula(snapshot: BrokerAccountSnapshot) -> None:
    """印出改動前的算式會得到什麼，讓修正的效果看得見"""

    logger.info("--- 對照：改動前的算式 ---")
    logger.info(
        "  舊算式 = 可用餘額 ＋ Σ 各策略持倉占用"
        f" = {snapshot.available_balance:,.0f} ＋ 0"
        f" = {snapshot.available_balance:,.0f}"
    )
    logger.info(
        "  （接管來的部位不屬於任何策略，在舊算式裡整個消失；未交割款同樣沒被算進去）"
    )


def main() -> int:
    """連模擬環境核對額度檢查的基準；不送委託、不寫資料庫"""

    args: argparse.Namespace = parse_arguments()
    quotas: Dict[str, float] = build_quotas(args.strategy)

    session: ShioajiSession = ShioajiSession(simulation=True, activate_ca=False)

    try:
        try:
            session.connect()
        except ConnectionError as exc:
            # 只印刮過憑證的單行原因；讓例外往上拋會把登入簽章負載印到終端
            logger.error(str(exc))
            for hint in CONNECT_FAILURE_HINTS:
                logger.error(f"  {hint}")
            return 1

        query: ShioajiAccountQuery = ShioajiAccountQuery(session.api, RateLimiter())
        # 與實盤一致：依本次載入的商品類別決定查哪個帳戶
        if args.strategy == "stock":
            snapshot: BrokerAccountSnapshot = query.get_stock_account()
        elif args.strategy == "futures":
            snapshot = query.get_futures_account()
        else:
            stock: BrokerAccountSnapshot = query.get_stock_account()
            futures: BrokerAccountSnapshot = query.get_futures_account()
            snapshot = BrokerAccountSnapshot(
                ts=stock.ts,
                available_balance=(stock.available_balance + futures.available_balance),
                total_equity=stock.total_equity + futures.total_equity,
                unrealized_pnl=stock.unrealized_pnl + futures.unrealized_pnl,
                raw={"stock": stock.raw, "futures": futures.raw},
            )

        report_snapshot(snapshot)
        compare_with_old_formula(snapshot)
        passed: bool = report_verdict(snapshot, quotas, simulation=True)
        return 0 if passed else 1

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
