import datetime
import queue
import time
from typing import Any, Callable, Iterator, List, Optional

import pytest

from core.broker.tw.shioaji_broker import ShioajiBroker
from core.broker.tw.shioaji_session import ShioajiSession
from core.config.settings import now_live
from core.models import (
    BaseQuote,
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderStatusEvent,
    OrderTicket,
    StockOrder,
)
from core.models.futures.order import FuturesOrder
from core.utils import (
    Action,
    FuturesOCType,
    FuturesPriceType,
    LiveOrderStatus,
    StockPriceType,
    Units,
)

"""
`ShioajiBroker` 在**模擬環境**的介面契約

`test_broker_contract.py` 驗的是介面，跑在 `FakeBroker` 上；假券商永遠照著腳本走，
真券商會在沒想到的地方回一個 None。本檔拿同一組介面承諾對真的 `ShioajiBroker`
（模擬環境）再驗一次，兩邊都通過才算介面成立。

**預設不跑**：要登入、要真實金鑰（`.env`），部分測試還要盤中。只有以 `-m` 明確
選到才會執行（見 `tests/conftest.py` 的 `pytest_collection_modifyitems`）：

    uv run pytest tests/live/test_shioaji_sim.py -m "shioaji_sim and not shioaji_sim_order"
    uv run pytest tests/live/test_shioaji_sim.py -m shioaji_sim_order

`shioaji_sim_order` 會在模擬環境**送出委託**，故與唯讀的測試分開標記：
來回測試以跌停價買進 1 單位（不會成交，測完即撤單）；成交測試（名稱含 `fill`）
以漲停價買進 1 單位、核對成交回報後以跌停價賣出平倉，模擬帳戶最後不留部位。只連模擬環境，沒有連正式環境的選項。
"""

pytestmark = pytest.mark.shioaji_sim

# 等券商推回報的上限（秒）；委託回報實測在 1 秒內到
REPORT_WAIT_SECONDS: float = 10.0
# 等第一筆逐筆行情的上限（秒）；2330 盤中幾秒就有一筆
TICK_WAIT_SECONDS: float = 30.0
# 跨多批的快照：`SNAPSHOT_BATCH_SIZE` 是 100，取 250 檔會切成 3 批
SNAPSHOT_SAMPLE_SIZE: int = 250

# 快照時間與現在的最大差距；盤中 2330 幾秒就有成交，給足一個段落的餘裕
SNAPSHOT_MAX_AGE: datetime.timedelta = datetime.timedelta(minutes=30)

STOCK_SYMBOL: str = "2330"
FUTURES_PRODUCT: str = "TX"


@pytest.fixture(scope="module")
def broker() -> Iterator[ShioajiBroker]:
    """整個檔案共用一次登入：登入有每日次數上限，逐條登入沒有必要"""

    instance: ShioajiBroker = ShioajiBroker(ShioajiSession(simulation=True))
    instance.connect()
    try:
        yield instance
    finally:
        instance.close()


def wait_for(
    broker: ShioajiBroker, predicate: Callable[[Any], bool], seconds: float
) -> List[Any]:
    """在時限內持續取出回報，直到有一筆符合條件；回傳期間取出的全部回報"""

    collected: List[Any] = []
    deadline: float = time.monotonic() + seconds
    while time.monotonic() < deadline:
        collected.extend(broker.drain_execution_queue())
        if any(predicate(item) for item in collected):
            return collected
        time.sleep(0.2)
    return collected


def today() -> datetime.date:
    return now_live().date()


# === 連線 ===
def test_login_exposes_stock_and_futures_accounts(broker: ShioajiBroker) -> None:
    """兩個帳號都要在；少一個時 `ShioajiOrderMapper` 對那個市場的單只能拋出"""

    assert broker.is_connected()
    assert broker.mapper.stock_account is not None
    assert broker.mapper.futopt_account is not None


