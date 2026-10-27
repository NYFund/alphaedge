import datetime
from typing import Dict, List

from core.backtest.models.event_counts import EVENT_KEYS, FUTURES_EVENT_KEYS
from core.backtest.models.fill_model import (
    BaseFillModel,
    TwFuturesFillModel,
    TwStockFillModel,
)
from core.market.tw.instrument_spec import TwFuturesSpec, TwStockSpec
from core.models import FuturesOrder, StockOrder
from core.models.fill_config import FillConfig, FuturesFillConfig
from core.utils import Action, PositionType
from tests.conftest import build_futures_quote, build_stock_quote

"""
成交價模型的對稱性：`validate()` 與 `fill()` 只有基底一份，兩個市場只實作鉤子

原本兩個子類各寫一份 `fill()`（後半段逐行相同）與 `validate()`（區間檢查與檔位警告重複），
基底沒有建構子、預設 `fill()` 不套滑價；查規格的鍵台股傳 `symbol`、期貨傳 `product`。
"""

DATE: datetime.date = datetime.date(2026, 1, 5)


def stock_order(price: float = 100.0) -> StockOrder:
    """一張 2330 的買進開倉單"""

    return StockOrder(
        stock_id="2330",
        date=DATE,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=price,
        volume=1,
    )


def futures_order(price: float = 18000.0) -> FuturesOrder:
    """一口 TX202601 的買進開倉單"""

    return FuturesOrder(
        product="TX",
        expiry="202601",
        date=DATE,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=price,
        volume=1,
    )


# === 樣板只有基底一份 ===
def test_templates_live_only_in_the_base() -> None:
    """子類不可再覆寫 `validate()`／`fill()`／`__init__` 之外的共用流程；鉤子各自提供"""

    for name in (
        "validate",
        "fill",
        "check_order_eligibility",
        "validate_market_rules",
    ):
        assert name in BaseFillModel.__dict__
    for model_cls in (TwStockFillModel, TwFuturesFillModel):
        assert "validate" not in model_cls.__dict__, (
            f"{model_cls.__name__} 覆寫了 validate()"
        )
        assert "fill" not in model_cls.__dict__, f"{model_cls.__name__} 覆寫了 fill()"
        assert "get_filled_price" in model_cls.__dict__

    # 台股有制度擋板、期貨沒有：期貨沿用基底的「一律通過」
    assert "check_order_eligibility" in TwStockFillModel.__dict__
    assert "validate_market_rules" in TwStockFillModel.__dict__
    assert "check_order_eligibility" not in TwFuturesFillModel.__dict__
    assert "validate_market_rules" not in TwFuturesFillModel.__dict__


def test_base_constructor_prepares_the_shared_state() -> None:
    """四個共用屬性由基底建構子備妥；自備計數器時各用各市場的 key 清單、設定各用各的類別"""

    stock: TwStockFillModel = TwStockFillModel()
    futures: TwFuturesFillModel = TwFuturesFillModel()

    assert isinstance(stock.instrument, TwStockSpec)
    assert isinstance(futures.instrument, TwFuturesSpec)
    assert type(stock.config) is FillConfig
    assert type(futures.config) is FuturesFillConfig
    assert list(stock.event_counts) == list(EVENT_KEYS)
    assert list(futures.event_counts) == list(FUTURES_EVENT_KEYS)
    for model in (stock, futures):
        assert model.intraday_range == {} and model.reference_prices == {}

    shared: Dict[str, int] = {"rejected_fill_price": 0}
    assert TwFuturesFillModel(event_counts=shared).event_counts is shared


# === fill：同一條流程，滑價各用各的單位 ===
def test_fill_applies_each_markets_slippage_through_the_same_template() -> None:
    """台股以基點、期貨以跳動點：都由基底的 `fill()` 套用，副本帶 `reference_price`"""

    stock: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=100.0)
    )
    filled: StockOrder = stock.fill(
        stock_order(100.0), build_stock_quote(cur_price=100.0)
    )
    assert filled.price == 101.0
    assert filled.reference_price == 100.0

    futures: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=2.0)
    )
    f_filled: FuturesOrder = futures.fill(
        futures_order(18000.0), build_futures_quote(expiry="202601", close=18000.0)
    )
    assert f_filled.price == 18002.0
    assert f_filled.reference_price == 18000.0


