import datetime
from typing import Dict, Optional

import pytest

from core.backtest.models.event_counts import new_event_counts
from core.backtest.models.fill_model import TwStockFillModel
from core.models import DayTradeListSnapshot, ShortSaleListSnapshot, StockOrder
from core.utils import Action, PositionType, ShortMethod
from tests.conftest import build_stock_quote

"""
交易所每日名單的放空檢核（`ShortConstraint.check_short_sale_list`／`check_day_trade_list`）

這兩個開關以前有定義、沒有呼叫端，設了不會生效。這裡反過來釘住「開了就會擋單」，
以及「沒開時一張單都不動」——後者是既有回測結果不變的保證。
"""

SYMBOL: str = "2330"
DATE: datetime.date = datetime.date(2024, 6, 13)
REFERENCE: float = 100.0  # 平盤（前一交易日收盤）


def short_order(
    short_method: ShortMethod, price: float = REFERENCE, symbol: str = SYMBOL
) -> StockOrder:
    """放空開倉單"""

    return StockOrder(
        stock_id=symbol,
        date=DATE,
        action=Action.SELL,
        position_type=PositionType.SHORT,
        price=price,
        volume=1,
        short_method=short_method,
    )


def make_fill_model(
    counts: Dict[str, int],
    short_sale_list: Optional[ShortSaleListSnapshot] = None,
    day_trade_list: Optional[DayTradeListSnapshot] = None,
    check_short_sale_list: bool = True,
    check_day_trade_list: bool = True,
) -> TwStockFillModel:
    """開啟名單檢核、平盤為 100 的成交模型"""

    model: TwStockFillModel = TwStockFillModel(
        event_counts=counts,
        check_short_sale_list=check_short_sale_list,
        check_day_trade_list=check_day_trade_list,
    )
    model.reference_prices[SYMBOL] = REFERENCE
    model.apply_trading_lists(short_sale_list, day_trade_list)
    return model


def fill(model: TwStockFillModel, order: StockOrder) -> Optional[StockOrder]:
    """以平盤附近的報價成交"""

    quote = build_stock_quote(
        stock_id=order.symbol, date=DATE, cur_price=REFERENCE, high=105.0, low=95.0
    )
    return model.fill(order, quote)


# === 平盤下得融（借）券賣出名單 ===
def test_margin_short_outside_the_list_is_rejected() -> None:
    """名單＝可融資融券的證券；不在名單上就不能融券賣出，平盤以上也一樣"""

    counts: Dict[str, int] = new_event_counts()
    model = make_fill_model(counts, ShortSaleListSnapshot(listed=frozenset({"2317"})))

    assert fill(model, short_order(ShortMethod.MARGIN, price=101.0)) is None
    assert counts["rejected_short_halted"] == 1


@pytest.mark.parametrize(
    ("short_method", "snapshot"),
    [
        (
            ShortMethod.MARGIN,
            ShortSaleListSnapshot(
                listed=frozenset({SYMBOL}), margin_halted=frozenset({SYMBOL})
            ),
        ),
        (
            ShortMethod.SBL,
            ShortSaleListSnapshot(
                listed=frozenset({SYMBOL}), sbl_halted=frozenset({SYMBOL})
            ),
        ),
    ],
    ids=["margin-halted", "sbl-halted"],
)
def test_halted_method_is_rejected(
    short_method: ShortMethod, snapshot: ShortSaleListSnapshot
) -> None:
    """被註記暫停的管道整天不能放空，與價位無關"""

    counts: Dict[str, int] = new_event_counts()
    model = make_fill_model(counts, snapshot)

    assert fill(model, short_order(short_method, price=101.0)) is None
    assert counts["rejected_short_halted"] == 1


