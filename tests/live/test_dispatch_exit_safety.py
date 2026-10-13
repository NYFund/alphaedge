import datetime
from typing import Any, Dict, List, Tuple

from core.utils import Action, ExecutionStyle, ExecutionTiming

from .test_live_execution_layer import events, sent_orders
from .test_live_trader_day import (
    TODAY,
    Harness,
    ScriptedStrategy,
    make_order,
    seed_holding,
)

"""
送單路徑對**出場單**的安全性

出場單（平倉、停損）在 `dispatch()` 裡與開倉單走同一條流程，但兩者的語意相反：
開倉是占用資金與曝險、出場是釋放。這裡盯住的是「把出場單當開倉單管」會造成的
出不了場：額度用滿時出場被資金保留擋下、被擋下後又沒有人補。
"""


class PlainExit(ScriptedStrategy):
    """只有一張平倉賣單（決策價 100，在漲跌停 110／90 之內）"""

    def __init__(self) -> None:
        super().__init__(
            "PlainExit", close_orders=[make_order(action=Action.SELL, price=100.0)]
        )
        self.live_execution = ExecutionStyle.MARKET


class PlainOpen(ScriptedStrategy):
    """只有一張開倉買單"""

    def __init__(self) -> None:
        super().__init__("PlainOpen", [make_order(price=100.0)])
        self.live_execution = ExecutionStyle.MARKET


# === 資金保留 ===
def test_exit_is_not_blocked_by_capital_reservation_when_quota_is_full() -> None:
    """
    額度用滿的策略仍然出得了場

    額度 5 萬、持有 1 張 2330（名目 10 萬）：可用額度 = 額度 − 持倉占用，已趨近 0。
    出場是釋放資金，不該先保留；先保留的話滿倉策略之後每一張平倉／停損都會被擋下。
    """

    harness: Harness = Harness([PlainExit()], quota=50_000.0)
    seed_holding(harness, "PlainExit", 1)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    sent: List[Any] = sent_orders(harness)
    assert [order.action for order in sent] == [Action.SELL]
    assert events(harness, "CAPITAL_EXHAUSTED") == []
    assert harness.allocator.reserved["PlainExit"] == 0.0


def test_open_still_needs_capital_reservation() -> None:
    """開倉單照舊要先保留：額度不足就擋下並寫事件，免保留只放寬出場"""

    harness: Harness = Harness([PlainOpen()], quota=50_000.0)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    exhausted: List[Tuple[Any, ...]] = events(harness, "CAPITAL_EXHAUSTED")
    assert harness.broker.placed_count == 0
    assert len(exhausted) == 1
    assert exhausted[0][0] == "PlainOpen"


# === 未送出的出場單 ===
class BlockedExit(ScriptedStrategy):
    """平倉賣單決策價 80 低於跌停 90：風控 PRICE_LIMIT 擋下，從未送出"""

    def __init__(self) -> None:
        super().__init__(
            "BlockedExit", close_orders=[make_order(action=Action.SELL, price=80.0)]
        )
        self.live_execution = ExecutionStyle.MARKET


class BlockedOpen(ScriptedStrategy):
    """開倉買單決策價 120 高於漲停 110：風控 PRICE_LIMIT 擋下"""

    def __init__(self) -> None:
        super().__init__("BlockedOpen", [make_order(price=120.0)])
        self.live_execution = ExecutionStyle.MARKET


class KillSwitchOn:
    """kill switch 檔案存在"""

    def exists(self) -> bool:
        return True


def pending_covers(harness: Harness) -> List[Dict[str, Any]]:
    """次日到期的補平待辦"""

    return harness.dao.get_pending_actions(TODAY + datetime.timedelta(days=1))


def test_exit_blocked_by_risk_creates_a_next_day_cover() -> None:
    """
    風控擋下的出場單要留下次日補平的待辦

    盤後的「出場必補」只掃 `live_order` 的未成交殘量；從未送出的出場單沒有這一列，
    不在這裡補的話部位會無聲留到隔夜、而且隔天沒有人處理。
    """

    harness: Harness = Harness([BlockedExit()])
    seed_holding(harness, "BlockedExit", 1)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    (action,) = pending_covers(harness)
    assert (
        action["symbol"],
        action["action"],
        action["position_type"],
        action["volume"],
        action["status"],
    ) == ("2330", "Sell", "LONG", 1, harness.dao.ACTION_PENDING)
    assert events(harness, "UNSENT_EXIT") == [("BlockedExit", "2330", "CRITICAL")]


def test_exit_skipped_by_the_execution_layer_creates_a_next_day_cover() -> None:
    """取不到漲跌停、執行層略過該檔，同樣要補"""

    harness: Harness = Harness([PlainExit()], price_limits=(None, None))
    seed_holding(harness, "PlainExit", 1)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert len(events(harness, "EXECUTION_UNAVAILABLE")) == 1
    assert [action["symbol"] for action in pending_covers(harness)] == ["2330"]


def test_exit_blocked_by_the_kill_switch_is_not_scheduled_for_cover() -> None:
    """kill switch 是「現在什麼都不動」的決定，替它排次日補平等於繞過那個決定"""

    harness: Harness = Harness([PlainExit()])
    seed_holding(harness, "PlainExit", 1)
    harness.risk.kill_switch_path = KillSwitchOn()
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert pending_covers(harness) == []
    assert events(harness, "UNSENT_EXIT") == []


def test_unsent_open_does_not_create_a_cover() -> None:
    """開倉未送出一律放棄，不會變成待辦"""

    harness: Harness = Harness([BlockedOpen()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert pending_covers(harness) == []
    assert events(harness, "UNSENT_EXIT") == []
