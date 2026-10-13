import datetime
from typing import Any, Dict, List, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.after_close import AfterCloseRunner
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.oms.order_manager import OrderManager
from core.models import ExecutionReport, OrderTicket, StockOrder
from core.utils import (
    Action,
    ExecutionTiming,
    LiveOrderStatus,
    PositionType,
    StockPriceType,
)

from .conftest import FakeBroker

"""
盤後以 `live_fill` 的成交事實回填委託與歸屬帳

盤中的認領（`OrderManager._claim_fill()`）只在擁有者行程還活著時有用；收線後才到、
或在認領機制上線前就被另一個行程寫走的成交，只剩盤後能補。沒有這一層的話，
盤後會把已成交的單標成「日終未成交」，再對已經平掉的部位排次日補平——2026-10-13 的實況。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 14, 30)
FILLED_AT: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 30)


class _Stub:
    """任何方法都回空 dict 的替身；盤後流程裡與本測試無關的元件"""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: {}


def make_order(symbol: str, action: Action, volume: int) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        action=action,
        volume=volume,
        price=46.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


def report_for(
    ticket: OrderTicket, volume: int, price: float, trade_id: str = "T1"
) -> ExecutionReport:
    order: Any = ticket.order
    return ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id=trade_id,
        symbol=order.symbol,
        action=order.action,
        price=price,
        volume=volume,
        ts=FILLED_AT,
    )


def store_without_owner(dao: LiveTradeDAO, report: ExecutionReport) -> None:
    """模擬另一個行程先寫下、沒有歸屬的成交列"""

    dao.insert_fill(
        {
            "broker_seqno": report.broker_seqno,
            "broker_trade_id": report.broker_trade_id,
            "client_order_id": None,
            "strategy_name": None,
            "symbol": report.symbol,
            "action": report.action.value,
            "price": report.price,
            "volume": report.volume,
            "filled_at": report.ts,
            "raw_json": "{}",
        }
    )


def open_lots(dao: LiveTradeDAO, strategy: str) -> List[Tuple[str, int]]:
    return [
        (str(row["symbol"]), int(row["volume"]))
        for row in dao.get_open_lots(strategy_name=strategy)
    ]


def categories(dao: LiveTradeDAO) -> List[str]:
    return [str(row["category"]) for row in dao.get_risk_events_by_date(TODAY)]


@pytest.fixture
def broker() -> FakeBroker:
    fake: FakeBroker = FakeBroker()
    fake.connect()
    fake.fill_ratio = 0.0
    return fake


@pytest.fixture
def oms(broker: FakeBroker, dao: LiveTradeDAO) -> OrderManager:
    return OrderManager(
        broker=broker,
        dao=dao,
        run_id="close",
        run_index=1,
        now_provider=lambda: FILLED_AT,
        strategy_names=["Alpha"],
    )


@pytest.fixture
def ledger(dao: LiveTradeDAO) -> PositionAttributionLedger:
    return PositionAttributionLedger(dao, lambda: NOW)


@pytest.fixture
def sync(dao: LiveTradeDAO, ledger: PositionAttributionLedger) -> AccountSynchronizer:
    return AccountSynchronizer(
        position_managers={}, ledger=ledger, dao=dao, now_provider=lambda: NOW
    )


@pytest.fixture
def runner(
    dao: LiveTradeDAO, oms: OrderManager, sync: AccountSynchronizer
) -> AfterCloseRunner:
    return AfterCloseRunner(
        data_feeds=[],
        broker=oms.broker,
        order_manager=oms,
        account_sync=sync,
        reconciler=_Stub(),
        reporter=_Stub(),
        mode_state=_Stub(),
        dao=dao,
        run_id="after",
        now_provider=lambda: NOW,
    )


def seed_long_lot(
    ledger: PositionAttributionLedger, symbol: str, volume: int, price: float
) -> None:
    ledger.open_lot(
        "Alpha",
        ExecutionReport(
            broker_seqno="S0",
            broker_trade_id="T0",
            symbol=symbol,
            action=Action.BUY,
            price=price,
            volume=volume,
            ts=FILLED_AT - datetime.timedelta(days=1),
        ),
        PositionType.LONG,
    )


# === 出場單：成交被另一個行程寫走 ===
def test_backfill_books_an_exit_fill_stored_by_another_process(
    runner: AfterCloseRunner,
    oms: OrderManager,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> None:
    """委託轉 FILLED、成交列歸屬補上、歸屬帳的多單沖掉；日終不再標撤、不再排補平"""

    seed_long_lot(ledger, "2468", 2, 44.5)
    ticket: OrderTicket = oms.submit(make_order("2468", Action.SELL, 2), "Alpha")
    store_without_owner(dao, report_for(ticket, 2, 46.45))

    assert runner.backfill_fills(TODAY) == 1

    row: Dict[str, Any] = dao.get_orders_by_date(TODAY)[0]
    assert row["status"] == "FILLED"
    assert row["filled_volume"] == 2
    assert row["avg_fill_price"] == pytest.approx(46.45)
    assert dao.get_fills_by_seqno(ticket.broker_seqno or "")[0]["client_order_id"] == (
        ticket.client_order_id
    )
    assert open_lots(dao, "Alpha") == []
    assert "FILL_BACKFILLED" in categories(dao)

    assert oms.expire_unfinished(TODAY) == []
    assert runner.handle_unfilled_remainders(TODAY) == 0
    assert dao.get_pending_actions(TODAY + datetime.timedelta(days=1)) == []


# === 開倉單：先前已被收成未歸屬 ===
def test_backfill_opens_a_lot_and_reclaims_the_unattributed_copy(
    runner: AfterCloseRunner,
    oms: OrderManager,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> None:
    """開倉成交先前被某次盤後收成 `__unattributed__`：改歸策略，不多出一份"""

    ticket: OrderTicket = oms.submit(make_order("1569", Action.BUY, 1), "Alpha")
    store_without_owner(dao, report_for(ticket, 1, 71.3))
    ledger.add_unattributed_lot("1569", PositionType.LONG.value, 1, 71.3)
    dao.commit()

    assert runner.backfill_fills(TODAY) == 1

    assert open_lots(dao, "Alpha") == [("1569", 1)]
    assert open_lots(dao, UNATTRIBUTED_STRATEGY) == []
    assert dao.get_orders_by_date(TODAY)[0]["status"] == "FILLED"


# === 只補沒套到的那部分 ===
def test_backfill_only_books_the_missing_part(
    runner: AfterCloseRunner, oms: OrderManager, dao: LiveTradeDAO
) -> None:
    """擁有者自己已套進 1 張、另一行程寫走 2 張：只回填那 2 張"""

    ticket: OrderTicket = oms.submit(make_order("2468", Action.BUY, 3), "Alpha")
    oms.apply_event(report_for(ticket, 1, 46.0, trade_id="T1"))
    assert ticket.filled_volume == 1
    store_without_owner(dao, report_for(ticket, 2, 46.5, trade_id="T2"))

    assert runner.backfill_fills(TODAY) == 1

    row: Dict[str, Any] = dao.get_orders_by_date(TODAY)[0]
    assert row["status"] == "FILLED"
    assert row["filled_volume"] == 3
    assert open_lots(dao, "Alpha") == [("2468", 2)]


def test_nothing_to_backfill_when_the_owner_already_booked_the_fill(
    runner: AfterCloseRunner, oms: OrderManager, dao: LiveTradeDAO
) -> None:
    """正常日：成交由擁有者即時套進，盤後不回填、不寫事件"""

    ticket: OrderTicket = oms.submit(make_order("2468", Action.BUY, 2), "Alpha")
    oms.apply_event(report_for(ticket, 2, 46.0))

    assert runner.backfill_fills(TODAY) == 0
    assert "FILL_BACKFILLED" not in categories(dao)
    assert ticket.status is LiveOrderStatus.FILLED


# === 在盤後流程裡的位置 ===
def test_run_backfills_before_expiring_orders(
    runner: AfterCloseRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回填排在日終標撤之前：順序反了，已成交的單會先被標成未成交"""

    calls: List[str] = []
    monkeypatch.setattr(
        runner, "backfill_fills", lambda today: calls.append("backfill")
    )
    monkeypatch.setattr(runner, "expire_open_orders", lambda: calls.append("expire"))
    monkeypatch.setattr(runner, "calibrate_costs", lambda today: calls.append("costs"))
    monkeypatch.setattr(
        runner, "handle_unfilled_remainders", lambda today: calls.append("remainders")
    )
    monkeypatch.setattr(
        runner.account_sync, "rebuild_from_broker", lambda positions: {}
    )

    runner.run()

    assert calls[:2] == ["backfill", "expire"]
    assert calls[-1] == "remainders"
