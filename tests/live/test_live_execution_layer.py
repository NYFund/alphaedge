import datetime
from typing import Any, List, Optional, Tuple

import pytest

from core.broker.tw.shioaji_order_mapper import ShioajiOrderMapper
from core.live.execution.base import ExecutionUnavailableError
from core.live.execution.futures import FuturesExecutionModel
from core.live.execution.stock import StockExecutionModel
from core.live.trader import mark_execution
from core.models import BaseOrder, BaseQuote, FuturesOrder, PendingAction, StockOrder
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    FuturesPriceType,
    LiveHook,
    OrderType,
    PositionType,
    StockPriceType,
)

from .test_live_trader_day import (
    NOW,
    TODAY,
    Harness,
    ScriptedStrategy,
    make_order,
    seed_holding,
)

"""
實盤執行層：策略只說要不要成交，價格類型與委託價由執行層依段落換算

本檔的委託一律**不帶 `price_type`**，與真實策略產出的委託同形狀。既有測試的委託
先前都預先填好限價，那正是「沒有任何程式填 `price_type`」這個缺口一直沒被抓到的原因——
演練第一次送單，每一張都在券商轉換層才失敗。
"""

LIMITS: Tuple[Optional[float], Optional[float]] = (110.0, 90.0)


def stock_order(action: Action = Action.BUY, price: float = 100.0) -> StockOrder:
    """與策略產出的委託同形狀：沒有價格類型"""

    return StockOrder(
        stock_id="2330",
        date=TODAY,
        action=action,
        position_type=PositionType.LONG,
        price=price,
        volume=1,
    )


def futures_order(action: Action = Action.BUY) -> FuturesOrder:
    return FuturesOrder(
        product="TX",
        expiry="202610",
        date=TODAY,
        action=action,
        position_type=PositionType.LONG,
        price=48000.0,
        volume=1,
    )


def marked(
    order: BaseOrder, timing: ExecutionTiming, style: ExecutionStyle
) -> BaseOrder:
    return mark_execution([order], timing, style)[0]


# === 股票：集合競價 ===
@pytest.mark.parametrize("timing", [ExecutionTiming.AT_OPEN, ExecutionTiming.AT_CLOSE])
@pytest.mark.parametrize("action, expected", [(Action.BUY, 110.0), (Action.SELL, 90.0)])
def test_auction_market_order_rests_at_the_price_limit(
    timing: ExecutionTiming, action: Action, expected: float
) -> None:
    """
    集合競價的要成交：買掛漲停、賣掛跌停，限價＋ROD

    集合競價以單一價格撮合，掛漲停只是確保排得進去，成交價仍是競價結果；
    集合競價時段不收市價單，所以送的是限價。
    """

    order: BaseOrder = marked(stock_order(action), timing, ExecutionStyle.MARKET)

    StockExecutionModel().apply(order, LIMITS)

    assert order.price == expected
    assert order.price_type is StockPriceType.LMT
    assert order.order_type is OrderType.ROD
    # 決策價保留策略給的價；事後比對執行品質要看它，不是看漲停價
    assert order.decision_price == 100.0


@pytest.mark.parametrize(
    "limits", [(None, None), (None, 90.0)], ids=["both-missing", "limit-up-missing"]
)
def test_auction_market_order_without_limits_is_skipped(
    limits: Tuple[Optional[float], Optional[float]],
) -> None:
    """取不到漲跌停就略過，不自行推算：除權息日的基準價另行公告，推算會整段偏移"""

    order: BaseOrder = marked(
        stock_order(Action.BUY), ExecutionTiming.AT_CLOSE, ExecutionStyle.MARKET
    )

    with pytest.raises(ExecutionUnavailableError, match="漲停"):
        StockExecutionModel().apply(order, limits)


# === 股票：照價掛單 ===
@pytest.mark.parametrize(
    "timing",
    [ExecutionTiming.AT_OPEN, ExecutionTiming.AT_CLOSE, ExecutionTiming.IMMEDIATE],
)
def test_limit_style_keeps_the_strategy_price(timing: ExecutionTiming) -> None:
    """照價掛單：任何段落都是策略給的價，限價＋ROD，也不需要漲跌停"""

    order: BaseOrder = marked(stock_order(price=95.5), timing, ExecutionStyle.LIMIT)

    StockExecutionModel().apply(order, (None, None))

    assert order.price == 95.5
    assert order.price_type is StockPriceType.LMT
    assert order.order_type is OrderType.ROD


