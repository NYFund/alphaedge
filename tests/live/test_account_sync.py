import datetime
import sqlite3
from typing import Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.factory import _build_futures_order
from core.live.reconciler import Reconciler, ReconcileResult
from core.managers.futures.position_manager import FuturesPositionManager
from core.managers.stock.position_manager import StockPositionManager
from core.models import (
    BrokerPositionSnapshot,
    ExecutionReport,
    FuturesAccount,
    FuturesOrder,
    StockAccount,
    StockOrder,
    StockPositionSnapshot,
)
from core.utils import Action, PositionType, StockOrderCond

"""
帳戶同步與對帳

兩條路徑刻意分成兩個函式，**不共用一個帶旗標的「同步」函式**：
- `rebuild_from_broker()`：啟動時以券商為準（本地還沒有任何狀態）。
- `apply_fill()`：運行中以回報為準（本地狀態由回報推導，差異本身就是訊號）。

共用旗標的話，那個旗標遲早會在錯的時機被打開，而「運行中以券商覆寫本地」
會把回報漏接這類 bug 蓋掉——蓋掉之後明天還會再發生一次。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


@pytest.fixture
def ledger(dao: LiveTradeDAO) -> PositionAttributionLedger:
    return PositionAttributionLedger(dao, now_provider=lambda: NOW)


def make_manager(capital: float = 10_000_000.0) -> StockPositionManager:
    account: StockAccount = StockAccount(init_capital=capital)
    return StockPositionManager(account)


@pytest.fixture
def managers() -> Dict[str, StockPositionManager]:
    return {"A": make_manager(), "B": make_manager()}


@pytest.fixture
def sync(
    managers: Dict[str, StockPositionManager],
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> AccountSynchronizer:
    return AccountSynchronizer(managers, ledger, dao, now_provider=lambda: NOW)


def make_fill(
    symbol: str = "2330",
    action: Action = Action.BUY,
    volume: int = 2,
    price: float = 1000.0,
    seqno: str = "000001",
) -> ExecutionReport:
    return ExecutionReport(
        broker_seqno=seqno,
        broker_trade_id=f"T{seqno}",
        symbol=symbol,
        action=action,
        price=price,
        volume=volume,
        ts=NOW,
    )


def register_order(dao: LiveTradeDAO, seqno: str, strategy_name: str) -> None:
    """在 `live_order` 建一張委託，讓成交反查得到策略"""

    dao.upsert_order(
        {
            "client_order_id": f"run1-{seqno}",
            "run_id": "run1",
            "strategy_name": strategy_name,
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 2,
            "status": "SUBMITTED",
            "broker_seqno": seqno,
            "custom_field": f"01{seqno[-4:]}",
            "created_at": NOW,
        }
    )
    dao.conn.commit()


# === 運行中 ===
def test_fill_updates_the_owning_strategy_only(
    sync: AccountSynchronizer,
    managers: Dict[str, StockPositionManager],
    dao: LiveTradeDAO,
    ledger: PositionAttributionLedger,
) -> None:
    """成交只動歸屬策略的帳戶與 lot"""

    register_order(dao, "000001", "A")
    assert sync.apply_fill(make_fill()) == "A"

    assert managers["A"].account.get_position_count() == 1
    assert managers["B"].account.get_position_count() == 0
    assert ledger.get_strategy_positions("A") == {("2330", "LONG"): 2}


def test_unattributable_fill_does_not_guess(
    sync: AccountSynchronizer, managers: Dict[str, StockPositionManager]
) -> None:
    """
    查不到策略時**不猜**

    猜一支策略會讓它的已實現損益直接錯掉；那筆成交仍在 `live_fill` 裡，
    差異由對帳抓出。
    """

    assert sync.apply_fill(make_fill(seqno="999999")) is None
    assert all(m.account.get_position_count() == 0 for m in managers.values())


def test_buy_that_covers_a_short_is_not_a_new_long(
    sync: AccountSynchronizer,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> None:
    """
    回補空單與買進開多都是 BUY

    只看買賣別會把回補記成新開一筆多單，於是帳上憑空多出一個部位、
    而空單永遠平不掉。
    """

    register_order(dao, "000001", "A")
    ledger.open_lot("A", make_fill(action=Action.SELL), PositionType.SHORT)

    sync.apply_fill(make_fill(action=Action.BUY))

    assert ledger.get_strategy_positions("A") == {}


def test_sell_that_closes_a_long_reduces_the_lot(
    sync: AccountSynchronizer,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> None:
    """賣出平多倉要沖銷 lot，不是開一筆空單"""

    register_order(dao, "000001", "A")
    sync.apply_fill(make_fill(action=Action.BUY))
    sync.apply_fill(make_fill(action=Action.SELL))

    assert ledger.get_strategy_positions("A") == {}


# === 啟動時 ===
def test_rebuild_uses_the_ledger_not_an_even_split(
    sync: AccountSynchronizer,
    ledger: PositionAttributionLedger,
    managers: Dict[str, StockPositionManager],
) -> None:
    """
    **多策略的重建走 lot 表，不是把券商部位平均分配**

    券商給的是合併部位，分不回策略；猜一個分法會讓兩支策略的已實現損益
    都是錯的，而合計仍然正確。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    ledger.open_lot("B", make_fill(volume=3), PositionType.LONG)

    sync.rebuild_from_broker(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=5)]
    )

    assert managers["A"].account.positions[0].volume == 2
    assert managers["B"].account.positions[0].volume == 3


