import datetime
from typing import Dict, List, Optional

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.models.event_counts import FUTURES_EVENT_KEYS, new_event_counts
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesQuote,
    StockAccount,
    StockOrder,
    StockPosition,
)
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.position.base.position_manager import BasePositionManager
from core.position.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.position.stock.position_manager import StockPositionManager
from core.strategies.futures import BaseFuturesStrategy
from core.utils import Action, PositionType, Scale

"""
部位管理層的對稱性：開倉判準只有基底一份，兩個市場只實作入帳

原本台股與期貨各寫一套開倉前檢查：判準相同（方向與動作一致、同標的不得雙向持倉）、
寫法不同——台股方向不一致時靜默回 None、期貨有 warning；台股的反向持倉檢查分散在
多空兩處；期貨保證金不足的拒單不計數（factory 沒注入計數器）；持有天數與日期正規化
各一份。改一邊另一邊不會跟著改，也不會有測試失敗。
"""

DATE: datetime.date = datetime.date(2026, 1, 5)


def stock_manager(
    init_capital: float = 1_000_000.0, event_counts: Optional[Dict[str, int]] = None
) -> StockPositionManager:
    """台股部位管理器（預設費率）"""

    return StockPositionManager(
        StockAccount(init_capital),
        TwStockCostModel(CostConfig.default()),
        event_counts=event_counts,
    )


def futures_manager(
    init_capital: float = 10_000_000.0, event_counts: Optional[Dict[str, int]] = None
) -> FuturesPositionManager:
    """期貨部位管理器（零成本、比率保證金）"""

    return FuturesPositionManager(
        FuturesAccount(init_capital),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
        event_counts=event_counts,
    )


def stock_order(
    action: Action, position_type: PositionType, price: float = 100.0, volume: int = 1
) -> StockOrder:
    """一張 2330 的訂單"""

    return StockOrder(
        stock_id="2330",
        date=DATE,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
    )


def futures_order(
    action: Action,
    position_type: PositionType,
    price: float = 18000.0,
    volume: int = 1,
) -> FuturesOrder:
    """一口 TX 的訂單"""

    return FuturesOrder(
        product="TX",
        expiry="202601",
        date=DATE,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
    )


# === 判準只有基底一份 ===
def test_open_template_lives_only_in_the_base() -> None:
    """子類不可再覆寫 `open_position()`，只能實作入帳鉤子；日期正規化與持有天數也只在基底"""

    for manager_cls in (StockPositionManager, FuturesPositionManager):
        assert "open_position" not in manager_cls.__dict__, (
            f"{manager_cls.__name__} 覆寫了 open_position()"
        )
        assert "_book_open" in manager_cls.__dict__
        assert "get_position_unit_size" in manager_cls.__dict__
        assert "calculate_holding_days" not in manager_cls.__dict__
        assert not hasattr(manager_cls, "normalize_date")

    assert "open_position" in BasePositionManager.__dict__
    assert getattr(BasePositionManager._book_open, "__isabstractmethod__", False)


@pytest.mark.parametrize(
    ("make_manager", "make_order"),
    [(stock_manager, stock_order), (futures_manager, futures_order)],
    ids=["stock", "futures"],
)
def test_direction_mismatch_is_rejected_before_booking(
    make_manager, make_order
) -> None:
    """多單賣出、空單買進都不是開倉：兩個市場都在入帳前拒掉，餘額不動"""

    manager: BasePositionManager = make_manager()
    balance_before: float = manager.account.balance

    assert manager.open_position(make_order(Action.SELL, PositionType.LONG)) is None
    assert manager.open_position(make_order(Action.BUY, PositionType.SHORT)) is None
    assert manager.account.positions == []
    assert manager.account.balance == balance_before


@pytest.mark.parametrize(
    ("make_manager", "make_order"),
    [(stock_manager, stock_order), (futures_manager, futures_order)],
    ids=["stock", "futures"],
)
def test_opposite_position_is_rejected_in_both_directions(
    make_manager, make_order
) -> None:
    """同標的不得雙向持倉，多空兩端都擋：先多後空、先空後多都拒"""

    manager: BasePositionManager = make_manager()

    assert manager.open_position(make_order(Action.BUY, PositionType.LONG)) is not None
    assert manager.open_position(make_order(Action.SELL, PositionType.SHORT)) is None
    assert len(manager.account.positions) == 1

    other: BasePositionManager = make_manager()
    assert other.open_position(make_order(Action.SELL, PositionType.SHORT)) is not None
    assert other.open_position(make_order(Action.BUY, PositionType.LONG)) is None
    assert len(other.account.positions) == 1


# === 期貨拒單也計數 ===
def test_futures_margin_rejection_is_counted() -> None:
    """可動用餘額付不出保證金：與台股做多餘額不足同一個 key，計進共用的計數器"""

    event_counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)
    manager: FuturesPositionManager = futures_manager(
        init_capital=1000.0, event_counts=event_counts
    )

    position: Optional[FuturesPosition] = manager.open_position(
        futures_order(Action.BUY, PositionType.LONG)
    )

    assert position is None
    assert event_counts["rejected_insufficient_balance"] == 1
    assert manager.account.balance == 1000.0


