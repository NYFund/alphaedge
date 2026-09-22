import datetime
from typing import List, Optional

import pandas as pd
import pytest

from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.models import FuturesAccount, FuturesQuote
from core.portfolio.signal import Signal
from core.strategies.futures import BaseFuturesStrategy
from core.strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from core.strategies.strategy_loader import StrategyLoader
from core.utils import Action, FuturesSession, InstrumentType, Market, PositionType
from tests.conftest import build_futures_quote

"""
台期貨策略層的介面測試

**期貨策略與股票策略的四個根本差異**，本檔逐一釘住——每一個都會讓沿用股票
習慣的人寫錯，而且不會報錯：

1. **一天不只一個報價**：同一天同一商品有多個到期月，策略必須自己挑契約。
2. **數量單位是口，且由保證金決定**，不是用契約價值除資金
   （TX 一口契約價值 900 萬、保證金只有 70 萬，用錯會低估可開口數十倍以上）。
3. **日盤與夜盤是兩筆獨立行情**，不過濾會讓訊號被算兩次。
4. 沒有股票的信用交易設定。

不連網路、不碰正式的 tw_futures.db。
"""

DATE: datetime.date = datetime.date(2026, 8, 26)
MULTIPLIER: int = 200


def make_quote(
    expiry: str,
    close: float = 18000,
    product: str = "TX",
    session: FuturesSession = FuturesSession.DAY,
) -> FuturesQuote:
    """組一筆期貨報價；OHLC 與 scale 留原值（本檔的測試只看 cur_price 與 close）"""

    return build_futures_quote(
        product=product,
        expiry=expiry,
        date=DATE,
        close=close,
        open=0.0,
        high=0.0,
        low=0.0,
        session=session,
        scale=None,
        multiplier=MULTIPLIER,
    )


class StubMarginAPI:
    """固定回傳每口保證金的假 API"""

    def __init__(self, per_lot: Optional[int]) -> None:
        self.per_lot = per_lot

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        return self.per_lot

    def get_covered_date_range(self, product):
        return {"earliest": "2020-03-13", "latest": "2026-08-12"}


def open_signals(
    strategy: MomentumFuturesStrategy, quotes: List[FuturesQuote]
) -> List[Signal]:
    """把報價直接當成開倉候選；驗的是口數換算，不是選契約的條件"""

    return [
        Signal(
            quote=quote,
            action=Action.BUY,
            position_type=strategy.position_type,
            order_price=quote.close,
        )
        for quote in quotes
    ]


@pytest.fixture
def strategy() -> MomentumFuturesStrategy:
    """已載入帳戶的示範策略"""

    instance = MomentumFuturesStrategy()
    instance.setup_account(FuturesAccount(init_capital=3_000_000))
    return instance


# === 載入 ===
def test_futures_strategy_is_auto_loaded() -> None:
    """
    新增 `core/strategies/futures/` 就會被自動收錄

    `StrategyLoader` 逐一掃描所有商品類別子套件，**不需要 `load_futures_strategies()`**
    ——backlog 原本規劃的那個方法在命名軸線收斂之後已經不需要了。
    """

    strategies = StrategyLoader.load_strategies()

    assert "MomentumFuturesStrategy" in strategies
    assert issubclass(strategies["MomentumFuturesStrategy"], BaseFuturesStrategy)


def test_abstract_base_is_not_loaded_as_a_strategy() -> None:
    """`BaseFuturesStrategy` 是抽象基底，不可被當成可執行策略收錄"""

    assert "BaseFuturesStrategy" not in StrategyLoader.load_strategies()


def test_axes_are_declared_for_factory_dispatch(
    strategy: MomentumFuturesStrategy,
) -> None:
    """兩條軸都要填，`factory` 以 `(market, instrument_type)` 分派"""

    assert strategy.market == Market.TW
    assert strategy.instrument_type == InstrumentType.FUTURE


