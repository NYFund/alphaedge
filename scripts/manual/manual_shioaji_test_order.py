import argparse
import sys
from typing import Any, List, Optional, Tuple

import shioaji as sj
from loguru import logger

from core.broker.tw.shioaji_session import ShioajiSession
from core.config.settings import now_live

"""
永豐 API 測試用的委託：模擬環境各送一筆股票與期貨 ROD 限價單

**這是 Phase0-1 的官方流程要求**，不是功能驗證：券商要看到模擬環境有實際委託
才會開通正式下單權限。`ShioajiOrderMapper` 的轉換已由單元測試覆蓋，
這支腳本要回答的是「券商真的收得到嗎、回什麼狀態」。

**價格一律取合約的 `limit_down`（買進）**，理由有兩個：

1. **保證落在漲跌停範圍內**。隨手填一個「合理價」在指數期貨上很容易落在 ±10%
   之外，交易所直接退單——而退單訊息看起來像權限問題，實際上是價格問題。
2. **掛著不會成交**。跌停價的買單除非市場崩到跌停否則不會撮合，
   測試完撤掉即可，不會留下部位。

執行（一律在專案根目錄以 `-m`，且要在**交易日盤中**）：

    .venv/bin/python -m scripts.manual.manual_shioaji_test_order --confirm

金鑰讀 `.env`（`API_KEY`／`API_SECRET_KEY`），**不接受命令列傳入**——
金鑰打在命令列會進 shell 歷史。

**本腳本只連模擬環境**，沒有連正式環境的選項。
"""

STOCK_SYMBOL: str = "2330"
FUTURES_PRODUCT: str = "TXF"

# 週選的代號尾碼；近月合約要把它們排除
WEEKLY_SUFFIXES: Tuple[str, ...] = ("R1", "R2")

# 送單、撤單、查狀態都**等券商回覆**，單位毫秒。
#
# **不可以用 `timeout=0`**：那是非阻塞，`place_order()` 會在券商確認之前就回傳，
# 於是狀態一定是 `Inactive`、委託序號一定是空的——看起來像被拒，其實只是
# 還沒收到回覆。2026-09-21 第一次實跑就是這樣，完全判斷不出委託到底有沒有進去
ORDER_TIMEOUT_MS: int = 5000


def parse_arguments() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="模擬環境的 API 測試委託（股票 ＋ 期貨各一筆）"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="確認要送出測試委託；**不帶這個旗標只做檢查不下單**",
    )
    parser.add_argument(
        "--cancel",
        action="store_true",
        help="送出後立刻撤單（跌停買單本來就不會成交，撤掉更乾淨）",
    )
    return parser.parse_args()


def near_month_futures(api: Any) -> Optional[Any]:
    """
    取最近交割月的台指期

    **排除週選**（代號尾碼 `R1`／`R2`）：它們的交割日比近月早，
    不排除的話 `min(delivery_date)` 會挑到週選，而那不是一般說的「近月」。
    """

    candidates: List[Any] = [
        contract
        for contract in api.Contracts.Futures.TXF
        if contract.code[-2:] not in WEEKLY_SUFFIXES
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda contract: contract.delivery_date)


def limit_down_price(contract: Any) -> Optional[float]:
    """
    取合約的跌停價

    取不到就回 None 而不是自己推算：漲跌停基準是券商給的，
    自己算出來的價格一旦落在範圍外就是退單。
    """

    raw: Any = getattr(contract, "limit_down", None)
    return float(raw) if raw else None


def describe(trade: Any) -> str:
    """把 `Trade` 壓成一行；欄位缺了就顯示 `-`，不要讓整份輸出因此中斷"""

    status: Any = getattr(trade, "status", None)
    order: Any = getattr(trade, "order", None)
    return (
        f"status={getattr(status, 'status', '-')} "
        f"seqno={getattr(status, 'id', '-')} "
        f"price={getattr(order, 'price', '-')} "
        f"qty={getattr(order, 'quantity', '-')}"
    )


def place_stock_order(api: Any, contract: Any, price: float) -> Any:
    """股票：ROD 限價買進 1 張"""

    order: Any = api.Order(
        action=sj.Action.Buy,
        price=price,
        quantity=1,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        account=api.stock_account,
    )
    return api.place_order(contract, order, timeout=ORDER_TIMEOUT_MS)