# === 帳務 ===
def test_stock_account_is_normalized(broker: ShioajiBroker) -> None:
    """帳務查詢回傳正規化物件，不是券商原生型別或 dict"""

    account: BrokerAccountSnapshot = broker.get_account()

    assert isinstance(account, BrokerAccountSnapshot)
    assert isinstance(account.available_balance, float)
    assert isinstance(account.total_equity, float)

    # 持倉成本要以股計：券商的數量是張、均價是每股，少乘 1000 時成本只剩幾百元
    # （2026-09-22 模擬帳戶 6 檔部位曾被算成 514.89 元）
    positions: List[BrokerPositionSnapshot] = broker.get_positions()
    stock_lots: List[BrokerPositionSnapshot] = [
        position for position in positions if position.symbol.isdigit()
    ]
    if stock_lots:
        cheapest: float = min(position.avg_price for position in stock_lots)
        assert account.raw["position_cost"] >= Units.LOT * cheapest


def test_futures_account_is_normalized(broker: ShioajiBroker) -> None:
    account: BrokerAccountSnapshot = broker.get_futures_account()

    assert isinstance(account, BrokerAccountSnapshot)


def test_positions_are_normalized(broker: ShioajiBroker) -> None:
    """股票與期貨部位合併回傳，每一筆都是正規化物件、數量為正"""

    positions: List[BrokerPositionSnapshot] = broker.get_positions()

    assert all(isinstance(position, BrokerPositionSnapshot) for position in positions)
    assert all(position.volume > 0 for position in positions)


# === 行情 ===
def test_snapshots_carry_todays_prices(broker: ShioajiBroker) -> None:
    """
    快照的價格與時間要是此刻的

    `Snapshot.ts` 是奈秒、而且是台北牆上時間當 UTC 編碼；單位或時區弄錯時，
    時間會落在 1970 年或偏 8 小時，價格卻看起來完全正常。**需要盤中**。
    """

    quotes: List[BaseQuote] = broker.get_snapshots([STOCK_SYMBOL, "2317"])

    assert sorted(quote.symbol for quote in quotes) == ["2317", STOCK_SYMBOL]
    for quote in quotes:
        assert quote.cur_price > 0
        assert quote.date.tzinfo is not None
        assert quote.date.date() == today()
        # 只比日期擋不住時區錯：2026-09-22 抓到快照時間整整晚了 8 小時，日期卻相同
        assert abs(quote.date - now_live()) < SNAPSHOT_MAX_AGE


def test_snapshots_span_multiple_batches(broker: ShioajiBroker) -> None:
    """
    超過單批上限時分批取回，筆數不能少

    券商對單次快照有上限，`SNAPSHOT_BATCH_SIZE` 是依此切批的值；
    這條確認切批後每一批都真的取回來了，沒有哪一批被靜靜丟掉。
    """

    symbols: List[str] = [
        contract.code
        for contract in broker.session.api.Contracts.Stocks.TSE
        if len(contract.code) == 4 and contract.code.isdigit()
    ][:SNAPSHOT_SAMPLE_SIZE]

    quotes: List[BaseQuote] = broker.get_snapshots(symbols)

    assert len(symbols) == SNAPSHOT_SAMPLE_SIZE
    assert {quote.symbol for quote in quotes} == set(symbols)


def test_unknown_symbol_is_rejected_before_querying(broker: ShioajiBroker) -> None:
    """
    合約檔查不到的代號在取快照前就拋出

    「查無合約」與「查無資料」是兩件事：前者是代號打錯或已下市，要當場講；
    後者（例如停牌）才是略過不回傳。
    """

    with pytest.raises(LookupError):
        broker.get_snapshots([STOCK_SYMBOL, "9999"])


def test_tick_subscription_delivers_quotes(broker: ShioajiBroker) -> None:
    """
    訂閱後要收得到逐筆行情，並能轉成報價

    **需要盤中**：收盤後不會有逐筆推播，這條會在等待上限後失敗。
    """

    # 直接讀行情佇列、自己轉換，不走 `route_events()`：那會把回報佇列也換成回呼，
    # 同一個 broker 之後的下單測試就取不到回報了
    received: List[BaseQuote] = []
    broker.subscribe_quotes([STOCK_SYMBOL])
    try:
        deadline: float = time.monotonic() + TICK_WAIT_SECONDS
        while not received and time.monotonic() < deadline:
            try:
                kind, _exchange, message = broker.quote_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "tick_stk":
                quote: Optional[BaseQuote] = broker.quote_stream.to_tick_quote(message)
                if quote is not None:
                    received.append(quote)
    finally:
        broker.unsubscribe_quotes([STOCK_SYMBOL])

    assert received, f"{TICK_WAIT_SECONDS} 秒內沒有收到 {STOCK_SYMBOL} 的逐筆行情"
    assert received[0].symbol == STOCK_SYMBOL
    assert received[0].cur_price > 0


