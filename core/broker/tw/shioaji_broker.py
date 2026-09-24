import datetime
import queue
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from core.broker.base import BaseBroker, CallbackQueue
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
    StockQuote,
)
from core.utils import (
    Action,
    FuturesOCType,
    LiveOrderStatus,
    PositionType,
    ShortMethod,
    Status,
    StockOrderLot,
)

"""
ShioajiBroker：把 session、合約解析、委託轉換、回報正規化、帳務查詢與行情這幾個
元件組成一個 `BaseBroker` 實作

**這一層只做委派與限流套用，不寫業務邏輯。** 委託前處理、風控、狀態機、歸屬
都在上層；這裡放進來的話，換券商時要連同那些一起重寫，而那些跟券商無關。

一個例外是 `Trade` 物件的保管：Shioaji 的撤單與改價都要傳回**原本那個 `Trade`**，
不是委託編號。那是券商 SDK 的形狀，只有這一層知道，所以由它存。
"""


# 合約 `day_trade` 欄位的值（2026-09-22 模擬環境實測值域：Yes／OnlyBuy／No）
DAY_TRADE_BOTH_WAYS: str = "Yes"  # 先買後賣、先賣後買都可以
DAY_TRADE_BUY_FIRST_ONLY: str = "OnlyBuy"  # 只能先買後賣


