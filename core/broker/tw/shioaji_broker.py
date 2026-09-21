import datetime
import queue
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from core.broker.base import BaseBroker
from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.broker.tw.shioaji_contract_resolver import ShioajiContractResolver
from core.broker.tw.shioaji_execution_handler import ShioajiExecutionHandler
from core.broker.tw.shioaji_order_mapper import ShioajiOrderMapper
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.broker.tw.shioaji_session import ShioajiSession
from core.config.settings import now_live
from core.models import (
    BaseQuote,
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    FuturesOrder,
    OrderTicket,
    StockOrder,
)
from core.utils import FuturesOCType, InstrumentType, LiveOrderStatus, Status

"""
ShioajiBroker：把 Phase2-2~Phase2-7 的元件組成一個 `BaseBroker` 實作

**這一層只做委派與限流套用，不寫業務邏輯。** 委託前處理、風控、狀態機、歸屬
都在上層；這裡放進來的話，換券商時要連同那些一起重寫，而那些跟券商無關。

一個例外是 `Trade` 物件的保管：Shioaji 的撤單與改價都要傳回**原本那個 `Trade`**，
不是委託編號。那是券商 SDK 的形狀，只有這一層知道，所以由它存。
"""


class ShioajiBroker(BaseBroker):
    """永豐金 Shioaji 的券商閘道實作"""

    def __init__(
        self,
        session: ShioajiSession,
        rate_limiter: Optional[RateLimiter] = None,
        record_path: Optional[Any] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立閘道（此時尚未連線）
        - Parameters:
            - session: ShioajiSession
                連線 session；**由外部注入**，因為一個帳戶只能有一個
            - rate_limiter: Optional[RateLimiter]
                限流器；未提供時沿用 session 持有的那一個
            - record_path: Optional[Any]
                回報錄製的 JSONL 路徑
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
        """

        super().__init__()

        self.session: ShioajiSession = session
        self.rate_limiter: RateLimiter = (
            rate_limiter if rate_limiter is not None else session.rate_limiter
        )
        self._record_path: Optional[Any] = record_path
        self._now: Callable[[], datetime.datetime] = now_provider

        # 行情事件與委託回報分屬不同佇列：行情量大且可丟（過期報價沒有價值），
        # 回報一筆都不能丟。混在一起會讓回報排在幾千筆行情後面
        self.quote_queue: queue.Queue = queue.Queue()

        self.resolver: Optional[ShioajiContractResolver] = None
        self.mapper: Optional[ShioajiOrderMapper] = None
        self.account_query: Optional[ShioajiAccountQuery] = None
        self.quote_stream: Optional[ShioajiQuoteStream] = None
        self.execution_handler: Optional[ShioajiExecutionHandler] = None

        # client_order_id → Shioaji 的 `Trade`。撤單與改價都要傳回原本那個物件
        self._trades: Dict[str, Any] = {}

    # === 連線 ===
    def connect(self) -> None:
        """登入並組裝所有元件；任何一步失敗都拋出，不會留下半成品狀態"""

        self.session.connect()
        self._bind_session()

    def _bind_session(self) -> None:
        """
        以目前的 session 重建所有元件與回呼

        **重連之後一定要重跑一次**：登入換了一個 api 物件，
        舊的解析器、回呼與行情訂閱全掛在已經死掉的那一個上——
        不重建的話行情與回報都進不來，而且不會有任何錯誤訊息。
        """

        api: Any = self.session.api

        self.resolver = ShioajiContractResolver(api)
        self.mapper = ShioajiOrderMapper(
            stock_account=getattr(api, "stock_account", None),
            futopt_account=getattr(api, "futopt_account", None),
        )
        self.account_query = ShioajiAccountQuery(
            api, self.rate_limiter, now_provider=self._now
        )
        self.quote_stream = ShioajiQuoteStream(
            api, self.rate_limiter, self.quote_queue, now_provider=self._now
        )
        self.execution_handler = ShioajiExecutionHandler(
            self.execution_queue,
            record_path=self._record_path,
            on_first_report_ts=self.session.check_report_clock_skew,
        )
        self.execution_handler.register(api)
        self.quote_stream.register_callbacks()

    def close(self) -> None:
        """關閉連線；可重複呼叫"""

        self.session.close()

    def reconnect(self) -> bool:
        """
        以 session 的退避重連；**不用骨架那個「關掉再連」的預設**

        Shioaji 的登入額度是每日 1,000 次，而需要重連 20 次的那一天本來就不該
        繼續交易。退避與每日上限都在 `ShioajiSession.reconnect()` 裡。

        重連成功後**回呼要重新註冊**：換了一個 api 物件，舊的回呼掛在已經死掉的
        session 上——不重掛的話行情與回報都進不來，而且不會有任何錯誤。
        """

        if not self.session.reconnect():
            return False

        self._bind_session()
        return self.session.is_connected()

    def is_connected(self) -> bool:
        """目前是否連線中"""

        return self.session.is_connected()

    def _require_ready(self) -> Any:
        """取得已連線的 API；未連線時拋出而不是靜默排隊"""

        if not self.is_connected() or self.session.api is None:
            raise ConnectionError("ShioajiBroker 尚未連線")
        return self.session.api

    # === 下單 ===
    def place_order(self, ticket: OrderTicket) -> OrderTicket:
        """
        - Description:
            送出委託

            順序固定：解析合約 → 轉換委託 → 取限流額度 → 送出 → 回填券商編號。
            **限流在送出前才取**，不在轉換前：轉換失敗時不該白白吃掉一個額度，
            而尾盤段的額度是有限的。
        - Parameters:
            - ticket: OrderTicket
                已寫入本地、狀態為 `PENDING_SUBMIT` 的委託
        - Return:
            - OrderTicket
                補上券商編號與最新狀態的同一張委託
        """

        api: Any = self._require_ready()
        contract, broker_order = self._build_order(ticket)

        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        trade: Any = api.place_order(contract, broker_order)

        self._trades[ticket.client_order_id] = trade
        self._apply_trade(ticket, trade)
        ticket.updated_at = self._now()
        return ticket

    def _build_order(self, ticket: OrderTicket) -> tuple:
        """依商品類別解析合約並轉換委託；轉換規則全在 mapper 裡"""

        order: Any = ticket.order
        if order is None:
            raise ValueError(f"委託 {ticket.client_order_id} 沒有訂單內容")

        custom_field: str = self._compress(ticket.client_order_id)

        if isinstance(order, StockOrder):
            contract: Any = self.resolver.resolve_stock(order.stock_id)
            return (
                contract,
                self.mapper.to_shioaji_stock_order(
                    order, custom_field=custom_field, contract=contract
                ),
            )

        if isinstance(order, FuturesOrder):
            contract = self.resolver.resolve_index_futures(order.product, order.expiry)
            octype: FuturesOCType = getattr(ticket, "octype", None) or FuturesOCType.New
            return (
                contract,
                self.mapper.to_shioaji_futures_order(
                    order, octype=octype, custom_field=custom_field
                ),
            )

        raise ValueError(f"不支援的訂單型別：{type(order).__name__}")

    def cancel_order(self, ticket: OrderTicket) -> OrderTicket:
        """
        - Description:
            撤銷未成交的委託

            已是終態的委託直接回傳，**不送撤單請求**：那會拿到一個看起來很像
            真問題的券商錯誤，而它只是「這張單早就沒了」。
        - Parameters:
            - ticket: OrderTicket
                要撤的委託
        - Return:
            - OrderTicket
                更新後的委託
        """

        api: Any = self._require_ready()
        if ticket.is_terminal:
            return ticket

        trade: Optional[Any] = self._trades.get(ticket.client_order_id)
        if trade is None:
            raise LookupError(
                f"找不到 {ticket.client_order_id} 對應的 Trade，無法撤單；"
                "重啟後要先以 refresh_order_status() 接管才撤得掉"
            )

        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        self._apply_trade(ticket, api.cancel_order(trade))
        ticket.updated_at = self._now()
        return ticket

    def update_order_price(self, ticket: OrderTicket, price: float) -> OrderTicket:
        """
        - Description:
            改價（平倉與停損單未成交時的唯一追價手段）
        - Parameters:
            - ticket: OrderTicket
                要改價的委託
            - price: float
                新的委託價
        - Return:
            - OrderTicket
                更新後的委託
        """

        api: Any = self._require_ready()
        if ticket.is_terminal:
            raise ValueError(f"委託 {ticket.client_order_id} 已終結，不可改價")

        trade: Optional[Any] = self._trades.get(ticket.client_order_id)
        if trade is None:
            raise LookupError(f"找不到 {ticket.client_order_id} 對應的 Trade，無法改價")

        aligned: float = self.mapper.align_price(price, ticket.order.action)
        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        self._apply_trade(ticket, api.update_order(trade, price=aligned))
        ticket.updated_at = self._now()
        return ticket

    def refresh_order_status(self) -> List[OrderTicket]:
        """
        - Description:
            向券商查詢當日所有委託的最新狀態

            **這是恢復流程用的，不是心跳**：`update_status` 算在下單類額度裡，
            拿它輪詢會把尾盤那 4 分鐘的送單額度吃光。
        - Return:
            - List[OrderTicket]
                券商端當日的委託清單
        """

        api: Any = self._require_ready()

        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        api.update_status(getattr(api, "stock_account", None))

        tickets: List[OrderTicket] = []
        for trade in api.list_trades() or []:
            ticket: OrderTicket = OrderTicket(updated_at=self._now())
            self._apply_trade(ticket, trade)
            self._trades[ticket.client_order_id or ticket.broker_seqno or ""] = trade
            tickets.append(ticket)
        return tickets

    # === 帳務 ===
    def get_positions(self) -> List[BrokerPositionSnapshot]:
        """取得券商端的部位（股票與期貨合併回傳）"""

        self._require_ready()
        positions: List[BrokerPositionSnapshot] = list(
            self.account_query.get_stock_positions()
        )
        positions.extend(self.account_query.get_futures_positions())
        return positions

    def get_account(self) -> BrokerAccountSnapshot:
        """取得股票帳戶的帳務快照；期貨保證金另以 `get_futures_account()` 取得"""

        self._require_ready()
        return self.account_query.get_stock_account()

    # === 行情 ===
    def get_snapshots(self, symbols: List[str]) -> List[BaseQuote]:
        """
        - Description:
            取得即時快照

            **只處理股票**：期貨要先知道到期月份才解析得出合約，那是引擎的資訊，
            不是這裡能猜的。期貨快照走 `get_futures_snapshots()`。
        - Parameters:
            - symbols: List[str]
                股票代號清單
        - Return:
            - List[BaseQuote]
                報價清單；查無資料的標的不會出現
        """

        self._require_ready()
        contracts: List[Any] = list(self.resolver.resolve_stocks(symbols).values())
        return list(self.quote_stream.get_stock_snapshots(contracts))

    def subscribe_quotes(self, symbols: List[str]) -> None:
        """訂閱股票逐筆行情"""

        self._require_ready()
        self.quote_stream.subscribe(
            list(self.resolver.resolve_stocks(symbols).values())
        )

    def unsubscribe_quotes(self, symbols: List[str]) -> None:
        """取消訂閱"""

        self._require_ready()
        self.quote_stream.unsubscribe(
            list(self.resolver.resolve_stocks(symbols).values())
        )

    # === 委託狀態轉換 ===
    def _apply_trade(self, ticket: OrderTicket, trade: Any) -> None:
        """把 Shioaji 的 `Trade` 回填到本地委託"""

        order: Any = getattr(trade, "order", None)
        status: Any = getattr(trade, "status", None)

        if order is not None:
            ticket.broker_seqno = str(getattr(order, "seqno", "") or "") or None
            ticket.broker_order_id = str(getattr(order, "ordno", "") or "") or None
            custom_field: str = str(getattr(order, "custom_field", "") or "")
            if custom_field and not ticket.client_order_id:
                ticket.client_order_id = custom_field

        if status is not None:
            ticket.status = self.to_live_status(getattr(status, "status", None))
            ticket.filled_volume = int(getattr(status, "deal_quantity", 0) or 0)
            ticket.reject_reason = str(getattr(status, "msg", "") or "") or None

    @staticmethod
    def to_live_status(broker_status: Any) -> LiveOrderStatus:
        """
        - Description:
            券商回報狀態 → 本地 OMS 狀態

            兩者刻意分開：`Status` 是券商說的，`LiveOrderStatus` 是本地狀態機。
            認不得的值一律當成 `FAILED` 而不是 `SUBMITTED`——**把未知當成「還在場上」
            會讓引擎繼續等一張其實已經不存在的單**，而當成失敗只會多做一次查詢。
        - Parameters:
            - broker_status: Any
                Shioaji 的 `Status`
        - Return:
            - LiveOrderStatus
                本地狀態
        """

        text: str = str(getattr(broker_status, "value", broker_status))
        mapping: Dict[str, LiveOrderStatus] = {
            Status.PendingSubmit.value: LiveOrderStatus.PENDING_SUBMIT,
            Status.PreSubmitted.value: LiveOrderStatus.PENDING_SUBMIT,
            Status.Submitted.value: LiveOrderStatus.SUBMITTED,
            Status.PartFilled.value: LiveOrderStatus.PARTIALLY_FILLED,
            Status.Filled.value: LiveOrderStatus.FILLED,
            Status.Cancelled.value: LiveOrderStatus.CANCELLED,
            Status.Inactive.value: LiveOrderStatus.CANCELLED,
            Status.Failed.value: LiveOrderStatus.FAILED,
        }
        if text not in mapping:
            logger.warning(f"未知的券商委託狀態：{text!r}，本地記為 FAILED")
        return mapping.get(text, LiveOrderStatus.FAILED)

    @staticmethod
    def _compress(client_order_id: str) -> str:
        """
        把 client order id 壓成 6 個字元放進 `custom_field`

        目前取末 6 碼；正式的 base36 壓縮碼在 OMS 產生識別碼時一併定案
        （那裡才知道 run 序號與委託序號的位數）。
        """

        return client_order_id[-ShioajiOrderMapper.CUSTOM_FIELD_MAX_LENGTH :]

    # === 期貨專用 ===
    def get_futures_account(self) -> BrokerAccountSnapshot:
        """取得期貨保證金帳務快照"""

        self._require_ready()
        return self.account_query.get_futures_account()

    def get_futures_snapshots(self, contracts: List[Any]) -> List[BaseQuote]:
        """
        取得期貨快照

        收的是**已解析的合約**而不是代號：期貨要先知道到期月份才解析得出合約，
        而那是引擎依換月規則決定的。
        """

        self._require_ready()
        return list(self.quote_stream.get_futures_snapshots(contracts))

    @staticmethod
    def supported_instruments() -> tuple:
        """本閘道支援的商品類別"""

        return (InstrumentType.STOCK, InstrumentType.FUTURES)
