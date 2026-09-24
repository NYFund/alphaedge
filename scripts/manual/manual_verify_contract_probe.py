import sys
from typing import Any, List, Optional

from loguru import logger

from core.broker.tw.shioaji_contract_resolver import ShioajiContractResolver
from core.broker.tw.shioaji_session import ShioajiSession
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed

"""
期貨合約到底有沒有 `update_date`？——連模擬環境唯讀核對

**動機**：實盤的交易日判定在平日只剩「券商合約檔更新日」一個佐證
（官方開休市日曆只替已入庫的年度作答）。期貨資料源必須問**期貨**合約——
拿股票合約去問，兩個市場開休市不一致的那天會誤判為開市。但前提是期貨合約
也帶得出 `update_date`：**帶不出來就從「錯的佐證」變成「沒有佐證」**，
期貨每天都會判不出開市與否而拒絕啟動。

欄位存不存在、型別是 `date` 還是 `'YYYY/MM/DD'` 字串，都只有實連問得出來
（合約檔的日期格式就是實連三次才發現不是 ISO 的）。

**唯讀**：只查合約檔，不送委託、不寫任何資料庫、不動交易模式。
**不印任何憑證**：登入走 `ShioajiSession`，失敗時只印刮過憑證的單行原因。
"""


CONNECT_FAILURE_HINTS: List[str] = [
    "1. `.env` 是否存在且被讀到。",
    "2. 模擬環境的金鑰與正式環境不同，確認用的是模擬那組。",
    "3. 券商端的模擬帳號是否仍在有效期內。",
]


class _ProbeBroker:
    """只帶 resolver 的最小 broker；資料源的探測只用得到它"""

    def __init__(self, resolver: Any) -> None:
        self.resolver: Any = resolver


def report_raw_field(label: str, contract: Any) -> None:
    """印出合約的 `update_date` 原貌（欄位名與型別都要看，不只看值）"""

    raw: Any = getattr(contract, "update_date", None)
    code: str = str(getattr(contract, "code", "?"))
    logger.info(f"  {label}：code={code} update_date={raw!r} type={type(raw).__name__}")


def main() -> int:
    """連模擬環境核對兩個市場的合約探測；唯讀"""

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

        resolver: ShioajiContractResolver = ShioajiContractResolver(session.api)
        broker: _ProbeBroker = _ProbeBroker(resolver)

        logger.info("=== 合約原貌 ===")
        report_raw_field("股票 2330", resolver.resolve_stock("2330"))

        expiries: List[str] = resolver.list_index_futures_expiries("TX")
        logger.info(f"  TX 掛牌月份：{expiries}")
        if not expiries:
            logger.error("❌ 查不到 TX 的掛牌月份，期貨探測會回 None")
            return 1
        report_raw_field(
            f"期貨 TX{expiries[0]}", resolver.resolve_index_futures("TX", expiries[0])
        )

        logger.info("=== 資料源實際解析出來的日期 ===")
        stock_feed: TwStockLiveDataFeed = TwStockLiveDataFeed(broker)
        futures_feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(broker)

        stock_date: Optional[Any] = stock_feed._broker_contract_update_date()
        futures_date: Optional[Any] = futures_feed._broker_contract_update_date()
        logger.info(f"  股票資料源：{stock_date!r}")
        logger.info(f"  期貨資料源：{futures_date!r}")

        if futures_date is None:
            logger.error(
                "❌ 期貨解析不出更新日期：改用期貨合約後，平日的交易日判定會失去唯一佐證"
            )
            return 1

        logger.success("✅ 期貨合約帶得出更新日期，改用期貨合約不會失去佐證")
        if stock_date != futures_date:
            logger.warning(
                f"⚠️ 兩個市場的更新日不同（股票 {stock_date}／期貨 {futures_date}）——"
                "這正是原本共用股票合約會判錯的情形"
            )
        return 0

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