def test_rebuild_adopts_the_gap_as_unattributed(
    sync: AccountSynchronizer, ledger: PositionAttributionLedger
) -> None:
    """券商多出來的部分收進 `__unattributed__`（只允許平倉）"""

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)

    rebuilt: Dict[str, int] = sync.rebuild_from_broker(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=5)]
    )

    assert rebuilt[UNATTRIBUTED_STRATEGY] == 1
    assert ledger.get_strategy_positions(UNATTRIBUTED_STRATEGY) == {("2330", "LONG"): 3}


def test_rebuild_preserves_the_original_open_date(
    sync: AccountSynchronizer, ledger: PositionAttributionLedger, dao: LiveTradeDAO
) -> None:
    """
    原始開倉日取自 lot 表

    推不出來而落到啟動日的話，持有天數與當沖判定都會算錯。
    """

    old: ExecutionReport = make_fill()
    old.ts = datetime.datetime(2026, 9, 15, 10, 0)
    ledger.open_lot("A", old, PositionType.LONG)

    sync.rebuild_from_broker([])

    assert dao.get_open_lots("A")[0]["open_date"] == "2026-09-15"


def test_rebuild_and_apply_fill_are_separate_functions() -> None:
    """
    兩條路徑是**不同的函式**，不是一個帶旗標的函式

    共用旗標的話，那個旗標遲早會在錯的時機被打開——而「運行中以券商覆寫本地」
    會把回報漏接這類 bug 蓋掉。
    """

    import inspect

    assert "flag" not in inspect.signature(AccountSynchronizer.apply_fill).parameters
    assert (
        "flag"
        not in inspect.signature(AccountSynchronizer.rebuild_from_broker).parameters
    )


# === 對帳 ===
@pytest.fixture
def degradations() -> List[str]:
    return []


@pytest.fixture
def reconciler(
    ledger: PositionAttributionLedger,
    managers: Dict[str, StockPositionManager],
    dao: LiveTradeDAO,
    degradations: List[str],
) -> Reconciler:
    return Reconciler(
        ledger,
        managers,
        dao,
        run_id="run1",
        on_degrade=degradations.append,
        now_provider=lambda: NOW,
    )


def test_consistent_reconcile_writes_snapshots_anyway(
    reconciler: Reconciler,
    sync: AccountSynchronizer,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
) -> None:
    """
    一致時也要寫快照

    出事後回頭查「昨天是不是就已經差了」，靠的就是這些快照；
    只在不一致時寫的話，那條時間軸會是斷的。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    sync.rebuild_from_broker([])

    result: ReconcileResult = reconciler.check(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=2)]
    )

    assert result.is_consistent is True
    assert (
        dao.conn.execute("SELECT COUNT(*) FROM live_position_snapshot").fetchone()[0]
        > 0
    )


def test_mismatch_degrades_at_account_level(
    reconciler: Reconciler,
    ledger: PositionAttributionLedger,
    dao: LiveTradeDAO,
    degradations: List[str],
) -> None:
    """
    差異**無法歸因到單支策略**，故走帳戶層降級

    猜是哪一支的代價是讓真正有問題的那支繼續交易。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)

    result: ReconcileResult = reconciler.check(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=5)]
    )

    assert result.is_consistent is False
    assert result.broker_differences == {("2330", "LONG"): (2, 5)}
    assert len(degradations) == 1
    assert (
        dao.conn.execute(
            "SELECT severity FROM live_risk_event WHERE category = 'RECONCILE_MISMATCH'"
        ).fetchone()[0]
        == "CRITICAL"
    )


