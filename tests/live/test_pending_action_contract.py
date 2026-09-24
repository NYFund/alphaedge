import datetime
from typing import Any, Dict, Tuple

import pytest

from core.live.attribution.position_ledger import PositionAttributionLedger
from core.models import PendingAction
from core.utils import Action, PositionType

"""
跨日待辦的型別化契約，與策略層快照的均價

`LiveTrader` 會把待辦整個交給**策略作者實作**的 `build_cover_order(action)`。
原本傳的是資料列 dict，等於把 `live_pending_action` 的 schema 變成策略層的
公開契約——而取不到的鍵只會安靜地變成 None，改個欄位名不會有任何錯誤。

均價那一項則是「欄位存在但永遠空白」：報表有 `Avg Price`，
而策略層那幾列從來沒填過值。
"""


def make_row(**overrides: Any) -> Dict[str, Any]:
    """紀錄庫實際存的形態：日期與時間都是 TEXT"""

    row: Dict[str, Any] = {
        "action_id": "2026-09-23-c1",
        "strategy_name": "Alpha",
        "symbol": "2330",
        "action": "Sell",
        "position_type": "LONG",
        "volume": 3,
        "due_date": "2026-09-24",
        "status": "PENDING",
        "reason": "平倉單未成交",
        "source_client_order_id": "c1",
        "created_at": "2026-09-23T13:30:00",
        "resolved_at": None,
    }
    row.update(overrides)
    return row


# === 型別轉換 ===
def test_text_columns_become_real_dates() -> None:
    """
    SQLite 把日期存成 TEXT，轉型要在建立時做完

    留著字串的話，`"2026-09-24" <= "2026-9-3"` 這種比較不會報錯，只會給錯的答案。
    """

    action: PendingAction = PendingAction.from_row(make_row())

    assert action.due_date == datetime.date(2026, 9, 24)
    assert action.created_at == datetime.datetime(2026, 9, 23, 13, 30)
    assert action.resolved_at is None


def test_enums_are_parsed_not_left_as_strings() -> None:
    """`action` 與 `position_type` 收 Enum：拼錯的值在建立時就過不了"""

    action: PendingAction = PendingAction.from_row(make_row())

    assert action.action is Action.SELL
    assert action.position_type is PositionType.LONG


def test_enum_members_still_compare_as_strings() -> None:
    """
    兩者都是 `str` 子類，當字典鍵與字串比較都照舊

    歸屬帳的鍵是 `(symbol, direction)` 的**字串** tuple，
    型別化不可以讓那組查詢查不到東西。
    """

    action: PendingAction = PendingAction.from_row(make_row())
    positions: Dict[Tuple[str, str], int] = {("2330", "LONG"): 5}

    assert positions.get((action.symbol, action.position_type or "")) == 5


def test_bad_enum_value_is_rejected_at_construction() -> None:
    """
    值域外的方向要當場拋出

    dict 那版會讓 `"LNOG"` 一路流到歸屬帳查詢，查不到就當成「沒部位」——
    於是待辦被標成完成，而部位還在。
    """

    with pytest.raises(ValueError):
        PendingAction.from_row(make_row(position_type="LNOG"))

    with pytest.raises(ValueError):
        PendingAction.from_row(make_row(action="賣出"))


def test_optional_columns_tolerate_empty() -> None:
    """選填欄位空著就是 None，**不要補一個預設值**"""

    action: PendingAction = PendingAction.from_row(
        make_row(position_type=None, reason=None, source_client_order_id=None)
    )

    assert action.position_type is None
    assert action.reason is None
    assert action.source_client_order_id is None


def test_missing_required_column_is_not_silently_none() -> None:
    """
    必填欄位缺了要拋出

    這正是 dict 契約的問題：`action["volume"]` 改名成 `qty` 之後，
    dict 版會 `KeyError`，而 `action.get("volume")` 版會安靜地送出 0 股。
    """

    row: Dict[str, Any] = make_row()
    del row["volume"]

    with pytest.raises(KeyError):
        PendingAction.from_row(row)


# === 策略層快照的均價 ===
class FakeLotDAO:
    """只回 lot 清單的最小替身"""

    def __init__(self, lots: Any) -> None:
        self._lots: Any = lots

    def get_open_lots(self, strategy_name: Any = None, symbol: Any = None) -> Any:
        return [lot for lot in self._lots if lot["strategy_name"] == strategy_name]


def lot(volume: int, price: float, symbol: str = "2330") -> Dict[str, Any]:
    return {
        "strategy_name": "Alpha",
        "symbol": symbol,
        "direction": "LONG",
        "volume": volume,
        "open_price": price,
    }


def test_avg_price_is_weighted_by_volume() -> None:
    """
    **以口數加權，不是各 lot 取算術平均**

    一筆 1 張的試單與一筆 50 張的主倉權重相同的話，算出來的成本可以差很遠：
    這裡算術平均會是 350.0，而真正的成本是 590.20。
    """

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        FakeLotDAO([lot(1, 100.0), lot(50, 600.0)])  # type: ignore[arg-type]
    )

    prices: Dict[Tuple[str, str], float] = ledger.get_strategy_avg_prices("Alpha")

    assert prices[("2330", "LONG")] == pytest.approx((100.0 + 600.0 * 50) / 51)
    assert prices[("2330", "LONG")] != pytest.approx(350.0)


def test_avg_price_separates_symbols() -> None:
    """不同標的不可混在一起算"""

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        FakeLotDAO([lot(2, 100.0), lot(2, 50.0, symbol="2317")])  # type: ignore[arg-type]
    )

    prices: Dict[Tuple[str, str], float] = ledger.get_strategy_avg_prices("Alpha")

    assert prices[("2330", "LONG")] == pytest.approx(100.0)
    assert prices[("2317", "LONG")] == pytest.approx(50.0)


def test_no_lots_yields_no_keys() -> None:
    """沒有部位就不要生出一個 0 元均價的鍵"""

    ledger: PositionAttributionLedger = PositionAttributionLedger(
        FakeLotDAO([])  # type: ignore[arg-type]
    )

    assert ledger.get_strategy_avg_prices("Alpha") == {}
