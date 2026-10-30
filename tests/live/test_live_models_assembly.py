import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.context import StrategyContext
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.factory import build_live_trader, make_trade_cost_estimator
from core.live.intraday.session_guard import uncovered_day_trade_positions
from core.live.trader import LiveTrader
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import FuturesAccount, RealizedTradeSnapshot, StockAccount, StockOrder
from core.models.cost_config import CostConfig
from core.position.assembly import (
    TwFuturesModels,
    TwStockModels,
    build_cost_config,
    build_tw_futures_models,
    build_tw_stock_models,
)
from core.strategies.base import BaseStrategy
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    InstrumentType,
    LiveHook,
    Market,
    PositionType,
    ShortMethod,
    TradeDirection,
)

from .conftest import FakeBroker

"""
實盤與回測共用同一份記帳 model 組法

以前實盤 factory 一律用 `CostConfig.default()`：當沖策略的空單在實盤記成融券、
`is_day_trade` 恆為 False，日終當沖回補認不出它們，而且不會有任何錯誤訊息。
這裡釘住三件事：同一支策略兩邊組出同一份成本設定、委託前處理補上放空欄位、
期貨的保證金模型與乘數 resolver 在實盤也接上。
"""

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
TODAY: datetime.date = datetime.date(2026, 9, 22)


class DayTradeShortStrategy(BaseStrategy):
    """最小的台股當沖放空策略：只有實盤替身會有的欄位，沒有 `BaseStockStrategy` 的"""

    def __init__(self) -> None:
        super().__init__()
        self.market = Market.TW
        self.instrument_type = InstrumentType.STOCK
        self.init_capital = 1_000_000.0
        self.live_ready = True
        self.direction = TradeDirection.SHORT
        self.allow_day_trade = True
        self.execution_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        self.live_execution = ExecutionStyle.MARKET
        self.symbols: List[str] = ["2330"]

    def setup_account(self, account: Any) -> None:
        self.account = account


class LiveFuturesStrategy(DayTradeShortStrategy):
    """最小的台期貨實盤策略"""

    def __init__(self) -> None:
        super().__init__()
        self.instrument_type = InstrumentType.FUTURE
        self.direction = TradeDirection.LONG
        self.allow_day_trade = False


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """歷史資料庫換成空的 in-memory 連線：本檔只驗組裝，紅綠不能取決於機器上有沒有資料庫"""

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr("core.datafeed.base.connect_sqlite", connect_in_memory)


def build(strategies: List[BaseStrategy], dao: LiveTradeDAO) -> LiveTrader:
    """以替身券商組出實盤交易器"""

    return build_live_trader(
        strategies,
        broker=FakeBroker(),
        dao=dao,
        run_id="20260922133000",
        now_provider=lambda: datetime.datetime(2026, 9, 22, 13, 30),
    )


# === 台股：成本設定 ===
def test_live_context_derives_cost_config_the_same_way_as_backtest(
    dao: LiveTradeDAO,
) -> None:
    """
    同一支策略，實盤脈絡的成本設定與回測 factory 推出的那一份相同

    以前實盤一律 `CostConfig.default()`：放空管道是融券、不當沖。
    """

    context: StrategyContext = build([DayTradeShortStrategy()], dao).contexts[0]

    assert isinstance(context.cost_model, TwStockCostModel)
    assert context.cost_model.config == build_cost_config(DayTradeShortStrategy())
    assert context.cost_model.config.short_method is ShortMethod.DAY_TRADE
    assert context.cost_model.config.is_day_trade is True
    # 委託前處理與本地帳記帳用的是同一個模型，兩邊的費率不可能分岔
    assert context.position_manager.cost_model is context.cost_model


def test_build_cost_config_tolerates_strategies_without_stock_fields() -> None:
    """只繼承 `BaseStrategy` 的替身沒有 `short_method`／`short_constraint`，照台股基底預設處理"""

    bare: Any = SimpleNamespace(allow_day_trade=False)
    config: CostConfig = build_cost_config(bare)

    assert config.short_method is ShortMethod.MARGIN
    assert config.is_day_trade is False
    assert config.short_constraint == CostConfig.default().short_constraint
    assert config.backtest_start_date is None


def test_build_tw_stock_models_wires_one_cost_model_and_the_shared_counts() -> None:
    """部位管理層用的成本模型就是回傳的那一個，事件計數用呼叫端給的那個 dict"""

    counts: Dict[str, int] = {}
    models: TwStockModels = build_tw_stock_models(
        DayTradeShortStrategy(), StockAccount(init_capital=1_000_000.0), counts
    )

    assert models.cost_model.config is models.cost_config
    assert models.position_manager.cost_model is models.cost_model
    assert models.position_manager.event_counts is counts
    assert models.cost_config.short_method is ShortMethod.DAY_TRADE


