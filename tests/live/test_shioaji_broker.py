import inspect
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import shioaji as sj

from core.broker.base import BaseBroker
from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.models import FuturesOrder, OrderTicket, StockOrder
from core.utils import (
    Action,
    FuturesPriceType,
    LiveOrderStatus,
    PositionType,
    Status,
    StockPriceType,
)

"""
`ShioajiBroker`：只做委派與限流套用，不寫業務邏輯

本檔以假 session／假 API 驗組裝與委派是否正確。**同一組介面承諾另由
`test_shioaji_sim.py` 在模擬環境對這個實作實連驗證**——只靠假券商測過的東西到了實盤不算數，
因為假券商永遠照腳本走，真券商會在你沒想到的地方回一個 None。
"""


class FakeOrder:
    def __init__(
        self, seqno: str = "000001", ordno: str = "AB123", custom_field: str = "010001"
    ) -> None:
        self.seqno: str = seqno
        self.ordno: str = ordno
        self.custom_field: str = custom_field


class FakeStatus:
    def __init__(
        self, status: str = "Submitted", deal_quantity: int = 0, msg: str = ""
    ) -> None:
        self.status: str = status
        self.deal_quantity: int = deal_quantity
        self.msg: str = msg


class FakeTrade:
    def __init__(
        self,
        status: str = "Submitted",
        deal_quantity: int = 0,
        msg: str = "",
        order: Optional[FakeOrder] = None,
    ) -> None:
        self.contract: Any = None
        self.order: FakeOrder = order or FakeOrder()
        self.status: FakeStatus = FakeStatus(status, deal_quantity, msg)


class FakeContract:
    def __init__(self, code: str = "2330") -> None:
        self.code: str = code
        self.limit_up: float = 1100.0
        self.limit_down: float = 900.0


class FakeStocks:
    """對應 shioaji 1.7 的 `api.Contracts.Stocks`：`get(code)` 查不到回 None"""

    def get(self, key: str) -> Optional[FakeContract]:
        return FakeContract(key) if key == "2330" else None


def make_account(account_id: str = "1234567") -> Any:
    """
    用真的 `shioaji.Account`，不是字串

    塞字串的話假物件照樣收，真的委託物件不收——「假物件太寬鬆，
    測過的東西到實盤不算數」的典型例子。shioaji 1.7 的帳號型別也不再收
    `"S"` 字串，要傳 `AccountType` 成員。
    """

    return sj.Account(
        account_type=sj.AccountType.Stock,
        person_id="A123456789",
        broker_id="9A95",
        account_id=account_id,
        signed=True,
        username="tester",
    )


class FakeApi:
    def __init__(self) -> None:
        self.stock_account: Any = make_account()
        self.futopt_account: Any = make_account("7654321")
        self.Contracts = type(
            "Contracts", (), {"Stocks": FakeStocks(), "Futures": None}
        )()
        # shioaji 1.7 的行情回呼直接掛在 api 上（`api.quote.*` 已棄用）
        self.quote_callbacks: Dict[str, Any] = {}
        self.placed: List[Any] = []
        self.cancelled: List[Any] = []
        self.updated: List[tuple] = []
        self.status_refreshes: int = 0
        self.refreshed_accounts: List[Any] = []
        self.on_broker: List[FakeTrade] = []  # 券商端當日的委託
        self.order_callback: Any = None
        # 每次下單類呼叫帶的 timeout；**假物件要求必填**，漏傳就會 TypeError
        self.timeouts: List[int] = []

    def set_order_callback(self, func: Any) -> None:
        self.order_callback = func

    def set_on_tick_stk_v1_callback(self, func: Any) -> None:
        self.quote_callbacks["tick_stk"] = func

    def set_on_bidask_stk_v1_callback(self, func: Any) -> None:
        self.quote_callbacks["bidask_stk"] = func

    def set_on_tick_fop_v1_callback(self, func: Any) -> None:
        self.quote_callbacks["tick_fop"] = func

    def set_on_bidask_fop_v1_callback(self, func: Any) -> None:
        self.quote_callbacks["bidask_fop"] = func

    def place_order(self, contract: Any, order: Any, timeout: int) -> FakeTrade:
        self.placed.append((contract, order))
        self.timeouts.append(timeout)
        # 與真券商一樣：配發 seqno，`custom_field` 原樣帶回
        trade: FakeTrade = FakeTrade(
            order=FakeOrder(
                seqno=f"{len(self.placed):06d}", custom_field=order.custom_field
            )
        )
        self.on_broker.append(trade)
        return trade

    def cancel_order(self, trade: Any, timeout: int) -> FakeTrade:
        self.cancelled.append(trade)
        self.timeouts.append(timeout)
        return FakeTrade(status="Cancelled")

    def update_order(self, trade: Any, price: float, timeout: int) -> FakeTrade:
        self.updated.append((trade, price))
        self.timeouts.append(timeout)
        return FakeTrade()

    def update_status(self, account: Any = None, timeout: int = 0) -> None:
        self.status_refreshes += 1
        self.refreshed_accounts.append(account)
        self.timeouts.append(timeout)

    def list_trades(self) -> List[FakeTrade]:
        # 送過單就回那些單（跨 broker 實例共用同一個 api 即模擬「重啟後向券商查」）；
        # 沒送過時回一筆已成交的單，供只驗查詢本身的測試使用
        return list(self.on_broker) or [FakeTrade(status="Filled", deal_quantity=2)]


