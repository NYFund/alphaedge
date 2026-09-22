import datetime
from typing import Dict, List, Optional, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.attribution.conflict_guard import (
    CROSS_STRATEGY_BLOCKED,
    CrossStrategyConflictGuard,
)
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.capital_allocator import CapitalAllocator
from core.models import BaseOrder, BrokerPositionSnapshot, ExecutionReport, StockOrder
from core.portfolio.aggregation import (
    allocate_capital,
    check_quota_against_equity,
    resolve_symbol_conflicts,
)
from core.utils import Action, PositionType

"""
多策略的資金分配與同標的仲裁

兩件事都是**只存在於實盤**的行為，回測沒有對應物。判定寫成純函式放在
`core/portfolio/aggregation.py`，日後補多策略組合回測時才能重用同一份邏輯——
否則組合回測與實盤又是兩套，那正是「策略層不分家」要避免的事。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


def make_order(
    symbol: str = "2330",
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        action=action,
        position_type=position_type,
        volume=1,
        price=1000.0,
    )


# === 純判定：額度 ===
def test_quota_check_uses_total_equity_not_available_balance() -> None:
    """
    **分母是總權益，不是可用餘額**

    拿可用餘額當分母的話，只要隔日還有部位在場上，餘額就已經被部位佔掉，
    檢查必然誤判成額度超標而拒絕啟動——而那是**完全正常的續跑狀態**。
    這個錯在「第一天空手啟動」的測試裡完全看不出來，要到第二天才爆。
    """

    quotas: Dict[str, float] = {"A": 500_000, "B": 400_000}

    # 隔日續跑：可用餘額只剩 20 萬（其餘被部位佔住），但總權益仍是 100 萬
    assert check_quota_against_equity(quotas, 1_000_000, 0.95) is None


def test_quota_check_rejects_and_lists_the_gap() -> None:
    """不通過時要列出各策略額度與缺口，不是只說「超過」"""

    problem: Optional[str] = check_quota_against_equity(
        {"A": 600_000, "B": 600_000}, 1_000_000, 0.95
    )

    assert problem is not None
    assert "缺口" in problem
    assert "A=600,000" in problem


def test_available_takes_the_smaller_of_two_constraints() -> None:
    """
    可用資金 ＝ `min(本策略額度 − 已用, 帳戶餘額 − 其他策略已保留)`

    只看第一項的話，兩支策略會同時看到「帳戶還有 100 萬」而各自下 80 萬；
    只看第二項的話，某支策略可以把整個帳戶的錢都壓進去，`init_capital` 形同虛設。
    """

    quotas: Dict[str, float] = {"A": 500_000, "B": 500_000}

    # A 受額度限制（500k − 100k 已用）
    assert allocate_capital(quotas, {}, {"A": 100_000}, 900_000)["A"] == 400_000
    # B 受帳戶餘額限制（300k − A 已保留 100k）
    assert allocate_capital(quotas, {"A": 100_000}, {}, 300_000)["B"] == 200_000


def test_available_never_goes_negative() -> None:
    """
    額度超用時回 0 而不是負值

    負的「可用資金」傳到下游會被當成一個可以下單的數字。
    """

    assert allocate_capital({"A": 100_000}, {}, {"A": 200_000}, 900_000)["A"] == 0.0


# === Allocator ===
def test_startup_rejects_over_allocation(dao: LiveTradeDAO) -> None:
    """
    額度總和超過帳戶總權益時**拒絕啟動**

    等到盤中被券商退單才發現，那時已經有部位在場上。
    """

    allocator: CapitalAllocator = CapitalAllocator(
        {"A": 600_000, "B": 600_000}, dao, "run1", now_provider=lambda: NOW
    )

    with pytest.raises(ValueError, match="超過帳戶總權益"):
        allocator.verify_quota(1_000_000)


def test_startup_passes_while_positions_are_still_open(dao: LiveTradeDAO) -> None:
    """
    **隔日持倉續跑不該被誤判**

    這正是「分母用可用餘額」會爆掉的那個情境。
    """

    allocator: CapitalAllocator = CapitalAllocator({"A": 900_000}, dao, "run1")
    allocator.refresh(available_balance=100_000, used={"A": 800_000})

    allocator.verify_quota(1_000_000)  # 不拋出即為通過


def test_concurrent_reservations_see_each_other(dao: LiveTradeDAO) -> None:
    """
    第二支策略拿到的可用資金已經扣掉第一支的保留

    沒有這件事的話，兩支會同時看到同一筆錢。
    """

    allocator: CapitalAllocator = CapitalAllocator(
        {"A": 500_000, "B": 500_000}, dao, "run1", now_provider=lambda: NOW
    )
    allocator.refresh(available_balance=600_000, used={})

    assert allocator.reserve("A", 400_000) is True
    assert allocator.available("B") == 200_000
    assert allocator.reserve("B", 300_000) is False


def test_failed_reservation_is_recorded(dao: LiveTradeDAO) -> None:
    """
    保留失敗要留下紀錄

    盤後的 parity 比對要靠它把「資金排擠造成的少送」歸類出來，
    而不是混進「未解釋」。
    """

    allocator: CapitalAllocator = CapitalAllocator(
        {"A": 100_000}, dao, "run1", now_provider=lambda: NOW
    )
    allocator.refresh(available_balance=100_000, used={})
    allocator.reserve("A", 200_000)

    assert (
        dao.conn.execute("SELECT category FROM live_risk_event").fetchone()[0]
        == "CAPITAL_EXHAUSTED"
    )


def test_release_returns_the_budget(dao: LiveTradeDAO) -> None:
    """成交、拒單、撤單、逾時各自釋放後，額度要回到原值"""

    allocator: CapitalAllocator = CapitalAllocator({"A": 500_000}, dao, "run1")
    allocator.refresh(available_balance=500_000, used={})

    allocator.reserve("A", 200_000)
    allocator.release("A", 200_000)

    assert allocator.available("A") == 500_000


def test_over_release_is_clamped(dao: LiveTradeDAO) -> None:
    """
    釋放量大於保留量時夾在 0

    負的保留會讓其他策略看到憑空多出來的錢。
    """

    allocator: CapitalAllocator = CapitalAllocator({"A": 500_000}, dao, "run1")
    allocator.refresh(available_balance=500_000, used={})

    allocator.reserve("A", 100_000)
    allocator.release("A", 999_999)

    assert allocator.reserved["A"] == 0.0


def test_release_all_frees_other_strategies(dao: LiveTradeDAO) -> None:
    """
    **策略降級時一定要 `release_all()`**

    只停掉送單而不回收保留的話，該策略的額度會被佔住到重啟為止——
    而被佔住的是**其他策略**的可用資金，症狀是別的策略莫名其妙送不出單。
    """

    allocator: CapitalAllocator = CapitalAllocator(
        {"A": 500_000, "B": 500_000}, dao, "run1"
    )
    allocator.refresh(available_balance=500_000, used={})
    allocator.reserve("A", 400_000)

    assert allocator.available("B") == 100_000

    allocator.release_all("A")

    assert allocator.available("B") == 500_000


def test_unknown_strategy_raises(dao: LiveTradeDAO) -> None:
    """沒登記額度的策略不可送單：靜默給 0 會讓它整天送不出單而沒人知道為什麼"""

    allocator: CapitalAllocator = CapitalAllocator({"A": 500_000}, dao, "run1")

    with pytest.raises(KeyError):
        allocator.reserve("B", 1000)


# === 純判定：同標的仲裁 ===
def test_conflict_blocks_the_second_opener() -> None:
    """先搶先贏；順序由呼叫端的排序決定，不是策略註冊順序"""

    orders: List[Tuple[str, BaseOrder]] = [
        ("A", make_order("2330")),
        ("B", make_order("2330")),
    ]

    allowed, blocked = resolve_symbol_conflicts(orders, {}, {})

    assert [name for name, _ in allowed] == ["A"]
    assert [name for name, _, _ in blocked] == ["B"]


def test_closing_orders_are_never_blocked() -> None:
    """
    平倉單不受限

    擋平倉會讓部位失去出場能力，那比衝突嚴重得多。
    """

    orders: List[Tuple[str, BaseOrder]] = [("B", make_order("2330", Action.SELL))]

    allowed, blocked = resolve_symbol_conflicts(orders, {"2330": "A"}, {0: True})

    assert len(allowed) == 1
    assert blocked == []


def test_same_strategy_can_add_to_its_own_position() -> None:
    """
    同一支策略對自己持有的標的加碼不受限

    回測支援分批建倉，擋掉會讓實盤和回測不一致。
    """

    orders: List[Tuple[str, BaseOrder]] = [("A", make_order("2330"))]

    allowed, blocked = resolve_symbol_conflicts(orders, {"2330": "A"}, {})

    assert len(allowed) == 1
    assert blocked == []


# === 守門 ===
def test_guard_blocks_against_the_ledger(dao: LiveTradeDAO) -> None:
    """守門要查歸屬帳，不是只看本批"""

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        dao, now_provider=lambda: NOW
    )
    ledger.open_lot(
        "A",
        ExecutionReport(symbol="2330", volume=2, price=1000.0, action=Action.BUY),
        PositionType.LONG,
    )
    guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(
        ledger, dao, "run1", now_provider=lambda: NOW
    )

    allowed: List[Tuple[str, BaseOrder]] = guard.filter([("B", make_order("2330"))])

    assert allowed == []
    assert (
        dao.conn.execute("SELECT category FROM live_risk_event").fetchone()[0]
        == CROSS_STRATEGY_BLOCKED
    )


def test_guard_blocks_unattributed_holders(dao: LiveTradeDAO) -> None:
    """
    持有者是 `__unattributed__` 時一律擋下

    未歸屬部位代表本地帳與券商帳對不起來，那正是最不該再疊新倉的時候。
    """

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        dao, now_provider=lambda: NOW
    )
    ledger.adopt_broker_positions(
        [BrokerPositionSnapshot(symbol="2330", direction=PositionType.LONG, volume=1)]
    )
    guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(ledger, dao, "run1")

    assert guard.filter([("A", make_order("2330"))]) == []
    assert ledger.get_holder("2330") == UNATTRIBUTED_STRATEGY


def test_guard_treats_pending_orders_as_holders(dao: LiveTradeDAO) -> None:
    """
    掛單中的標的同樣算被佔住

    等它成交才發現衝突就來不及了——那時兩張反向的單都已經在場上。
    """

    ledger: PositionAttributionLedger = PositionAttributionLedger(dao)
    guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(ledger, dao, "run1")

    allowed: List[Tuple[str, BaseOrder]] = guard.filter(
        [("B", make_order("2330"))], pending_holders={"2330": "A"}
    )

    assert allowed == []


def test_guard_lets_closing_orders_through(dao: LiveTradeDAO) -> None:
    """守門認得平倉單：判定委派給共用的動作推導，不自己寫一份 if"""

    ledger: PositionAttributionLedger = PositionAttributionLedger(dao)
    guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(ledger, dao, "run1")

    allowed: List[Tuple[str, BaseOrder]] = guard.filter(
        [("B", make_order("2330", Action.SELL))], pending_holders={"2330": "A"}
    )

    assert len(allowed) == 1


def test_guard_recognises_short_closing(dao: LiveTradeDAO) -> None:
    """SHORT 的買進是平倉，不可當成新倉擋掉"""

    ledger: PositionAttributionLedger = PositionAttributionLedger(dao)
    guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(ledger, dao, "run1")

    allowed: List[Tuple[str, BaseOrder]] = guard.filter(
        [("B", make_order("2330", Action.BUY, PositionType.SHORT))],
        pending_holders={"2330": "A"},
    )

    assert len(allowed) == 1
