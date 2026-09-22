import datetime
import sqlite3
from typing import Any, Dict, List, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.attribution.resync import (
    RESYNC_ADOPT,
    RESYNC_CLOSE,
    RESYNC_EVENT_CATEGORY,
    RESYNC_REDUCE,
    ResyncPlan,
    ResyncRefusedError,
    apply_resync,
    plan_resync,
)
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import build_live_trader
from core.live.risk.trading_mode import TradingMode
from core.live.trader import LiveTrader
from core.models import BrokerPositionSnapshot
from core.utils import PositionType

from .conftest import FakeBroker
from .test_live_factory_and_entry import LiveStockStrategy

"""
以券商部位重建歸屬帳

對帳不一致之後唯一的恢復路徑。規則逐標的、逐方向比對：券商多的收進未歸屬、
券商少的 FIFO 扣減唯一持有者、方向相反是兩者的組合、兩個持有者整份拒絕。

最要緊的是**只列計畫時一筆都不能動**：人是看著計畫決定要不要寫入的，
列計畫的那一次若已經改了東西，確認這一步就沒有意義了。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 15, 0)


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """實盤資料源的歷史資料庫換成空的 in-memory 連線；本檔不需要任何歷史資料"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


@pytest.fixture
def ledger(dao: LiveTradeDAO) -> PositionAttributionLedger:
    return PositionAttributionLedger(dao, now_provider=lambda: NOW)


def add_lot(
    dao: LiveTradeDAO,
    lot_id: str,
    strategy_name: str,
    volume: int,
    symbol: str = "2330",
    direction: str = "LONG",
    open_date: datetime.date = datetime.date(2026, 9, 1),
) -> None:
    dao.open_lot(
        {
            "lot_id": lot_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "direction": direction,
            "volume": volume,
            "open_date": open_date,
            "open_price": 1000.0,
            "client_order_id": None,
        }
    )
    dao.conn.commit()


def broker_position(
    volume: int,
    symbol: str = "2330",
    direction: PositionType = PositionType.LONG,
    avg_price: float = 1010.0,
) -> BrokerPositionSnapshot:
    return BrokerPositionSnapshot(
        symbol=symbol, direction=direction, volume=volume, avg_price=avg_price
    )


def lot_state(dao: LiveTradeDAO) -> List[Tuple[Any, ...]]:
    """lot 表的完整內容（含已平倉），用來確認有沒有被動過"""

    return dao.conn.execute(
        "SELECT * FROM live_position_lot ORDER BY lot_id"
    ).fetchall()


def resync_events(dao: LiveTradeDAO) -> List[Tuple[Any, ...]]:
    return dao.conn.execute(
        "SELECT strategy_name, severity, symbol FROM live_risk_event "
        "WHERE category = ? ORDER BY event_id",
        (RESYNC_EVENT_CATEGORY,),
    ).fetchall()


# === 判定規則（純函式） ===
def test_broker_more_than_local_adopts_the_gap_into_unattributed(
    dao: LiveTradeDAO,
) -> None:
    """券商 > 本地：差額收進未歸屬，開倉價用券商均價"""

    add_lot(dao, "L1", "Alpha", 2)

    plan: ResyncPlan = plan_resync([broker_position(5)], dao.get_open_lots())

    assert [
        (a.kind, a.strategy_name, a.volume, a.open_price) for a in plan.actions
    ] == [(RESYNC_ADOPT, UNATTRIBUTED_STRATEGY, 3, 1010.0)]


def test_broker_less_than_local_reduces_the_holder_fifo(dao: LiveTradeDAO) -> None:
    """
    券商 < 本地：從最早的 lot 開始扣，扣到 0 的標平倉

    順序要與平倉沖銷一致（FIFO）：扣錯一筆，留下來的開倉價就不同，
    之後的已實現損益會對不上。lot 刻意以「晚開的先寫入」的順序建立，
    確認順序來自開倉日而不是寫入順序。
    """

    add_lot(dao, "L2", "Alpha", 3, open_date=datetime.date(2026, 9, 2))
    add_lot(dao, "L1", "Alpha", 2, open_date=datetime.date(2026, 9, 1))

    lots: List[Dict[str, object]] = list(reversed(dao.get_open_lots()))
    plan: ResyncPlan = plan_resync([broker_position(1)], lots)

    assert [(a.kind, a.lot_id, a.volume) for a in plan.actions] == [
        (RESYNC_CLOSE, "L1", 2),
        (RESYNC_REDUCE, "L2", 2),
    ]