# === 一天不只一個報價 ===
def test_near_month_is_picked_from_all_contracts(
    strategy: MomentumFuturesStrategy,
) -> None:
    """從當日所有到期月挑最近的一個"""

    quotes: List[FuturesQuote] = [
        make_quote("202612"),
        make_quote("202609"),
        make_quote("202610"),
    ]

    assert strategy.select_near_month(quotes, "TX").expiry == "202609"


def test_near_month_ignores_other_products(
    strategy: MomentumFuturesStrategy,
) -> None:
    """挑契約時只看指定商品，不可跨商品比到期月"""

    quotes: List[FuturesQuote] = [
        make_quote("202609", product="MTX"),
        make_quote("202610", product="TX"),
    ]

    assert strategy.select_near_month(quotes, "TX").expiry == "202610"


def test_near_month_returns_none_when_absent(
    strategy: MomentumFuturesStrategy,
) -> None:
    """該商品當日無報價時回 None，不可拋錯"""

    assert (
        strategy.select_near_month([make_quote("202609", product="MTX")], "TX") is None
    )


# === 日夜盤是兩筆獨立行情 ===
def test_night_session_quotes_are_filtered_out(
    strategy: MomentumFuturesStrategy,
) -> None:
    """
    不過濾時段的話同一契約一天會出現兩筆，訊號被算兩次

    這是期貨與股票最容易被忽略的差異之一。
    """

    quotes: List[FuturesQuote] = [
        make_quote("202609", session=FuturesSession.DAY),
        make_quote("202609", session=FuturesSession.NIGHT),
    ]

    filtered = strategy.filter_session(quotes)

    assert len(filtered) == 1
    assert filtered[0].session == FuturesSession.DAY


# === 口數由保證金決定 ===
def test_max_lots_is_driven_by_margin_not_contract_value(
    strategy: MomentumFuturesStrategy,
) -> None:
    """
    可開口數 = 可動用餘額 × 資金使用上限 ÷ **每口保證金**

    用契約價值算會嚴重低估：TX 一口契約價值 18000×200 = 360 萬，
    而保證金只有 70 萬。
    """

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=700000))
    quote: FuturesQuote = make_quote("202609")

    # 3,000,000 × 0.5 = 1,500,000 ÷ 700,000 = 2 口
    assert strategy.calculate_max_lots(quote) == 2
    # 若誤用契約價值（360 萬）只會算出 0 口
    assert strategy.calculate_max_lots(quote) != 0


def test_ratio_mode_matches_the_position_manager(
    strategy: MomentumFuturesStrategy,
) -> None:
    """
    沒有 API 時退回「契約價值 × 比率」，與 `FuturesPositionManager` 一致

    兩處若不一致，策略算出的口數會開不進去（或開得太少）。
    """

    quote: FuturesQuote = make_quote("202609", close=18000)

    assert strategy.get_margin_per_lot(quote) == 18000 * MULTIPLIER * 0.1


def test_unavailable_margin_yields_zero_lots(
    strategy: MomentumFuturesStrategy,
) -> None:
    """
    查表模式下查不到保證金時開 0 口

    策略層選擇不開倉而非拋錯——真正會 raise 的是部位管理層，
    那才是「已經決定要開倉卻算不出保證金」的地方。
    """

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=None))

    assert strategy.calculate_max_lots(make_quote("202609")) == 0


def test_capital_usage_caps_the_budget(strategy: MomentumFuturesStrategy) -> None:
    """
    單次開倉最多動用一定比例的可動用餘額

    保證金交易若不設限，一次就能把帳戶壓到追繳邊緣。
    """

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))
    strategy.max_capital_usage = 1.0

    assert strategy.calculate_max_lots(make_quote("202609")) == 30

    strategy.max_capital_usage = 0.5
    assert strategy.calculate_max_lots(make_quote("202609")) == 15


