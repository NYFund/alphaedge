import datetime
from typing import List, Optional

import pytest

from core.backtest.models.cost_model import (
    FuturesCostConfig,
    StockCostModel,
    TwFuturesCostModel,
)
from core.backtest.models.fill_model import FuturesFillConfig, TwFuturesFillModel
from core.backtest.models.instrument_spec import TwFuturesSpec
from core.managers.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.models import FuturesAccount, FuturesOrder, FuturesTradeRecord
from core.utils import Action, FuturesCost, PositionType
from core.utils.constant import FUTURES_MULTIPLIER, FUTURES_TICK_SIZE

"""
台期貨交易成本測試

**期貨成本與股票沒有一項可以共用**，本檔逐一釘住——每一項都不會報錯，
只會讓績效靜默偏掉：

1. **期交稅買賣各課一次**（證交稅只課賣出）：複用股票那套會讓開倉少收一次稅。
2. **稅基是契約價值**（價格 × 乘數 × 口數），不是成交金額。
3. **手續費是每口固定金額**，沒有費率、折扣，也沒有最低收費。
4. **滑價以跳動點表達**，不是基點——同一個基點數在不同價位是不同的檔數。
5. **大台與小台要能分開設**，用同一個數字會低估小台的成本。
"""

INIT_CAPITAL: float = 10_000_000
MULTIPLIER: int = FUTURES_MULTIPLIER["TX"]
DAY_1: datetime.date = datetime.date(2026, 1, 5)
DAY_2: datetime.date = datetime.date(2026, 1, 6)


@pytest.fixture
def cost_model() -> TwFuturesCostModel:
    """預設費率的成本模型（期交稅法規值 ＋ 每口手續費市場常見值）"""

    return TwFuturesCostModel()


def make_order(
    action: Action,
    position_type: PositionType,
    price: float,
    volume: int = 1,
    date: datetime.date = DAY_1,
    product: str = "TX",
    expiry: str = "202601",
) -> FuturesOrder:
    """組一張期貨訂單"""

    return FuturesOrder(
        product=product,
        expiry=expiry,
        date=date,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
    )


# === 期交稅 ===
def test_tax_is_charged_on_both_sides(cost_model: TwFuturesCostModel) -> None:
    """
    **期交稅買賣各課一次**

    股票的 `tax()` 在買進恆為 0；期貨兩邊都課，這正是「不可複用證交稅」的核心。
    """

    buy_tax: int = cost_model.tax(20000, 1, MULTIPLIER)
    sell_tax: int = cost_model.tax(20000, 1, MULTIPLIER)

    assert buy_tax == sell_tax > 0
    # 對照組：同樣的參數在股票模型上，買進是 0
    assert StockCostModel().tax(20000, 1, action=Action.BUY) == 0


def test_tax_base_is_contract_value(cost_model: TwFuturesCostModel) -> None:
    """稅基是契約價值：20,000 點 × 乘數 200 × 十萬分之二 ＝ 80 元／口"""

    assert cost_model.tax(20000, 1, MULTIPLIER) == 80
    assert cost_model.tax(20000, 3, MULTIPLIER) == 240
    # 乘數不同稅就不同——沒帶乘數會少收 200 倍
    assert cost_model.tax(20000, 1, FUTURES_MULTIPLIER["MTX"]) == 20


def test_tax_rate_is_the_statutory_value(cost_model: TwFuturesCostModel) -> None:
    """稅率取自 `FuturesCost.TaxRate`（法規值十萬分之二），不寫死在模型裡"""

    assert cost_model.config.tax_rate == float(FuturesCost.TaxRate) == 0.00002


# === 手續費 ===
def test_commission_is_per_lot_and_price_independent(
    cost_model: TwFuturesCostModel,
) -> None:
    """
    手續費是**每口固定金額**：與價格無關，也沒有最低收費

    股票是「費率 × 金額 × 折扣，未達 20 元以 20 元計」，兩者沒有一項相同。
    """

    assert cost_model.commission(volume=2) == cost_model.commission(
        price=99999, volume=2
    )
    assert cost_model.commission(volume=2) == 2 * float(FuturesCost.CommissionPerLot)
    assert cost_model.commission(volume=0) == 0


def test_commission_can_be_set_per_product() -> None:
    """
    大台與小台要能分開設

    小型契約的實務行情明顯低於大台，用同一個數字會高估小台的成本。
    """

    config: FuturesCostConfig = FuturesCostConfig(
        commission_per_lot=50.0,
        commission_per_lot_by_product={"MTX": 30.0},
    )
    model: TwFuturesCostModel = TwFuturesCostModel(config)

    assert model.commission(volume=1, product="TX") == 50
    assert model.commission(volume=1, product="MTX") == 30
    # 未列的商品沿用共用值，不會靜默變成 0
    assert model.commission(volume=1, product="TE") == 50


# === 來回成本與損益兩平 ===
def test_round_trip_cost_counts_four_items(cost_model: TwFuturesCostModel) -> None:
    """一口來回 ＝ 開倉手續費 ＋ 開倉稅 ＋ 平倉手續費 ＋ 平倉稅"""

    assert cost_model.round_trip_cost(20000, 20000, 1, MULTIPLIER) == 50 + 80 + 50 + 80


