import datetime
from typing import Dict, List, Optional

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.models.event_counts import (
    FUTURES_EVENT_KEYS,
    SHARED_EVENT_KEYS,
    new_event_counts,
)
from core.backtest.models.settlement_model import (
    BaseSettlementModel,
    TwFuturesSettlementModel,
    TwStockSettlementModel,
)
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesQuote,
    StockAccount,
    StockOrder,
    StockPosition,
)
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.position.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.position.stock.position_manager import StockPositionManager
from core.strategies.futures import BaseFuturesStrategy
from core.utils import Action, PositionType, Scale
from tests.conftest import build_futures_quote, build_stock_quote

"""
結算模型的對稱性：無報價計數、持有天數與持有成本鉤子只有基底一份

原本無報價計數台股存在部位上、期貨存在 model 的 dict；持有天數只有台股空單會更新；
期貨的無報價門檻是類別常數、多單到期出場記在空單的 key；台股 `check_margin_call()`
一支做四件事。本檔釘住收斂後的契約。
"""

DAY_1: datetime.date = datetime.date(2026, 1, 5)
DAY_2: datetime.date = datetime.date(2026, 1, 8)


def make_stock_settlement(
    init_capital: float = 1_000_000.0,
    max_no_quote_days: Optional[int] = None,
    max_holding_days: Optional[int] = None,
) -> TwStockSettlementModel:
    """台股結算模型（預設費率、融券）"""

    cost_model: TwStockCostModel = TwStockCostModel(CostConfig.default())
    manager: StockPositionManager = StockPositionManager(
        StockAccount(init_capital), cost_model
    )
    return TwStockSettlementModel(
        position_manager=manager,
        cost_model=cost_model,
        reference_prices={},
        max_no_quote_days=max_no_quote_days,
        max_holding_days=max_holding_days,
    )


