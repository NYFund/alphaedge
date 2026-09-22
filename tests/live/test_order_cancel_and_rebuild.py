import datetime
from typing import Any, Dict, List, Optional

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.after_close import AfterCloseRunner
from core.live.factory import make_order_rebuilder
from core.live.oms.order_manager import OrderManager
from core.models import ExecutionReport, OrderStatusEvent, OrderTicket, StockOrder
from core.utils import (
    Action,
    ExecutionTiming,
    LiveOrderStatus,
    PositionType,
    StockPriceType,
)

from .conftest import FakeBroker

"""
撤單的競態與跨行程重建

兩個 bug 的共同後果都是**隔天把部位做反**，只是路徑不同：

1. **撤單請求一送出就在本地轉 CANCELLED**：撤單與成交在交易所是競態，請求送出前
   撮合的量照樣會回報。以前那些成交走 `CANCELLED → FILLED` 被狀態機判成非法而丟掉，
   帳戶同步收不到；盤後以 `live_order` 的舊成交量算出殘量，對已平掉的部位再寫一筆
   隔日補平。
2. **盤後是另一個行程**：重建的委託不帶原始訂單，寫回 DB 時標的與數量被蓋成空值，
   殘量算成 0 而略過——未成交出場單的隔日補平靜靜消失（反方向的同一種錯：
   該補的沒補，留下預期外的隔夜部位）。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 13, 25)
TODAY: datetime.date = NOW.date()


def make_oms(
    broker: FakeBroker, dao: LiveTradeDAO, run_id: str, run_index: int
) -> OrderManager:
    return OrderManager(
        broker=broker,
        dao=dao,
        run_id=run_id,
        run_index=run_index,
        now_provider=lambda: NOW,
        order_rebuilder=make_order_rebuilder({}, lambda: NOW),
    )


def exit_order(volume: int = 2) -> StockOrder:
    """平掉多單的賣出單：未成交時盤後要寫隔日補平"""

    return StockOrder(
        stock_id="2330",
        date=NOW,
        action=Action.SELL,
        position_type=PositionType.LONG,
        volume=volume,
        price=1000.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


def fill_for(ticket: OrderTicket, volume: int, trade_id: str) -> ExecutionReport:
    return ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id=trade_id,
        symbol="2330",
        action=Action.SELL,
        price=1000.0,
        volume=volume,
        ts=NOW,
    )


def cancel_report_for(ticket: OrderTicket) -> OrderStatusEvent:
    return OrderStatusEvent(
        broker_seqno=ticket.broker_seqno or "",
        op_type="Cancel",
        op_code="00",
        symbol="2330",
        exchange_ts=NOW + datetime.timedelta(seconds=30),
    )


def order_row(dao: LiveTradeDAO, client_order_id: str) -> Dict[str, Any]:
    return next(
        row
        for row in dao.get_orders_by_date(TODAY)
        if row["client_order_id"] == client_order_id
    )


def after_close(dao: LiveTradeDAO, oms: OrderManager) -> AfterCloseRunner:
    """只用到殘量處理，其餘元件不需要"""

    return AfterCloseRunner(
        data_feeds=[],
        broker=oms.broker,
        order_manager=oms,
        account_sync=None,
        reconciler=None,
        reporter=None,
        mode_state=None,
        dao=dao,
        run_id="run9",
        now_provider=lambda: NOW,
    )


# === 撤單不提前轉終態 ===
def test_cancel_request_keeps_the_order_open_until_the_broker_confirms(
    dao: LiveTradeDAO,
) -> None:
    """送出撤單請求後狀態不變；撤單回報進來才轉 CANCELLED"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(), "Alpha")

    assert oms.cancel_open_orders() == [ticket]
    assert ticket.status is LiveOrderStatus.SUBMITTED

    oms.drain_executions()

    assert ticket.status is LiveOrderStatus.CANCELLED
    assert order_row(dao, ticket.client_order_id)["status"] == "CANCELLED"


def test_cancel_request_is_not_sent_twice(dao: LiveTradeDAO) -> None:
    """撤單回報還沒到之前再撤一次，不重送請求"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(), "Alpha")

    oms.cancel_open_orders()
    oms.cancel_open_orders()

    assert broker.cancel_requests == [ticket.client_order_id]


def test_fill_already_queued_before_cancel_reaches_the_account(
    dao: LiveTradeDAO,
) -> None:
    """
    佇列裡已有成交 → 撤單 → 消化回報：成交要交給帳戶同步，DB 成交量正確

    以前撤單當下就轉 CANCELLED，佇列裡那筆成交變成非法轉移，`drain_executions()`
    回傳空清單——而 `live_fill` 裡明明有那一筆。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.5
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(volume=2), "Alpha")

    oms.cancel_open_orders()
    fills: List[ExecutionReport] = oms.drain_executions()

    assert [fill.volume for fill in fills] == [1]
    assert ticket.filled_volume == 1
    assert ticket.status is LiveOrderStatus.CANCELLED
    assert order_row(dao, ticket.client_order_id)["filled_volume"] == 1


