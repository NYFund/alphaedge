import datetime
from typing import Any, Dict, List, Optional

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.after_close import AfterCloseRunner
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.factory import make_order_rebuilder
from core.live.oms.order_manager import OrderManager
from core.models import ExecutionReport, OrderTicket, StockAccount, StockOrder
from core.position.stock.position_manager import StockPositionManager
from core.utils import (
    Action,
    ExecutionTiming,
    LiveOrderStatus,
    PositionType,
    StockPriceType,
)

from .conftest import FakeBroker

"""
成交回報的跨行程歸屬

股票線與期貨線是兩個行程、登入同一個帳號、共用同一個紀錄庫，**券商的成交回呼兩邊都收得到**。
`live_fill` 以 `(broker_seqno, broker_trade_id)` 為主鍵，誰先寫誰佔住那一列。
以前只有「寫入成功」才把成交套進委託，於是非擁有者先收到時，擁有者的寫入被當成重複：
委託停在送出狀態、盤後被標成日終未成交，已經賣掉的部位還被排了隔日補平。

三層各自擋一種情況：
- 擁有者後到：認領空歸屬的那一列，照常套進委託。
- 非擁有者先到：寫入但不歸屬、不寫 `FILL_UNMATCHED`、不交給自己的帳戶同步。
- 擁有者整筆漏接：盤後在日終失效之前認領，並補進歸屬帳。
"""

NOW: datetime.datetime = datetime.datetime(2026, 10, 13, 13, 30)
TODAY: datetime.date = NOW.date()

STOCK: str = "Alpha"
FUTURES: str = "Fut"


def make_oms(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    run_id: str,
    run_index: int,
    strategy_names: List[str],
) -> OrderManager:
    return OrderManager(
        broker=broker,
        dao=dao,
        run_id=run_id,
        run_index=run_index,
        now_provider=lambda: NOW,
        order_rebuilder=make_order_rebuilder({}, lambda: NOW),
        strategy_names=strategy_names,
    )


def exit_order(volume: int = 2) -> StockOrder:
    """平掉多單的賣出單：未成交時盤後要寫隔日補平"""

    return StockOrder(
        stock_id="2468",
        date=NOW,
        action=Action.SELL,
        position_type=PositionType.LONG,
        volume=volume,
        price=44.5,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


def fill_for(
    ticket: OrderTicket, volume: int = 2, trade_id: str = "T1"
) -> ExecutionReport:
    return ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id=trade_id,
        symbol="2468",
        action=Action.SELL,
        price=44.5,
        volume=volume,
        ts=NOW,
    )


def deliver(
    oms: OrderManager, broker: FakeBroker, report: ExecutionReport
) -> List[Any]:
    """把一筆回報送進某個行程並消化"""

    broker.execution_queue.put(report)
    return oms.drain_executions()


def fill_rows(dao: LiveTradeDAO) -> List[Dict[str, Any]]:
    return dao.get_fills_by_date(TODAY)


def event_categories(dao: LiveTradeDAO) -> List[str]:
    return [
        row[0]
        for row in dao.conn.execute(
            "SELECT category FROM live_risk_event ORDER BY event_id"
        )
    ]


def order_row(dao: LiveTradeDAO, client_order_id: str) -> Dict[str, Any]:
    return next(
        row
        for row in dao.get_orders_by_date(TODAY)
        if row["client_order_id"] == client_order_id
    )


def submit_stock_exit(broker: FakeBroker, dao: LiveTradeDAO) -> OrderTicket:
    broker.connect()
    broker.fill_ratio = 0.0
    stock: OrderManager = make_oms(broker, dao, "run1", 1, [STOCK])
    return stock.submit(exit_order(), STOCK)