# === 下單（會在模擬環境送出委託）===
def limit_down(contract: Any) -> float:
    """跌停價：在漲跌停範圍內且不會成交"""

    return float(contract.limit_down)


def near_month_expiry(broker: ShioajiBroker) -> str:
    """
    台指期近月的到期月份（`YYYYMM`）

    排除連續月別名（`TXFR1`／`TXFR2`）：它們與近月同一個 `delivery_month`，
    挑到的話解析器拿到的是別名而不是實際合約。
    """

    contracts: List[Any] = [
        contract
        for contract in broker.session.api.Contracts.Futures.TXF
        if not contract.code.endswith(("R1", "R2"))
    ]
    return str(
        min(contracts, key=lambda contract: contract.delivery_date).delivery_month
    )


def make_ticket(order: Any, custom_field: str) -> OrderTicket:
    return OrderTicket(
        client_order_id=f"sim-{custom_field}",
        custom_field=custom_field,
        strategy_name="ShioajiSimContract",
        order=order,
        created_at=now_live(),
    )


def assert_round_trip(broker: ShioajiBroker, ticket: OrderTicket) -> None:
    """送出 → 收到委託回報 → 撤單 → 收到撤單回報 → 刷新後為 CANCELLED 且識別碼帶回"""

    broker.place_order(ticket)
    seqno: Optional[str] = ticket.broker_seqno

    assert seqno
    assert ticket.status not in (LiveOrderStatus.REJECTED, LiveOrderStatus.FAILED)

    def is_event(op_type: str) -> Callable[[Any], bool]:
        return lambda item: (
            isinstance(item, OrderStatusEvent)
            and item.broker_seqno == seqno
            and item.op_type == op_type
        )

    assert any(map(is_event("New"), wait_for(broker, is_event("New"), 5.0)))

    broker.cancel_order(ticket)
    events: List[Any] = wait_for(broker, is_event("Cancel"), REPORT_WAIT_SECONDS)
    cancelled: List[OrderStatusEvent] = [
        item for item in events if is_event("Cancel")(item)
    ]
    assert cancelled, "沒有收到撤單回報"
    assert cancelled[0].op_code == "00"

    refreshed: List[OrderTicket] = [
        item for item in broker.refresh_order_status() if item.broker_seqno == seqno
    ]
    assert len(refreshed) == 1
    assert refreshed[0].status is LiveOrderStatus.CANCELLED
    assert refreshed[0].custom_field == ticket.custom_field


@pytest.mark.shioaji_sim_order
def test_stock_order_round_trip(broker: ShioajiBroker) -> None:
    contract: Any = broker.resolver.resolve_stock(STOCK_SYMBOL)
    order: StockOrder = StockOrder(
        stock_id=STOCK_SYMBOL,
        action=Action.BUY,
        volume=1,
        price=limit_down(contract),
        date=now_live(),
        price_type=StockPriceType.LMT,
    )

    assert_round_trip(broker, make_ticket(order, "0S0001"))


@pytest.mark.shioaji_sim_order
def test_futures_order_round_trip(broker: ShioajiBroker) -> None:
    """
    期貨同樣要走完一輪

    刷新那一步正是 2026-09-22 抓到的 bug：只刷股票帳號時，期貨委託撤單後
    仍停在 PendingSubmit。
    """

    expiry: str = near_month_expiry(broker)
    contract: Any = broker.resolver.resolve_index_futures(FUTURES_PRODUCT, expiry)
    order: FuturesOrder = FuturesOrder(
        product=FUTURES_PRODUCT,
        expiry=expiry,
        action=Action.BUY,
        volume=1,
        price=limit_down(contract),
        date=now_live(),
        price_type=FuturesPriceType.LMT,
    )

    assert_round_trip(broker, make_ticket(order, "0F0001"))