def test_below_reference_is_rejected_when_banned() -> None:
    """前一交易日收盤跌停：本日在名單內也不得平盤下融券，但平盤本身可以"""

    counts: Dict[str, int] = new_event_counts()
    snapshot = ShortSaleListSnapshot(
        listed=frozenset({SYMBOL}), below_reference_banned=frozenset({SYMBOL})
    )
    model = make_fill_model(counts, snapshot)

    assert fill(model, short_order(ShortMethod.MARGIN, price=99.5)) is None
    assert counts["rejected_below_reference"] == 1
    assert fill(model, short_order(ShortMethod.MARGIN, price=REFERENCE)) is not None


def test_sbl_outside_the_list_is_limited_to_reference_or_above() -> None:
    """借券賣出不以融券資格為前提：名單外平盤以上可以，平盤下不行"""

    counts: Dict[str, int] = new_event_counts()
    model = make_fill_model(counts, ShortSaleListSnapshot(listed=frozenset()))

    assert fill(model, short_order(ShortMethod.SBL, price=REFERENCE)) is not None
    assert fill(model, short_order(ShortMethod.SBL, price=99.5)) is None
    assert counts["rejected_below_reference"] == 1


def test_listed_symbol_can_short_below_reference() -> None:
    """名單內、沒有任何註記：平盤下融券可以成交"""

    model = make_fill_model(
        new_event_counts(), ShortSaleListSnapshot(listed=frozenset({SYMBOL}))
    )

    assert fill(model, short_order(ShortMethod.MARGIN, price=99.5)) is not None


# === 現股當沖名單 ===
def test_day_trade_short_requires_the_day_trade_list() -> None:
    """現股當沖先賣：不在當沖名單、或被註記暫停先賣後買，都不能賣"""

    counts: Dict[str, int] = new_event_counts()
    halted = make_fill_model(
        counts,
        day_trade_list=DayTradeListSnapshot(
            day_tradable=frozenset({SYMBOL}), sell_first_halted=frozenset({SYMBOL})
        ),
    )
    missing = make_fill_model(counts, day_trade_list=DayTradeListSnapshot())

    assert fill(halted, short_order(ShortMethod.DAY_TRADE)) is None
    assert fill(missing, short_order(ShortMethod.DAY_TRADE)) is None
    assert counts["rejected_not_day_tradable"] == 2


def test_day_trade_short_ignores_the_below_reference_rule() -> None:
    """
    先賣後買是現股賣出，交易所不以平盤下規則限制它

    即使平盤下名單把它註記成跌停禁止，現股當沖仍可以在平盤以下先賣。
    """

    snapshot = ShortSaleListSnapshot(
        listed=frozenset(), below_reference_banned=frozenset({SYMBOL})
    )
    model = make_fill_model(
        new_event_counts(),
        snapshot,
        DayTradeListSnapshot(day_tradable=frozenset({SYMBOL})),
    )

    assert fill(model, short_order(ShortMethod.DAY_TRADE, price=99.5)) is not None


# === 開關與資料 ===
def test_disabled_checks_leave_orders_untouched() -> None:
    """沒開檢核時回傳原物件本身：既有回測結果不變的保證"""

    model = make_fill_model(
        new_event_counts(), check_short_sale_list=False, check_day_trade_list=False
    )
    order: StockOrder = short_order(ShortMethod.MARGIN, price=90.0)

    assert fill(model, order) is order


def test_long_orders_are_never_checked() -> None:
    """名單只管放空開倉；做多買進與賣出不受影響"""

    model = make_fill_model(new_event_counts(), ShortSaleListSnapshot())
    buy = StockOrder(
        stock_id=SYMBOL,
        date=DATE,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=REFERENCE,
        volume=1,
    )

    assert fill(model, buy) is buy


def test_enabled_check_without_a_list_raises() -> None:
    """
    開了檢核卻沒有當日名單：拋錯而不是放行

    資料源在回測開始前就確認過每天都有名單，走到這裡代表那道檢查被繞過；
    照常放行會讓結果看起來有檢查、其實沒有。
    """

    model = make_fill_model(new_event_counts(), short_sale_list=None)

    with pytest.raises(RuntimeError, match="沒有名單"):
        fill(model, short_order(ShortMethod.MARGIN))