def test_opposite_direction_closes_local_and_adopts_broker(dao: LiveTradeDAO) -> None:
    """方向相反：本地多單全部平倉，券商的空單整筆收進未歸屬"""

    add_lot(dao, "L1", "Alpha", 2)

    plan: ResyncPlan = plan_resync(
        [broker_position(4, direction=PositionType.SHORT)], dao.get_open_lots()
    )

    assert sorted(
        (a.kind, a.strategy_name, a.direction, a.volume) for a in plan.actions
    ) == [
        (RESYNC_ADOPT, UNATTRIBUTED_STRATEGY, "SHORT", 4),
        (RESYNC_CLOSE, "Alpha", "LONG", 2),
    ]


def test_two_holders_of_one_symbol_refuse_the_whole_plan(dao: LiveTradeDAO) -> None:
    """
    同一標的兩個持有者：整份拒絕，**連別的標的也不處理**

    兩個持有者代表紀錄已經壞了，扣誰都是猜；只處理其他標的的話，
    人會以為重建完成了，而壞掉的那一檔還在。
    """

    add_lot(dao, "L1", "Alpha", 2)
    add_lot(dao, "L2", UNATTRIBUTED_STRATEGY, 1)
    add_lot(dao, "L3", "Alpha", 5, symbol="2317")

    plan: ResyncPlan = plan_resync(
        [broker_position(1), broker_position(1, symbol="2317")], dao.get_open_lots()
    )

    assert plan.is_refused
    assert plan.conflicts == {"2330": ["Alpha", UNATTRIBUTED_STRATEGY]}
    assert plan.actions == []


def test_consistent_positions_produce_an_empty_plan(dao: LiveTradeDAO) -> None:
    add_lot(dao, "L1", "Alpha", 2)

    plan: ResyncPlan = plan_resync([broker_position(2)], dao.get_open_lots())

    assert not plan.is_refused
    assert plan.actions == []


# === 寫入 ===
def test_apply_writes_every_change_with_a_risk_event(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger
) -> None:
    """扣減記 CRITICAL（損益要人補登）、收進記 WARNING；寫完與券商一致"""

    add_lot(dao, "L1", "Alpha", 2)
    add_lot(dao, "L2", "Beta", 1, symbol="2317")
    positions: List[BrokerPositionSnapshot] = [
        broker_position(0),
        broker_position(3, symbol="2317"),
    ]

    apply_resync(ledger, plan_resync(positions, dao.get_open_lots()), "R1", lambda: NOW)

    assert ledger.diff_against_broker(positions) == {}
    assert resync_events(dao) == [
        (UNATTRIBUTED_STRATEGY, "WARNING", "2317"),
        ("Alpha", "CRITICAL", "2330"),
    ]


