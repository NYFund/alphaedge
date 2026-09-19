import datetime
from typing import List, Tuple

import pytest

from core.models import (
    BaseOrder,
    FuturesAccount,
    FuturesQuote,
    StockAccount,
    StockPosition,
    StockQuote,
)
from core.portfolio.construction import (
    FuturesPortfolioConstructor,
    StockPortfolioConstructor,
)
from core.portfolio.signal import Signal
from core.strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from core.strategies.stock.foreign_sell_short_day_trade_strategy import (
    ForeignSellShortDayTradeStrategy,
)
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
from core.utils import Action, FuturesSession, PositionType, Scale

"""
部位建構層的 A／B 測試：新路徑與既有策略的 `calculate_position_size()` 逐筆相同

這一層是純搬移，**唯一有意義的驗收就是新舊輸出一致**。故本檔不自己算一遍
「應該幾張」——那只會變成把同一個公式抄第二遍——而是直接拿三支既有策略的
`calculate_position_size()` 當基準，兩邊餵同一組輸入後逐欄比對。

`./scripts/run_regression.sh` 只能在 S6 改寫策略之後才蓋得到這段；在那之前
本檔是唯一的護欄。
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


# === 台股做多：等權資金切分 ===
@pytest.fixture
def long_strategy() -> MomentumStrategy1:
    """已載入帳戶的做多動能策略"""

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.setup_account(StockAccount(1_000_000.0))
    return strategy


def test_stock_long_matches_calculate_position_size(
    long_strategy: MomentumStrategy1, make_quote
) -> None:
    """`MomentumStrategy1` 的開倉單：新舊路徑逐筆相同"""

    quotes: List[StockQuote] = [
        make_quote(stock_id="2330", date=DAY_1, cur_price=100.0),
        make_quote(stock_id="2317", date=DAY_1, cur_price=50.0),
        make_quote(stock_id="2454", date=DAY_1, cur_price=1000.0),
    ]

    legacy: List[BaseOrder] = long_strategy.calculate_position_size(quotes, Action.BUY)

    signals: List[Signal] = [
        Signal(
            quote=quote,
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=quote.cur_price,
            sizing_price=quote.close,
        )
        for quote in quotes
    ]
    built: List[BaseOrder] = StockPortfolioConstructor(
        long_strategy.sizer, long_strategy.max_holdings
    ).build(signals, long_strategy.account)

    assert order_fields(built) == order_fields(legacy)
    assert built, "這組輸入本來就該開得出倉，空清單代表測試沒測到東西"


def test_stock_long_respects_existing_holdings(
    long_strategy: MomentumStrategy1, make_quote
) -> None:
    """已有持倉時可開名額變少，新舊路徑一致"""

    long_strategy.max_holdings = 2
    long_strategy.account.positions.append(
        StockPosition(id=1, stock_id="2330", date=DAY_1, price=100.0, volume=1)
    )

    quotes: List[StockQuote] = [
        make_quote(stock_id="2317", date=DAY_1, cur_price=50.0),
        make_quote(stock_id="2454", date=DAY_1, cur_price=80.0),
    ]

    legacy: List[BaseOrder] = long_strategy.calculate_position_size(quotes, Action.BUY)

    signals: List[Signal] = [
        Signal(
            quote=quote,
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=quote.cur_price,
            sizing_price=quote.close,
        )
        for quote in quotes
    ]
    built: List[BaseOrder] = StockPortfolioConstructor(
        long_strategy.sizer, long_strategy.max_holdings
    ).build(signals, long_strategy.account)

    assert order_fields(built) == order_fields(legacy)
    assert len(built) == 1


# === 台股放空：算量價與下單價都是開盤價 ===
def test_stock_short_matches_calculate_position_size(make_quote) -> None:
    """`ForeignSellShortDayTradeStrategy` 的放空開倉單：新舊路徑逐筆相同"""

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()
    strategy.setup_account(StockAccount(1_000_000.0))

    quotes: List[StockQuote] = [
        make_quote(stock_id="2330", date=DAY_1, cur_price=105.0, open=100.0),
        make_quote(stock_id="2317", date=DAY_1, cur_price=52.0, open=50.0),
    ]

    legacy: List[BaseOrder] = strategy.calculate_position_size(quotes, Action.SELL)

    signals: List[Signal] = [
        Signal(
            quote=quote,
            action=Action.SELL,
            position_type=PositionType.SHORT,
            order_price=quote.open,
            sizing_price=quote.open,
        )
        for quote in quotes
    ]
    built: List[BaseOrder] = StockPortfolioConstructor(
        strategy.sizer, strategy.max_holdings
    ).build(signals, strategy.account)

    assert order_fields(built) == order_fields(legacy)
    # 放空的下單價是開盤價，不是 cur_price——搬移時最容易混掉的一欄
    assert all(order.price == 100.0 for order in built if order.symbol == "2330")


def test_stock_open_signal_without_sizing_price_raises(make_quote) -> None:
    """開倉訊號少了算量價就當場拋出，不默默少下一張單"""

    signal: Signal = Signal(
        quote=make_quote(stock_id="2330", date=DAY_1, cur_price=100.0),
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=100.0,
    )

    with pytest.raises(ValueError, match="2330"):
        StockPortfolioConstructor(MomentumStrategy1().sizer, 5).build(
            [signal], StockAccount(1_000_000.0)
        )


# === 台期貨：保證金約束 ===
@pytest.fixture
def futures_strategy() -> MomentumFuturesStrategy:
    """已載入帳戶的期貨動能策略（比率模式，不連資料庫）"""

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.setup_account(FuturesAccount(init_capital=3_000_000))
    return strategy


def futures_constructor(
    strategy: MomentumFuturesStrategy,
) -> FuturesPortfolioConstructor:
    """以策略當下的設定組出對應的 constructor"""

    return FuturesPortfolioConstructor(
        max_lots=strategy.max_lots,
        max_capital_usage=strategy.max_capital_usage,
        margin_config=strategy.margin_config,
        log_context=strategy.strategy_name,
    )


def test_futures_matches_calculate_position_size(
    futures_strategy: MomentumFuturesStrategy,
) -> None:
    """`MomentumFuturesStrategy` 的開倉單：新舊路徑逐筆相同"""

    quotes: List[FuturesQuote] = [make_futures_quote("202403")]

    legacy: List[BaseOrder] = futures_strategy.calculate_position_size(
        quotes, Action.OPEN
    )

    signals: List[Signal] = [
        Signal(
            quote=quote,
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=quote.close,
        )
        for quote in quotes
    ]
    built: List[BaseOrder] = futures_constructor(futures_strategy).build(
        signals, futures_strategy.account
    )

    assert order_fields(built) == order_fields(legacy)
    assert built, "3,000,000 的帳戶在比率模式下開得出口數"


def test_futures_stops_at_remaining_lots(
    futures_strategy: MomentumFuturesStrategy,
) -> None:
    """剩餘口數用完就停，逐筆遞減的行為與舊路徑一致"""

    futures_strategy.max_lots = 1

    quotes: List[FuturesQuote] = [
        make_futures_quote("202403"),
        make_futures_quote("202406"),
    ]

    legacy: List[BaseOrder] = futures_strategy.calculate_position_size(
        quotes, Action.OPEN
    )

    signals: List[Signal] = [
        Signal(
            quote=quote,
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=quote.close,
        )
        for quote in quotes
    ]
    built: List[BaseOrder] = futures_constructor(futures_strategy).build(
        signals, futures_strategy.account
    )

    assert order_fields(built) == order_fields(legacy)
    assert len(built) == 1


def test_futures_zero_max_lots_opens_nothing(
    futures_strategy: MomentumFuturesStrategy,
) -> None:
    """`max_lots` 為 0 表示不開倉"""

    futures_strategy.max_lots = 0

    signals: List[Signal] = [
        Signal(
            quote=make_futures_quote("202403"),
            action=Action.BUY,
            position_type=PositionType.LONG,
            order_price=18000.0,
        )
    ]

    assert (
        futures_constructor(futures_strategy).build(signals, futures_strategy.account)
        == []
    )
