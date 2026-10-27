import datetime
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.oms.order_manager import OrderManager
from core.models import ExecutionReport, OrderTicket, StockOrder
from core.utils import Action, ExecutionTiming, LiveOrderStatus, StockPriceType

from .conftest import FakeBroker

"""
成交回報的跨行程歸屬：股票線與期貨線是兩個行程、同一個帳號，券商的成交回報兩個行程都收得到

2026-10-13 的事故：兩筆股票成交先被期貨收盤段的行程收到、以空的歸屬寫進 `live_fill`；
股票段隨後收到同一筆，`INSERT OR IGNORE` 回 0 列被當成重放，委託從未更新、沒有任何 log。
盤後把兩張單標成日終未成交，對已經賣掉的部位排了次日補平。

這裡釘住三件事：非擁有者以紀錄庫查到的委託歸屬寫入、擁有者後到時認領並補套、
真正的重放不重複累加。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 30)


def connected_broker() -> FakeBroker:
    """已連線、不自動成交的假券商；成交回報由測試自己推"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    return broker


def make_oms(
    dao: LiveTradeDAO, run_id: str, run_index: int, strategy: str
) -> OrderManager:
    """以指定的執行代號與策略建立委託管理器"""

    return OrderManager(
        broker=connected_broker(),
        dao=dao,
        run_id=run_id,
        run_index=run_index,
        now_provider=lambda: NOW,
        strategy_names=[strategy],
    )


def buy_order(volume: int = 2) -> StockOrder:
    """組一張 2330 買單"""

    return StockOrder(
        stock_id="2330",
        action=Action.BUY,
        volume=volume,
        price=1000.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


def fill_for(
    ticket: OrderTicket, volume: int = 2, trade_id: str = "T1"
) -> ExecutionReport:
    """依委託單組出成交回報"""

    return ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id=trade_id,
        symbol="2330",
        action=Action.BUY,
        price=1000.0,
        volume=volume,
        ts=NOW,
    )


def fill_rows(dao: LiveTradeDAO) -> List[Dict[str, Any]]:
    """當日所有成交紀錄"""

    return dao.get_fills_by_date(TODAY)


def categories(dao: LiveTradeDAO) -> List[str]:
    """當日風控事件的類別清單"""

    return [str(row["category"]) for row in dao.get_risk_events_by_date(TODAY)]


def store_without_owner(
    dao: LiveTradeDAO, report: ExecutionReport, client_order_id: Any
) -> None:
    """模擬另一個行程（或舊版程式）先寫下的成交列"""

    dao.insert_fill(
        {
            "broker_seqno": report.broker_seqno,
            "broker_trade_id": report.broker_trade_id,
            "client_order_id": client_order_id,
            "strategy_name": None if client_order_id is None else "Other",
            "symbol": report.symbol,
            "action": report.action.value,
            "price": report.price,
            "volume": report.volume,
            "filled_at": report.ts,
            "raw_json": "{}",
        }
    )


@pytest.fixture
def owner(dao: LiveTradeDAO) -> OrderManager:
    """股票線：擁有委託的行程"""

    return make_oms(dao, "stock", 1, "Alpha")


@pytest.fixture
def other(dao: LiveTradeDAO) -> OrderManager:
    """期貨線：同一個帳號、另一個行程，共用同一個紀錄庫"""

    return make_oms(dao, "futures", 2, "Beta")


# === 非擁有者先收到 ===
def test_non_owner_attributes_the_fill_to_the_owners_order(
    owner: OrderManager, other: OrderManager, dao: LiveTradeDAO
) -> None:
    """另一個行程先收到：以紀錄庫查到的委託歸屬寫入，不記 FILL_UNMATCHED、不動擁有者的狀態機"""

    ticket: OrderTicket = owner.submit(buy_order(), "Alpha")

    assert other.apply_event(fill_for(ticket)) is None

    rows: List[Dict[str, Any]] = fill_rows(dao)
    assert len(rows) == 1
    assert rows[0]["client_order_id"] == ticket.client_order_id
    assert rows[0]["strategy_name"] == "Alpha"
    assert "FILL_UNMATCHED" not in categories(dao)
    assert ticket.status is LiveOrderStatus.SUBMITTED