# === 下單口數 ===
def test_position_size_is_capped_by_max_lots(
    strategy: MomentumFuturesStrategy,
) -> None:
    """保證金允許再多，也不可超過策略宣告的總口數上限"""

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))
    strategy.max_lots = 3

    orders = strategy.make_portfolio_constructor().build(
        open_signals(strategy, [make_quote("202609")]), strategy.account
    )

    assert len(orders) == 1
    assert orders[0].volume == 3  # 保證金允許 15 口，被 max_lots 壓到 3


def test_orders_carry_contract_identity(strategy: MomentumFuturesStrategy) -> None:
    """訂單要帶得出商品與到期月，否則下游對不回契約"""

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))
    orders = strategy.make_portfolio_constructor().build(
        open_signals(strategy, [make_quote("202609")]), strategy.account
    )

    assert orders[0].product == "TX"
    assert orders[0].expiry == "202609"
    assert orders[0].contract_id == "TX202609"
    assert orders[0].action == Action.BUY
    assert orders[0].position_type == PositionType.LONG


def test_no_orders_when_max_lots_is_zero(strategy: MomentumFuturesStrategy) -> None:
    """口數上限為 0 時一律不開倉"""

    strategy.max_lots = 0

    assert strategy.check_open_signal([make_quote("202609")]) == []


# === 開平倉訊號 ===
class StubPriceAPI:
    """回傳固定收盤序列的假行情 API"""

    def __init__(self, closes: List[float]) -> None:
        self.closes = closes

    def get_close_series(self, product, expiry, start_date, end_date, session=None):
        return pd.Series(self.closes)


def test_open_signal_needs_momentum(strategy: MomentumFuturesStrategy) -> None:
    """漲幅未達門檻時不開倉"""

    strategy.futures_price = StubPriceAPI([18000, 18050])  # +0.28%
    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))

    assert strategy.check_open_signal([make_quote("202609", close=18050)]) == []


def test_open_signal_fires_on_momentum(strategy: MomentumFuturesStrategy) -> None:
    """漲幅達門檻（預設 1%）時開倉"""

    strategy.futures_price = StubPriceAPI([18000, 18400])  # +2.2%
    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))

    orders = strategy.check_open_signal([make_quote("202609", close=18400)])

    assert len(orders) == 1
    assert orders[0].volume == strategy.max_lots


def test_open_signal_skips_when_already_holding(
    strategy: MomentumFuturesStrategy,
) -> None:
    """本策略不加碼：已有該商品部位就不再開"""

    from core.managers.futures.position_manager import FuturesPositionManager
    from core.models import FuturesOrder

    strategy.futures_price = StubPriceAPI([18000, 18400])
    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))
    manager = FuturesPositionManager(
        strategy.account, margin_config=strategy.margin_config
    )
    manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202609",
            date=DATE,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=18000,
            volume=1,
        )
    )

    assert strategy.check_open_signal([make_quote("202609", close=18400)]) == []


def test_close_signal_respects_minimum_holding_days(
    strategy: MomentumFuturesStrategy,
) -> None:
    """持有未滿門檻天數不平倉"""

    from core.managers.futures.position_manager import FuturesPositionManager
    from core.models import FuturesOrder

    strategy.margin_config = FuturesMarginConfig(api=StubMarginAPI(per_lot=100000))
    manager = FuturesPositionManager(
        strategy.account, margin_config=strategy.margin_config
    )
    manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202609",
            date=DATE,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=18000,
            volume=1,
        )
    )

    # 同一天 → 不平倉
    assert strategy.check_close_signal([make_quote("202609")]) == []

    # 隔一天 → 平倉
    next_day_quote: FuturesQuote = make_quote("202609")
    next_day_quote.date = DATE + datetime.timedelta(days=1)
    orders = strategy.check_close_signal([next_day_quote])

    assert len(orders) == 1
    assert orders[0].action == Action.SELL
    assert orders[0].volume == 1


def test_stop_loss_is_not_implemented(strategy: MomentumFuturesStrategy) -> None:
    """示範策略未實作停損，一律回傳空 list"""

    assert strategy.check_stop_loss_signal([make_quote("202609")]) == []