# === 股票：連續交易 ===
@pytest.mark.parametrize(
    "action, decision, expected",
    [
        (Action.BUY, 100.0, 102.0),
        (Action.SELL, 100.0, 98.0),
        # 51.3 × 1.02 = 52.326，0.05 檔位買單往下對齊 → 52.3
        (Action.BUY, 51.3, 52.3),
        # 51.3 × 0.98 = 50.274，0.05 檔位賣單往上對齊 → 50.3
        (Action.SELL, 51.3, 50.3),
    ],
)
def test_continuous_market_order_uses_a_protection_price(
    action: Action, decision: float, expected: float
) -> None:
    """連續交易的要成交：決策價 ± 2%、對齊檔位，限價＋IOC（不送市價單）"""

    order: BaseOrder = marked(
        stock_order(action, decision), ExecutionTiming.IMMEDIATE, ExecutionStyle.MARKET
    )

    StockExecutionModel().apply(order, (200.0, 10.0))

    assert order.price == pytest.approx(expected)
    assert order.price_type is StockPriceType.LMT
    assert order.order_type is OrderType.IOC
    assert order.decision_price == decision


@pytest.mark.parametrize(
    "action, decision, expected",
    [(Action.BUY, 109.0, 110.0), (Action.SELL, 91.0, 90.0)],
)
def test_protection_price_is_clamped_within_the_price_limits(
    action: Action, decision: float, expected: float
) -> None:
    """保護價超出漲跌停會被交易所退單，夾在區間內"""

    order: BaseOrder = marked(
        stock_order(action, decision), ExecutionTiming.IMMEDIATE, ExecutionStyle.MARKET
    )

    StockExecutionModel().apply(order, LIMITS)

    assert order.price == expected


def test_continuous_market_order_without_limits_is_skipped() -> None:
    """夾不了漲跌停的保護價可能被退單，與集合競價同樣略過"""

    order: BaseOrder = marked(
        stock_order(), ExecutionTiming.IMMEDIATE, ExecutionStyle.MARKET
    )

    with pytest.raises(ExecutionUnavailableError):
        StockExecutionModel().apply(order, (None, None))


# === 期貨 ===
@pytest.mark.parametrize("timing", list(ExecutionTiming))
def test_futures_market_order_is_a_ranged_market_ioc(timing: ExecutionTiming) -> None:
    """
    期貨的兩個段落都在連續交易時段：要成交送範圍市價＋IOC

    期交所不收「市價＋ROD」；範圍市價本身附保護範圍，不需要漲跌停。
    """

    order: BaseOrder = marked(futures_order(), timing, ExecutionStyle.MARKET)

    FuturesExecutionModel().apply(order, (None, None))

    assert order.price_type is FuturesPriceType.MKP
    assert order.order_type is OrderType.IOC
    assert order.decision_price == 48000.0


def test_futures_limit_style_keeps_the_strategy_price() -> None:
    order: BaseOrder = marked(
        futures_order(), ExecutionTiming.AT_CLOSE, ExecutionStyle.LIMIT
    )

    FuturesExecutionModel().apply(order, (None, None))

    assert (order.price, order.price_type, order.order_type) == (
        48000.0,
        FuturesPriceType.LMT,
        OrderType.ROD,
    )


# === 未標註 ===
def test_unmarked_order_is_refused_not_guessed() -> None:
    """沒標註執行方式代表某條送單路徑漏接了執行層；猜一個值等於把漏洞蓋掉"""

    order: StockOrder = stock_order()
    order.timing = ExecutionTiming.AT_CLOSE

    with pytest.raises(ValueError, match="沒有執行方式"):
        StockExecutionModel().apply(order, LIMITS)


def test_order_without_timing_is_refused() -> None:
    order: StockOrder = stock_order()
    order.execution_style = ExecutionStyle.MARKET

    with pytest.raises(ValueError, match="沒有執行段落"):
        StockExecutionModel().apply(order, LIMITS)


