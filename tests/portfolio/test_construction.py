import datetime
from typing import List, Tuple

import pytest

from core.models import (
    BaseOrder,
    FuturesAccount,
    FuturesQuote,
    StockAccount,
    StockPosition,
)
from core.portfolio.construction import (
    FuturesPortfolioConstructor,
    StockPortfolioConstructor,
)
from core.portfolio.signal import Signal
from core.portfolio.sizing import EqualWeightSizer
from core.utils import Action, FuturesSession, PositionType, Scale

"""
部位建構層：訊號換算成訂單的欄位與數量

**公式本身由 `test_position_sizer.py` 與回歸 baseline 把關**（LONG 線的 915 筆
直接踩在等權切分的取整規則上）。本檔要釘的是這一層獨有的事：

- 訂單價取 `order_price`，**不是** `sizing_price`——兩者在現行資料源同值，
  拿錯不會有任何一筆交易變動來提醒你。
- `action`／`position_type` 照訊號搬，不由本層推導。
- 期貨的口數受保證金與剩餘口數雙重約束。

本檔原本以三支策略的 `calculate_position_size()` 當 A／B 基準，該方法已於 S6
隨策略改寫刪除，改為逐項寫明預期值。
"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)
MULTIPLIER: int = 200


def order_fields(orders: List[BaseOrder]) -> List[Tuple]:
    """把訂單攤成可逐筆比對的欄位；價格與數量錯一項就代表搬移改到了邏輯"""

    return [
        (
            order.symbol,
            order.date,
            order.action,
            order.position_type,
            order.price,
            order.volume,
        )
        for order in orders
    ]


def make_futures_quote(expiry: str, close: float = 18000.0) -> FuturesQuote:
    """組一筆期貨日盤報價"""

    return FuturesQuote(
        product="TX",
        expiry=expiry,
        scale=Scale.DAY,
        date=DAY_1,
        cur_price=close,
        volume=1000,
        close=close,
        session=FuturesSession.DAY,
        multiplier=MULTIPLIER,
    )


# === 台股：等權資金切分 ===
def test_stock_split_uses_sizing_price_and_orders_at_order_price(make_quote) -> None:
    """張數用 `sizing_price` 算，訂單價用 `order_price`——兩欄不可混用"""

    account: StockAccount = StockAccount(1_000_000.0)
    signals: List[Signal] = [
        Signal(
            quote=make_quote(stock_id="2330", date=DAY_1, cur_price=101.0),
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=101.0,
            sizing_price=100.0,
        ),
        Signal(
            quote=make_quote(stock_id="2317", date=DAY_1, cur_price=52.0),
            action=Action.SELL,
            position_type=PositionType.SHORT,
            order_price=52.0,
            sizing_price=50.0,
        ),
    ]

    orders: List[BaseOrder] = StockPortfolioConstructor(
        EqualWeightSizer(), max_holdings=2
    ).build(signals, account)

    # 兩個名額均分 1,000,000 → 每檔 500,000；以 sizing_price 換算張數
    assert order_fields(orders) == [
        ("2330", DAY_1, Action.BUY, PositionType.LONG, 101.0, 5),
        ("2317", DAY_1, Action.SELL, PositionType.SHORT, 52.0, 10),
    ]


def test_stock_split_respects_existing_holdings(make_quote) -> None:
    """已有持倉時可開名額變少，剩下的名額分到更多資金"""

    account: StockAccount = StockAccount(1_000_000.0)
    account.positions.append(
        StockPosition(id=1, stock_id="2330", date=DAY_1, price=100.0, volume=1)
    )

    signals: List[Signal] = [
        Signal(
            quote=make_quote(stock_id="2317", date=DAY_1, cur_price=50.0),
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=50.0,
            sizing_price=50.0,
        )
    ]

    orders: List[BaseOrder] = StockPortfolioConstructor(
        EqualWeightSizer(), max_holdings=2
    ).build(signals, account)

    # 剩 1 個名額 → 1,000,000 / (50 × 1000) = 20 張
    assert order_fields(orders) == [
        ("2317", DAY_1, Action.BUY, PositionType.LONG, 50.0, 20)
    ]


def test_stock_open_signal_without_sizing_price_raises(make_quote) -> None:
    """開倉訊號少了算量價就當場拋出，不默默少下一張單"""

    signal: Signal = Signal(
        quote=make_quote(stock_id="2330", date=DAY_1, cur_price=100.0),
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=100.0,
    )

    with pytest.raises(ValueError, match="2330"):
        StockPortfolioConstructor(EqualWeightSizer(), 5).build(
            [signal], StockAccount(1_000_000.0)
        )


def test_stock_empty_signals_returns_empty() -> None:
    """沒有訊號時不必碰 sizer"""

    assert (
        StockPortfolioConstructor(EqualWeightSizer(), 5).build(
            [], StockAccount(1_000_000.0)
        )
        == []
    )


# === 台期貨：保證金約束 ===
def futures_signal(quote: FuturesQuote) -> Signal:
    """期貨開倉訊號：口數由保證金決定，故不給 sizing_price"""

    return Signal(
        quote=quote,
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=quote.close,
    )


def test_futures_lots_come_from_margin_not_contract_value() -> None:
    """
    口數＝可動用預算 ÷ 每口保證金

    比率模式下每口保證金 ＝ 18000 × 200 × 0.1 ＝ 360,000；
    預算 ＝ 3,000,000 × 0.5 ＝ 1,500,000 → 4 口。
    **拿契約價值（360 萬）去除會算成 0 口**，那正是這一層存在的理由。
    """

    account: FuturesAccount = FuturesAccount(init_capital=3_000_000)
    orders: List[BaseOrder] = FuturesPortfolioConstructor(
        max_lots=10, max_capital_usage=0.5
    ).build([futures_signal(make_futures_quote("202403"))], account)

    assert order_fields(orders) == [
        ("TX202403", DAY_1, Action.BUY, PositionType.LONG, 18000.0, 4)
    ]


def test_futures_stops_at_remaining_lots() -> None:
    """剩餘口數逐筆遞減，用完即停"""

    account: FuturesAccount = FuturesAccount(init_capital=3_000_000)
    signals: List[Signal] = [
        futures_signal(make_futures_quote("202403")),
        futures_signal(make_futures_quote("202406")),
    ]

    orders: List[BaseOrder] = FuturesPortfolioConstructor(
        max_lots=1, max_capital_usage=0.5
    ).build(signals, account)

    assert order_fields(orders) == [
        ("TX202403", DAY_1, Action.BUY, PositionType.LONG, 18000.0, 1)
    ]


def test_futures_zero_max_lots_opens_nothing() -> None:
    """`max_lots` 為 0 表示不開倉"""

    account: FuturesAccount = FuturesAccount(init_capital=3_000_000)

    assert (
        FuturesPortfolioConstructor(max_lots=0).build(
            [futures_signal(make_futures_quote("202403"))], account
        )
        == []
    )