def test_internal_inconsistency_is_caught_separately(
    reconciler: Reconciler, ledger: PositionAttributionLedger
) -> None:
    """
    本地兩份紀錄對不上也要擋

    那代表回報處理有 bug，與券商無關；兩份都不可信的時候，
    再送單只會讓事情更複雜。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)  # 只進 lot 表

    result: ReconcileResult = reconciler.check(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=2)]
    )

    assert result.broker_differences == {}
    assert result.internal_differences == {("2330", "LONG"): (0, 2)}
    assert result.is_consistent is False


def test_multiple_order_conds_on_one_symbol_are_flagged(
    reconciler: Reconciler,
) -> None:
    """
    同一檔出現多種融資券別要示警

    只比「代號 ＋ 方向 ＋ 數量」的話，現股多單與融券空單互換時數字會剛好對得上。
    """

    result: ReconcileResult = reconciler.check(
        [
            StockPositionSnapshot(
                symbol="2330",
                direction=PositionType.LONG,
                volume=1,
                order_cond=StockOrderCond.Cash,
            ),
            StockPositionSnapshot(
                symbol="2330",
                direction=PositionType.SHORT,
                volume=1,
                order_cond=StockOrderCond.ShortSelling,
            ),
        ]
    )

    assert "2330" in result.order_cond_differences


def test_reconciler_does_not_change_local_positions(
    reconciler: Reconciler,
    ledger: PositionAttributionLedger,
) -> None:
    """
    **不自動修正本地部位**

    自動修正會把真正的 bug（例如回報漏接）蓋掉，而被蓋掉的 bug 明天還會再發生一次。
    """

    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)
    reconciler.check(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=5)]
    )

    assert ledger.get_account_positions() == {("2330", "LONG"): 2}


def test_rebuild_is_not_blocked_by_the_balance_check(
    ledger: PositionAttributionLedger, dao: LiveTradeDAO
) -> None:
    """
    **重建期間要停用餘額檢查**

    `open_position()` 會檢查「餘額夠不夠買」——那在交易時是對的，在重建時是錯的：
    帳戶的餘額**已經**被這些部位佔住了，再檢查一次必然不足，部位會被靜默丟掉
    （只留一行 warning），然後對帳每天都報「本地兩份紀錄不一致」。
    """

    poor: StockPositionManager = make_manager(capital=1.0)  # 連一張都買不起
    sync: AccountSynchronizer = AccountSynchronizer(
        {"A": poor}, ledger, dao, now_provider=lambda: NOW
    )
    ledger.open_lot("A", make_fill(volume=2, price=1000.0), PositionType.LONG)

    sync.rebuild_from_broker([])

    assert poor.account.get_position_count() == 1


def test_rebuild_sets_the_balance_from_the_broker(
    ledger: PositionAttributionLedger, dao: LiveTradeDAO
) -> None:
    """重建後的餘額由券商帳務決定，不是 `init_capital` 減去重建消耗"""

    manager: StockPositionManager = make_manager()
    sync: AccountSynchronizer = AccountSynchronizer(
        {"A": manager}, ledger, dao, now_provider=lambda: NOW
    )
    ledger.open_lot("A", make_fill(volume=2), PositionType.LONG)

    sync.rebuild_from_broker([], balances={"A": 123_456.0})

    assert manager.account.balance == 123_456.0


# === 期貨：訂單型別要跟著策略的商品走 ===
def make_futures_sync(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger
) -> AccountSynchronizer:
    """建一個餵期貨部位管理器的同步器；建構器由組裝層注入的那一份"""

    account: FuturesAccount = FuturesAccount(init_capital=10_000_000.0)
    return AccountSynchronizer(
        {"F": FuturesPositionManager(account)},
        ledger,
        dao,
        now_provider=lambda: NOW,
        order_builders={"F": _build_futures_order},
    )


def test_futures_fill_opens_a_position_instead_of_crashing(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger
) -> None:
    """
    期貨策略的第一筆成交不可以炸

    `FuturesPositionManager.open_position()` 會讀 `order.product`，而 `StockOrder`
    沒有這個屬性——注入建構器之前，這條路徑是 `AttributeError`，
    而訊息完全看不出問題出在帳戶同步器。
    """

    sync: AccountSynchronizer = make_futures_sync(dao, ledger)

    name: str = sync.apply_fill(
        make_fill(symbol="TX202601", volume=1, price=23000.0), strategy_name="F"
    )

    assert name == "F"
    manager: FuturesPositionManager = sync.position_managers["F"]
    assert manager.account.get_position_count() == 1


def test_futures_order_carries_product_and_expiry(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger
) -> None:
    """契約代號要拆回 product／expiry，只塞 symbol 會讓乘數與保證金查錯商品"""

    sync: AccountSynchronizer = make_futures_sync(dao, ledger)

    order: FuturesOrder = sync._to_order(
        "F", make_fill(symbol="TX202601", volume=1, price=23000.0), PositionType.LONG
    )

    assert isinstance(order, FuturesOrder)
    assert (order.product, order.expiry) == ("TX", "202601")
    assert order.contract_id == "TX202601"


def test_futures_rebuild_restores_a_futures_order(
    dao: LiveTradeDAO, ledger: PositionAttributionLedger
) -> None:
    """重啟接管走的是 lot 表，同樣不可以還原成股票訂單"""

    lot: Dict[str, object] = {
        "symbol": "TX202601",
        "direction": PositionType.LONG.value,
        "open_date": datetime.date(2026, 9, 18),
        "open_price": 23000.0,
        "volume": 1,
    }

    order: FuturesOrder = AccountSynchronizer._lot_to_order(lot, _build_futures_order)

    assert isinstance(order, FuturesOrder)
    assert (order.product, order.expiry) == ("TX", "202601")
    assert order.date == datetime.date(2026, 9, 18)


def test_stock_strategy_still_gets_a_stock_order(
    sync: AccountSynchronizer,
) -> None:
    """股票線不受影響：未注入建構器時退回股票版"""

    order = sync._to_order("A", make_fill(), PositionType.LONG)

    assert isinstance(order, StockOrder)
    assert order.stock_id == "2330"
