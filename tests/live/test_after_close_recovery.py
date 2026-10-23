import datetime
from typing import Any, List, Tuple

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
盤後先接管當日委託，再向券商刷新、標撤與排補平

盤後是獨立行程，`OrderManager.tickets` 為空：不先由紀錄庫重建，刷新沒有對象、
`FILL_REPORT_MISSING` 沒有執行端，日終標撤會把券商端已成交的出場單標成未成交，
殘量處理再排「次日對已平掉的部位再賣一次」。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 14, 30)
FILLED_AT: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 30)


class _Stub:
    """任何方法都回空 dict 的替身；盤後流程裡與本測試無關的元件"""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: {}


def exit_order(volume: int = 2) -> StockOrder:
    """2330 賣出平多的限價單"""

    return StockOrder(
        stock_id="2330",
        action=Action.SELL,
        position_type=PositionType.LONG,
        volume=volume,
        price=46.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


@pytest.fixture
def broker() -> FakeBroker:
    fake: FakeBroker = FakeBroker()
    fake.connect()
    fake.fill_ratio = 0.0
    return fake


@pytest.fixture
def ledger(dao: LiveTradeDAO) -> PositionAttributionLedger:
    return PositionAttributionLedger(dao, lambda: NOW, run_id="after")


@pytest.fixture
def sync(dao: LiveTradeDAO, ledger: PositionAttributionLedger) -> AccountSynchronizer:
    return AccountSynchronizer(
        position_managers={}, ledger=ledger, dao=dao, now_provider=lambda: NOW
    )


def make_runner(
    broker: FakeBroker, dao: LiveTradeDAO, sync: AccountSynchronizer
) -> AfterCloseRunner:
    """盤後行程：自己的委託管理器，手上沒有任何 ticket"""

    oms: OrderManager = OrderManager(
        broker=broker,
        dao=dao,
        run_id="after",
        run_index=9,
        now_provider=lambda: NOW,
        order_rebuilder=lambda row: exit_order(int(row["volume"])),
        strategy_names=["Alpha"],
    )
    return AfterCloseRunner(
        data_feeds=[],
        broker=broker,
        order_manager=oms,
        account_sync=sync,
        reconciler=_Stub(),
        reporter=_Stub(),
        mode_state=_Stub(),
        dao=dao,
        run_id="after",
        now_provider=lambda: NOW,
    )


def submit_in_a_dead_process(
    broker: FakeBroker, dao: LiveTradeDAO, volume: int = 2, status: str = "SUBMITTED"
) -> OrderTicket:
    """尾盤段送出一張出場單後行程死掉；券商端留著這張單"""

    ticket: OrderTicket = OrderManager(
        broker, dao, "close", run_index=1, now_provider=lambda: FILLED_AT
    ).submit(exit_order(volume), "Alpha")
    if status != "SUBMITTED":
        dao.update_order_state(ticket.client_order_id, {"status": status})
        dao.conn.commit()
    return ticket


def broker_fills(broker: FakeBroker, ticket: OrderTicket, volume: int) -> None:
    """券商端把這張單記成已成交 `volume`"""

    side: OrderTicket = broker.tickets[ticket.client_order_id]
    side.filled_volume = volume
    side.status = (
        LiveOrderStatus.FILLED
        if side.order is not None and volume >= side.order.volume
        else LiveOrderStatus.PARTIALLY_FILLED
    )


def store_fill_without_owner(
    dao: LiveTradeDAO, ticket: OrderTicket, volume: int
) -> None:
    """另一個行程在擁有者死後收到成交：寫進 live_fill、沒有歸屬"""

    dao.insert_fill(
        {
            "broker_seqno": ticket.broker_seqno,
            "broker_trade_id": "T1",
            "client_order_id": None,
            "strategy_name": None,
            "symbol": "2330",
            "action": "Sell",
            "price": 46.0,
            "volume": volume,
            "filled_at": FILLED_AT,
            "raw_json": "{}",
        }
    )
    dao.conn.commit()


def seed_long_lot(ledger: PositionAttributionLedger, volume: int) -> None:
    ledger.open_lot(
        "Alpha",
        ExecutionReport(
            broker_seqno="S0",
            broker_trade_id="T0",
            symbol="2330",
            action=Action.BUY,
            price=40.0,
            volume=volume,
            ts=FILLED_AT - datetime.timedelta(days=1),
        ),
        PositionType.LONG,
    )


def order_state(dao: LiveTradeDAO, client_order_id: str) -> Tuple[str, int]:
    return dao.conn.execute(
        "SELECT status, filled_volume FROM live_order WHERE client_order_id = ?",
        (client_order_id,),
    ).fetchone()


def categories(dao: LiveTradeDAO) -> List[str]:
    return [str(row["category"]) for row in dao.get_risk_events_by_date(TODAY)]


def pending_volumes(dao: LiveTradeDAO) -> List[int]:
    return [
        int(row["volume"])
        for row in dao.get_pending_actions(TODAY + datetime.timedelta(days=1))
    ]


def test_filled_exit_with_the_fill_stored_elsewhere_is_not_covered_again(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
    sync: AccountSynchronizer,
) -> None:
    """
    紀錄庫 SUBMITTED、成交 0；券商 FILLED 2/2、成交在 live_fill 無歸屬 → 盤後 FILLED 2、lot 平掉、不排補平

    2026-10-13 的實況是反過來的：標成日終未成交，再對已經平掉的部位排次日補平。
    """

    seed_long_lot(ledger, 2)
    ticket: OrderTicket = submit_in_a_dead_process(broker, dao)
    broker_fills(broker, ticket, 2)
    store_fill_without_owner(dao, ticket, 2)

    make_runner(broker, dao, sync).run()

    assert order_state(dao, ticket.client_order_id) == ("FILLED", 2)
    assert ledger.get_strategy_positions("Alpha") == {}
    assert pending_volumes(dao) == []
    assert "FILL_BACKFILLED" in categories(dao)
    assert "FILL_REPORT_MISSING" not in categories(dao)


def test_filled_exit_with_no_fill_record_is_flagged_not_covered_again(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
    sync: AccountSynchronizer,
) -> None:
    """成交回報完全沒收到：狀態仍推到 FILLED、寫 `FILL_REPORT_MISSING`，不排補平（補平會把部位做反）"""

    seed_long_lot(ledger, 2)
    ticket: OrderTicket = submit_in_a_dead_process(broker, dao)
    broker_fills(broker, ticket, 2)

    make_runner(broker, dao, sync).run()

    assert order_state(dao, ticket.client_order_id)[0] == "FILLED"
    assert "FILL_REPORT_MISSING" in categories(dao)
    assert pending_volumes(dao) == []


def test_partial_fill_remainder_uses_the_broker_volume(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
    sync: AccountSynchronizer,
) -> None:
    """本地 1/5、券商 3/5：狀態相同也要比成交量——寫事件，殘量照券商算 2 而不是 4"""

    seed_long_lot(ledger, 5)
    ticket: OrderTicket = submit_in_a_dead_process(broker, dao, volume=5)
    broker.execution_queue.put(
        ExecutionReport(
            broker_seqno=ticket.broker_seqno or "",
            broker_trade_id="T1",
            symbol="2330",
            action=Action.SELL,
            price=46.0,
            volume=1,
            ts=FILLED_AT,
        )
    )
    OrderManager(
        broker, dao, "close", run_index=1, now_provider=lambda: FILLED_AT
    ).tickets[ticket.client_order_id] = ticket
    # 擁有者行程只來得及收到 1 張
    dao.insert_fill(
        {
            "broker_seqno": ticket.broker_seqno,
            "broker_trade_id": "T1",
            "client_order_id": ticket.client_order_id,
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "price": 46.0,
            "volume": 1,
            "filled_at": FILLED_AT,
            "raw_json": "{}",
        }
    )
    dao.update_order_state(
        ticket.client_order_id, {"status": "PARTIALLY_FILLED", "filled_volume": 1}
    )
    dao.conn.commit()
    broker_fills(broker, ticket, 3)

    make_runner(broker, dao, sync).run()

    assert "FILL_REPORT_MISSING" in categories(dao)
    assert pending_volumes(dao) == [2]


def test_failed_send_the_broker_filled_is_recovered_after_close(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
    sync: AccountSynchronizer,
) -> None:
    """
    2026-10-20 的情境：開倉腿送單逾時被標 FAILED、沒有序號，券商已成交、成交被收成未歸屬

    盤後接管以壓縮碼對回序號 → 回填認領成交、從 `__unattributed__` 沖回策略 → 不排補平。
    當天是人工補序號再補跑盤後才修回來的。
    """

    oms: OrderManager = OrderManager(
        broker, dao, "close", run_index=1, now_provider=lambda: FILLED_AT
    )
    original = broker.place_order

    def timing_out(ticket: OrderTicket) -> OrderTicket:
        original(ticket)
        # 券商受理了，但本地沒拿到回應：序號留在券商端
        ticket.broker_seqno = None
        ticket.broker_order_id = None
        raise TimeoutError("Timeout Topic: api/v1/paper/place_order")

    broker.place_order = timing_out  # type: ignore[assignment]
    buy: StockOrder = StockOrder(
        stock_id="2330",
        action=Action.BUY,
        position_type=PositionType.LONG,
        volume=1,
        price=46.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )
    with pytest.raises(TimeoutError):
        oms.submit(buy, "Alpha")
    ticket: OrderTicket = next(iter(oms.tickets.values()))
    assert order_state(dao, ticket.client_order_id)[0] == "FAILED"
    side: OrderTicket = broker.tickets[ticket.client_order_id]
    side.broker_seqno, side.broker_order_id = "00D23D", "17E54A"
    side.status, side.filled_volume = LiveOrderStatus.FILLED, 1
    dao.insert_fill(
        {
            "broker_seqno": "00D23D",
            "broker_trade_id": "00D23D",
            "client_order_id": None,
            "strategy_name": None,
            "symbol": "2330",
            "action": "Buy",
            "price": 46.0,
            "volume": 1,
            "filled_at": FILLED_AT,
            "raw_json": "{}",
        }
    )
    dao.conn.commit()
    ledger.add_unattributed_lot("2330", "LONG", 1, 46.0)
    dao.conn.commit()

    runner: AfterCloseRunner = make_runner(broker, dao, sync)
    runner.order_manager.order_rebuilder = lambda row: buy
    runner.run()

    assert order_state(dao, ticket.client_order_id) == ("FILLED", 1)
    assert ledger.get_strategy_positions("Alpha") == {("2330", "LONG"): 1}
    assert ledger.get_strategy_positions(UNATTRIBUTED_STRATEGY) == {}
    assert pending_volumes(dao) == []


def test_run_recovers_before_backfilling_and_expiring(
    broker: FakeBroker,
    dao: LiveTradeDAO,
    sync: AccountSynchronizer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """順序：接管 → 回填 → 刷新與標撤 → … → 殘量"""

    runner: AfterCloseRunner = make_runner(broker, dao, sync)
    calls: List[str] = []
    for name in (
        "recover_orders",
        "backfill_fills",
        "calibrate_costs",
        "handle_unfilled_remainders",
    ):
        monkeypatch.setattr(runner, name, lambda today, _n=name: calls.append(_n))
    monkeypatch.setattr(runner, "expire_open_orders", lambda: calls.append("expire"))
    monkeypatch.setattr(
        runner.account_sync, "rebuild_from_broker", lambda positions: {}
    )

    runner.run()

    assert calls[:3] == ["recover_orders", "backfill_fills", "expire"]
    assert calls[-1] == "handle_unfilled_remainders"


def test_remainders_ignore_the_other_lines_orders(
    broker: FakeBroker, dao: LiveTradeDAO, sync: AccountSynchronizer
) -> None:
    """
    盤後殘量只處理本行程策略的委託

    股票線與期貨線共用紀錄庫；股票盤後 14:30 若掃到期貨線還沒結束的出場單，
    會以它過期的成交量替它排待辦並重複推播，期貨盤後 14:35 再排一次。
    """

    dao.upsert_order(
        {
            "client_order_id": "fut-0001",
            "run_id": "futures",
            "strategy_name": "MomentumFuturesStrategy",
            "symbol": "TX202611",
            "action": "Sell",
            "position_type": "LONG",
            "price": 46000.0,
            "volume": 1,
            "status": "SUBMITTED",
            "custom_field": "f10001",
            "created_at": FILLED_AT,
        }
    )
    dao.conn.commit()

    assert make_runner(broker, dao, sync).handle_unfilled_remainders(TODAY) == 0
    assert pending_volumes(dao) == []
