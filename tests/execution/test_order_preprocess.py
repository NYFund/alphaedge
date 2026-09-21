import datetime
from typing import Dict, List, Optional, Set

import pytest

from core.execution import order_preprocess
from core.models import BaseOrder, StockOrder
from core.utils import Action, BarExecutionOrder, PositionType

"""
共用委託前處理：回測與實盤唯一的一份

這一層是兩邊不漂移的關鍵。方向白名單、`max_holdings`、排序這三件事如果各寫一份，
**漂移不會報錯**——只會讓實盤少送或多送一張單，而回測績效仍然漂亮。

回歸雙線已經釘住「回測那一側行為不變」；本檔驗的是這些函式**不依賴策略物件**
也算得出同樣的結果，那是實盤能重用它們的前提。
"""

DATE: datetime.date = datetime.date(2026, 9, 19)


def make_order(
    symbol: str = "2330",
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
    date: Optional[datetime.date] = None,
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        action=action,
        position_type=position_type,
        date=date or DATE,
        volume=1,
        price=100.0,
    )


# === 方向白名單 ===
def test_allowed_directions_default_to_the_declared_one() -> None:
    """未指定白名單時等同策略宣告的方向"""

    assert order_preprocess.get_allowed_directions(None, PositionType.SHORT) == {
        PositionType.SHORT
    }


def test_explicit_whitelist_wins() -> None:
    """明確宣告時以宣告為準（多空都做的策略）"""

    both: Set[PositionType] = {PositionType.LONG, PositionType.SHORT}

    assert order_preprocess.get_allowed_directions(both, PositionType.LONG) == both


# === 單根 bar 的執行順序 ===
@pytest.mark.parametrize(
    "position_type, enable_intraday, expected",
    [
        (PositionType.LONG, True, BarExecutionOrder.CLOSE_THEN_OPEN),
        (PositionType.LONG, False, BarExecutionOrder.CLOSE_THEN_OPEN),
        (PositionType.SHORT, True, BarExecutionOrder.OPEN_THEN_CLOSE),
        (PositionType.SHORT, False, BarExecutionOrder.CLOSE_THEN_OPEN),
    ],
)
def test_execution_order_derivation(
    position_type: PositionType,
    enable_intraday: bool,
    expected: BarExecutionOrder,
) -> None:
    """
    推導表

    SHORT ＋ 當沖採先開後平：現股當沖沖賣必須先賣才可能同日回補。
    **LONG 不自動切換**：`enable_intraday` 預設為 True，自動切換等於在無人宣告的
    情況下改掉每一支做多策略的成交順序與回測結果。
    """

    assert (
        order_preprocess.get_execution_order(None, position_type, enable_intraday)
        is expected
    )


def test_explicit_execution_order_bypasses_the_table() -> None:
    """策略填了就完全不參與推導——推導出的是預設建議，不是政策"""

    assert (
        order_preprocess.get_execution_order(
            BarExecutionOrder.OPEN_THEN_CLOSE, PositionType.LONG, False
        )
        is BarExecutionOrder.OPEN_THEN_CLOSE
    )


# === 動作推導 ===
@pytest.mark.parametrize(
    "position_type, open_action, close_action",
    [
        (PositionType.LONG, Action.BUY, Action.SELL),
        (PositionType.SHORT, Action.SELL, Action.BUY),
    ],
)
def test_action_resolution(
    position_type: PositionType, open_action: Action, close_action: Action
) -> None:
    """開平倉動作依**訂單方向**推導，不看策略宣告的方向"""

    assert order_preprocess.resolve_open_action(position_type) is open_action
    assert order_preprocess.resolve_close_action(position_type) is close_action


# === 方向檢查 ===
def test_orders_outside_the_whitelist_are_rejected_and_counted() -> None:
    """
    不合法的方向要剔除並計數，**禁止靜默丟棄**

    靜默丟棄的後果是策略以為自己送出了一張單、引擎以為沒有，
    兩邊都不會報錯，只有部位對不上。
    """

    counts: Dict[str, int] = {}
    orders: List[BaseOrder] = [
        make_order(),
        make_order(action=Action.SELL, position_type=PositionType.SHORT),
    ]

    valid: List[BaseOrder] = order_preprocess.validate_orders(
        orders, "open", {PositionType.LONG}, counts
    )

    assert len(valid) == 1
    assert counts["rejected_direction"] == 1


def test_wrong_action_for_the_stage_is_rejected() -> None:
    """開倉階段收到平倉動作也要剔除：那通常是策略把兩個鉤子的輸出接反了"""

    counts: Dict[str, int] = {}
    valid: List[BaseOrder] = order_preprocess.validate_orders(
        [make_order(action=Action.SELL)], "open", {PositionType.LONG}, counts
    )

    assert valid == []
    assert counts["rejected_direction"] == 1


