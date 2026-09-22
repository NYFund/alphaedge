import datetime
from typing import Dict, Optional

from core.backtest.models.fill_model import (
    FillConfig,
    TwStockFillModel,
    VolumeCapPolicy,
)
from core.backtest.models.instrument_spec import TwStockSpec
from core.models import StockOrder, StockQuote
from core.utils import Action, PositionType, Scale, ShortMethod

"""
成交假設測試：滑價、成交量上限、券源檢核

三項預設皆為關閉；本檔第一組測試即驗證「未啟用時 `fill()` 回傳的是**原物件本身**」，
這是既有回測結果不變的保證——只要回傳原物件，下游就不可能拿到被改過的價量。
"""

STOCK_ID: str = "2330"
DATE: datetime.date = datetime.date(2024, 6, 13)


def make_order(
    action: Action = Action.BUY,
    position_type: PositionType = PositionType.LONG,
    price: float = 100.0,
    volume: int = 10,
    short_method: Optional[ShortMethod] = None,
    is_day_trade: bool = False,
) -> StockOrder:
    """建立測試用訂單（short_method 與 is_day_trade 平時由引擎補值，預設留空）"""

    return StockOrder(
        stock_id=STOCK_ID,
        date=DATE,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
        short_method=short_method,
        is_day_trade=is_day_trade,
    )


def make_quote(volume: int = 10_000, scale: Scale = Scale.DAY) -> StockQuote:
    """建立測試用報價（volume 單位為張）"""

    return StockQuote(
        stock_id=STOCK_ID,
        scale=scale,
        date=DATE,
        cur_price=100.0,
        volume=volume,
        open=100.0,
        high=105.0,
        low=95.0,
        close=100.0,
    )


def make_event_counts() -> Dict[str, int]:
    """與引擎共用的事件計數器（只取本檔會用到的那幾個 key）"""

    return {
        "rejected_fill_price": 0,
        "rejected_no_borrow": 0,
        "rejected_volume_cap": 0,
        "truncated_by_volume": 0,
        "rejected_limit_up_locked": 0,
        "rejected_limit_down_locked": 0,
    }


# === 預設關閉：行為零改變 ===
def test_default_config_returns_the_same_object() -> None:
    """未啟用任何假設時回傳原物件本身，下游不可能拿到被改過的價量"""

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=make_event_counts())
    order: StockOrder = make_order()

    filled = fill_model.fill(order, make_quote())

    assert filled is order


# === 滑價 ===
def test_slippage_moves_price_against_the_trader() -> None:
    """買進加價、賣出減價——兩者都是對下單者不利的方向"""

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=50.0, slippage_bps_sell=50.0),
        event_counts=make_event_counts(),
    )

    buy = fill_model.fill(make_order(action=Action.BUY), make_quote())
    sell = fill_model.fill(make_order(action=Action.SELL), make_quote())

    assert buy.price > 100.0
    assert sell.price < 100.0


def test_slippage_does_not_mutate_the_original_order() -> None:
    """絕不就地修改策略持有的 order——否則策略下一根 bar 會看到被引擎改過的價"""

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=50.0),
        event_counts=make_event_counts(),
    )
    order: StockOrder = make_order(action=Action.BUY, price=100.0)

    filled = fill_model.fill(order, make_quote())

    assert order.price == 100.0
    assert filled is not order
    assert filled.price != order.price


def test_slippage_result_is_tick_aligned() -> None:
    """調整後須對齊檔位，否則會算出不可能成交的價格"""

    spec: TwStockSpec = TwStockSpec()
    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=50.0), event_counts=make_event_counts()
    )

    filled = fill_model.fill(make_order(action=Action.BUY, price=100.0), make_quote())

    assert spec.round_to_tick(filled.price, "nearest") == filled.price


def test_zero_slippage_is_a_no_op() -> None:
    """係數為 0 時原價回傳，這是既有回歸得以逐筆相同的前提"""

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=0.0, slippage_bps_sell=0.0),
        event_counts=make_event_counts(),
    )

    for action in (Action.BUY, Action.SELL):
        assert fill_model.fill(make_order(action=action), make_quote()).price == 100.0


# === 成交量上限 ===
def test_volume_cap_truncates() -> None:
    """超過當日成交量上限時縮量，並計入 truncated_by_volume"""

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(max_volume_share=0.1), event_counts=counts
    )

    # 當日成交量 100 張、上限 10%，下 50 張應被縮到 10 張
    filled = fill_model.fill(make_order(volume=50), make_quote(volume=100))

    assert filled.volume == 10
    assert counts["truncated_by_volume"] == 1


