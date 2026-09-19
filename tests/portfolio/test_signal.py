import datetime

from core.models import FuturesQuote, StockQuote
from core.portfolio.signal import Signal
from core.utils import Action, FuturesSession, PositionType, Scale

"""Signal 的契約測試

這個型別沒有邏輯，要守的是**欄位語意**：
`volume is None` 分開倉與平倉、`sizing_price` 與 `order_price` 不可混用、
`symbol` 不另存一份。三者一旦被後人「簡化」掉，破的是部位大小與平倉張數，
而回歸不一定會當場告訴你。
"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)


def test_open_signal_leaves_volume_to_portfolio_layer(make_quote) -> None:
    """開倉訊號不帶數量：張數由 portfolio 層算"""

    signal: Signal = Signal(
        quote=make_quote(stock_id="2330", date=DAY_1, cur_price=100.0),
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=100.0,
        sizing_price=100.0,
    )

    assert signal.volume is None


def test_close_signal_carries_volume_from_position(make_quote) -> None:
    """平倉訊號的數量由策略填（取自持倉），不經過 portfolio 層"""

    signal: Signal = Signal(
        quote=make_quote(stock_id="2330", date=DAY_1, cur_price=100.0),
        action=Action.SELL,
        position_type=PositionType.LONG,
        order_price=100.0,
        volume=3,
    )

    assert signal.volume == 3
    # 平倉不算量，故沒有 sizing_price
    assert signal.sizing_price is None


def test_sizing_price_and_order_price_are_independent(make_quote) -> None:
    """算量價與下單價是兩個欄位，給了不同值就要各自保留"""

    quote: StockQuote = make_quote(
        stock_id="2330", date=DAY_1, cur_price=101.0, close=100.0
    )

    signal: Signal = Signal(
        quote=quote,
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=quote.cur_price,
        sizing_price=quote.close,
    )

    assert (signal.sizing_price, signal.order_price) == (100.0, 101.0)


def test_futures_open_signal_has_no_sizing_price() -> None:
    """期貨走保證金約束，開倉訊號不需要算量參考價"""

    quote: FuturesQuote = FuturesQuote(
        product="TX",
        expiry="202403",
        scale=Scale.DAY,
        date=DAY_1,
        session=FuturesSession.DAY,
        cur_price=18000.0,
        close=18000.0,
        multiplier=200,
    )

    signal: Signal = Signal(
        quote=quote,
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=quote.close,
    )

    assert signal.sizing_price is None
    assert signal.volume is None


def test_symbol_follows_quote(make_quote) -> None:
    """symbol 取自報價而非另存一份：改了報價就該跟著改"""

    quote: StockQuote = make_quote(stock_id="2330", date=DAY_1)
    signal: Signal = Signal(
        quote=quote,
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=100.0,
    )

    assert signal.symbol == "2330"

    quote.symbol = "2317"
    assert signal.symbol == "2317"


def test_strength_defaults_to_none(make_quote) -> None:
    """訊號強度目前沒有策略使用，預設為 None"""

    signal: Signal = Signal(
        quote=make_quote(date=DAY_1),
        action=Action.BUY,
        position_type=PositionType.LONG,
        order_price=100.0,
    )

    assert signal.strength is None