# === 擁有者後到：認領 ===
def test_owner_claims_a_fill_written_first_by_the_other_process(
    dao: LiveTradeDAO,
) -> None:
    """
    期貨行程先收到股票的成交，股票行程後到：委託仍要轉 FILLED，該列歸屬到股票策略

    2026-10-13 的實況：股票段的寫入回 0 列、被當成重複，兩張委託停在送出狀態，沒有任何 log。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    stock: OrderManager = make_oms(broker, dao, "run1", 1, [STOCK])
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    ticket: OrderTicket = stock.submit(exit_order(), STOCK)

    assert deliver(futures, broker, fill_for(ticket)) == []
    applied: List[Any] = deliver(stock, broker, fill_for(ticket))

    assert len(applied) == 1
    assert ticket.status is LiveOrderStatus.FILLED
    assert order_row(dao, ticket.client_order_id)["filled_volume"] == 2
    assert [
        (row["client_order_id"], row["strategy_name"]) for row in fill_rows(dao)
    ] == [(ticket.client_order_id, STOCK)]


def test_a_fill_already_attributed_is_a_real_duplicate(dao: LiveTradeDAO) -> None:
    """
    該列已歸屬到這張委託時，再收到同一筆就是重複：不可再累加一次

    認領只對「歸屬為空」的列生效；放寬成「已歸屬到同一張也套用」的話，
    斷線重連的重放會讓成交量加倍，帳上多出一個不存在的部位。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    stock: OrderManager = make_oms(broker, dao, "run1", 1, [STOCK])
    ticket: OrderTicket = stock.submit(exit_order(volume=4), STOCK)

    assert stock._apply_fill(fill_for(ticket, volume=2)) is True
    assert stock._apply_fill(fill_for(ticket, volume=2)) is False

    assert ticket.filled_volume == 2
    assert ticket.status is LiveOrderStatus.PARTIALLY_FILLED


# === 非擁有者先到：不搶 ===
def test_the_other_process_records_the_fill_without_claiming_it(
    dao: LiveTradeDAO,
) -> None:
    """
    非擁有者先收到：成交照寫、歸屬留空、不寫 `FILL_UNMATCHED`、不交給自己的帳戶同步

    兩個行程同收一筆回呼是常態；當成未歸屬寫 CRITICAL 的話，對方的每一筆成交都會告警一次。
    """

    broker: FakeBroker = FakeBroker()
    ticket: OrderTicket = submit_stock_exit(broker, dao)
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])

    assert deliver(futures, broker, fill_for(ticket)) == []

    assert [row["client_order_id"] for row in fill_rows(dao)] == [None]
    assert "FILL_UNMATCHED" not in event_categories(dao)


def test_owner_first_leaves_the_other_process_silent(dao: LiveTradeDAO) -> None:
    """擁有者先到（2026-10-12 的情況）：非擁有者後到時不改歸屬、不寫事件"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    stock: OrderManager = make_oms(broker, dao, "run1", 1, [STOCK])
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    ticket: OrderTicket = stock.submit(exit_order(), STOCK)

    assert len(deliver(stock, broker, fill_for(ticket))) == 1
    assert deliver(futures, broker, fill_for(ticket)) == []

    assert [row["strategy_name"] for row in fill_rows(dao)] == [STOCK]
    assert event_categories(dao) == []


def test_fill_for_no_known_order_is_still_unmatched(dao: LiveTradeDAO) -> None:
    """全庫都查不到委託的成交才是真的未歸屬：照舊寫 `FILL_UNMATCHED`"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    report: ExecutionReport = ExecutionReport(
        broker_seqno="999999",
        broker_trade_id="TX",
        symbol="2468",
        action=Action.SELL,
        price=44.5,
        volume=1,
        ts=NOW,
    )

    assert len(deliver(futures, broker, report)) == 1
    assert event_categories(dao) == ["FILL_UNMATCHED"]


# === 擁有者整筆漏接：盤後認領 ===
def make_after_close(
    dao: LiveTradeDAO, oms: OrderManager, sync: Optional[AccountSynchronizer]
) -> AfterCloseRunner:
    return AfterCloseRunner(
        data_feeds=[],
        broker=oms.broker,
        order_manager=oms,
        account_sync=sync,
        reconciler=None,
        reporter=None,
        mode_state=None,
        dao=dao,
        run_id="run9",
        now_provider=lambda: NOW,
    )