def test_fill_returns_the_same_object_when_nothing_applies() -> None:
    """未啟用任何假設時兩個市場都回傳原物件（既有回測結果不變的保證）"""

    order: StockOrder = stock_order()
    assert TwStockFillModel().fill(order, build_stock_quote()) is order

    f_order: FuturesOrder = futures_order()
    assert (
        TwFuturesFillModel().fill(f_order, build_futures_quote(expiry="202601"))
        is f_order
    )


def test_eligibility_hook_runs_before_pricing() -> None:
    """制度擋板擋下的單不會走到滑價與成交量上限（兩個市場同一個順序）"""

    class RejectingStock(TwStockFillModel):
        """一律不合格"""

        def check_order_eligibility(self, order) -> bool:
            return False

    model: RejectingStock = RejectingStock(config=FillConfig(slippage_bps_buy=100.0))
    priced: List[float] = []
    model.get_filled_price = lambda order: priced.append(order.price) or order.price

    assert model.fill(stock_order(), build_stock_quote()) is None
    assert priced == []


# === validate：同一條流程，市場規則各自插入 ===
def test_validate_runs_market_rules_after_the_range_check() -> None:
    """區間檢查擋下的單不會問市場規則；通過區間的單才交給 `validate_market_rules()`"""

    asked: List[float] = []

    class SpyStock(TwStockFillModel):
        """記下哪些單走到了市場規則"""

        def validate_market_rules(self, order, quote) -> bool:
            asked.append(order.price)
            return True

    model: SpyStock = SpyStock()
    quote = build_stock_quote(cur_price=100.0, high=101.0, low=99.0)

    assert model.validate(stock_order(105.0), quote) is False
    assert model.validate(stock_order(100.0), quote) is True
    assert asked == [100.0]


def test_stock_market_rules_still_reject_limit_and_lock() -> None:
    """台股的漲跌停與全日鎖死搬到鉤子後行為不變"""

    model: TwStockFillModel = TwStockFillModel()
    model.update_reference_prices([build_stock_quote(cur_price=100.0)])

    # 區間本身允許 115，但超出前收 +10%
    wide = build_stock_quote(cur_price=115.0, high=115.0, low=100.0)
    assert model.validate(stock_order(115.0), wide) is False
    assert model.event_counts["rejected_fill_price"] == 1

    # 開高低收都是漲停價 110：全日鎖漲停，買進開倉拒單
    locked = build_stock_quote(cur_price=110.0, high=110.0, low=110.0)
    locked.open = 110.0
    assert model.validate(stock_order(110.0), locked) is False
    assert model.event_counts["rejected_limit_up_locked"] == 1


def test_futures_has_no_market_rules_but_keeps_the_range_guard() -> None:
    """期貨不查漲跌停（較前收 +80% 仍成交），但超出當根 bar 區間照樣拒"""

    model: TwFuturesFillModel = TwFuturesFillModel()
    model.update_reference_prices([build_futures_quote(expiry="202601", close=10000.0)])
    quote = build_futures_quote(
        expiry="202601", close=18000.0, high=18050.0, low=17950.0
    )

    assert model.validate(futures_order(18000.0), quote) is True
    assert model.validate(futures_order(18100.0), quote) is False
    assert model.event_counts["rejected_fill_price"] == 1


# === 規格鍵由 InstrumentSpec 從訂單取 ===
def test_spec_resolves_the_product_key_from_the_order() -> None:
    """台股的鍵是代號、期貨的鍵是商品代碼；成交價模型不再各自決定傳哪個欄位"""

    assert TwStockSpec().resolve_product(stock_order()) == "2330"
    assert TwFuturesSpec().resolve_product(futures_order()) == "TX"

    # 期貨的跳動點滑價因此查得到商品：TE 的跳動點是 0.05，不是 TX 的 1
    model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=1.0)
    )
    te_order: FuturesOrder = FuturesOrder(
        product="TE",
        expiry="202601",
        date=DATE,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=1000.0,
        volume=1,
    )
    assert model.get_filled_price(te_order) == 1000.05