def test_build_tw_futures_models_writes_margin_objects_back_to_the_strategy() -> None:
    """
    保證金設定與模型回寫給策略，部位管理層與兩個 resolver 都接同一份

    策略層的 sizer 與部位管理層用不同物件的話，口數與應繳保證金就會各算各的。
    """

    strategy: Any = SimpleNamespace(cost_config=None, margin_config=None)
    counts: Dict[str, int] = {}

    def multiplier(product: str, date: datetime.date) -> int:
        return 200

    def underlying(product: str, date: datetime.date) -> Optional[float]:
        return 100.0

    models: TwFuturesModels = build_tw_futures_models(
        strategy,
        FuturesAccount(init_capital=1_000_000.0),
        multiplier_resolver=multiplier,
        underlying_price_resolver=underlying,
        event_counts=counts,
    )

    assert strategy.margin_config is models.margin_config
    assert strategy.margin_model is models.margin_model
    assert models.margin_model.config is models.margin_config
    assert models.position_manager.margin_model is models.margin_model
    assert models.position_manager.cost_model is models.cost_model
    assert models.cost_model.config is models.cost_config
    assert models.position_manager.multiplier_resolver is multiplier
    assert models.margin_model.underlying_price_resolver is underlying
    assert models.position_manager.event_counts is counts


# === 台股：委託前處理補值 ===
@pytest.mark.parametrize(
    "stage, action",
    [("open", Action.SELL), ("close", Action.BUY)],
)
def test_preprocess_enriches_short_orders_from_the_strategy_cost_config(
    dao: LiveTradeDAO, stage: str, action: Action
) -> None:
    """
    SHORT 委託經前處理後帶上放空管道與當沖旗標，開倉與回補都補

    少了這一步，SHORT 委託到券商轉換層才因 `short_method` 為 None 被拒，
    當沖空單的部位也不會被日終回補認出來。
    """

    trader: LiveTrader = build([DayTradeShortStrategy()], dao)
    context: StrategyContext = trader.contexts[0]
    order: StockOrder = StockOrder(
        stock_id="2330",
        action=action,
        position_type=PositionType.SHORT,
        price=1000.0,
        volume=1,
    )

    processed: List[Any] = trader.signals._preprocess(context, [order], stage)

    assert len(processed) == 1
    assert processed[0].short_method is ShortMethod.DAY_TRADE
    assert processed[0].is_day_trade is True


def test_enriched_short_opens_a_day_trade_position_the_guard_can_find(
    dao: LiveTradeDAO,
) -> None:
    """
    補值後開出的空單部位 `is_day_trade` 為真，日終當沖回補才認得它

    以前實盤 SHORT 部位的 `is_day_trade` 恆為 False，
    `uncovered_day_trade_positions()` 永遠回空，強制回補等於沒有作用。
    """

    trader: LiveTrader = build([DayTradeShortStrategy()], dao)
    context: StrategyContext = trader.contexts[0]
    order: StockOrder = StockOrder(
        stock_id="2330",
        date=datetime.datetime(2026, 9, 22, 9, 5),
        action=Action.SELL,
        position_type=PositionType.SHORT,
        price=1000.0,
        volume=1,
    )

    processed: List[Any] = trader.signals._preprocess(context, [order], "open")
    position: Any = context.position_manager.open_position(processed[0])

    assert position is not None and position.is_day_trade is True
    assert uncovered_day_trade_positions(context.account) == [position]


def test_preprocess_leaves_orders_alone_without_a_cost_model(dao: LiveTradeDAO) -> None:
    """純記憶體測試的脈絡沒有成本模型：不補值，也不能因此出錯"""

    trader: LiveTrader = build([DayTradeShortStrategy()], dao)
    context: StrategyContext = trader.contexts[0]
    context.cost_model = None
    order: StockOrder = StockOrder(
        stock_id="2330", action=Action.SELL, position_type=PositionType.SHORT, volume=1
    )

    processed: List[Any] = trader.signals._preprocess(context, [order], "open")

    assert processed[0].short_method is None
    assert processed[0].is_day_trade is False