def test_after_close_claims_a_fill_the_owner_never_received(
    dao: LiveTradeDAO,
) -> None:
    """
    回報只被期貨行程收到：盤後要先認領再判殘量——委託 FILLED、不排補平、歸屬帳扣掉

    這是保險絲：前兩層靠擁有者「有收到」回報，收不到時只剩盤後這一關。
    沒有它，出場單會被標日終未成交，隔天對已賣掉的 2 張再送一次賣單。
    """

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        dao, now_provider=lambda: NOW
    )
    ledger.open_lot(
        STOCK,
        ExecutionReport(
            broker_seqno="000000",
            broker_trade_id="T0",
            symbol="2468",
            action=Action.BUY,
            price=44.0,
            volume=2,
            ts=NOW - datetime.timedelta(days=1),
        ),
        PositionType.LONG,
    )
    sync: AccountSynchronizer = AccountSynchronizer(
        {STOCK: StockPositionManager(StockAccount(init_capital=1_000_000.0))},
        ledger,
        dao,
        now_provider=lambda: NOW,
    )

    broker: FakeBroker = FakeBroker()
    ticket: OrderTicket = submit_stock_exit(broker, dao)
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    deliver(futures, broker, fill_for(ticket))
    # 券商端這張單已全部成交；盤後刷新看到的是這個狀態
    broker.refresh_order_status = lambda: [  # type: ignore[assignment]
        OrderTicket(
            client_order_id="",
            status=LiveOrderStatus.FILLED,
            broker_seqno=ticket.broker_seqno,
            filled_volume=2,
        )
    ]

    after_close_oms: OrderManager = make_oms(broker, dao, "run9", 9, [STOCK])
    runner: AfterCloseRunner = make_after_close(dao, after_close_oms, sync)
    runner.expire_open_orders()

    assert order_row(dao, ticket.client_order_id)["status"] == "FILLED"
    assert order_row(dao, ticket.client_order_id)["filled_volume"] == 2
    assert runner.handle_unfilled_remainders(TODAY) == 0
    assert dao.get_pending_actions(TODAY + datetime.timedelta(days=1)) == []
    assert ledger.get_strategy_positions(STOCK).get(("2468", "LONG"), 0) == 0
    assert [row["strategy_name"] for row in fill_rows(dao)] == [STOCK]
    assert event_categories(dao) == ["FILL_CLAIMED_AFTER_CLOSE"]


def test_claim_runs_before_the_broker_refresh(dao: LiveTradeDAO) -> None:
    """
    OMS 手上已有這張委託時，認領要排在刷新之前

    刷新先把狀態轉成已成交（成交量仍是 0），認領的成交再套上去就是 FILLED → FILLED
    的非法轉移而被跳過；成交量留在 0，殘量處理照樣排出隔日補平。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    stock: OrderManager = make_oms(broker, dao, "run1", 1, [STOCK])
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    ticket: OrderTicket = stock.submit(exit_order(), STOCK)
    deliver(futures, broker, fill_for(ticket))
    broker.refresh_order_status = lambda: [  # type: ignore[assignment]
        OrderTicket(
            client_order_id="",
            status=LiveOrderStatus.FILLED,
            broker_seqno=ticket.broker_seqno,
            filled_volume=2,
        )
    ]

    runner: AfterCloseRunner = make_after_close(dao, stock, None)
    runner.expire_open_orders()

    assert order_row(dao, ticket.client_order_id)["filled_volume"] == 2
    assert runner.handle_unfilled_remainders(TODAY) == 0


def test_after_close_leaves_the_other_line_fills_alone(dao: LiveTradeDAO) -> None:
    """期貨行程的盤後不可認領股票委託的成交：只處理本行程策略的委託"""

    broker: FakeBroker = FakeBroker()
    ticket: OrderTicket = submit_stock_exit(broker, dao)
    futures: OrderManager = make_oms(broker, dao, "run2", 2, [FUTURES])
    deliver(futures, broker, fill_for(ticket))

    futures_after_close: OrderManager = make_oms(broker, dao, "run9", 9, [FUTURES])

    assert futures_after_close.claim_unattributed_fills(TODAY) == []
    assert [row["client_order_id"] for row in fill_rows(dao)] == [None]