def place_futures_order(api: Any, contract: Any, price: float) -> Any:
    """
    期貨：ROD 限價買進 1 口

    兩個容易寫錯的地方：

    1. **`price_type` 要用 `FuturesPriceType` 不是 `StockPriceType`**：兩者的
       `LMT` 剛好同值所以不會當場炸，但期貨多一個 `MKP`（範圍市價），
       混用遲早會送出一個股票那邊沒有的值，而錯誤要到券商退單才出現。
    2. **`order_type` 是共用的 `OrderType`，沒有 `FuturesOrderType`**：
       shioaji 只有 `OrderType`（ROD／IOC／FOK；1.3.3 與 1.7.5 皆然，2026-09-21 實查），
       寫成 `FuturesOrderType` 會在送單前就 `AttributeError`。
    """

    order: Any = api.Order(
        action=sj.Action.Buy,
        price=price,
        quantity=1,
        price_type=sj.FuturesPriceType.LMT,
        order_type=sj.OrderType.ROD,
        octype=sj.FuturesOCType.Auto,
        account=api.futopt_account,
    )
    return api.place_order(contract, order, timeout=ORDER_TIMEOUT_MS)


def main() -> int:
    """
    - Description:
        登入模擬環境 → 檢查帳號 → 各送一筆測試單 → 印出狀態 → 登出
    - Return:
        - int
            0 成功；1 連線失敗；2 帳號或合約不齊；3 未帶 `--confirm`
    """

    args: argparse.Namespace = parse_arguments()
    session: ShioajiSession = ShioajiSession(simulation=True)

    try:
        session.connect()
    except Exception as exc:
        logger.error(f"登入失敗：{exc}")
        return 1

    try:
        api: Any = session.api
        logger.info(f"台北時間 {now_live():%Y-%m-%d %H:%M:%S}（模擬環境）")

        has_stock: bool = getattr(api, "stock_account", None) is not None
        has_futopt: bool = getattr(api, "futopt_account", None) is not None
        logger.info(f"股票帳號：{'有' if has_stock else '**無**'}")
        logger.info(f"期貨帳號：{'有' if has_futopt else '**無**'}")

        stock_contract: Any = api.Contracts.Stocks.get(STOCK_SYMBOL)
        futures_contract: Optional[Any] = (
            near_month_futures(api) if has_futopt else None
        )

        if stock_contract is None:
            logger.error(f"取不到 {STOCK_SYMBOL} 的合約")
            return 2

        stock_price: Optional[float] = limit_down_price(stock_contract)
        logger.info(f"{STOCK_SYMBOL} 跌停價 {stock_price}")

        if futures_contract is not None:
            logger.info(
                f"近月台指期 {futures_contract.code}"
                f"（交割 {futures_contract.delivery_date}）"
                f" 跌停價 {limit_down_price(futures_contract)}"
            )
        elif has_futopt:
            logger.error("有期貨帳號但取不到台指期合約")
            return 2

        if not args.confirm:
            logger.warning("未帶 --confirm，只做檢查不送單")
            return 3

        # **一律以跌停價買進**：在漲跌停範圍內且不會成交
        trades: List[Tuple[str, Any]] = []
        if stock_price is not None:
            trades.append(("股票", place_stock_order(api, stock_contract, stock_price)))

        if futures_contract is not None:
            futures_price: Optional[float] = limit_down_price(futures_contract)
            if futures_price is not None:
                trades.append(
                    ("期貨", place_futures_order(api, futures_contract, futures_price))
                )

        api.update_status(timeout=ORDER_TIMEOUT_MS)
        for label, trade in trades:
            logger.info(f"{label}委託：{describe(trade)}")

        if args.cancel:
            for label, trade in trades:
                try:
                    api.cancel_order(trade, timeout=ORDER_TIMEOUT_MS)
                    logger.info(f"{label}委託已送出撤單")
                except Exception as exc:
                    logger.warning(f"{label}撤單失敗（跌停買單本來就不會成交）：{exc}")
            api.update_status(timeout=ORDER_TIMEOUT_MS)
            for label, trade in trades:
                logger.info(f"{label}撤單後：{describe(trade)}")

        if not has_futopt:
            logger.warning(
                "期貨帳號仍為 None，只送出了股票測試單；"
                "請確認期貨 API 權限已開通（可能需要一個工作日）"
            )
            return 2
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