def test_volume_cap_rejects_when_policy_is_reject() -> None:
    """政策設為拒單時整張退回，並計入 rejected_volume_cap"""

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(
            max_volume_share=0.1, volume_cap_policy=VolumeCapPolicy.REJECT
        ),
        event_counts=counts,
    )

    assert fill_model.fill(make_order(volume=50), make_quote(volume=100)) is None
    assert counts["rejected_volume_cap"] == 1


def test_volume_cap_rejects_when_cap_is_under_one_lot() -> None:
    """上限不足一張時不可縮成 0 張成交，須拒單"""

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(max_volume_share=0.1), event_counts=counts
    )

    assert fill_model.fill(make_order(volume=5), make_quote(volume=5)) is None
    assert counts["rejected_volume_cap"] == 1


def test_volume_cap_allows_orders_within_limit() -> None:
    """未超量時原封不動放行"""

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(max_volume_share=0.1), event_counts=make_event_counts()
    )
    order: StockOrder = make_order(volume=5)

    assert fill_model.fill(order, make_quote(volume=100)) is order


def test_volume_cap_skips_tick_scale() -> None:
    """
    TICK 級別不套用本檢查

    `quote.volume` 在 DAY 是當日總量、在 TICK 是單筆成交量，
    以單筆量當分母沒有意義（累計量檢查屬後續工作）
    """

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(max_volume_share=0.1), event_counts=make_event_counts()
    )
    order: StockOrder = make_order(volume=50)

    assert fill_model.fill(order, make_quote(volume=100, scale=Scale.TICK)) is order


# === 券源檢核 ===
def test_borrow_check_rejects_when_balance_is_insufficient() -> None:
    """融券餘額不足時拒絕放空開倉"""

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=counts, check_borrowable=True
    )
    fill_model.apply_short_balance({STOCK_ID: 3})

    order: StockOrder = make_order(
        action=Action.SELL, position_type=PositionType.SHORT, volume=10
    )

    assert fill_model.fill(order, make_quote()) is None
    assert counts["rejected_no_borrow"] == 1


def test_borrow_check_passes_when_balance_is_enough() -> None:
    """餘額足夠時放行"""

    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=make_event_counts(), check_borrowable=True
    )
    fill_model.apply_short_balance({STOCK_ID: 100})

    order: StockOrder = make_order(
        action=Action.SELL, position_type=PositionType.SHORT, volume=10
    )

    assert fill_model.fill(order, make_quote()) is order


def test_borrow_check_passes_when_data_is_missing() -> None:
    """
    查無融券資料時**放行**，不可當成「借不到券」

    `margin` 表的歷史回補是獨立作業，尚未執行時整場回測都會查無資料；
    若預設拒單，放空策略會一張單都開不出來卻找不出原因
    """

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=counts, check_borrowable=True
    )
    fill_model.apply_short_balance({})

    order: StockOrder = make_order(
        action=Action.SELL, position_type=PositionType.SHORT, volume=10
    )

    assert fill_model.fill(order, make_quote()) is order
    assert counts["rejected_no_borrow"] == 0


def test_borrow_check_only_applies_to_short_open() -> None:
    """做多賣出與放空回補都不需要券源，不得被擋"""

    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=make_event_counts(), check_borrowable=True
    )
    fill_model.apply_short_balance({STOCK_ID: 0})

    long_sell: StockOrder = make_order(
        action=Action.SELL, position_type=PositionType.LONG, volume=10
    )
    short_cover: StockOrder = make_order(
        action=Action.BUY, position_type=PositionType.SHORT, volume=10
    )

    assert fill_model.fill(long_sell, make_quote()) is long_sell
    assert fill_model.fill(short_cover, make_quote()) is short_cover


def test_borrow_check_skips_day_trade_short() -> None:
    """現股當沖沖賣不需券源，餘額為 0 也要放行"""

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=counts, check_borrowable=True
    )
    fill_model.apply_short_balance({STOCK_ID: 0})

    order: StockOrder = make_order(
        action=Action.SELL,
        position_type=PositionType.SHORT,
        volume=10,
        short_method=ShortMethod.DAY_TRADE,
        is_day_trade=True,
    )

    assert fill_model.fill(order, make_quote()) is order
    assert counts["rejected_no_borrow"] == 0