def test_fill_arriving_after_the_cancel_report_is_still_applied(
    dao: LiveTradeDAO,
) -> None:
    """
    撤單回報先到、成交後到：成交照收，狀態停在 CANCELLED

    撤單與成交是競態，兩者的回報順序不保證。成交是事實，
    被狀態機丟掉的話帳上就少一筆，盤後還會對它算出殘量。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(volume=2), "Alpha")

    oms.apply_event(cancel_report_for(ticket))
    fill: Optional[ExecutionReport] = oms.apply_event(fill_for(ticket, 2, "T001"))

    assert fill is not None
    assert ticket.status is LiveOrderStatus.CANCELLED
    assert order_row(dao, ticket.client_order_id)["filled_volume"] == 2


def test_fully_filled_exit_does_not_leave_a_pending_cover(dao: LiveTradeDAO) -> None:
    """
    出場單在撤單後才回報全部成交：盤後不可寫隔日補平

    這就是「把部位做反」的那條路徑：成交被丟掉 → DB 成交量為 0 →
    盤後算出殘量 2 → 隔天對已經平掉的部位再賣 2。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(volume=2), "Alpha")

    oms.cancel_open_orders()
    broker.execution_queue.put(fill_for(ticket, 2, "T001"))
    oms.drain_executions()

    assert after_close(dao, oms).handle_unfilled_remainders(TODAY) == 0
    assert dao.get_pending_actions(TODAY + datetime.timedelta(days=1)) == []


def test_refresh_does_not_overwrite_the_filled_volume(dao: LiveTradeDAO) -> None:
    """
    盤後刷新只套狀態，成交量只由成交回報累加

    以券商累計量覆寫之後，同一筆成交的回報再進來會重複累加。
    券商量比本地多時寫事件——那代表有成交回報沒收到，帳戶也少記了。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    oms: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = oms.submit(exit_order(volume=2), "Alpha")

    broker_side: OrderTicket = OrderTicket(
        client_order_id="",
        status=LiveOrderStatus.PARTIALLY_FILLED,
        broker_seqno=ticket.broker_seqno,
        filled_volume=1,
    )
    broker.refresh_order_status = lambda: [broker_side]  # type: ignore[assignment]
    oms.refresh_from_broker()

    assert ticket.status is LiveOrderStatus.PARTIALLY_FILLED
    assert ticket.filled_volume == 0
    assert [
        row[0]
        for row in dao.conn.execute(
            "SELECT category FROM live_risk_event ORDER BY event_id"
        )
    ] == ["FILL_REPORT_MISSING"]


# === 跨行程重建不清空委託列 ===
def test_rebuilt_ticket_keeps_the_order_row_intact(dao: LiveTradeDAO) -> None:
    """
    盤後行程標記日終失效：該列的標的、數量、壓縮碼、送單時間與 run 都不變

    盤後的 `self.tickets` 是空的，委託要從 DB 重建。以前重建不帶原始訂單，
    寫回時標的變 `''`、數量變 0，壓縮碼以盤後的 run 重算。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    run1: OrderManager = make_oms(broker, dao, "run1", 1)
    ticket: OrderTicket = run1.submit(exit_order(volume=2), "Alpha")
    before: Dict[str, Any] = order_row(dao, ticket.client_order_id)

    run9: OrderManager = make_oms(broker, dao, "run9", 9)
    run9.expire_unfinished(TODAY)
    after: Dict[str, Any] = order_row(dao, ticket.client_order_id)

    assert after["status"] == "CANCELLED"
    for column in (
        "symbol",
        "action",
        "position_type",
        "price",
        "volume",
        "custom_field",
        "created_at",
        "run_id",
    ):
        assert after[column] == before[column], column


def test_expired_exit_order_creates_one_pending_cover(dao: LiveTradeDAO) -> None:
    """未成交的出場單經跨行程日終失效後，盤後正確產生 1 筆隔日補平"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    make_oms(broker, dao, "run1", 1).submit(exit_order(volume=2), "Alpha")

    run9: OrderManager = make_oms(broker, dao, "run9", 9)
    run9.expire_unfinished(TODAY)

    assert after_close(dao, run9).handle_unfilled_remainders(TODAY) == 1
    pending: List[Dict[str, Any]] = dao.get_pending_actions(
        TODAY + datetime.timedelta(days=1)
    )
    assert [(row["symbol"], row["volume"]) for row in pending] == [("2330", 2)]


def test_recovered_order_reaches_filled_and_occupies_its_symbol(
    dao: LiveTradeDAO,
) -> None:
    """
    重啟接管的委託全額成交要轉 FILLED，且帶著標的

    重建不帶訂單時不知道委託量，全額成交也只會停在 PARTIALLY_FILLED；
    也不知道標的，這張單就不佔 `max_holdings` 的名額。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    ticket: OrderTicket = make_oms(broker, dao, "run1", 1).submit(
        exit_order(volume=2), "Alpha"
    )

    run2: OrderManager = make_oms(broker, dao, "run2", 2)
    recovered: OrderTicket = run2.recover(TODAY)[0]

    assert recovered.order is not None
    assert recovered.order.symbol == "2330"
    assert recovered.order.timing is ExecutionTiming.AT_CLOSE

    run2.apply_event(fill_for(ticket, 2, "T001"))

    assert recovered.status is LiveOrderStatus.FILLED


def test_rebuild_without_an_order_only_updates_state_columns(
    dao: LiveTradeDAO,
) -> None:
    """還原不出訂單（沒有注入建構器）時，寫回只動狀態欄位，標的與數量不被清空"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    ticket: OrderTicket = make_oms(broker, dao, "run1", 1).submit(
        exit_order(volume=2), "Alpha"
    )

    bare: OrderManager = OrderManager(
        broker=broker, dao=dao, run_id="run9", run_index=9, now_provider=lambda: NOW
    )
    bare.expire_unfinished(TODAY)
    after: Dict[str, Any] = order_row(dao, ticket.client_order_id)

    assert after["status"] == "CANCELLED"
    assert (after["symbol"], after["volume"], after["run_id"]) == ("2330", 2, "run1")
