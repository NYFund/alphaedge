import datetime
from typing import Any, Callable, Iterable, List, Optional, Tuple

from loguru import logger

from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.config.settings import now_live
from core.models import (
    BrokerAccountSnapshot,
    FuturesAccountSnapshot,
    FuturesPositionSnapshot,
    RealizedTradeSnapshot,
    StockPositionSnapshot,
)
from core.utils import Action, PositionType, StockOrderCond, Units
from core.utils.instrument import FuturesUtils

"""
帳務查詢：把券商的餘額、交割款、部位與保證金轉成正規化快照

對帳與資金分配都建立在這裡的數字上，所以兩件事特別要緊：

1. **交割款依「欄位名」加總，不用位置索引。** Shioaji 的 `Settlement` 本來就有
   `t_money`／`t1_money`／`t2_money` 具名欄位；靠列號取值（例如取第 1、2 列當成
   T+1、T+2）時，交割日數或回傳列數一變就會安靜地算到別的金額。
2. **帳務查詢全部走 `ACCOUNT` 類限流**（25 次／5 秒）。它與下單額度分開計算，
   但一樣會因為超限被暫停服務——而對帳失敗會讓整段交易停下來。
"""


class ShioajiAccountQuery:
    """券商帳務查詢；每個方法都是一次外部呼叫，全部經過限流"""

    def __init__(
        self,
        api: Any,
        rate_limiter: RateLimiter,
        now_provider: Callable[[], datetime.datetime] = now_live,
        futures_symbol: Optional[Callable[[str], str]] = None,
    ) -> None:
        """
        - Description:
            建立查詢器
        - Parameters:
            - api: Any
                已登入的 Shioaji API 物件
            - rate_limiter: RateLimiter
                **與下單共用的同一個實例**：額度是帳戶級的
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware），供快照時戳使用
            - futures_symbol: Optional[Callable[[str], str]]
                期貨月份字母碼（`TXFJ6`）→ 專案代號（`TX202610`）；None 時不轉換
        """

        self.api: Any = api
        self._futures_symbol: Optional[Callable[[str], str]] = futures_symbol
        self.rate_limiter: RateLimiter = rate_limiter
        self._now: Callable[[], datetime.datetime] = now_provider

    # === 股票 ===
    def get_stock_account(self) -> BrokerAccountSnapshot:
        """
        - Description:
            股票帳戶快照

            `available_balance` 取帳戶餘額（今天就能動用的錢）；
            `total_equity` 另加**未交割款**與**持倉市值**（成本 ＋ 未實現損益）——
            它是資金額度檢查的分母，拿可用餘額當分母的話，只要隔日還有部位在場上
            就必然誤判成額度超標。

            **成本要乘每張股數**：`list_positions()` 的 `quantity` 單位是張、
            `price` 是每股均價（2026-09-22 模擬環境實測），直接相乘會少 1000 倍，
            總權益被低估到幾乎只剩未實現損益。
        - Return:
            - BrokerAccountSnapshot
                正規化後的帳務快照
        """

        self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
        balance: Any = self.api.account_balance()

        self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
        settlements: Any = self.api.list_settlements(self.api.stock_account)

        positions: List[StockPositionSnapshot] = self.get_stock_positions()

        available: float = float(getattr(balance, "acc_balance", 0.0) or 0.0)
        pending: float = self.sum_pending_settlements(settlements)
        position_cost: float = sum(
            position.volume * Units.LOT * position.avg_price for position in positions
        )
        unrealized: float = sum(position.unrealized_pnl for position in positions)

        return BrokerAccountSnapshot(
            ts=self._now(),
            available_balance=available,
            total_equity=available + pending + position_cost + unrealized,
            unrealized_pnl=unrealized,
            raw={
                "acc_balance": available,
                "pending_settlement": pending,
                "position_cost": position_cost,
            },
        )

    def get_stock_positions(self) -> List[StockPositionSnapshot]:
        """
        - Description:
            股票部位快照

            **帶上 `cond`（融資券別）**：同一檔股票的現股多單與融券空單在券商端是
            兩筆不同的部位，只比對「代號 ＋ 方向 ＋ 數量」的話，兩者互換時
            數字會剛好對得上，對帳就看不出差異。
        - Return:
            - List[StockPositionSnapshot]
                部位清單；沒有部位時為空清單
        """

        self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
        raw_positions: Iterable[Any] = (
            self.api.list_positions(self.api.stock_account) or []
        )

        return [
            StockPositionSnapshot(
                symbol=str(getattr(position, "code", "")),
                direction=self.to_position_type(getattr(position, "direction", None)),
                volume=int(getattr(position, "quantity", 0) or 0),
                avg_price=float(getattr(position, "price", 0.0) or 0.0),
                unrealized_pnl=float(getattr(position, "pnl", 0.0) or 0.0),
                order_cond=self.to_order_cond(getattr(position, "cond", None)),
                raw=self._dump(position),
            )
            for position in raw_positions
        ]

    # === 期貨 ===
    def get_futures_account(self) -> FuturesAccountSnapshot:
        """
        - Description:
            期貨帳戶快照

            期貨的「還能不能再開一口」看的是**可用保證金**，不是帳戶餘額，
            故三個保證金欄位都要帶。
        - Return:
            - FuturesAccountSnapshot
                正規化後的保證金帳務快照
        """

        self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
        margin: Any = self.api.margin(self.api.futopt_account)

        available: float = float(getattr(margin, "available_margin", 0.0) or 0.0)
        equity: float = float(getattr(margin, "equity_amount", 0.0) or 0.0)

        return FuturesAccountSnapshot(
            ts=self._now(),
            available_balance=available,
            total_equity=equity,
            initial_margin=float(getattr(margin, "initial_margin", 0.0) or 0.0),
            maintenance_margin=float(getattr(margin, "maintenance_margin", 0.0) or 0.0),
            available_margin=available,
            raw=self._dump(margin),
        )

    def get_futures_positions(self) -> List[FuturesPositionSnapshot]:
        """
        - Description:
            期貨部位快照

            **`code` 要拆成 `product` ＋ `expiry`**：換月期間同一商品會同時有兩個月份的
            部位，只看合併後的代號會把「還沒平掉的舊月」與「已經開好的新月」
            當成同一件事。
        - Return:
            - List[FuturesPositionSnapshot]
                部位清單
        """

        self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
        raw_positions: Iterable[Any] = (
            self.api.list_positions(self.api.futopt_account) or []
        )

        snapshots: List[FuturesPositionSnapshot] = []
        for position in raw_positions:
            # 部位的代碼是月份字母碼（`TXFJ6`），換成與 `FuturesOrder.symbol` 相同的
            # `{商品}{YYYYMM}`，對帳才比對得到本地歸屬帳
            code: str = str(getattr(position, "code", ""))
            symbol: str = (
                self._futures_symbol(code) if self._futures_symbol is not None else code
            )
            product, expiry = self.split_contract_code(symbol)
            snapshots.append(
                FuturesPositionSnapshot(
                    symbol=symbol,
                    direction=self.to_position_type(
                        getattr(position, "direction", None)
                    ),
                    volume=int(getattr(position, "quantity", 0) or 0),
                    avg_price=float(getattr(position, "price", 0.0) or 0.0),
                    unrealized_pnl=float(getattr(position, "pnl", 0.0) or 0.0),
                    product=product,
                    expiry=expiry,
                    raw=self._dump(position),
                )
            )
        return snapshots

    # === 已實現損益 ===
    def get_realized_trades(
        self, run_date: datetime.date
    ) -> List[RealizedTradeSnapshot]:
        """
        - Description:
            當日已平倉的交易（股票與期貨），盤後校正成本用

            `list_profit_loss()` 以「一組開平倉」為單位（2026-09-22 模擬環境實測）：
            股票只給淨損益、平倉價與開倉委託序號，**沒有費用與稅**；期貨給開平倉價、
            `fee`、`tax`，**沒有委託序號**。`list_profit_loss_detail()` 回空，不用它。
            期貨代碼是月份字母碼，換成 `{商品}{YYYYMM}` 與本地一致。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[RealizedTradeSnapshot]
                已平倉交易；某一邊帳號不存在時略過該邊
        """

        day: str = run_date.isoformat()
        trades: List[RealizedTradeSnapshot] = []

        if getattr(self.api, "stock_account", None) is not None:
            self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
            for row in (
                self.api.list_profit_loss(
                    self.api.stock_account, begin_date=day, end_date=day
                )
                or []
            ):
                trades.append(
                    RealizedTradeSnapshot(
                        symbol=str(getattr(row, "code", "")),
                        quantity=int(getattr(row, "quantity", 0) or 0),
                        pnl=float(getattr(row, "pnl", 0.0) or 0.0),
                        cover_price=float(getattr(row, "price", 0.0) or 0.0),
                        open_seqno=str(getattr(row, "seqno", "") or "") or None,
                        raw=self._dump(row),
                    )
                )

        if getattr(self.api, "futopt_account", None) is not None:
            self.rate_limiter.acquire(RateLimitCategory.ACCOUNT)
            for row in (
                self.api.list_profit_loss(
                    self.api.futopt_account, begin_date=day, end_date=day
                )
                or []
            ):
                code: str = str(getattr(row, "code", ""))
                trades.append(
                    RealizedTradeSnapshot(
                        symbol=(
                            self._futures_symbol(code)
                            if self._futures_symbol is not None
                            else code
                        ),
                        quantity=int(getattr(row, "quantity", 0) or 0),
                        pnl=float(getattr(row, "pnl", 0.0) or 0.0),
                        cover_price=float(getattr(row, "cover_price", 0.0) or 0.0),
                        entry_price=float(getattr(row, "entry_price", 0.0) or 0.0),
                        fee=float(getattr(row, "fee", 0.0) or 0.0),
                        tax=float(getattr(row, "tax", 0.0) or 0.0),
                        is_futures=True,
                        raw=self._dump(row),
                    )
                )

        return trades

    # === 轉換工具 ===
    @staticmethod
    def sum_pending_settlements(settlements: Any) -> float:
        """
        - Description:
            加總尚未交割的款項（T+1 與 T+2）

            **依欄位名取值，不用位置索引。** 兩種回傳形狀都吃得下：
            - `list_settlements()` 的 `Settlement`：具名的 `t1_money`／`t2_money`。
            - `settlements()` 的 `SettlementV1`：每列一筆，`T` 是第幾天、`amount` 是金額。

            兩種都支援，是因為券商的兩支查詢 API 回傳不同形狀，呼叫端可能拿到任一種。
        - Parameters:
            - settlements: Any
                券商回傳的交割款資料
        - Return:
            - float
                未交割款合計
        """

        if settlements is None:
            return 0.0

        # `Settlement`：單一物件，具名欄位
        if hasattr(settlements, "t1_money") or hasattr(settlements, "t2_money"):
            return float(getattr(settlements, "t1_money", 0.0) or 0.0) + float(
                getattr(settlements, "t2_money", 0.0) or 0.0
            )

        total: float = 0.0
        for item in settlements:
            if hasattr(item, "t1_money") or hasattr(item, "t2_money"):
                total += float(getattr(item, "t1_money", 0.0) or 0.0)
                total += float(getattr(item, "t2_money", 0.0) or 0.0)
                continue
            # `SettlementV1`：`T` 為 0 代表今天已交割，不算未交割款
            if int(getattr(item, "T", 0) or 0) > 0:
                total += float(getattr(item, "amount", 0.0) or 0.0)
        return total

    @staticmethod
    def to_position_type(direction: Any) -> PositionType:
        """
        - Description:
            券商的部位方向 → 本專案的 `PositionType`

            券商用的是買賣別（`Buy`／`Sell`），依**值**比對；認不得時一律當 LONG
            會把空單記成多單，故拋出讓呼叫端處理。
        - Parameters:
            - direction: Any
                券商的方向欄位
        - Return:
            - PositionType
                多空方向
        - Raise:
            - ValueError
                無法辨識的方向
        """

        text: str = str(getattr(direction, "value", direction))
        if text == Action.BUY.value:
            return PositionType.LONG
        if text == Action.SELL.value:
            return PositionType.SHORT
        raise ValueError(f"無法辨識的部位方向：{direction!r}")

    @staticmethod
    def to_order_cond(cond: Any) -> Optional[StockOrderCond]:
        """
        - Description:
            券商的融資券別 → 本專案的 `StockOrderCond`

            認不得時回 `None` 並記 warning，**不阻擋**：對帳少一個維度比整段停擺好，
            而未知的券別本來就該由人看一眼。
        - Parameters:
            - cond: Any
                券商的 `cond` 欄位
        - Return:
            - Optional[StockOrderCond]
                委託條件；無法辨識時為 None
        """

        if cond is None:
            return None

        text: str = str(getattr(cond, "value", cond))
        for member in StockOrderCond:
            if member.value == text:
                return member

        logger.warning(f"無法辨識的融資券別：{text!r}，本筆部位的券別記為 None")
        return None

    @staticmethod
    def split_contract_code(code: str) -> Tuple[str, str]:
        """
        - Description:
            把期貨合約代號拆成商品與到期月份

            **只是 `FuturesUtils.split_contract_id()` 的別名**：實盤換月那條路徑
            用的是同一條規則，而本層與 `core/live/` 不可互相 import，故權威實作
            下推到 `core/utils/`。原本兩邊各有一份逐位元組相同的實作，
            而分岔不會報錯——只會讓某一邊認不出該換月的合約。
        - Parameters:
            - code: str
                合約代號
        - Return:
            - Tuple[str, str]
                `(product, expiry)`
        """

        return FuturesUtils.split_contract_id(code)

    @staticmethod
    def _dump(model: Any) -> dict:
        """
        把券商的 pydantic 物件轉成 dict 存進 `raw`

        **一定要留原始值**：欄位語意猜錯時，這是唯一能事後重建真相的東西，
        而對帳用的數字不可能重來一次。
        """

        for attribute in ("model_dump", "dict"):
            dumper: Optional[Callable[[], dict]] = getattr(model, attribute, None)
            if callable(dumper):
                try:
                    return dumper()
                except Exception:
                    break
        return {"repr": repr(model)}
