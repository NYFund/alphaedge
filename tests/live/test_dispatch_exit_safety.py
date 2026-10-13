from typing import Any, List, Tuple

from core.utils import Action, ExecutionStyle, ExecutionTiming

from .test_live_execution_layer import events, sent_orders
from .test_live_trader_day import Harness, ScriptedStrategy, make_order, seed_holding

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