class FakeSession:
    def __init__(self, api: FakeApi, limiter: RateLimiter) -> None:
        self.api: Optional[FakeApi] = api
        self.rate_limiter: RateLimiter = limiter
        self.connected: bool = False
        self.skew_checks: List[Any] = []

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected and self.api is not None

    def check_report_clock_skew(self, ts: Any) -> float:
        self.skew_checks.append(ts)
        return 0.0


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(time_source=lambda: 0.0, sleep=lambda seconds: None)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def broker(api: FakeApi, limiter: RateLimiter) -> ShioajiBroker:
    instance: ShioajiBroker = ShioajiBroker(FakeSession(api, limiter), limiter)
    instance.connect()
    return instance


def make_ticket(volume: int = 2, price: float = 1000.0) -> OrderTicket:
    return OrderTicket(
        client_order_id="run1-0001",
        custom_field="010001",
        strategy_name="MomentumStrategy1",
        order=StockOrder(
            stock_id="2330",
            action=Action.BUY,
            volume=volume,
            price=price,
            price_type=StockPriceType.LMT,
        ),
    )


# === 介面 ===
def test_implements_every_abstract_method() -> None:
    """
    每個抽象方法都要實作

    少一個會在**盤中第一次呼叫它**時才炸，而那時已經有部位在場上。
    """

    missing: List[str] = sorted(
        name
        for name in BaseBroker.__abstractmethods__
        if getattr(ShioajiBroker, name, None) is getattr(BaseBroker, name, None)
    )

    assert missing == []


@pytest.mark.parametrize("name", sorted(BaseBroker.__abstractmethods__))
def test_signature_matches_the_interface(name: str) -> None:
    """實作的參數名要與介面相同：引擎一律以關鍵字傳參"""

    assert list(inspect.signature(getattr(ShioajiBroker, name)).parameters) == list(
        inspect.signature(getattr(BaseBroker, name)).parameters
    )


def test_calls_before_connect_are_rejected(api: FakeApi, limiter: RateLimiter) -> None:
    """未連線時一律拋出，不靜默排隊等重連"""

    broker: ShioajiBroker = ShioajiBroker(FakeSession(api, limiter), limiter)

    with pytest.raises(ConnectionError):
        broker.get_positions()


# === 組裝 ===
def test_connect_wires_every_component(broker: ShioajiBroker, api: FakeApi) -> None:
    """連線後五個元件都要就位，回呼也要掛上"""

    assert broker.resolver is not None
    assert broker.mapper is not None
    assert broker.account_query is not None
    assert broker.quote_stream is not None
    assert api.order_callback is not None