def test_event_count_keys_are_stable() -> None:
    """
    計數器的 key 不可更名

    報表與回歸比對都吃這些 key，改名不會報錯，只會讓那一欄永遠是 0。
    """

    counts: Dict[str, int] = {}
    order_preprocess.validate_orders(
        [make_order(action=Action.SELL)], "open", {PositionType.LONG}, counts
    )
    order_preprocess.check_max_holdings(make_order(), 1, {"2317"}, counts)

    assert set(counts) == {"rejected_direction", "rejected_max_holdings"}


def test_validation_works_without_a_counter() -> None:
    """不提供計數器時照樣運作：實盤那側不一定有回測的事件計數"""

    assert (
        order_preprocess.validate_orders([make_order()], "open", {PositionType.LONG})
        != []
    )


# === 持倉檔數上限 ===
def test_none_means_unlimited() -> None:
    """None 表示不限制，與 `EqualWeightSizer` 的語意一致"""

    held: Set[str] = {str(index) for index in range(999)}
    assert order_preprocess.check_max_holdings(make_order(), None, held) is True


def test_max_holdings_uses_live_position_count() -> None:
    """
    看的是**即時**持倉數

    未成交的單不增加持倉，後面的單因此仍可能被放行——這正是它與
    `EqualWeightSizer.size()` 的同名檢查不等價的地方，兩道都要有。
    """

    four: Set[str] = {"1101", "1102", "1103", "1104"}
    assert order_preprocess.check_max_holdings(make_order(), 5, four) is True
    assert (
        order_preprocess.check_max_holdings(make_order(), 5, four | {"1105"}) is False
    )


def test_adding_to_a_held_symbol_is_exempt_even_when_full() -> None:
    """
    滿額時對已持有標的加碼照樣放行：加碼不增加檔數

    這條豁免以前只寫在實盤：上限 2、已持有 A 與 B，再開 A——回測剔除、實盤送出，
    parity 比對每天多一筆 `UNEXPLAINED`。現在兩邊呼叫同一份判定。
    """

    held: Set[str] = {"2330", "2317"}

    assert order_preprocess.check_max_holdings(make_order("2330"), 2, held) is True
    assert order_preprocess.check_max_holdings(make_order("2454"), 2, held) is False


# === 排序 ===
def test_sort_is_deterministic_by_date_and_symbol() -> None:
    """
    排序不依賴上游容器的迭代順序

    委託的到達順序繼承自報價順序，而報價來自沒有 `ORDER BY` 的查詢——
    列順序取決於 SQLite 當下選到哪個索引，換一次 schema 就可能翻掉，
    且翻掉時不會報錯，只會讓回測結果無聲改變。
    """

    orders: List[BaseOrder] = [
        make_order(symbol="2454"),
        make_order(symbol="2317"),
        make_order(symbol="2330"),
    ]

    assert [order.symbol for order in order_preprocess.sort_orders(orders)] == [
        "2317",
        "2330",
        "2454",
    ]


def test_sort_is_stable_for_the_same_symbol() -> None:
    """
    同一標的的多筆委託維持策略給定的先後

    不穩定的話，分批建倉與部分平倉的意圖會被打散。
    """

    first: StockOrder = make_order(symbol="2330")
    second: StockOrder = make_order(symbol="2330")
    first.price, second.price = 100.0, 200.0

    ordered: List[BaseOrder] = order_preprocess.sort_orders([first, second])

    assert [order.price for order in ordered] == [100.0, 200.0]


def test_sort_orders_across_dates() -> None:
    """跨日的委託先依日期排：tick 回測會把同一天的壓成依代號排序"""

    older: StockOrder = make_order(symbol="2454", date=datetime.date(2026, 9, 18))
    newer: StockOrder = make_order(symbol="2317", date=datetime.date(2026, 9, 19))

    assert [order.symbol for order in order_preprocess.sort_orders([newer, older])] == [
        "2454",
        "2317",
    ]


# === 分層 ===
def test_module_does_not_import_strategy_or_engine() -> None:
    """
    **不可 import 策略或引擎**

    收了策略物件就會多一條 `core.execution` → `core.strategies.base` 的同層邊，
    而這一層要能被更低層重用。這條由 `check_layer_deps.py` 守著，
    這裡再釘一次是因為它是本模組存在的前提。
    """

    source: str = (
        __import__("pathlib")
        .Path("core/execution/order_preprocess.py")
        .read_text(encoding="utf-8")
    )

    assert "core.strategies" not in source
    assert "core.backtest" not in source
    assert "core.live" not in source
