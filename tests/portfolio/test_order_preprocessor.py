import ast
from pathlib import Path
from typing import List, Set

from core.models import BaseOrder, StockOrder
from core.portfolio.order_preprocessor import (
    STAGE_CLOSE,
    STAGE_OPEN,
    OrderPreprocessor,
    PreprocessResult,
)
from core.utils import Action, PositionType

"""
委託前處理只有一份：方向驗證 → 補值 → 排序 → 持倉檔數上限

串接順序以前寫了三份（實盤訊號元件、日 K simulator、逐筆 simulator），其中一份
平倉不補值。分岔不會報錯，只會讓實盤與回測對同一張單做出不同的事。
"""

ROOT: Path = Path(__file__).resolve().parents[2]


class _Enricher:
    """記下補值時收到哪些單的成本模型替身"""

    def __init__(self) -> None:
        self.seen: List[List[BaseOrder]] = []

    def enrich_orders(self, orders: List[BaseOrder]) -> List[BaseOrder]:
        self.seen.append(list(orders))
        return orders


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
        price=100.0,
    )


def test_wrong_direction_is_rejected_and_reported() -> None:
    """方向不在白名單、或動作與方向不符：剔除並由回傳值帶出，不靜默丟棄"""

    preprocessor: OrderPreprocessor = OrderPreprocessor({PositionType.LONG}, None, None)
    good: StockOrder = make_order()
    wrong_side: StockOrder = make_order("2317", Action.SELL, PositionType.SHORT)
    wrong_action: StockOrder = make_order("2454", Action.SELL, PositionType.LONG)

    result: PreprocessResult = preprocessor.prepare_orders(
        [good, wrong_side, wrong_action], STAGE_OPEN
    )

    assert result.orders == [good]
    assert result.rejected_direction == [wrong_side, wrong_action]


def test_every_stage_is_enriched() -> None:
    """開倉、平倉都補值：策略自己組的回補單漏填放空管道時，不該到券商轉換層才被拒"""

    enricher: _Enricher = _Enricher()
    preprocessor: OrderPreprocessor = OrderPreprocessor(
        {PositionType.LONG},
        enricher,
        None,  # type: ignore[arg-type]
    )
    entry: StockOrder = make_order()
    exit_order: StockOrder = make_order(action=Action.SELL)

    preprocessor.prepare_orders([entry], STAGE_OPEN)
    preprocessor.prepare_orders([exit_order], STAGE_CLOSE)

    assert enricher.seen == [[entry], [exit_order]]


def test_max_holdings_counts_orders_accepted_earlier_in_the_batch() -> None:
    """排序後才套上限；同一批前面通過的單佔走名額，呼叫端傳入的集合跟著更新"""

    preprocessor: OrderPreprocessor = OrderPreprocessor({PositionType.LONG}, None, 2)
    occupied: Set[str] = {"1101"}

    result: PreprocessResult = preprocessor.prepare_orders(
        [make_order("2454"), make_order("2330"), make_order("2317")],
        STAGE_OPEN,
        occupied,
    )

    # 排序後 2317 在前；1101 已佔一格，只剩一格給 2317
    assert [order.symbol for order in result.orders] == ["2317"]
    assert [order.symbol for order in result.rejected_max_holdings] == ["2330", "2454"]
    assert occupied == {"1101", "2317"}


def test_adding_to_a_held_symbol_does_not_take_a_slot() -> None:
    preprocessor: OrderPreprocessor = OrderPreprocessor({PositionType.LONG}, None, 1)

    result: PreprocessResult = preprocessor.prepare_orders(
        [make_order("2330")], STAGE_OPEN, {"2330"}
    )

    assert [order.symbol for order in result.orders] == ["2330"]


def test_without_an_occupied_set_the_cap_is_left_to_the_caller() -> None:
    """日 K 回測逐單判：名額在成交後才被佔走，一批之前就判會讓沒成交的單白佔名額"""

    preprocessor: OrderPreprocessor = OrderPreprocessor({PositionType.LONG}, None, 1)

    result: PreprocessResult = preprocessor.prepare_orders(
        [make_order("2330"), make_order("2317")], STAGE_OPEN
    )

    assert len(result.orders) == 2 and result.rejected_max_holdings == []
    assert preprocessor.admit(make_order("2330"), {"2317"}) is False
    assert preprocessor.admit(make_order("2330"), set()) is True


def _attribute_calls(path: Path) -> Set[str]:
    """檔案裡所有 `x.<name>(...)` 的方法名"""

    tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_callers_do_not_chain_the_steps_themselves() -> None:
    """實盤訊號元件與兩支 simulator 不再自己串接前處理；`Backtester` 也不再提供薄包裝"""

    forbidden: Set[str] = {"validate_orders", "enrich_orders", "sort_orders"}
    for relative in (
        "core/live/signals.py",
        "core/backtest/simulator/base.py",
        "core/backtest/simulator/bar_simulator.py",
        "core/backtest/simulator/tick_simulator.py",
    ):
        called: Set[str] = _attribute_calls(ROOT / relative) & forbidden
        assert not called, f"{relative} 自己串接了前處理：{sorted(called)}"

    backtester: ast.Module = ast.parse(
        (ROOT / "core/backtest/backtester.py").read_text(encoding="utf-8")
    )
    defined: Set[str] = {
        node.name for node in ast.walk(backtester) if isinstance(node, ast.FunctionDef)
    }
    assert not (defined & forbidden), (
        f"Backtester 仍有薄包裝：{sorted(defined & forbidden)}"
    )