def test_borrow_check_still_applies_to_margin_day_trade() -> None:
    """
    融券當沖仍要檢核券源

    融券賣出後當日買回的 `is_day_trade` 同樣是 True，但它確實借了券；
    豁免條件只能看 `short_method`，看 `is_day_trade` 會連融券當沖一起放掉
    """

    counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(
        event_counts=counts, check_borrowable=True
    )
    fill_model.apply_short_balance({STOCK_ID: 0})

    order: StockOrder = make_order(
        action=Action.SELL,
        position_type=PositionType.SHORT,
        volume=10,
        short_method=ShortMethod.MARGIN,
        is_day_trade=True,
    )

    assert fill_model.fill(order, make_quote()) is None
    assert counts["rejected_no_borrow"] == 1


def test_borrow_check_disabled_by_default() -> None:
    """未開啟檢核時，即使餘額為 0 也照常放行"""

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=make_event_counts())
    fill_model.apply_short_balance({STOCK_ID: 0})

    order: StockOrder = make_order(
        action=Action.SELL, position_type=PositionType.SHORT, volume=10
    )

    assert fill_model.fill(order, make_quote()) is order


# === 成交路徑防線===
def test_slippage_pushed_price_is_clamped_back() -> None:
    """
    滑價把價格推出當日區間時要夾回

    `validate()` 跑在 `fill()` **之前**，於是滑價之後沒有任何檢查——
    一筆本來在區間內的單，加了滑價就可能成交在當日根本沒出現過的價位。
    """

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)
    quote: StockQuote = make_quote()  # 區間 [95, 105]

    order: StockOrder = make_order(price=110.0)
    clamped = fill_model.clamp_filled_price(order, quote)

    assert clamped.price == 105.0
    assert event_counts["fill_price_clamped"] == 1


def test_price_inside_range_is_untouched() -> None:
    """區間內的價格不動，也不計數"""

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)

    order: StockOrder = make_order(price=100.0)
    result = fill_model.clamp_filled_price(order, make_quote())

    assert result is order
    assert result.price == 100.0
    assert "fill_price_clamped" not in event_counts


def test_clamping_does_not_mutate_the_caller_order() -> None:
    """
    夾價改的是副本，傳入的訂單不被動到

    理由與 `fill()` 的「絕不就地修改傳入的 order」相同：策略可能持有同一個物件。
    今天夾價只會發生在 `fill()` 產生的副本上（因為 `validate()` 先擋掉了區間外
    的單），但那是呼叫順序的巧合，不該當成保證。
    """

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=make_event_counts())
    order: StockOrder = make_order(price=110.0)

    clamped = fill_model.clamp_filled_price(order, make_quote())  # 區間 [95, 105]

    assert clamped is not order
    assert clamped.price == 105.0
    assert order.price == 110.0


def test_close_leg_only_warns_and_counts() -> None:
    """
    平倉腿超出區間只計數，不改價也不拒單

    拒掉一張平倉單會讓部位被迫留倉，那是比價格偏一點嚴重得多的失真。
    """

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)

    order: StockOrder = make_order(price=110.0)
    fill_model.warn_close_price_out_of_range(order, make_quote())

    assert order.price == 110.0, "平倉價不可被改動"
    assert event_counts["close_price_out_of_range"] == 1


def test_no_volume_day_is_rejected() -> None:
    """
    當日無成交量一律拒單

    無成交日的 `high`／`low` 都是 0，`get_price_range()` 回 `(None, None)`
    而**跳過**區間檢查——策略因此能在整天無量的標的上以任意價格成交。
    """

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)

    quote: StockQuote = StockQuote(
        stock_id=STOCK_ID,
        scale=Scale.DAY,
        date=DATE,
        cur_price=0.0,
        volume=0,
        open=0.0,
        high=0.0,
        low=0.0,
        close=0.0,
    )

    assert not fill_model.validate(make_order(price=100.0), quote)
    assert event_counts["rejected_fill_price"] == 1


def test_normal_day_still_passes() -> None:
    """有量的正常交易日不受影響"""

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=make_event_counts())

    assert fill_model.validate(make_order(price=100.0), make_quote())


def test_tick_scale_is_never_rejected_for_volume() -> None:
    """TICK 的每一筆本來就是成交，不套用「當日無量」的判斷"""

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=make_event_counts())
    quote: StockQuote = make_quote(volume=0, scale=Scale.TICK)

    assert fill_model.has_tradable_quote(quote)


def test_tick_quote_carries_a_price() -> None:
    """
    tick 路徑的 `cur_price`／`close`／`volume` 必須帶出來

    舊版只掛 `tick=tick_quote`，OHLC 與 cur_price 全部留在預設值 0.0，
    於是任何讀 `quote.close` 的地方都拿到 0 元。
    """

    from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter

    class _Tick:
        stock_id = STOCK_ID
        time = DATE
        close = 601.0
        volume = 3
        bid_price = 600.0
        bid_volume = 1
        ask_price = 602.0
        ask_volume = 2
        tick_type = 1

    quote: StockQuote = StockQuoteAdapter.generate_stock_quote(
        _Tick(), STOCK_ID, DATE, Scale.TICK
    )

    assert quote.close == 601.0
    assert quote.cur_price == 601.0
    assert quote.volume == 3