def test_stock_balance_rejection_still_counts_the_same_key() -> None:
    """台股做多餘額不足照舊計入同一個 key（行為未變，只是改經基底的計數器）"""

    event_counts: Dict[str, int] = new_event_counts()
    manager: StockPositionManager = stock_manager(
        init_capital=1000.0, event_counts=event_counts
    )

    assert manager.open_position(stock_order(Action.BUY, PositionType.LONG)) is None
    assert event_counts["rejected_insufficient_balance"] == 1


def test_standalone_manager_counts_without_an_injected_dict() -> None:
    """單獨建管理器時自備計數器，key 不存在也不會 KeyError"""

    manager: FuturesPositionManager = futures_manager(init_capital=1000.0)

    manager.open_position(futures_order(Action.BUY, PositionType.LONG))

    assert manager.event_counts == {"rejected_insufficient_balance": 1}


class MinimalFuturesStrategy(BaseFuturesStrategy):
    """只為了經 factory 組裝的期貨策略"""

    def __init__(self) -> None:
        super().__init__()

        self.strategy_name: str = "MinimalFutures"
        self.init_capital: float = 3_000_000.0
        self.products: List[str] = ["TX"]
        self.max_lots: int = 1
        self.scale: Scale = Scale.DAY
        self.start_date: datetime.date = DATE
        self.end_date: datetime.date = DATE
        self.cost_config: FuturesCostConfig = FuturesCostConfig.free()

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


def test_factory_injects_the_shared_counter_into_the_futures_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期貨 factory 把引擎的計數器交給部位管理層，保證金不足的拒單才會進事件報表"""

    monkeypatch.setattr(Backtester, "setup", lambda self: None)
    backtester: Backtester = build_backtester(MinimalFuturesStrategy())

    assert backtester.position_manager.event_counts is backtester.event_counts
    assert "rejected_insufficient_balance" in backtester.event_counts


# === 持有天數：一份公式、接受 date 與 datetime ===
def test_holding_days_accepts_dates_and_datetimes() -> None:
    """Tick 級別的 datetime 只取日期部分；缺任一端為 0；同日為 0"""

    entry: datetime.datetime = datetime.datetime(2026, 1, 5, 9, 30)
    exit_: datetime.date = datetime.date(2026, 1, 8)

    assert BasePositionManager.calculate_holding_days(entry, exit_) == 3
    assert BasePositionManager.calculate_holding_days(exit_, exit_) == 0
    assert BasePositionManager.calculate_holding_days(None, exit_) == 0
    assert BasePositionManager.calculate_holding_days(entry, None) == 0


def test_closed_trade_records_carry_the_same_holding_days_in_both_markets() -> None:
    """
    兩個市場的交易紀錄都經同一條公式算持有天數

    台股這裡用空單：目前只有空單的平倉會算持有天數（多單的 `holding_days` 仍為 0，
    那是結算模型的持有天數上移要處理的事，不在本檔範圍）。
    """

    stock: StockPositionManager = stock_manager()
    stock.open_position(stock_order(Action.SELL, PositionType.SHORT))
    stock_record = stock.close_position(
        StockOrder(
            stock_id="2330",
            date=datetime.datetime(2026, 1, 8, 13, 30),
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=99.0,
            volume=1,
        )
    )[0]

    futures: FuturesPositionManager = futures_manager()
    futures.open_position(futures_order(Action.BUY, PositionType.LONG))
    futures_record = futures.close_position(
        FuturesOrder(
            product="TX",
            expiry="202601",
            date=datetime.datetime(2026, 1, 8, 13, 30),
            action=Action.SELL,
            position_type=PositionType.LONG,
            price=18100.0,
            volume=1,
        )
    )[0]

    assert stock_record.holding_days == 3
    assert futures_record.holding_days == 3


# === 滑價統計走基底，計價單位由各市場提供 ===
def test_slippage_is_accrued_by_the_base_with_each_markets_unit_size() -> None:
    """台股一張 1,000 股、期貨為契約乘數；兩邊都由基底在開成之後累計"""

    stock: StockPositionManager = stock_manager()
    order: StockOrder = stock_order(Action.BUY, PositionType.LONG, price=100.5)
    order.reference_price = 100.0
    stock.open_position(order)
    assert stock.account.total_slippage_cost == 500.0

    futures: FuturesPositionManager = futures_manager()
    f_order: FuturesOrder = futures_order(Action.BUY, PositionType.LONG, price=18001.0)
    f_order.reference_price = 18000.0
    position: FuturesPosition = futures.open_position(f_order)
    assert futures.get_position_unit_size(position) == position.multiplier == 200
    assert futures.account.total_slippage_cost == 200.0

    unit: StockPosition = stock.account.positions[0]
    assert stock.get_position_unit_size(unit) == 1000
