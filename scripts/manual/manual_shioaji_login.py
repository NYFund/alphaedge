import argparse
import sys
from typing import Any, List, Optional

from loguru import logger

from core.broker.tw.shioaji_session import ShioajiSession
from core.config.settings import now_live

"""
`ShioajiSession` 的模擬環境冒煙腳本：登入 → 列出帳號 → 核對合約欄位 → 登出

**本檔是手動執行的腳本，不是測試**：它需要真實的 Shioaji 金鑰並實際連線。
`ShioajiSession` 的行為（預設模擬環境、登入失敗要拋出、憑證失敗要拋出、
時鐘檢查、重連退避與單日上限）已由 `tests/live/test_shioaji_session.py` 以假 API 覆蓋；
這支腳本要回答的是另一個問題——**券商真的會回什麼**。

執行（一律在專案根目錄以 `-m`）：

    .venv/bin/python -m scripts.manual.manual_shioaji_login
    .venv/bin/python -m scripts.manual.manual_shioaji_login --activate-ca

`--activate-ca` 對應官方在正式下單前要求的模擬環境 API 測試流程；平時不需要，
開發機與 CI 不該接觸正式憑證。

**本腳本不會下單**，也**不提供連正式環境的選項**：正式環境只能由
`run.py --production --confirm-production` 進入。
"""

# 要核對的合約欄位：實盤的取價、風控與股期解析都假設它們存在，以實連確認
CONTRACT_FIELDS_TO_VERIFY: List[str] = [
    "reference",  # 參考價（漲跌停基準，取代公式推算）
    "limit_up",
    "limit_down",
    "update_date",  # 合約檔更新日（本機日期檢查的依據）
    "day_trade",  # 可否當沖
    "margin_trading_balance",  # 融資餘額
    "short_selling_balance",  # 融券餘額
    "unit",  # 契約單位（股期乘數的來源）
    "multiplier",
    "underlying_code",  # 股期標的代號（股期合約解析的對照鍵）
]


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Shioaji 模擬環境登入冒煙測試（不下單）"
    )
    parser.add_argument(
        "--activate-ca",
        action="store_true",
        help="在模擬環境啟用 CA 憑證（官方 API 測試流程要求時才需要）",
    )
    parser.add_argument("--stock-id", default="2330", help="用來核對合約欄位的股票代號")
    return parser.parse_args()


def report_contract_fields(api: Any, stock_id: str) -> None:
    """逐一印出合約欄位的實際值與型別（欄位名對了、型別不對照樣會出事）"""

    try:
        contract: Any = api.Contracts.Stocks.TSE[stock_id]
    except Exception as exc:
        logger.opt(exception=True).error(f"取不到 {stock_id} 的合約：{exc}")
        return

    logger.info(f"=== {stock_id} 合約欄位實測 ===")
    for field in CONTRACT_FIELDS_TO_VERIFY:
        value: Any = getattr(contract, field, "<欄位不存在>")
        logger.info(f"  {field}: {value!r}（{type(value).__name__}）")


def report_futures_categories(api: Any) -> None:
    """列出期貨分類代碼，核對 `SHIOAJI_FUTURES_CATEGORY` 是否仍然正確"""

    try:
        # shioaji 1.7 的合約容器是原生物件，`dir()` 列不出分類；
        # 改為迭代各群組，讀第一檔合約的 `root`（分類代碼）
        categories: List[str] = sorted(
            {
                str(getattr(contract, "root", ""))
                for group in api.Contracts.Futures
                for contract in list(group)[:1]
                if getattr(contract, "root", "")
            }
        )
    except Exception as exc:
        logger.opt(exception=True).error(f"列出期貨分類失敗：{exc}")
        return

    logger.info(f"=== 期貨分類代碼（{len(categories)} 類）===")
    logger.info(f"  {categories}")


# 登入失敗時的排查順序。**先問「是不是現在不該跑」再問「是不是壞了」**：
# 這支腳本最常見的失敗是在非交易時段跑，而那不是故障
CONNECT_FAILURE_HINTS: List[str] = [
    "1. 現在是不是模擬環境的服務時段？非交易時段（尤其是深夜）連不上是常態，"
    "請在交易日盤中重跑一次再判斷。",
    "2. TCP 是否通？`python -c \"import socket; socket.create_connection(('210.59.255.161', 80), 3)\"`；"
    "秒回代表網路沒問題，卡的是 session 協商。",
    "3. API 使用條款是否同意、股票與期貨 API 權限是否都已開通。",
    "4. `.env` 的 API_KEY／API_SECRET_KEY 是否為這個帳號的金鑰。",
]


def main() -> int:
    """
    - Description:
        登入模擬環境、列出帳號與合約欄位，最後登出
    - Return:
        - int
            0 成功；1 連線失敗（**不印 traceback**，理由見下方註解）
    """

    args: argparse.Namespace = parse_arguments()

    session: ShioajiSession = ShioajiSession(
        simulation=True, activate_ca=args.activate_ca
    )

    try:
        try:
            session.connect()
        except ConnectionError as exc:
            # 只印刮過憑證的單行原因＋排查清單。
            #
            # **刻意不讓例外往上拋**：未接住的 traceback 會把登入請求的簽章負載
            # 印到終端（`ShioajiSession.redact_credentials()` 已刮過，但沒必要再冒一次險），
            # 而且真正的原因會被埋在四百多字後面，肉眼掃不到。
            logger.error(str(exc))
            for hint in CONNECT_FAILURE_HINTS:
                logger.error(f"  {hint}")
            return 1

        api: Optional[Any] = session.api
        # **一定要用 `now_live()`**：`datetime.now()` 回的是本機時區，
        # 在非台灣的機器上會印出一個標著「台北」卻不是台北的時間——
        # 而這支腳本正是拿來查時鐘問題的
        logger.info(f"連線時間（台北）：{now_live().isoformat()}")
        logger.info(f"stock_account: {getattr(api, 'stock_account', None)}")
        logger.info(f"futopt_account: {getattr(api, 'futopt_account', None)}")

        report_contract_fields(api, args.stock_id)
        report_futures_categories(api)
    finally:
        # `close()` 是冪等的，連線失敗的路徑上呼叫它也安全
        session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