def test_clock_skew_hook_is_wired_to_the_session(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """
    回報時戳要接回 session 的時鐘檢查

    登入時只拿得到合約檔的日期（沒有秒），秒級偏差要等回報進來才驗得到——
    沒接上的話那道檢查等於不存在。
    """

    assert (
        broker.execution_handler._on_first_report_ts
        == broker.session.check_report_clock_skew
    )


def test_quote_and_execution_use_separate_queues(broker: ShioajiBroker) -> None:
    """
    行情與回報分兩個佇列

    行情量大且可丟（過期報價沒有價值），回報一筆都不能丟。
    混在一起會讓回報排在幾千筆行情後面。
    """

    assert broker.quote_queue is not broker.execution_queue


# === 下單 ===
def test_place_order_fills_in_broker_ids(broker: ShioajiBroker) -> None:
    """送出後要回填券商編號與狀態"""

    ticket: OrderTicket = broker.place_order(make_ticket())

    assert ticket.broker_seqno == "000001"
    assert ticket.broker_order_id == "AB123"
    assert ticket.status is LiveOrderStatus.SUBMITTED
    assert ticket.updated_at is not None


def test_place_order_consumes_order_budget(
    broker: ShioajiBroker, limiter: RateLimiter
) -> None:
    """送單走下單類額度"""

    small: RateLimiter = RateLimiter(
        limits={RateLimitCategory.ORDER: limiter.limits[RateLimitCategory.ORDER]},
        time_source=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    broker.rate_limiter = small
    broker.place_order(make_ticket())

    assert small.try_acquire(RateLimitCategory.ORDER) is True


def test_conversion_failure_does_not_consume_budget(broker: ShioajiBroker) -> None:
    """
    轉換失敗時不該白白吃掉一個送單額度

    尾盤段只有 13:25~13:29，額度是有限的。
    """

    ticket: OrderTicket = make_ticket()
    ticket.order.price_type = None  # 前處理漏填

    with pytest.raises(ValueError):
        broker.place_order(ticket)

    assert broker.rate_limiter.wait_stats() == {}


def test_unresolvable_symbol_raises_before_sending(broker: ShioajiBroker) -> None:
    """代號查不到要在送出前拋出，不可把 None 合約傳下去"""

    ticket: OrderTicket = make_ticket()
    ticket.order = StockOrder(
        stock_id="9999", volume=1, price=10.0, price_type=StockPriceType.LMT
    )

    with pytest.raises(LookupError):
        broker.place_order(ticket)


# === 撤單與改價 ===
def test_order_calls_pass_an_explicit_timeout(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """
    送單、撤單、查狀態都要明寫 timeout

    shioaji 1.7 把預設從 5 秒改成 30 秒；靠預設值的話，尾盤那 4 分鐘裡
    一張卡住的單能吃掉的時間會隨套件版本悄悄變長。
    """

    ticket: OrderTicket = broker.place_order(make_ticket())
    broker.refresh_order_status()
    broker.cancel_order(ticket)

    # 送單 1 次、兩個帳號各刷 1 次、撤單 1 次
    assert api.timeouts == [ShioajiBroker.ORDER_TIMEOUT_MS] * 4


@pytest.mark.parametrize(
    ("action", "expected"),
    [(Action.BUY, sj.FuturesOCType.New), (Action.SELL, sj.FuturesOCType.Cover)],
)
def test_futures_order_derives_octype(
    broker: ShioajiBroker, api: FakeApi, action: Action, expected: Any
) -> None:
    """
    期貨委託的開平倉別依方向與買賣別推導，平倉送 `Cover`

    以前閘道一律送 `New`：多單的賣出平倉單會開出一口空單，而不是平掉多單。
    """

    contract: Any = SimpleNamespace(code="TXFJ6", root="TXF", delivery_month="202610")
    api.Contracts.Futures = SimpleNamespace(TXF=[contract])
    ticket: OrderTicket = OrderTicket(
        client_order_id="run1-0002",
        custom_field="010002",
        strategy_name="FuturesStrategy",
        order=FuturesOrder(
            product="TX",
            expiry="202610",
            action=action,
            position_type=PositionType.LONG,
            volume=1,
            price=48000.0,
            price_type=FuturesPriceType.LMT,
        ),
    )

    broker.place_order(ticket)

    assert api.placed[-1][1].octype == expected


def test_cancel_uses_the_stored_trade(broker: ShioajiBroker, api: FakeApi) -> None:
    """
    撤單要傳回**原本那個 `Trade`**

    Shioaji 的撤單不吃委託編號，只吃 Trade 物件——那是 SDK 的形狀，
    只有這一層知道，所以由它保管。
    """

    ticket: OrderTicket = broker.place_order(make_ticket())
    broker.cancel_order(ticket)

    assert len(api.cancelled) == 1


def test_cancel_and_update_do_not_touch_status_or_filled_volume(
    broker: ShioajiBroker,
) -> None:
    """
    撤單與改價不回填狀態與成交量

    撤單請求不等於撤單成功，撤單前撮合的量照樣會以成交回報進來；
    閘道在這裡把狀態改成 CANCELLED、把成交量覆寫成券商的累計量的話，
    OMS 看到狀態相同就不寫 DB，同一筆成交的回報再進來又會重複累加。
    """

    ticket: OrderTicket = broker.place_order(make_ticket())
    ticket.status = LiveOrderStatus.PARTIALLY_FILLED
    ticket.filled_volume = 1

    broker.update_order_price(ticket, price=1003.2)
    broker.cancel_order(ticket)

    assert ticket.status is LiveOrderStatus.PARTIALLY_FILLED
    assert ticket.filled_volume == 1


def test_cancel_of_terminal_order_is_a_no_op(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """已終結的委託不送撤單請求：那會拿到一個看起來很像真問題的券商錯誤"""

    ticket: OrderTicket = make_ticket()
    ticket.status = LiveOrderStatus.FILLED
    broker.cancel_order(ticket)

    assert api.cancelled == []


def test_cancel_without_a_trade_raises(broker: ShioajiBroker) -> None:
    """
    找不到 Trade 時要明講

    重啟後本地沒有 Trade 物件，得先 `refresh_order_status()` 接管才撤得掉。
    """

    with pytest.raises(LookupError, match="refresh_order_status"):
        broker.cancel_order(make_ticket())


def test_update_price_aligns_to_tick(broker: ShioajiBroker, api: FakeApi) -> None:
    """改價也要對齊檔位，而且方向一樣保守（買單往下）"""

    ticket: OrderTicket = broker.place_order(make_ticket())
    broker.update_order_price(ticket, price=1003.2)

    assert api.updated[0][1] == pytest.approx(1000.0)


# === 狀態轉換 ===
@pytest.mark.parametrize(
    "broker_status, expected",
    [
        (Status.PendingSubmit, LiveOrderStatus.PENDING_SUBMIT),
        (Status.PreSubmitted, LiveOrderStatus.PENDING_SUBMIT),
        (Status.Submitted, LiveOrderStatus.SUBMITTED),
        (Status.PartFilled, LiveOrderStatus.PARTIALLY_FILLED),
        (Status.Filled, LiveOrderStatus.FILLED),
        (Status.Cancelled, LiveOrderStatus.CANCELLED),
        (Status.Inactive, LiveOrderStatus.CANCELLED),
        (Status.Failed, LiveOrderStatus.FAILED),
    ],
)
def test_status_mapping(broker_status: Status, expected: LiveOrderStatus) -> None:
    """券商狀態與本地狀態機的對照要完整"""

    assert ShioajiBroker.to_live_status(broker_status) is expected


def test_unknown_status_becomes_failed_not_submitted() -> None:
    """
    認不得的狀態當成 `FAILED`，不是 `SUBMITTED`

    把未知當成「還在場上」會讓引擎繼續等一張其實已經不存在的單；
    當成失敗只會多做一次查詢。
    """

    assert ShioajiBroker.to_live_status("SomethingNew") is LiveOrderStatus.FAILED


def test_refresh_order_status_uses_order_budget(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """
    `update_status` 算在**下單類**額度

    它是查詢語意卻吃送單額度，拿它當心跳輪詢會在尾盤那 4 分鐘把送單額度吃光。
    """

    tickets: List[OrderTicket] = broker.refresh_order_status()

    assert api.status_refreshes == 2
    assert len(tickets) == 1
    assert tickets[0].status is LiveOrderStatus.FILLED
    assert tickets[0].filled_volume == 2


def test_refresh_order_status_refreshes_both_accounts(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """
    股票與期貨帳號都要刷

    `update_status()` 只更新傳入那個帳號的委託：只刷股票帳號時，期貨委託撤單之後
    仍停在 PendingSubmit，收尾與恢復流程永遠看不到它終結。
    """

    broker.refresh_order_status()

    assert api.refreshed_accounts == [api.stock_account, api.futopt_account]


def test_refresh_order_status_skips_a_missing_account(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """期貨權限還沒開通（帳號為 None）時只刷股票帳號，不可傳 None 進去"""

    api.futopt_account = None

    broker.refresh_order_status()

    assert api.refreshed_accounts == [api.stock_account]


# === 行情 ===
def test_snapshots_resolve_symbols_first(broker: ShioajiBroker) -> None:
    """查無合約的代號要在取快照前就拋出"""

    with pytest.raises(LookupError):
        broker.get_snapshots(["2330", "9999"])


def test_custom_field_is_the_one_oms_generated(
    broker: ShioajiBroker, api: FakeApi
) -> None:
    """
    送給券商的 `custom_field` 就是 OMS 產生、存進 DB 的那一個

    broker 以前自己取 client id 末 6 碼：`20260919090512-0001` 與
    `20260919132032-0001` 送出去都是 `2-0001`，OMS 存的卻是 base36 壓縮碼——
    重啟接管的精確比對永遠比不到，同一天不同 run 的單還會互撞。
    """

    ticket: OrderTicket = make_ticket()
    ticket.custom_field = "u80001"
    broker.place_order(ticket)

    assert api.placed[0][1].custom_field == "u80001"


def test_order_without_custom_field_is_refused(broker: ShioajiBroker) -> None:
    """沒有壓縮碼的委託不送：另算一份就是兩邊對不上的開始"""

    ticket: OrderTicket = make_ticket()
    ticket.custom_field = None

    with pytest.raises(ValueError, match="custom_field"):
        broker.place_order(ticket)


def test_refreshed_ticket_keeps_custom_field_out_of_client_id(
    broker: ShioajiBroker,
) -> None:
    """
    刷新回來的委託，壓縮碼放在自己的欄位

    塞進 `client_order_id` 的話，後續以 client id 找 `Trade` 會找到一個
    不存在的 id，接管後的單就撤不掉。
    """

    broker.place_order(make_ticket())

    refreshed: List[OrderTicket] = broker.refresh_order_status()

    assert refreshed[0].custom_field == "010001"
    assert refreshed[0].client_order_id == ""


@pytest.mark.parametrize("seqno_saved", [True, False])
def test_recovered_order_can_be_cancelled_by_a_new_broker(
    api: FakeApi, limiter: RateLimiter, seqno_saved: bool
) -> None:
    """
    送單 → 行程重啟（新的 broker 與 OMS）→ 接管 → 撤單，撤單請求要真的送出

    接管後的單撤不掉的話，`finish()` 吞掉 `LookupError`，單子就留在場上過夜。
    `seqno_saved=False` 模擬在 `place_order()` 回傳前崩潰、本地沒存到 seqno——
    那時只剩 `custom_field` 能精確比對，兩邊的壓縮碼不一致就只能退回模糊比對。
    """

    import datetime
    import sqlite3

    from core.dao.tw.live_trade_dao import LiveTradeDAO
    from core.live.oms.order_manager import OrderManager

    now: datetime.datetime = datetime.datetime(2026, 9, 19, 9, 5, 12)
    dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    dao.ensure_tables()

    first_broker: ShioajiBroker = ShioajiBroker(FakeSession(api, limiter), limiter)
    first_broker.connect()
    first_oms: OrderManager = OrderManager(
        first_broker, dao, "20260919090512", run_index=1, now_provider=lambda: now
    )
    submitted: OrderTicket = first_oms.submit(make_ticket().order, "Alpha")
    if not seqno_saved:
        dao.conn.execute("UPDATE live_order SET broker_seqno = NULL")
        dao.conn.commit()

    # 重啟：券商端的單還在（同一個 api），本地的 broker 與 OMS 都是新的
    second_broker: ShioajiBroker = ShioajiBroker(FakeSession(api, limiter), limiter)
    second_broker.connect()
    second_oms: OrderManager = OrderManager(
        second_broker, dao, "20260919132032", run_index=2, now_provider=lambda: now
    )
    recovered: List[OrderTicket] = second_oms.recover(now.date())
    second_oms.cancel_open_orders()

    assert submitted.custom_field == first_oms.compress(submitted.client_order_id)
    assert api.placed[0][1].custom_field == submitted.custom_field
    assert recovered[0].custom_field == submitted.custom_field
    assert recovered[0].broker_seqno == "000001"
    assert len(api.cancelled) == 1