# === 到券商轉換層為止 ===
def test_strategy_shaped_order_reaches_the_broker_mapper() -> None:
    """
    與真實策略同形狀的委託，經過執行層後能轉成券商委託

    演練第一次送單就是在這一步失敗：委託沒有價格類型，轉換層無從下手。
    """

    order: BaseOrder = marked(
        stock_order(), ExecutionTiming.AT_CLOSE, ExecutionStyle.MARKET
    )
    StockExecutionModel().apply(order, LIMITS)

    broker_order: Any = ShioajiOrderMapper().to_shioaji_stock_order(order)

    assert float(broker_order.price) == 110.0
    assert str(broker_order.price_type.value) == StockPriceType.LMT.value
    assert str(broker_order.order_type.value) == OrderType.ROD.value


# === 引擎：送單路徑 ===
class MarketAlpha(ScriptedStrategy):
    """要成交的策略；開倉一張 2330"""

    def __init__(self) -> None:
        super().__init__("MarketAlpha", [make_order()])
        self.live_execution = ExecutionStyle.MARKET


def sent_orders(harness: Harness) -> List[BaseOrder]:
    return [ticket.order for ticket in harness.broker.tickets.values()]


def events(harness: Harness, category: str) -> List[Tuple[Any, ...]]:
    return harness.dao.conn.execute(
        "SELECT strategy_name, symbol, severity FROM live_risk_event "
        "WHERE category = ?",
        (category,),
    ).fetchall()