# === 成交（會在模擬環境真的成交，並立刻反向平倉）===
# 成交時間與現在的最大差距；超過代表時戳單位或時區解錯（快照就曾整整偏 8 小時）
FILL_MAX_AGE: datetime.timedelta = datetime.timedelta(minutes=5)


def limit_up(contract: Any) -> float:
    """漲停價：買進必定可成交的最高合法價"""

    return float(contract.limit_up)


def fill_and_check(broker: ShioajiBroker, ticket: OrderTicket) -> ExecutionReport:
    """
    送出一張必定成交的委託，核對成交回報的每個欄位並回傳

    成交回報與委託回報是兩種推播：前者走 `parse_deal()`，欄位是平的
    （`trade_id`／`seqno`／`code`／`action`／`price`／`quantity`／`ts`），
    而 `ts` 的單位與時區只有真的成交一次才驗得到。
    """

    order: Any = ticket.order
    broker.place_order(ticket)
    seqno: Optional[str] = ticket.broker_seqno
    assert seqno

    def is_fill(item: Any) -> bool:
        return isinstance(item, ExecutionReport) and item.broker_seqno == seqno

    fills: List[ExecutionReport] = [
        item for item in wait_for(broker, is_fill, REPORT_WAIT_SECONDS) if is_fill(item)
    ]
    assert fills, f"委託 {seqno} 在 {REPORT_WAIT_SECONDS} 秒內沒有成交回報"

    fill: ExecutionReport = fills[0]
    print(f"\n成交回報原始內容（{type(order).__name__}）：{fill.raw}")

    assert fill.broker_trade_id
    assert fill.symbol
    assert fill.action is order.action
    assert fill.price > 0
    assert sum(item.volume for item in fills) == order.volume
    assert fill.ts is not None
    assert fill.ts.tzinfo is not None
    assert abs(fill.ts - now_live()) < FILL_MAX_AGE, (
        f"成交時間 {fill.ts} 與現在 {now_live()} 差太多，時戳單位或時區解錯"
    )
    return fill


def with_octype(ticket: OrderTicket, octype: FuturesOCType) -> OrderTicket:
    """期貨委託要帶開平倉別；`ShioajiBroker` 讀 ticket 上的 `octype`"""

    ticket.octype = octype
    return ticket


@pytest.mark.shioaji_sim_order
def test_stock_fill_report(broker: ShioajiBroker) -> None:
    """漲停價買進 1 張 → 核對成交回報 → 跌停價賣出平倉（當日先買後賣）"""

    contract: Any = broker.resolver.resolve_stock(STOCK_SYMBOL)

    def stock_order(action: Action, price: float) -> StockOrder:
        return StockOrder(
            stock_id=STOCK_SYMBOL,
            action=action,
            volume=1,
            price=price,
            date=now_live(),
            price_type=StockPriceType.LMT,
        )

    bought: ExecutionReport = fill_and_check(
        broker, make_ticket(stock_order(Action.BUY, limit_up(contract)), "0S0002")
    )
    assert bought.symbol == STOCK_SYMBOL

    fill_and_check(
        broker, make_ticket(stock_order(Action.SELL, limit_down(contract)), "0S0003")
    )


@pytest.mark.shioaji_sim_order
def test_futures_fill_report(broker: ShioajiBroker) -> None:
    """
    漲停價買進 1 口（新倉）→ 核對成交回報 → 跌停價賣出平倉（平倉）

    開平倉別明寫 `New`／`Cover`：mapper 刻意拒絕 `Auto`，它在換月時行為不透明。
    """

    expiry: str = near_month_expiry(broker)
    contract: Any = broker.resolver.resolve_index_futures(FUTURES_PRODUCT, expiry)

    def futures_order(action: Action, price: float) -> FuturesOrder:
        return FuturesOrder(
            product=FUTURES_PRODUCT,
            expiry=expiry,
            action=action,
            volume=1,
            price=price,
            date=now_live(),
            price_type=FuturesPriceType.LMT,
        )

    fill_and_check(
        broker,
        with_octype(
            make_ticket(futures_order(Action.BUY, limit_up(contract)), "0F0002"),
            FuturesOCType.New,
        ),
    )
    fill_and_check(
        broker,
        with_octype(
            make_ticket(futures_order(Action.SELL, limit_down(contract)), "0F0003"),
            FuturesOCType.Cover,
        ),
    )