# === 台期貨：保證金模型與乘數 ===
def test_futures_context_shares_one_margin_model_with_the_strategy(
    dao: LiveTradeDAO,
) -> None:
    """
    策略層的 sizer 與部位管理層用同一個保證金模型與同一個設定物件

    不同物件的話，策略算得出口數、部位管理層卻開不進去，而且不會有任何錯誤訊息。
    """

    strategy: LiveFuturesStrategy = LiveFuturesStrategy()
    context: StrategyContext = build([strategy], dao).contexts[0]

    assert context.position_manager.margin_model is strategy.margin_model
    assert context.position_manager.margin_config is strategy.margin_config
    assert isinstance(context.cost_model, TwFuturesCostModel)
    assert context.position_manager.cost_model is context.cost_model


def test_futures_context_resolves_multipliers_through_the_live_feed(
    dao: LiveTradeDAO,
) -> None:
    """
    部位管理層的乘數由實盤資料源解析，與回測同一個契約

    以前實盤沒注入 resolver，部位管理層退回常數表；股期不在表內，第一筆開倉就 KeyError。
    """

    context: StrategyContext = build([LiveFuturesStrategy()], dao).contexts[0]
    feed: Any = context.data_feed

    assert context.position_manager.multiplier_resolver == feed.resolve_multiplier
    assert feed.resolve_multiplier("TX", TODAY) == 200
    # 假券商沒有合約檔：登錄表查不到就要拋，猜一個值會讓保證金與 PnL 靜默偏掉
    with pytest.raises(KeyError):
        feed.resolve_multiplier("ZZZ", TODAY)


def test_live_feed_falls_back_to_the_broker_contract_for_unlisted_products() -> None:
    """登錄表沒有的商品改查券商合約檔的契約單位"""

    class _Resolver:
        def list_index_futures_expiries(self, product: str) -> List[str]:
            return ["202612"] if product == "ZZZ" else []

        def resolve_index_futures(self, product: str, expiry: str) -> Any:
            return SimpleNamespace(code="ZZZ202612", multiplier=2000)

    broker: FakeBroker = FakeBroker()
    broker.resolver = _Resolver()
    feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
        broker,
        now_provider=lambda: datetime.datetime(2026, 9, 22, 13, 30),
        margin_config=FuturesMarginConfig.default(),
        roll_config=FuturesRollConfig(),
    )

    assert feed.resolve_multiplier("ZZZ", TODAY) == 2000
    with pytest.raises(KeyError):
        feed.resolve_multiplier("YYY", TODAY)


# === 盤後成本校正 ===
def test_cost_estimator_uses_the_strategy_cost_model(dao: LiveTradeDAO) -> None:
    """
    校正估算器用實盤記帳的那一個成本模型，不是另一份預設

    策略自訂費率時，用預設費率去比對券商數字，校正出來的偏差其實是兩份設定的差。
    """

    custom: CostConfig = CostConfig.default()
    custom.comm_discount = 1.0  # 不打折，與預設折扣分得開
    strategy: DayTradeShortStrategy = DayTradeShortStrategy()
    strategy.cost_config = custom
    trader: LiveTrader = build([strategy], dao)
    model: Any = trader.contexts[0].cost_model
    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="2330", quantity=1, pnl=0.0, cover_price=2475.0, open_seqno="S1"
    )
    opening: List[Dict[str, Any]] = [
        {
            "action": "Buy",
            "price": 2480.0,
            "volume": 1,
            "filled_at": "2026-09-22T12:00:39+08:00",
        }
    ]

    # 走 trader 實際持有的估算器：factory 要把脈絡傳給它，不是測試自己傳
    estimated: Optional[float] = trader.after_close.cost_estimator(
        trade, opening, TODAY
    )

    expected: int = (
        model.commission(2480.0, 1)
        + model.commission(2475.0, 1)
        + model.tax(2475.0, 1, Action.SELL, is_day_trade=True, date=TODAY)
    )
    assert model.config.comm_discount == 1.0
    assert estimated == float(expected)
    assert estimated != make_trade_cost_estimator()(trade, opening, TODAY)


# === 守門：兩個 factory 都走共用組裝 ===
@pytest.mark.parametrize("path", ["core/backtest/factory.py", "core/live/factory.py"])
def test_both_factories_assemble_models_through_the_shared_builders(path: str) -> None:
    """
    回測與實盤 factory 都呼叫 `build_tw_stock_models()`／`build_tw_futures_models()`

    任何一邊自己 new 部位管理層或保證金模型，兩邊的組法就會再次分岔。
    """

    source: str = (PROJECT_ROOT / path).read_text(encoding="utf-8")

    assert "build_tw_stock_models(" in source
    assert "build_tw_futures_models(" in source
    for forbidden in (
        "StockPositionManager(",
        "FuturesPositionManager(",
        "FuturesMarginModel(",
    ):
        assert forbidden not in source, f"{path} 自己建了 {forbidden}"