# === 擁有者後到：認領 ===
def test_owner_books_a_fill_another_process_stored_first(
    owner: OrderManager, other: OrderManager, dao: LiveTradeDAO
) -> None:
    """擁有者後到：寫入被忽略不是重放，要認領並套進委託（事故當天漏掉的那一步）"""

    ticket: OrderTicket = owner.submit(buy_order(), "Alpha")
    report: ExecutionReport = fill_for(ticket)
    other.apply_event(report)

    assert owner.apply_event(report) is report

    assert ticket.status is LiveOrderStatus.FILLED
    assert ticket.filled_volume == 2
    assert dao.get_orders_by_date(TODAY)[0]["status"] == "FILLED"
    assert len(fill_rows(dao)) == 1
    assert "FILL_CLAIMED" in categories(dao)


def test_owner_claims_a_row_stored_without_attribution(
    owner: OrderManager, dao: LiveTradeDAO
) -> None:
    """舊版行程寫下的空歸屬列（事故當天的形狀）：擁有者認領後補上歸屬並套進委託"""

    ticket: OrderTicket = owner.submit(buy_order(), "Alpha")
    report: ExecutionReport = fill_for(ticket)
    store_without_owner(dao, report, None)

    assert owner.apply_event(report) is report

    row: Dict[str, Any] = fill_rows(dao)[0]
    assert row["client_order_id"] == ticket.client_order_id
    assert row["strategy_name"] == "Alpha"
    assert ticket.status is LiveOrderStatus.FILLED


def test_partial_fills_are_claimed_one_by_one(
    owner: OrderManager, other: OrderManager, dao: LiveTradeDAO
) -> None:
    """兩筆部分成交都先被另一個行程寫入：擁有者各認領一次，累計與狀態正確"""

    ticket: OrderTicket = owner.submit(buy_order(volume=2), "Alpha")
    first: ExecutionReport = fill_for(ticket, volume=1, trade_id="T1")
    second: ExecutionReport = fill_for(ticket, volume=1, trade_id="T2")
    other.apply_event(first)
    other.apply_event(second)

    assert owner.apply_event(first) is first
    assert ticket.status is LiveOrderStatus.PARTIALLY_FILLED
    assert ticket.filled_volume == 1

    assert owner.apply_event(second) is second
    assert ticket.status is LiveOrderStatus.FILLED
    assert ticket.filled_volume == 2


# === 真正的重放與別人的成交 ===
def test_replay_after_restart_is_not_counted_twice(
    owner: OrderManager, dao: LiveTradeDAO
) -> None:
    """重啟後同一筆再推一次：委託由紀錄重建、已含這筆，不再累加"""

    ticket: OrderTicket = owner.submit(buy_order(), "Alpha")
    report: ExecutionReport = fill_for(ticket)
    owner.apply_event(report)

    restarted: OrderManager = make_oms(dao, "stock2", 3, "Alpha")
    row: Dict[str, Any] = dao.get_orders_by_date(TODAY)[0]
    restarted.tickets[ticket.client_order_id] = restarted._rebuild_ticket(row)

    assert restarted.apply_event(report) is None
    assert restarted.tickets[ticket.client_order_id].filled_volume == 2
    assert dao.get_orders_by_date(TODAY)[0]["filled_volume"] == 2


def test_fill_attributed_to_another_order_is_not_claimed(
    owner: OrderManager, dao: LiveTradeDAO
) -> None:
    """成交已歸屬到別張委託：不搶、不套，讓對帳抓出來"""

    ticket: OrderTicket = owner.submit(buy_order(), "Alpha")
    report: ExecutionReport = fill_for(ticket)
    store_without_owner(dao, report, "someone-else-0001")

    assert owner.apply_event(report) is None
    assert ticket.filled_volume == 0
    assert fill_rows(dao)[0]["client_order_id"] == "someone-else-0001"


def test_unknown_fill_is_still_recorded_as_unmatched(
    owner: OrderManager, dao: LiveTradeDAO
) -> None:
    """全庫都查不到的成交：照舊記 FILL_UNMATCHED、保留成交並交給帳戶同步"""

    report: ExecutionReport = ExecutionReport(
        broker_seqno="999999",
        broker_trade_id="TX",
        symbol="2330",
        action=Action.BUY,
        price=1000.0,
        volume=1,
        ts=NOW,
    )

    assert owner.apply_event(report) is report
    assert fill_rows(dao)[0]["client_order_id"] is None
    assert categories(dao) == ["FILL_UNMATCHED"]