# === 全日鎖漲跌停 ===
def make_locked_quote(
    open_: float, high: float, low: float, close: float
) -> StockQuote:
    """建立全日鎖住（或未鎖住）的報價"""

    quote: StockQuote = make_quote()
    quote.open, quote.high, quote.low, quote.close = open_, high, low, close
    quote.cur_price = close
    return quote


def test_buy_open_is_rejected_when_locked_at_limit_up() -> None:
    """
    前收 100、開高低收皆 110（全日鎖漲停）時，買進開倉被拒

    整天沒有人願意在漲停以下賣出，實務上排隊也買不到。`MomentumStrategy1`
    的訊號正是「當日漲幅 ≥ 9%、以收盤價買進」，這類標的大量是鎖漲停，
    照常成交會讓做多績效系統性偏樂觀且沒有任何徵兆。
    """

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)
    fill_model.prev_close[STOCK_ID] = 100.0

    locked: StockQuote = make_locked_quote(110.0, 110.0, 110.0, 110.0)

    assert fill_model.validate(make_order(price=110.0), locked) is False
    assert event_counts["rejected_limit_up_locked"] == 1


def test_short_open_is_rejected_when_locked_at_limit_down() -> None:
    """對稱情形：全日鎖跌停時賣不掉，放空開倉同樣被拒"""

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)
    fill_model.prev_close[STOCK_ID] = 100.0

    locked: StockQuote = make_locked_quote(90.0, 90.0, 90.0, 90.0)
    order = make_order(action=Action.SELL, position_type=PositionType.SHORT, price=90.0)

    assert fill_model.validate(order, locked) is False
    assert event_counts["rejected_limit_down_locked"] == 1


def test_touching_the_limit_without_locking_is_allowed() -> None:
    """
    盤中觸及漲停但曾經打開過，照常成交（防止改過頭）

    最低價低於漲停價就代表有人在漲停以下賣出過，那是買得到的。
    """

    event_counts: Dict[str, int] = make_event_counts()
    fill_model: TwStockFillModel = TwStockFillModel(event_counts=event_counts)
    fill_model.prev_close[STOCK_ID] = 100.0

    touched: StockQuote = make_locked_quote(105.0, 110.0, 104.0, 110.0)

    assert fill_model.validate(make_order(price=110.0), touched) is True
    assert event_counts["rejected_limit_up_locked"] == 0


# === 單獨建模型（不由引擎注入計數器）===
def test_standalone_fill_model_counts_volume_cap_rejections() -> None:
    """
    `event_counts=None` 時的自備計數器要涵蓋全部 key

    那條路徑的註解寫著「供單獨測試」，但 fallback 只塞了一個
    `rejected_fill_price`——一走到成交量上限就 `KeyError`，
    也就是說這條為測試保留的路徑本身是壞的。
    """

    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(
            max_volume_share=0.001, volume_cap_policy=VolumeCapPolicy.REJECT
        )
    )

    # 委託 10 張、當日量 10,000 張 × 0.1% ＝ 上限 10 張…改用更小的量觸發拒單
    assert fill_model.get_filled_volume(make_order(volume=50), make_quote()) is None
    assert fill_model.event_counts["rejected_volume_cap"] == 1


def test_standalone_futures_fill_model_counts_volume_cap_rejections() -> None:
    """期貨側同樣的路徑；它在三處直接索引 key，缺一個就整條崩"""

    from core.backtest.models.fill_model import FuturesFillConfig, TwFuturesFillModel
    from core.models import FuturesOrder, FuturesQuote

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(
            max_volume_share=0.001, volume_cap_policy=VolumeCapPolicy.REJECT
        )
    )
    order: FuturesOrder = FuturesOrder(
        product="TX",
        expiry="202610",
        date=DATE,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=20000.0,
        volume=50,
    )
    quote: FuturesQuote = FuturesQuote(
        product="TX",
        expiry="202610",
        scale=Scale.DAY,
        date=DATE,
        cur_price=20000.0,
        volume=10_000,
        open=20000.0,
        high=20100.0,
        low=19900.0,
        close=20000.0,
    )

    assert fill_model.get_filled_volume(order, quote) is None
    assert fill_model.event_counts["rejected_volume_cap"] == 1
