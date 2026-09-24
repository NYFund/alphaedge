import sys
from typing import Any, List

from loguru import logger

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.broker.tw.shioaji_session import ShioajiSession

"""
單日損益的來源欄位長什麼樣？——連模擬環境唯讀核對

**動機**：單日虧損檢查（`RiskConfig.daily_loss_ratio`）的損益一律以券商端為準，
所以要確認的是各欄位**實際取得到什麼值**：

- 期貨：`Margin` 的 `future_open_position`（疑為未沖銷部位損益）與
  `future_settle_profitloss`（疑為平倉損益）——**名稱像不等於語意對**，
  本專案的合約檔日期格式就是實連三次才發現不是 ISO 的。
- 股票：帳戶快照的 `unrealized_pnl` 已由部位加總得出，已實現則要另外查。

**唯讀**：只呼叫帳務與部位查詢，不送委託、不寫任何資料庫、不動交易模式。
**不印任何憑證**：登入走 `ShioajiSession`，失敗時只印刮過憑證的單行原因。
"""


CONNECT_FAILURE_HINTS: List[str] = [
    "1. `.env` 是否存在且被讀到。",
    "2. 模擬環境的金鑰與正式環境不同，確認用的是模擬那組。",
]

# 想看的保證金欄位：前兩個是候選的損益來源，其餘用來交叉驗證數字對不對得上
MARGIN_FIELDS: List[str] = [
    "future_open_position",
    "future_settle_profitloss",
    "option_open_position",
    "option_settle_profitloss",
    "equity_amount",
    "available_margin",
    "initial_margin",
    "royalty_revenue_expenditure",
]


def main() -> int:
    """連模擬環境核對單日損益的來源欄位；唯讀"""

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

        logger.info("=== 期貨保證金原始欄位 ===")
        margin: Any = session.api.margin(session.api.futopt_account)
        for field in MARGIN_FIELDS:
            logger.info(f"  {field} = {getattr(margin, field, '(無此欄位)')!r}")

        logger.info("=== 正規化後的帳戶快照 ===")
        futures: Any = query.get_futures_account()
        logger.info(
            f"  期貨：total_equity={futures.total_equity!r} "
            f"unrealized_pnl={futures.unrealized_pnl!r} "
            f"realized_pnl={futures.realized_pnl!r}"
        )
        stock: Any = query.get_stock_account()
        logger.info(
            f"  股票：total_equity={stock.total_equity!r} "
            f"unrealized_pnl={stock.unrealized_pnl!r} "
            f"realized_pnl={stock.realized_pnl!r}"
        )

        logger.info("=== 股票部位的逐檔未實現損益（加總＝快照的 unrealized_pnl）===")
        positions: List[Any] = query.get_stock_positions()
        total: float = 0.0
        for position in positions:
            total += float(position.unrealized_pnl)
            logger.info(
                f"  {position.symbol} {position.direction.value} "
                f"{position.volume} 張 未實現 {position.unrealized_pnl!r}"
            )
        logger.info(f"  加總 = {total!r}")
        return 0

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