def test_apply_rolls_back_everything_on_failure(
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    寫到一半失敗整批回滾

    否則歸屬帳會停在「扣了一半、還沒收進」的狀態：既不是本地原本的樣子，
    也不是券商的樣子，之後連要怎麼重建都判斷不出來。
    """

    add_lot(dao, "L1", "Alpha", 2)
    add_lot(dao, "L2", "Beta", 1, symbol="2317")
    before: List[Tuple[Any, ...]] = lot_state(dao)

    def explode(lot_id: str, closed_at: datetime.datetime) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dao, "close_lot", explode)
    plan: ResyncPlan = plan_resync(
        [broker_position(0), broker_position(3, symbol="2317")], dao.get_open_lots()
    )

    with pytest.raises(sqlite3.OperationalError):
        apply_resync(ledger, plan, "R1", lambda: NOW)

    # 收進未歸屬排在扣減前面，確認已寫的那一筆也被撤掉了
    assert plan.actions[0].kind == RESYNC_ADOPT
    assert lot_state(dao) == before
    assert resync_events(dao) == []


# === 引擎：只列計畫、確認寫入、拒絕 ===
def build(dao: LiveTradeDAO, broker: FakeBroker) -> LiveTrader:
    return build_live_trader(
        [LiveStockStrategy()],
        broker=broker,
        dao=dao,
        run_id="20260921150000",
        now_provider=lambda: NOW,
        phase="resync",
    )


def test_plan_only_leaves_the_records_untouched(dao: LiveTradeDAO) -> None:
    """只列計畫：lot 表與事件表前後完全相同"""

    add_lot(dao, "L1", "LiveStockStrategy", 2)
    broker: FakeBroker = FakeBroker()
    broker.positions = [broker_position(0), broker_position(1, symbol="2317")]
    before: List[Tuple[Any, ...]] = lot_state(dao)

    plan: ResyncPlan = build(dao, broker).resync_from_broker(confirm=False)

    assert len(plan.actions) == 2
    assert lot_state(dao) == before
    assert resync_events(dao) == []


def test_confirmed_resync_reconciles_with_the_broker(dao: LiveTradeDAO) -> None:
    """確認寫入後重建帳戶並對帳，結果要一致"""

    add_lot(dao, "L1", "LiveStockStrategy", 5)
    broker: FakeBroker = FakeBroker()
    broker.positions = [broker_position(2)]

    trader: LiveTrader = build(dao, broker)
    trader.resync_from_broker(confirm=True)

    assert trader.last_reconcile is not None
    assert trader.last_reconcile.is_consistent
    assert trader.ledger.get_strategy_positions("LiveStockStrategy") == {
        ("2330", "LONG"): 2
    }


def test_conflict_is_refused_and_records_stay_untouched(dao: LiveTradeDAO) -> None:
    add_lot(dao, "L1", "LiveStockStrategy", 2)
    add_lot(dao, "L2", UNATTRIBUTED_STRATEGY, 1)
    broker: FakeBroker = FakeBroker()
    broker.positions = [broker_position(1)]
    before: List[Tuple[Any, ...]] = lot_state(dao)

    with pytest.raises(ResyncRefusedError):
        build(dao, broker).resync_from_broker(confirm=True)

    assert lot_state(dao) == before
    assert resync_events(dao) == []


def test_unfinished_orders_refuse_the_resync(
    dao: LiveTradeDAO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """還有未終結的委託時券商部位仍在變，算出來的差額下一秒就不對了"""

    add_lot(dao, "L1", "LiveStockStrategy", 2)
    broker: FakeBroker = FakeBroker()
    broker.positions = [broker_position(0)]
    before: List[Tuple[Any, ...]] = lot_state(dao)
    trader: LiveTrader = build(dao, broker)
    monkeypatch.setattr(
        dao, "get_unfinished_orders", lambda run_date: [{"client_order_id": "C1"}]
    )

    with pytest.raises(ResyncRefusedError, match="未終結"):
        trader.resync_from_broker(confirm=True)

    assert lot_state(dao) == before


def test_resync_keeps_the_previous_degraded_mode(dao: LiveTradeDAO) -> None:
    """
    重建不解除降級

    本次結束時會把帳戶層模式寫進 `live_run`；沒先讀回上次的模式的話，
    寫進去的是預設的 NORMAL——一次只列計畫的重建就把降級擦掉了。
    """

    dao.insert_run(
        {
            "run_id": "20260920133000",
            "started_at": datetime.datetime(2026, 9, 20, 13, 30),
            "phase": "close",
            "simulation": 1,
            "dry_run": 0,
        }
    )
    dao.finish_run(
        "20260920133000",
        datetime.datetime(2026, 9, 20, 13, 40),
        "對帳不一致",
        TradingMode.REDUCE_ONLY.value,
    )
    dao.conn.commit()
    broker: FakeBroker = FakeBroker()

    build(dao, broker).resync_from_broker(confirm=False)

    assert dao.get_last_account_mode() == TradingMode.REDUCE_ONLY.value