def test_breakeven_points_translates_cost_into_index_points(
    cost_model: TwFuturesCostModel,
) -> None:
    """
    成本換算成「幾點」才看得出訊號夠不夠強

    TX 在 20,000 點的來回成本 260 元／口，換算 1.3 點——
    低於這個幅度的訊號一律是負期望值。
    """

    assert cost_model.breakeven_points(20000, 1, MULTIPLIER) == 1.3
    # 小台乘數只有 50，同樣的手續費換算出的點數更高（成本佔比更重）
    assert cost_model.breakeven_points(20000, 1, FUTURES_MULTIPLIER["MTX"]) > 1.3


# === 與部位管理層的接線 ===
def test_position_manager_uses_the_cost_model() -> None:
    """
    **費率只有一份**：部位管理器不自己算，一律問 CostModel

    兩處各算一份必然漂移——這是成本收斂到 model 的理由。
    """

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    model: TwFuturesCostModel = TwFuturesCostModel(
        FuturesCostConfig(commission_per_lot=77.0, tax_rate=0.0)
    )
    manager: FuturesPositionManager = FuturesPositionManager(
        account, cost_model=model, margin_config=FuturesMarginConfig()
    )

    assert manager.cost_config is model.config
    assert manager.calculate_commission(2, "TX") == 154


def test_costs_are_deducted_from_realized_pnl() -> None:
    """整筆交易的損益要扣掉四項費用（開平倉各一次手續費與稅）"""

    account: FuturesAccount = FuturesAccount(init_capital=INIT_CAPITAL)
    manager: FuturesPositionManager = FuturesPositionManager(
        account, margin_config=FuturesMarginConfig(initial_margin_ratio=0.1)
    )

    manager.open_position(make_order(Action.BUY, PositionType.LONG, price=18000))
    records: List[FuturesTradeRecord] = manager.close_position(
        make_order(Action.SELL, PositionType.LONG, price=18100, date=DAY_2)
    )

    expected_cost: float = 50 + 72 + 50 + 72  # 18,000／18,100 的稅取整後皆為 72
    assert records[0].transaction_cost == expected_cost
    assert records[0].realized_pnl == (18100 - 18000) * MULTIPLIER - expected_cost


# === 滑價（跳動點）===
def test_slippage_in_ticks_moves_price_against_the_order() -> None:
    """滑價以跳動點計，方向一律對下單者不利"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=2, slippage_ticks_sell=2)
    )

    buy: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=18000)
    sell: FuturesOrder = make_order(Action.SELL, PositionType.SHORT, price=18000)

    assert fill_model.get_filled_price(buy) == 18002.0
    assert fill_model.get_filled_price(sell) == 17998.0


def test_slippage_ticks_can_be_set_per_product() -> None:
    """大台與小台的流動性不同，滑價要能分開設"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(
            slippage_ticks_buy=1,
            slippage_ticks_by_product={"MTX": 3},
        )
    )

    tx: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=18000)
    mtx: FuturesOrder = make_order(
        Action.BUY, PositionType.LONG, price=18000, product="MTX"
    )

    assert fill_model.get_filled_price(tx) == 18001.0
    assert fill_model.get_filled_price(mtx) == 18003.0


def test_tick_slippage_wins_over_bps() -> None:
    """兩種都設時以跳動點為準（期貨的價差本來就以檔數報價）"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=1, slippage_bps_buy=100)
    )

    order: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=18000)

    assert fill_model.get_filled_price(order) == 18001.0  # 不是 18180（100 bps）


def test_slippage_respects_the_contract_tick_size() -> None:
    """跳動點大小取自 `InstrumentSpec`，不是寫死 1 點"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        instrument=TwFuturesSpec(tick_size=0.05),
        config=FuturesFillConfig(slippage_ticks_buy=2),
    )

    order: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=100.0)

    assert fill_model.get_filled_price(order) == 100.10


