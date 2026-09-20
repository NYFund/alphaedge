import inspect
from typing import Any, Dict, List, Optional

import pytest
import shioaji.account as sj_account

from core.broker.base import BaseBroker
from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.models import OrderTicket, StockOrder
from core.utils import Action, LiveOrderStatus, Status, StockPriceType

"""
`ShioajiBroker`：只做委派與限流套用，不寫業務邏輯

本檔以假 session／假 API 驗組裝與委派是否正確。**真正的介面契約測試
（`test_broker_contract.py`）要到能連上模擬環境時，以 `-m shioaji_sim` 在
這個實作上再跑一次**——只靠假券商測過的東西到了實盤不算數，
因為假券商永遠照腳本走，真券商會在你沒想到的地方回一個 None。
"""


class FakeOrder:
    def __init__(self, seqno: str = "000001", ordno: str = "AB123") -> None:
        self.seqno: str = seqno
        self.ordno: str = ordno
        self.custom_field: str = "1-0001"


class FakeStatus:
    def __init__(
        self, status: str = "Submitted", deal_quantity: int = 0, msg: str = ""
    ) -> None:
        self.status: str = status
        self.deal_quantity: int = deal_quantity
        self.msg: str = msg


class FakeTrade:
    def __init__(
        self, status: str = "Submitted", deal_quantity: int = 0, msg: str = ""
    ) -> None:
        self.contract: Any = None
        self.order: FakeOrder = FakeOrder()
        self.status: FakeStatus = FakeStatus(status, deal_quantity, msg)


class FakeContract:
    def __init__(self, code: str = "2330") -> None:
        self.code: str = code
        self.symbol: str = f"TSE{code}"
        self.limit_up: float = 1100.0
        self.limit_down: float = 900.0


class FakeStocks:
    def __getitem__(self, key: str) -> Optional[FakeContract]:
        return FakeContract(key) if key == "2330" else None


class FakeQuoteManager:
    def __init__(self) -> None:
        self.callbacks: Dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            self.callbacks[name] = args[0] if args else None

        return _noop


def make_account(account_id: str = "1234567") -> Any:
    """
    用真的 `shioaji.account.Account`，不是字串

    委託的 `account` 欄位有 pydantic 驗證，塞字串會被擋下——而這正是
    「假物件太寬鬆，測過的東西到實盤不算數」的典型例子。
    """

    return sj_account.StockAccount(
        account_type="S",
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
        self.quote: FakeQuoteManager = FakeQuoteManager()
        self.placed: List[Any] = []
        self.cancelled: List[Any] = []
        self.updated: List[tuple] = []
        self.status_refreshes: int = 0
        self.order_callback: Any = None

    def set_order_callback(self, func: Any) -> None:
        self.order_callback = func

    def place_order(self, contract: Any, order: Any) -> FakeTrade:
        self.placed.append((contract, order))
        return FakeTrade()

    def cancel_order(self, trade: Any) -> FakeTrade:
        self.cancelled.append(trade)
        return FakeTrade(status="Cancelled")

    def update_order(self, trade: Any, price: float) -> FakeTrade:
        self.updated.append((trade, price))
        return FakeTrade()

    def update_status(self, account: Any = None) -> None:
        self.status_refreshes += 1

    def list_trades(self) -> List[FakeTrade]:
        return [FakeTrade(status="Filled", deal_quantity=2)]


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
def test_cancel_uses_the_stored_trade(broker: ShioajiBroker, api: FakeApi) -> None:
    """
    撤單要傳回**原本那個 `Trade`**

    Shioaji 的撤單不吃委託編號，只吃 Trade 物件——那是 SDK 的形狀，
    只有這一層知道，所以由它保管。
    """

    ticket: OrderTicket = broker.place_order(make_ticket())
    broker.cancel_order(ticket)

    assert len(api.cancelled) == 1
    assert ticket.status is LiveOrderStatus.CANCELLED


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

    assert api.status_refreshes == 1
    assert len(tickets) == 1
    assert tickets[0].status is LiveOrderStatus.FILLED
    assert tickets[0].filled_volume == 2


# === 行情 ===
def test_snapshots_resolve_symbols_first(broker: ShioajiBroker) -> None:
    """查無合約的代號要在取快照前就拋出"""

    with pytest.raises(LookupError):
        broker.get_snapshots(["2330", "9999"])


def test_custom_field_fits_the_six_character_limit(broker: ShioajiBroker) -> None:
    """壓縮碼必須塞得進 6 個字元，否則 shioaji 的欄位驗證會擋下整張單"""

    compressed: str = ShioajiBroker._compress("run1-0001")

    assert len(compressed) <= 6