class ShioajiBroker(BaseBroker):
    """永豐金 Shioaji 的券商閘道實作"""

    # 下單類呼叫（送單、撤單、改價、查狀態）的阻塞上限（毫秒）。**明寫而不用預設值**：
    # shioaji 1.7 把預設從 5 秒改成 30 秒，尾盤段只有 4 分鐘，
    # 一張卡住的單不能吃掉整段送單時間，這個上限不該跟著套件版本浮動
    ORDER_TIMEOUT_MS: int = 5000

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

        # broker_seqno → Shioaji 的 `Trade`。撤單與改價都要傳回原本那個物件。
        # **以 seqno 為鍵**：它是券商配發、跨行程不變的編號；以 client order id 為鍵的話，
        # 重啟後由 `refresh_order_status()` 接管的單查不到（那時還不知道 client id）
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
            api,
            self.rate_limiter,
            now_provider=self._now,
            futures_symbol=self.resolver.to_futures_symbol,
        )
        self.quote_stream = ShioajiQuoteStream(
            api, self.rate_limiter, self.quote_queue, now_provider=self._now
        )
        self.execution_handler = ShioajiExecutionHandler(
            self.execution_queue,
            record_path=self._record_path,
            on_first_report_ts=self.session.check_report_clock_skew,
            futures_symbol=self.resolver.to_futures_symbol,
        )
        self.execution_handler.register(api)
        self.quote_stream.register_callbacks()

    def close(self) -> None:
        """關閉連線；可重複呼叫"""

        self.session.close()

    def route_events(
        self,
        on_quote: Callable[[Any], None],
        on_execution: Callable[[Any], None],
    ) -> None:
        """
        行情與回報都導向事件迴圈，**行情在這裡就轉成 `StockQuote`**

        轉換放這一層而不是讓迴圈自己做：`from_tick_message()` 是 Shioaji 的
        anti-corruption layer，把它往上搬會讓引擎認得券商的資料形狀。

        試撮與盤中零股由轉換層回 `None`，這裡直接略過——**它們不是報價**。
        """

        def forward_quote(item: Any) -> None:
            kind, _exchange, message = item
            if kind != "tick_stk":
                # 委買賣（bidask）目前沒有消費者；轉成報價會讓「收到一筆行情」
                # 的語意變成兩種東西，逐筆觸發的次數也會憑空變兩倍
                return

            quote: Optional[StockQuote] = self.quote_stream.from_tick_message(message)
            if quote is not None:
                on_quote(quote)

        super().route_events(on_quote, on_execution)
        self.quote_queue = CallbackQueue(forward_quote)
        self._bind_session()

    def reconnect(self) -> bool:
        """
        以 session 的退避重連；**不用骨架那個「關掉再連」的預設**

        Shioaji 的登入額度是每日 1,000 次，而需要重連 20 次的那一天本來就不該
        繼續交易。退避與每日上限都在 `ShioajiSession.reconnect()` 裡。

        重連成功後一律重跑 `_bind_session()`，把元件與回呼掛到新的 api 物件上。
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

        rejection: Optional[str] = self._check_stock_eligibility(ticket.order, contract)
        if rejection is not None:
            # **在本地就拒，不送出、不佔下單額度**：券商也會退，但退單訊息看起來
            # 像別的問題，而那一趟來回在尾盤段要花掉寶貴的秒數
            logger.warning(f"委託 {ticket.client_order_id} 送出前被拒：{rejection}")
            ticket.status = LiveOrderStatus.REJECTED
            ticket.reject_reason = rejection
            ticket.updated_at = self._now()
            return ticket

        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        trade: Any = api.place_order(
            contract, broker_order, timeout=self.ORDER_TIMEOUT_MS
        )

        self._apply_trade(ticket, trade)
        self._remember_trade(ticket, trade)
        ticket.updated_at = self._now()
        return ticket

    def _check_stock_eligibility(self, order: Any, contract: Any) -> Optional[str]:
        """
        - Description:
            台股委託送出前的券商端資格檢查；通過時回 None

            - **先賣後買的當沖**：合約的 `day_trade` 要是 `Yes`。`OnlyBuy` 只允許
              先買後賣，`No` 兩者都不行（2026-09-22 實測值域：`Yes`／`OnlyBuy`／`No`）。
            - **先買後賣的當沖**：`Yes` 或 `OnlyBuy`。
            - **融券賣出**：先查券源，不足就不送——對應回測的 `rejected_no_borrow`。
              查不到券源時**一律不送**：不知道借不借得到就送出，等於把判斷交給券商退單。
            - 借券（`SBLShort`）的額度由券商議借，這裡不檢查。

            欄位值認不得（例如 `None`）時照「不允許」處理，不猜。
        - Parameters:
            - order: Any
                本專案的訂單
            - contract: Any
                已解析的 Shioaji 合約
        - Return:
            - Optional[str]
                拒絕原因；通過時為 None
        """

        if not isinstance(order, StockOrder):
            return None

        # 盤中零股在實盤還不能送：金額換算、成本模型、部位管理、歸屬帳與對帳全部以
        # 「張」為單位，一張 500 股的零股單會在下游各處被當成 500 張。
        # 回測也不支援零股，要開放得整條路徑一起改單位
        if order.order_lot is StockOrderLot.IntradayOdd:
            return f"{order.symbol} 盤中零股尚未支援（部位與金額皆以張計）"

        raw: Any = getattr(contract, "day_trade", None)
        day_trade: str = str(getattr(raw, "value", raw) or "")
        symbol: str = order.symbol

        if (
            order.position_type is PositionType.SHORT
            and order.short_method is ShortMethod.DAY_TRADE
            and order.action is Action.SELL
            and day_trade != DAY_TRADE_BOTH_WAYS
        ):
            return f"{symbol} 不可先賣後買當沖（合約 day_trade={day_trade or '未知'}）"

        if (
            order.is_day_trade
            and order.position_type is PositionType.LONG
            and order.action is Action.BUY
            and day_trade not in (DAY_TRADE_BOTH_WAYS, DAY_TRADE_BUY_FIRST_ONLY)
        ):
            return f"{symbol} 不可當沖（合約 day_trade={day_trade or '未知'}）"

        if (
            order.position_type is PositionType.SHORT
            and order.short_method is ShortMethod.MARGIN
            and order.action is Action.SELL
        ):
            available: Optional[int] = self._query_short_source(contract)
            if available is None:
                return f"{symbol} 查不到券源，不送融券賣出"
            if available < order.volume:
                return f"{symbol} 券源不足：可借 {available} 張，需要 {order.volume} 張"

        return None

    def _query_short_source(self, contract: Any) -> Optional[int]:
        """查單一標的的可融券數（張）；查詢失敗或回傳裡沒有這檔時回 None"""

        api: Any = self._require_ready()
        self.rate_limiter.acquire(RateLimitCategory.MARKET_DATA)
        try:
            rows: Any = api.short_stock_sources(
                [contract], timeout=self.ORDER_TIMEOUT_MS
            )
        except Exception as exc:
            logger.opt(exception=True).warning(f"券源查詢失敗：{exc}")
            return None

        code: str = str(getattr(contract, "code", ""))
        for row in rows or []:
            if str(getattr(row, "code", "")) == code:
                return int(getattr(row, "short_stock_source", 0) or 0)
        return None

    def _build_order(self, ticket: OrderTicket) -> tuple:
        """依商品類別解析合約並轉換委託；轉換規則全在 mapper 裡"""

        order: Any = ticket.order
        if order is None:
            raise ValueError(f"委託 {ticket.client_order_id} 沒有訂單內容")

        # 壓縮碼只由 OMS 產生；這裡另算一份的話，送出去的與本地存的對不上，
        # 重啟接管的精確比對就永遠比不到
        custom_field: Optional[str] = ticket.custom_field
        if not custom_field:
            raise ValueError(
                f"委託 {ticket.client_order_id} 沒有 custom_field；"
                "壓縮碼要由 OrderManager 在送單前產生"
            )

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
            # 明寫在委託上的優先（換月等要指定的情況）；否則依方向與買賣別推導。
            # **不可一律送 `New`**：平倉單會因此開出一口反向新倉
            octype: FuturesOCType = getattr(
                ticket, "octype", None
            ) or self.mapper.derive_octype(order)
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

        trade: Optional[Any] = self._find_trade(ticket)
        if trade is None:
            raise LookupError(
                f"找不到 {ticket.client_order_id} 對應的 Trade，無法撤單；"
                "重啟後要先以 refresh_order_status() 接管才撤得掉"
            )

        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        self._apply_trade(
            ticket,
            api.cancel_order(trade, timeout=self.ORDER_TIMEOUT_MS),
            apply_status=False,
        )
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

        trade: Optional[Any] = self._find_trade(ticket)
        if trade is None:
            raise LookupError(f"找不到 {ticket.client_order_id} 對應的 Trade，無法改價")

        aligned: float = self.mapper.align_price(price, ticket.order.action)
        self.rate_limiter.acquire(RateLimitCategory.ORDER)
        self._apply_trade(
            ticket,
            api.update_order(trade, price=aligned, timeout=self.ORDER_TIMEOUT_MS),
            apply_status=False,
        )
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

        # **股票與期貨帳號要分開刷**：`update_status()` 只更新傳入那個帳號的委託，
        # 只刷股票帳號時，期貨委託撤單之後仍停在 PendingSubmit（模擬環境實測），
        # 收尾與恢復流程就永遠看不到它終結
        for account_name in ("stock_account", "futopt_account"):
            account: Any = getattr(api, account_name, None)
            if account is None:
                continue
            self.rate_limiter.acquire(RateLimitCategory.ORDER)
            api.update_status(account, timeout=self.ORDER_TIMEOUT_MS)

        tickets: List[OrderTicket] = []
        for trade in api.list_trades() or []:
            # 券商端的單還不知道對應哪一張本地委託，`client_order_id` 留空；
            # 由 OMS 以 seqno／`custom_field` 比對後接管
            ticket: OrderTicket = OrderTicket(updated_at=self._now())
            self._apply_trade(ticket, trade)
            self._remember_trade(ticket, trade)
            tickets.append(ticket)
        return tickets

    def _remember_trade(self, ticket: OrderTicket, trade: Any) -> None:
        """保管 `Trade`；沒有 seqno（券商未配發）時退而以 client order id 為鍵"""

        key: str = ticket.broker_seqno or ticket.client_order_id
        if key:
            self._trades[key] = trade

    def _find_trade(self, ticket: OrderTicket) -> Optional[Any]:
        """依 seqno、再依 client order id 找回保管的 `Trade`"""

        for key in (ticket.broker_seqno, ticket.client_order_id):
            if key and key in self._trades:
                return self._trades[key]
        return None

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
    def _apply_trade(
        self, ticket: OrderTicket, trade: Any, apply_status: bool = True
    ) -> None:
        """
        - Description:
            把 Shioaji 的 `Trade` 回填到本地委託

            **撤單與改價不回填狀態與成交量**（`apply_status=False`）：委託送出之後，
            狀態只能由 OMS 經狀態機推進、成交量只能由成交回報累加。這裡直接改的話，
            OMS 看到狀態已相同就不寫 DB；成交量被覆寫成券商的累計量後，
            同一筆成交的回報再進來又會重複累加。撤單結果另有撤單回報。
        - Parameters:
            - ticket: OrderTicket
                本地委託
            - trade: Any
                Shioaji 的 `Trade`
            - apply_status: bool
                是否回填狀態、成交量與原因；只有送單當下（OMS 隨後以狀態機套用）才要
        """

        order: Any = getattr(trade, "order", None)
        status: Any = getattr(trade, "status", None)

        if order is not None:
            ticket.broker_seqno = str(getattr(order, "seqno", "") or "") or None
            ticket.broker_order_id = str(getattr(order, "ordno", "") or "") or None
            custom_field: str = str(getattr(order, "custom_field", "") or "")
            if custom_field and not ticket.custom_field:
                ticket.custom_field = custom_field

        if status is not None and apply_status:
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

    # === 期貨專用 ===
    def get_realized_trades(self, run_date: datetime.date) -> List[Any]:
        """當日已平倉的交易（股票與期貨）；盤後校正成本估算用"""

        self._require_ready()
        return list(self.account_query.get_realized_trades(run_date))

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