def make_futures_settlement(
    max_no_quote_days: Optional[int] = 3,
) -> TwFuturesSettlementModel:
    """期貨結算模型（零成本、比率保證金）"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(10_000_000.0),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )
    return TwFuturesSettlementModel(manager, max_no_quote_days=max_no_quote_days)


def open_stock(
    settlement: TwStockSettlementModel,
    position_type: PositionType = PositionType.LONG,
    date: datetime.date = DAY_1,
    stock_id: str = "2330",
) -> StockPosition:
    """開一張股票（預設 2330）"""

    return settlement.position_manager.open_position(
        StockOrder(
            stock_id=stock_id,
            date=date,
            action=Action.BUY if position_type == PositionType.LONG else Action.SELL,
            position_type=position_type,
            price=100.0,
            volume=1,
        )
    )


def open_futures(
    settlement: TwFuturesSettlementModel,
    position_type: PositionType = PositionType.LONG,
) -> None:
    """開一口 TX202601"""

    settlement.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202601",
            date=DAY_1,
            action=Action.BUY if position_type == PositionType.LONG else Action.SELL,
            position_type=position_type,
            price=18000.0,
            volume=1,
        )
    )


# === 只有基底一份 ===
def test_counters_live_only_in_the_base() -> None:
    """無報價計數、持有天數更新與門檻判定都不可在子類再寫一份；部位上也不再有計數欄位"""

    for name in (
        "update_no_quote_days",
        "update_holding_days",
        "has_exceeded_no_quote_limit",
    ):
        assert name in BaseSettlementModel.__dict__
        for model_cls in (TwStockSettlementModel, TwFuturesSettlementModel):
            assert name not in model_cls.__dict__, f"{model_cls.__name__} 覆寫了 {name}"

    assert not hasattr(TwFuturesSettlementModel, "MAX_NO_QUOTE_DAYS")
    assert not hasattr(StockPosition(stock_id="2330"), "no_quote_days")


def test_stock_counter_is_keyed_by_symbol_in_the_model() -> None:
    """
    台股改記在 model 的 dict：同一檔的兩筆部位共用一個計數，一天只加一次

    期貨原本逐部位累加，同契約加碼兩口時計數會兩倍速衝到門檻、提早被當成到期出場。
    """

    settlement: TwStockSettlementModel = make_stock_settlement(max_no_quote_days=2)
    open_stock(settlement)
    open_stock(settlement)
    positions = settlement.position_manager.account.get_positions()

    settlement.update_no_quote_days({}, positions)
    assert settlement.no_quote_days == {"2330": 1}
    assert not settlement.has_exceeded_no_quote_limit("2330")

    settlement.update_no_quote_days({}, positions)
    assert settlement.has_exceeded_no_quote_limit("2330")

    settlement.update_no_quote_days({"2330": build_stock_quote()}, positions)
    assert settlement.no_quote_days == {"2330": 0}


def test_no_limit_means_never_exceeded() -> None:
    """門檻為 None（台股預設、期貨策略可選）時永遠不觸發"""

    settlement: TwFuturesSettlementModel = make_futures_settlement(
        max_no_quote_days=None
    )
    open_futures(settlement)
    account: FuturesAccount = settlement.position_manager.account

    for _ in range(10):
        settlement.settle_day(DAY_2, [], account, new_event_counts(FUTURES_EVENT_KEYS))

    assert settlement.no_quote_days["TX202601"] == 10
    assert account.get_positions()


# === 期貨門檻由策略決定、事件 key 依方向 ===
KEEP_DEFAULT: object = object()


class ThresholdFuturesStrategy(BaseFuturesStrategy):
    """只用來經 factory 組裝的期貨策略；不傳門檻就沿用基底預設"""

    def __init__(self, max_no_quote_days: object = KEEP_DEFAULT) -> None:
        super().__init__()

        self.strategy_name: str = "ThresholdFutures"
        self.init_capital: float = 3_000_000.0
        self.products: List[str] = ["TX"]
        self.max_lots: int = 1
        self.scale: Scale = Scale.DAY
        self.start_date: datetime.date = DAY_1
        self.end_date: datetime.date = DAY_2
        self.cost_config: FuturesCostConfig = FuturesCostConfig.free()
        if max_no_quote_days is not KEEP_DEFAULT:
            self.max_no_quote_days = max_no_quote_days

    def setup_account(self, account: FuturesAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: FuturesAccount = account

    def setup_apis(self, feed=None) -> None:
        """測試不使用任何資料 API"""

        pass

    def generate_open_signals(self, quotes: List[FuturesQuote]) -> List:
        """不開倉"""

        return []

    def generate_close_signals(self, quotes: List[FuturesQuote]) -> List:
        """不平倉"""

        return []

    def generate_stop_loss_signals(self, quotes: List[FuturesQuote]) -> List:
        """不停損"""

        return []


def test_futures_threshold_defaults_agree_and_flow_from_the_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """策略欄位預設 3 與模型預設相同；factory 把策略的值交給結算模型"""

    assert (
        ThresholdFuturesStrategy().max_no_quote_days
        == TwFuturesSettlementModel.DEFAULT_MAX_NO_QUOTE_DAYS
        == 3
    )

    monkeypatch.setattr(Backtester, "setup", lambda self: None)
    backtester: Backtester = build_backtester(ThresholdFuturesStrategy(5))

    assert backtester.settlement.max_no_quote_days == 5


@pytest.mark.parametrize(
    ("position_type", "event_key"),
    [
        (PositionType.LONG, "forced_exit_no_quote"),
        (PositionType.SHORT, "forced_cover_no_quote"),
    ],
    ids=["long", "short"],
)
def test_futures_expiry_exit_counts_by_direction(
    position_type: PositionType, event_key: str
) -> None:
    """到期兜底出場：多單記 `forced_exit_no_quote`、空單記 `forced_cover_no_quote`，與台股同一組 key"""

    settlement: TwFuturesSettlementModel = make_futures_settlement(max_no_quote_days=2)
    open_futures(settlement, position_type)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)

    for _ in range(2):
        settlement.settle_day(DAY_2, [], account, event_counts)

    assert account.get_positions() == []
    assert event_counts[event_key] == 1
    assert "forced_exit_no_quote" in SHARED_EVENT_KEYS


# === 持有天數：兩個市場、多空都更新 ===
def test_holding_days_are_updated_for_every_position() -> None:
    """台股多單、期貨部位的 `holding_days` 也會逐日更新（曆日，不是 bar 數）"""

    stock: TwStockSettlementModel = make_stock_settlement()
    long_position: StockPosition = open_stock(stock, PositionType.LONG)
    # 同標的不得雙向持倉，空單開在另一檔
    short_position: StockPosition = open_stock(
        stock, PositionType.SHORT, date=DAY_2, stock_id="2317"
    )
    stock.update_holding_days(
        datetime.datetime(2026, 1, 12, 13, 30), [long_position, short_position]
    )

    assert long_position.holding_days == 7
    assert short_position.holding_days == 4

    futures: TwFuturesSettlementModel = make_futures_settlement()
    open_futures(futures)
    account: FuturesAccount = futures.position_manager.account
    futures.settle_day(
        DAY_2,
        [build_futures_quote(expiry="202601", close=18000.0, settlement_price=18000.0)],
        account,
        new_event_counts(FUTURES_EVENT_KEYS),
    )

    assert account.get_positions()[0].holding_days == 3


def test_stock_max_holding_still_reads_the_updated_days() -> None:
    """台股空單的最長持有判定讀的是基底更新的持有天數（公式沒變）"""

    settlement: TwStockSettlementModel = make_stock_settlement(max_holding_days=3)
    open_stock(settlement, PositionType.SHORT)
    account: StockAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = new_event_counts()
    quotes = [build_stock_quote(stock_id="2330", cur_price=100.0, date=DAY_2)]

    settlement.execute_daily_position_check(DAY_2, quotes, account, event_counts)

    assert event_counts["forced_cover_max_holding"] == 1
    assert account.get_positions() == []


# === 台股：單一迴圈，先強制出場再維持率 ===
def test_stock_margin_call_runs_forced_exits_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_margin_call()` 對每筆空單先問 `check_short_forced_exits()`，已出場的就不再算維持率"""

    settlement: TwStockSettlementModel = make_stock_settlement(max_no_quote_days=1)
    open_stock(settlement, PositionType.SHORT)
    account: StockAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = new_event_counts()
    asked: List[str] = []

    original = settlement.cost_model.check_margin_call

    def spy(**kwargs) -> bool:
        asked.append("margin")
        return original(**kwargs)

    monkeypatch.setattr(settlement.cost_model, "check_margin_call", spy)

    # 連續一天無報價就達門檻：出場走無報價那一桶，維持率根本不用算
    settlement.update_no_quote_days({}, account.get_positions())
    settlement.check_margin_call(DAY_2, {}, account, event_counts)

    assert event_counts["forced_cover_no_quote"] == 1
    assert event_counts["forced_cover_margin_call"] == 0
    assert asked == []


# === 持有成本鉤子 ===
def test_accrued_holding_cost_hook() -> None:
    """基底預設 0（期貨沒有這種成本）；台股回 SBL 已計提的借券費"""

    futures: TwFuturesSettlementModel = make_futures_settlement()
    open_futures(futures)
    assert (
        futures.get_accrued_holding_cost(
            futures.position_manager.account.get_positions()[0]
        )
        == 0.0
    )

    stock: TwStockSettlementModel = make_stock_settlement()
    position: StockPosition = open_stock(stock, PositionType.SHORT)
    position.accrued_borrow_fee = 123.0
    assert stock.get_accrued_holding_cost(position) == 123.0
    # 盯市時從空單價值扣掉
    value: float = stock.mark_position(position, 100.0, 1000)
    assert value == position.margin - 123.0
