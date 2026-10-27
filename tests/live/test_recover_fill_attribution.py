import datetime
from typing import Any, Dict, List, Tuple

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer, build_stock_order
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.fill_backfill import FillBackfiller
from core.live.oms.order_manager import OrderManager
from core.models import (
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderTicket,
    StockAccount,
    StockOrder,
)
from core.position.stock.position_manager import StockPositionManager
from core.utils import (
    Action,
    ExecutionTiming,
    LiveOrderStatus,
    PositionType,
    StockPriceType,
)

from .conftest import FakeBroker
from .test_live_trader_day import Harness, ScriptedStrategy, start_run

"""
送單後行程死掉、成交由另一個行程收下、重啟：部位要歸回原策略，而不是收進 `__unattributed__`

重啟的 `prepare()` 先接管委託再以歸屬帳重建部位。以前接管把券商的累計成交量直接抄進
`filled_volume`，接著的重建把券商多出的部位收進 `__unattributed__`——對帳一致、不降級、
只有一行 warning，而那口部位從此不再被策略管理。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)
STRATEGY: str = "VolumeBreakoutMomentumStrategy"


def make_order(volume: int = 2) -> StockOrder:
    return StockOrder(
        stock_id="2330",
        action=Action.BUY,
        position_type=PositionType.LONG,
        volume=volume,
        price=1000.0,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


def make_oms(
    broker: FakeBroker, dao: LiveTradeDAO, run_id: str, index: int
) -> OrderManager:
    return OrderManager(
        broker,
        dao,
        run_id,
        run_index=index,
        now_provider=lambda: NOW,
        order_rebuilder=lambda row: make_order(int(row["volume"])),
        strategy_names=[STRATEGY],
    )


def fill_for(ticket: OrderTicket, volume: int) -> ExecutionReport:
    return ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id="T001",
        symbol="2330",
        action=Action.BUY,
        price=1000.0,
        volume=volume,
        ts=NOW,
    )


def store_as_the_other_process_would(
    dao: LiveTradeDAO, report: ExecutionReport
) -> None:
    """另一個行程收到成交：以紀錄庫查到的委託歸屬寫入，不動擁有者的狀態機"""

    owner: Dict[str, Any] = (
        dao.find_order_by_broker_seqno(report.broker_seqno, TODAY) or {}
    )
    dao.insert_fill(
        {
            "broker_seqno": report.broker_seqno,
            "broker_trade_id": report.broker_trade_id,
            "client_order_id": owner.get("client_order_id"),
            "strategy_name": owner.get("strategy_name"),
            "symbol": report.symbol,
            "action": report.action.value,
            "price": report.price,
            "volume": report.volume,
            "filled_at": report.ts,
            "raw_json": "{}",
        }
    )
    dao.conn.commit()


def categories(dao: LiveTradeDAO) -> List[str]:
    return [str(row["category"]) for row in dao.get_risk_events_by_date(TODAY)]


def open_lots(dao: LiveTradeDAO) -> List[Tuple[str, str, int]]:
    return [
        (str(row["strategy_name"]), str(row["symbol"]), int(row["volume"]))
        for row in dao.get_open_lots()
    ]


def test_restart_books_the_fill_to_the_original_strategy(dao: LiveTradeDAO) -> None:
    """
    送單 → 行程死 → 另一行程收到成交 → 重啟：接管 → 回填 → 重建，部位歸原策略

    回填一定在重建之前：重建先跑的話，券商多出的那口會被收進 `__unattributed__`。
    """

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.fill_ratio = 0.0
    ticket: OrderTicket = make_oms(broker, dao, "run1", 1).submit(
        make_order(), STRATEGY
    )
    # 行程死掉；成交在券商端發生，另一個行程寫進 live_fill
    broker.tickets[ticket.client_order_id].status = LiveOrderStatus.FILLED
    broker.tickets[ticket.client_order_id].filled_volume = 2
    store_as_the_other_process_would(dao, fill_for(ticket, 2))
    broker.positions = [
        BrokerPositionSnapshot(
            symbol="2330", direction=PositionType.LONG, volume=2, avg_price=1000.0
        )
    ]

    # 重啟：prepare() 的順序
    oms: OrderManager = make_oms(broker, dao, "run2", 2)
    ledger: PositionAttributionLedger = PositionAttributionLedger(
        dao, now_provider=lambda: NOW, run_id="run2"
    )
    manager: StockPositionManager = StockPositionManager(
        StockAccount(init_capital=10_000_000.0)
    )
    sync: AccountSynchronizer = AccountSynchronizer(
        {STRATEGY: manager},
        ledger,
        dao,
        now_provider=lambda: NOW,
        order_builders={STRATEGY: build_stock_order},
    )
    recovered: List[OrderTicket] = oms.recover(TODAY)
    FillBackfiller(oms, sync, dao, "run2", lambda: NOW).backfill(TODAY)
    sync.rebuild_from_broker(broker.positions)

    assert recovered[0].status is LiveOrderStatus.FILLED
    assert recovered[0].filled_volume == 2
    assert open_lots(dao) == [(STRATEGY, "2330", 2)]
    assert manager.account.get_position_count() == 1
    assert "FILL_BACKFILLED" in categories(dao)
    assert "POSITION_UNATTRIBUTED" not in categories(dao)


def test_trader_startup_backfills_before_rebuilding_positions() -> None:
    """`LiveTrader.prepare()` 本身的順序：接管 → 回填 → 重建；重建後策略帳上有那口部位"""

    class Alpha(ScriptedStrategy):
        """不送新單的腳本策略"""

        def __init__(self) -> None:
            super().__init__("Alpha", [])

    harness: Harness = Harness([Alpha()])
    harness.dao.upsert_order(
        {
            "client_order_id": "run0-0001",
            "run_id": "run0",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 100.0,
            "volume": 1,
            "status": "SUBMITTED",
            "broker_seqno": "000001",
            "custom_field": "000001",
            "created_at": harness.clock[0],
        }
    )
    harness.dao.insert_fill(
        {
            "broker_seqno": "000001",
            "broker_trade_id": "T1",
            "client_order_id": "run0-0001",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "price": 100.0,
            "volume": 1,
            "filled_at": harness.clock[0],
            "raw_json": "{}",
        }
    )
    harness.dao.conn.commit()
    harness.broker.tickets["run0-0001"] = OrderTicket(
        client_order_id="run0-0001",
        order=make_order(1),
        status=LiveOrderStatus.FILLED,
        broker_seqno="000001",
        broker_order_id="A000001",
        filled_volume=1,
        custom_field="000001",
    )
    harness.broker.positions = [
        BrokerPositionSnapshot(
            symbol="2330", direction=PositionType.LONG, volume=1, avg_price=100.0
        )
    ]
    start_run(harness)

    harness.trader.prepare()

    assert harness.ledger.get_strategy_positions("Alpha") == {("2330", "LONG"): 1}
    assert harness.ledger.get_strategy_positions(UNATTRIBUTED_STRATEGY) == {}
    assert harness.contexts[0].position_manager.account.get_position_count() == 1
    assert harness.trader.last_reconcile is not None
    seen: List[str] = [
        str(row["category"])
        for row in harness.dao.get_risk_events_by_date(harness.clock[0].date())
    ]
    assert "FILL_BACKFILLED" in seen
    assert "POSITION_UNATTRIBUTED" not in seen