def test_segment_sends_the_strategy_order_through_the_execution_layer() -> None:
    """尾盤段的要成交買單：送出的是漲停限價＋ROD，紀錄庫同時留下決策價"""

    harness: Harness = Harness([MarketAlpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    (order,) = sent_orders(harness)
    assert (order.price, order.price_type, order.order_type) == (
        110.0,
        StockPriceType.LMT,
        OrderType.ROD,
    )
    row: Tuple[Any, ...] = harness.dao.conn.execute(
        "SELECT price, price_type, timing, execution_style, decision_price "
        "FROM live_order"
    ).fetchone()
    assert row == (110.0, "LMT", "AT_CLOSE", "MARKET", 100.0)


def test_reservation_uses_the_decision_price() -> None:
    """
    資金保留以決策價計算

    以漲停價保留的話，額度剛好切滿的策略會因為多出的 10% 而保留不到，
    掛漲停只是為了排得進集合競價，成交價仍是競價結果。
    """

    # 一張 100 元 ＝ 10 萬；以漲停價 110 元計算就要 11 萬，超過額度
    harness: Harness = Harness([MarketAlpha()], quota=100_000.0)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1


def test_missing_price_limits_skip_the_order_and_release_the_reservation() -> None:
    """取不到漲跌停：本檔不送、寫事件、放掉保留，其餘照常"""

    harness: Harness = Harness([MarketAlpha()], price_limits=(None, None))
    context: Any = harness.contexts[0]
    harness.trader.prepare()

    order: BaseOrder = marked(
        make_order(), ExecutionTiming.AT_CLOSE, ExecutionStyle.MARKET
    )
    unsent: List[Any] = harness.trader.dispatch([(context, order)], None)

    assert len(unsent) == 1
    assert harness.broker.placed_count == 0
    assert harness.allocator.reserved["MarketAlpha"] == 0.0
    assert events(harness, "EXECUTION_UNAVAILABLE") == [("MarketAlpha", "2330", "WARN")]


def test_unmarked_order_is_blocked_before_the_broker() -> None:
    """
    沒經過送單路徑標註的委託在本地擋下，不退回任何預設

    先前的臨時補值會把這種委託默默送成「限價、照策略價」，掩蓋漏接執行層的路徑。
    """

    harness: Harness = Harness([MarketAlpha()])
    harness.trader.prepare()

    unsent: List[Any] = harness.trader.dispatch(
        [(harness.contexts[0], make_order())], None
    )

    assert len(unsent) == 1
    assert harness.broker.placed_count == 0
    assert events(harness, "EXECUTION_UNAVAILABLE") == [
        ("MarketAlpha", "2330", "CRITICAL")
    ]


def test_context_without_an_execution_model_sends_nothing() -> None:
    harness: Harness = Harness([MarketAlpha()])
    harness.contexts[0].execution_model = None
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert len(events(harness, "EXECUTION_UNAVAILABLE")) == 1


def test_risk_receives_the_price_limits_and_blocks_a_bad_decision_price() -> None:
    """
    風控拿到同一份漲跌停，並以決策價檢查

    之前引擎呼叫風控時只傳基準價，「超出漲跌停」這條檢查從未生效。
    策略把價格算到漲停之外，代表它算錯了，不該靠執行層把價格蓋掉而放行。
    """

    class Wrong(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Wrong", [make_order(price=120.0)])
            self.live_execution = ExecutionStyle.MARKET

    harness: Harness = Harness([Wrong()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert events(harness, "PRICE_LIMIT") == [("Wrong", "2330", "WARN")]


def test_protection_price_does_not_trip_the_deviation_check() -> None:
    """
    偏離檢查比對決策價，不比對掛出去的漲停價

    比對委託價的話，漲 2% 的股票掛漲停就偏離 8%，超過預設 3% 的上限，
    每一張要成交的單都會被擋下。
    """

    harness: Harness = Harness([MarketAlpha()], price_limits=(130.0, 70.0))
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    (order,) = sent_orders(harness)
    assert order.price == 130.0
    assert events(harness, "PRICE_DEVIATION") == []


def test_stop_loss_is_always_market_even_for_a_limit_strategy() -> None:
    """停損固定要成交：出場不該因為價格掛不到而失敗"""

    class Patient(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Patient", [])
            self.live_execution = ExecutionStyle.LIMIT

        def check_stop_loss_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
            return [make_order(action=Action.SELL)]

    harness: Harness = Harness([Patient()])
    seed_holding(harness, "Patient", 1)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    (order,) = sent_orders(harness)
    assert order.execution_style is ExecutionStyle.MARKET
    assert order.price == 90.0


def test_close_hook_follows_the_strategy_style() -> None:
    """一般平倉沿用策略宣告的執行方式"""

    class Patient(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__(
                "Patient", [], close_orders=[make_order(action=Action.SELL)]
            )
            self.live_execution = ExecutionStyle.LIMIT

    harness: Harness = Harness([Patient()])
    seed_holding(harness, "Patient", 1)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    (order,) = sent_orders(harness)
    assert (order.execution_style, order.price) == (ExecutionStyle.LIMIT, 100.0)


def test_pending_cover_is_sent_as_must_fill_in_the_opening_auction() -> None:
    """隔日補平是系統產生的出場單：一律要成交，開盤集合競價賣掛跌停"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [])
            self.live_schedule = {
                LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
                LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
            }

        def build_cover_order(self, action: PendingAction) -> StockOrder:
            return stock_order(Action.SELL)

    harness: Harness = Harness([Alpha()])
    seed_holding(harness, "Alpha", 1)
    harness.dao.insert_pending_action(
        {
            "action_id": "P1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": 1,
            "due_date": TODAY,
            "status": harness.dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )

    harness.trader.run(ExecutionTiming.AT_OPEN)

    (order,) = sent_orders(harness)
    assert (order.timing, order.execution_style) == (
        ExecutionTiming.AT_OPEN,
        ExecutionStyle.MARKET,
    )
    assert (order.price, order.order_type) == (90.0, OrderType.ROD)


def test_day_trade_cover_is_sent_with_a_protection_price() -> None:
    """當沖回補在逐筆交易時段：保護價限價＋IOC"""

    harness: Harness = Harness([MarketAlpha()])
    context: Any = harness.contexts[0]
    harness.trader.prepare()
    cover: BaseOrder = marked(
        make_order(), ExecutionTiming.IMMEDIATE, ExecutionStyle.MARKET
    )

    harness.trader.dispatch([(context, cover)], None)

    (order,) = sent_orders(harness)
    assert (order.price, order.order_type) == (102.0, OrderType.IOC)


def test_mark_execution_overrides_the_strategy_timing() -> None:
    """段落以實際送出的段落為準：標錯段落會送出交易所不收的組合"""

    order: StockOrder = stock_order()
    order.timing = ExecutionTiming.AT_OPEN

    mark_execution([order], ExecutionTiming.AT_CLOSE, ExecutionStyle.LIMIT)

    assert (order.timing, order.execution_style) == (
        ExecutionTiming.AT_CLOSE,
        ExecutionStyle.LIMIT,
    )


def test_now_is_inside_the_closing_auction_window() -> None:
    """本檔的引擎測試假設在尾盤段送單；時間改了，集合競價的斷言就失去意義"""

    assert datetime.time(13, 25) <= NOW.time() <= datetime.time(13, 29)
