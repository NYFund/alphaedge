import datetime
from typing import Any, Callable, List, Optional

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.after_close import AfterCloseRunner
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.factory import make_position_scope
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler, ReconcileResult
from core.models import (
    BrokerPositionSnapshot,
    ExecutionReport,
    FuturesPositionSnapshot,
    StockPositionSnapshot,
)
from core.utils import Action, InstrumentType, PositionType

from .conftest import FakeBroker

"""
盤後對帳只比本線（股票或期貨）的部位

券商只有一本合併帳，股票線與期貨線是兩個行程、盤後段相隔 5 分鐘。期貨線送單逾時但
已成交的出場單要等期貨盤後段接管才對得回來；股票盤後段先跑、比到全帳戶就因為
「本地多一口 TX」把整個帳戶降為 `REDUCE_ONLY`（2026-10-21、2026-10-29）。
"""

NOW: datetime.datetime = datetime.datetime(2026, 10, 29, 14, 30)


def _fill(symbol: str, volume: int, price: float) -> ExecutionReport:
    return ExecutionReport(
        broker_seqno="000001",
        broker_trade_id=f"T-{symbol}",
        symbol=symbol,
        action=Action.BUY,
        price=price,
        volume=volume,
        ts=NOW,
    )


@pytest.fixture
def ledger(dao: LiveTradeDAO) -> PositionAttributionLedger:
    return PositionAttributionLedger(dao, lambda: NOW, run_id="after")


@pytest.fixture
def degradations() -> List[str]:
    return []


@pytest.fixture
def reconciler(
    ledger: PositionAttributionLedger, dao: LiveTradeDAO, degradations: List[str]
) -> Reconciler:
    return Reconciler(
        ledger,
        {},
        dao,
        run_id="after",
        on_degrade=degradations.append,
        now_provider=lambda: NOW,
    )


def _stock_scope() -> Callable[[str], bool]:
    scope: Optional[Callable[[str], bool]] = make_position_scope(
        [_Strategy(InstrumentType.STOCK)]
    )
    assert scope is not None
    return scope


class _Strategy:
    """只帶商品類別的策略替身"""

    def __init__(self, instrument_type: Optional[InstrumentType]) -> None:
        self.instrument_type: Optional[InstrumentType] = instrument_type


# === 範圍判定 ===
def test_position_scope_follows_the_loaded_instrument() -> None:
    stock_scope: Optional[Callable[[str], bool]] = make_position_scope(
        [_Strategy(InstrumentType.STOCK)]
    )
    futures_scope: Optional[Callable[[str], bool]] = make_position_scope(
        [_Strategy(InstrumentType.FUTURE)]
    )
    assert stock_scope is not None and futures_scope is not None

    for symbol in ("2330", "0050", "00631L"):
        assert stock_scope(symbol) is True
        assert futures_scope(symbol) is False
    for symbol in ("TX202611", "MTX202612"):
        assert stock_scope(symbol) is False
        assert futures_scope(symbol) is True


def test_mixed_instruments_reconcile_the_whole_account() -> None:
    assert (
        make_position_scope(
            [_Strategy(InstrumentType.STOCK), _Strategy(InstrumentType.FUTURE)]
        )
        is None
    )


# === 對帳器 ===
def test_other_line_difference_is_left_to_its_own_after_close(
    reconciler: Reconciler,
    ledger: PositionAttributionLedger,
    degradations: List[str],
    dao: LiveTradeDAO,
) -> None:
    """
    股票線盤後：本地還記著期貨那 1 口（期貨線尚未回填），不算不一致、不降級

    那一口由期貨盤後段接管回填後自己對帳；股票線在這裡降級，次日全帳戶都不開新倉。
    """

    ledger.open_lot("Alpha", _fill("2330", 2, 1000.0), PositionType.LONG)
    ledger.open_lot("Futures", _fill("TX202611", 1, 46_000.0), PositionType.LONG)
    positions: List[BrokerPositionSnapshot] = [
        StockPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=2),
        # 券商端 TX202611 已平掉（出場單逾時但成交）
    ]

    result: ReconcileResult = reconciler.check(positions, scope=_stock_scope())

    assert result.is_consistent is True
    assert degradations == []
    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'RECONCILE_MISMATCH'"
        ).fetchone()[0]
        == 0
    )
    # 快照仍寫全帳戶：那是事實紀錄，不隨判定範圍縮小
    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_position_snapshot WHERE symbol = 'TX202611'"
        ).fetchone()[0]
        > 0
    )


def test_own_line_difference_still_degrades(
    reconciler: Reconciler,
    ledger: PositionAttributionLedger,
    degradations: List[str],
) -> None:
    """範圍只是縮小，不是放水：本線的差異照舊走帳戶層降級"""

    ledger.open_lot("Alpha", _fill("2330", 2, 1000.0), PositionType.LONG)
    positions: List[BrokerPositionSnapshot] = [
        StockPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=1),
        FuturesPositionSnapshot(
            symbol="TX202611", direction=PositionType.LONG, volume=1
        ),
    ]

    result: ReconcileResult = reconciler.check(positions, scope=_stock_scope())

    assert result.is_consistent is False
    assert list(result.broker_differences) == [("2330", "LONG")]
    assert len(degradations) == 1


def test_without_scope_the_whole_account_is_compared(
    reconciler: Reconciler, ledger: PositionAttributionLedger, degradations: List[str]
) -> None:
    """段落啟動與重連後的對帳不帶範圍，另一線的差異照樣抓"""

    ledger.open_lot("Futures", _fill("TX202611", 1, 46_000.0), PositionType.LONG)

    result: ReconcileResult = reconciler.check([])

    assert result.is_consistent is False
    assert len(degradations) == 1


# === 盤後作業 ===
class _RecordingReconciler:
    """記下 `check()` 收到的範圍"""

    def __init__(self) -> None:
        self.scopes: List[Any] = []

    def check(self, positions: List[Any], scope: Any = None) -> ReconcileResult:
        self.scopes.append(scope)
        return ReconcileResult()


class _Stub:
    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: {}


def test_after_close_reconciles_only_its_own_line(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger, fake_broker: FakeBroker
) -> None:
    """盤後作業把本線範圍交給對帳器；段落啟動與重連後的對帳不經這裡"""

    fake_broker.connect()
    recording: _RecordingReconciler = _RecordingReconciler()
    scope: Callable[[str], bool] = _stock_scope()
    runner: AfterCloseRunner = AfterCloseRunner(
        data_feeds=[],
        broker=fake_broker,
        order_manager=OrderManager(
            fake_broker, dao, "after", run_index=9, now_provider=lambda: NOW
        ),
        account_sync=AccountSynchronizer(
            position_managers={}, ledger=ledger, dao=dao, now_provider=lambda: NOW
        ),
        reconciler=recording,  # type: ignore[arg-type]
        reporter=_Stub(),
        mode_state=_Stub(),
        dao=dao,
        run_id="after",
        now_provider=lambda: NOW,
        position_scope=scope,
    )

    runner.run()

    assert recording.scopes == [scope]