def test_no_slippage_returns_the_original_price() -> None:
    """未設定滑價時原價回傳，行為與導入前逐筆相同"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel()
    order: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=18000)

    assert fill_model.get_filled_price(order) == 18000.0


# === 跳動點逐商品查表 ===
@pytest.mark.parametrize("product", sorted(FUTURES_TICK_SIZE))
def test_one_tick_of_slippage_equals_one_tick_of_that_product(product: str) -> None:
    """
    每個已登錄商品在 `slippage_ticks=1` 下的價差等於它自己的一個跳動點

    寫死 1 點的話，TE 的一檔（0.05 點）會被當成 20 檔——滑價靜默偏 20 倍，
    而且不會有任何徵兆。
    """

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=1, slippage_ticks_sell=1)
    )
    tick: float = FUTURES_TICK_SIZE[product]

    buy: FuturesOrder = make_order(
        Action.BUY, PositionType.LONG, price=18000.0, product=product
    )
    sell: FuturesOrder = make_order(
        Action.SELL, PositionType.SHORT, price=18000.0, product=product
    )

    assert fill_model.get_filled_price(buy) == pytest.approx(18000.0 + tick)
    assert fill_model.get_filled_price(sell) == pytest.approx(18000.0 - tick)


def test_unregistered_product_raises_on_lookup() -> None:
    """
    未登錄的商品查表時 `KeyError`——**這是刻意的**

    規矩與 `FUTURES_MULTIPLIER` 相同：給預設值等於讓猜錯的跳動點靜默生效，
    而中斷至少擋得住。XIF 的乘數曾經變更過，跳動點同樣未查證。
    """

    with pytest.raises(KeyError):
        FUTURES_TICK_SIZE["XIF"]


def test_unregistered_product_falls_back_instead_of_breaking_the_backtest() -> None:
    """
    成交模型遇到未登錄商品退回預設值並警告，不讓回測整場中斷

    跳動點只在「對齊檔位」與「以檔數表達的滑價」兩處生效；為此炸掉一場
    本來跑得起來的回測不成比例，但那一次退回要被看見（見 `get_tick_size()`）。
    """

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(slippage_ticks_buy=1)
    )
    order: FuturesOrder = make_order(
        Action.BUY, PositionType.LONG, price=18000.0, product="XIF"
    )

    assert fill_model.get_filled_price(order) == 18001.0


def test_explicit_tick_size_overrides_the_lookup() -> None:
    """建構時明確指定的跳動點蓋過查表（留給尚未登錄的商品與單一契約的測試）"""

    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        instrument=TwFuturesSpec(tick_size=0.05),
        config=FuturesFillConfig(slippage_ticks_buy=2),
    )
    order: FuturesOrder = make_order(Action.BUY, PositionType.LONG, price=18000.0)

    assert fill_model.get_filled_price(order) == 18000.10


def test_tick_table_matches_the_multiplier_table() -> None:
    """
    兩張表登錄的商品必須一致，且每跳金額算得出來

    跳動點 × 乘數 ＝ TAIFEX 規格頁標示的每跳金額；漏改一張表會讓兩者對不上。
    """

    assert set(FUTURES_TICK_SIZE) == set(FUTURES_MULTIPLIER)
    assert FUTURES_TICK_SIZE["TE"] * FUTURES_MULTIPLIER["TE"] == pytest.approx(200.0)
    assert FUTURES_TICK_SIZE["ZFF"] * FUTURES_MULTIPLIER["ZFF"] == pytest.approx(50.0)


# === 期貨的 fill_config 型別 ===
def test_base_fill_config_is_rejected_for_futures() -> None:
    """
    期貨策略給基底 `FillConfig` 時當場中斷（D6）

    型別標註擋不住這件事：寫成 `FillConfig(slippage_bps_buy=10)` 照樣跑得完，
    只是 `get_slippage_ticks()` 回 0、整組假設默默換成基點。警告在批次跑參數
    掃描時會被洗掉，等於沒擋。
    """

    from core.backtest.backtester import Backtester
    from core.backtest.factory import build_backtester
    from core.backtest.models.fill_model import FillConfig
    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    original_setup = Backtester.setup
    Backtester.setup = lambda self: None
    try:
        strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
        strategy.fill_config = FillConfig(slippage_bps_buy=10.0)

        with pytest.raises(TypeError, match="FuturesFillConfig"):
            build_backtester(strategy)
    finally:
        Backtester.setup = original_setup


@pytest.mark.parametrize("fill_config", [None, FuturesFillConfig(slippage_ticks_buy=1)])
def test_futures_fill_config_is_accepted(fill_config) -> None:
    """`None` 與 `FuturesFillConfig` 都照常組裝（防止防呆擋過頭）"""

    from core.backtest.backtester import Backtester
    from core.backtest.factory import build_backtester
    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    original_setup = Backtester.setup
    Backtester.setup = lambda self: None
    try:
        strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
        strategy.fill_config = fill_config
        backtester: Backtester = build_backtester(strategy)
    finally:
        Backtester.setup = original_setup

    # 強制出場與轉倉走的是與引擎同一個 fill_model
    assert backtester.settlement.fill_model is backtester.fill_model


# === 策略層可覆寫 ===
def test_strategy_cost_config_flows_into_the_backtester() -> None:
    """策略宣告的費率要能傳到部位管理層，否則設定形同無效"""

    from core.backtest.backtester import Backtester
    from core.backtest.factory import build_backtester
    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    original_setup = Backtester.setup
    Backtester.setup = lambda self: None
    try:
        strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
        strategy.cost_config = FuturesCostConfig(commission_per_lot=13.0)
        backtester: Backtester = build_backtester(strategy)
    finally:
        Backtester.setup = original_setup

    assert backtester.cost_model.config.commission_per_lot == 13.0
    assert backtester.position_manager.cost_config is backtester.cost_model.config


def test_default_strategy_uses_real_rates() -> None:
    """
    示範策略沒有覆寫費率時，走的是**帶費用的預設值**

    零成本是驗證接線用的口徑（`FuturesCostConfig.free()`），不該是預設。
    """

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    config: Optional[FuturesCostConfig] = strategy.cost_config or (
        FuturesCostConfig.default()
    )

    assert config.tax_rate > 0
    assert config.commission_per_lot > 0
