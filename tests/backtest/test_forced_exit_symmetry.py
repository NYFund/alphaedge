import datetime
from typing import Dict, List

from core.backtest.models.event_counts import FUTURES_EVENT_KEYS, new_event_counts
from core.backtest.models.fill_model import TwFuturesFillModel, TwStockFillModel
from core.backtest.models.settlement_model import (
    BaseSettlementModel,
    TwFuturesSettlementModel,
    TwStockSettlementModel,
)
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.models import (
    FuturesAccount,
    FuturesOrder,
    StockAccount,
    StockOrder,
    StockPosition,
)
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.models.fill_config import FillConfig, FuturesFillConfig
from core.position.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.position.stock.position_manager import StockPositionManager
from core.utils import Action, PositionType, ShortMethod
from tests.conftest import build_futures_quote, build_stock_quote

"""
引擎強制出場只有基底一支：台股空單、台股多單、期貨原本各走各的

三支的差異只在「訂單怎麼組」（台股要帶放空管道與當沖註記、期貨要帶商品與到期月），
滑價口徑與入帳方式應該相同；各寫一份時，改了其中一支的滑價處理另外兩支不會跟著改。
"""

DAY_1: datetime.date = datetime.date(2026, 1, 5)
DAY_2: datetime.date = datetime.date(2026, 1, 8)


def make_stock_settlement(
    slippage_bps: float = 0.0, max_no_quote_days: int = 1
) -> TwStockSettlementModel:
    """台股結算模型；要吃滑價就給基點"""

    cost_model: TwStockCostModel = TwStockCostModel(
        CostConfig.default(ShortMethod.MARGIN)
    )
    manager: StockPositionManager = StockPositionManager(
        StockAccount(2_000_000.0), cost_model
    )
    fill_model: TwStockFillModel = TwStockFillModel(
        config=FillConfig(slippage_bps_buy=slippage_bps, slippage_bps_sell=slippage_bps)
    )
    return TwStockSettlementModel(
        position_manager=manager,
        cost_model=cost_model,
        reference_prices={},
        fill_model=fill_model,
        max_no_quote_days=max_no_quote_days,
    )


def make_futures_settlement(slippage_ticks: float = 0.0) -> TwFuturesSettlementModel:
    """期貨結算模型；要吃滑價就給跳動點"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(10_000_000.0),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )
    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        config=FuturesFillConfig(
            slippage_ticks_buy=slippage_ticks, slippage_ticks_sell=slippage_ticks
        )
    )
    return TwFuturesSettlementModel(manager, fill_model=fill_model, max_no_quote_days=1)


def open_stock(
    settlement: TwStockSettlementModel,
    position_type: PositionType,
    volume: int = 1,
    stock_id: str = "2330",
) -> StockPosition:
    """開倉一檔股票"""

    return settlement.position_manager.open_position(
        StockOrder(
            stock_id=stock_id,
            date=DAY_1,
            action=Action.BUY if position_type == PositionType.LONG else Action.SELL,
            position_type=position_type,
            price=100.0,
            volume=volume,
        )
    )


def test_forced_exit_lives_only_in_the_base() -> None:
    """三支舊的強制出場都不見了，子類只剩 `build_exit_order()`"""

    assert "force_close_position" in BaseSettlementModel.__dict__
    for model_cls in (TwStockSettlementModel, TwFuturesSettlementModel):
        assert "force_close_position" not in model_cls.__dict__
        assert "build_exit_order" in model_cls.__dict__
    for stale in ("force_cover_position", "force_exit_long_position"):
        assert not hasattr(TwStockSettlementModel, stale)
    assert not hasattr(TwFuturesSettlementModel, "close_position_at")


def test_stock_exit_order_carries_the_short_fields() -> None:
    """台股空單的平倉單要帶放空管道與當沖註記（稅率與費用看它們）；多單是賣出"""

    settlement: TwStockSettlementModel = make_stock_settlement()
    short_position: StockPosition = open_stock(settlement, PositionType.SHORT)
    long_position: StockPosition = open_stock(
        settlement, PositionType.LONG, stock_id="2317"
    )

    cover: StockOrder = settlement.build_exit_order(short_position, DAY_2, 99.0)
    sell: StockOrder = settlement.build_exit_order(long_position, DAY_2, 101.0)

    assert (cover.action, cover.position_type) == (Action.BUY, PositionType.SHORT)
    assert cover.short_method == ShortMethod.MARGIN
    assert cover.is_day_trade is False
    assert (sell.action, sell.position_type) == (Action.SELL, PositionType.LONG)
    assert sell.volume == long_position.volume


def test_futures_exit_order_targets_the_same_contract() -> None:
    """期貨的平倉單帶商品與到期月；多單賣出、空單買進"""

    settlement: TwFuturesSettlementModel = make_futures_settlement()
    settlement.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202601",
            date=DAY_1,
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=18000.0,
            volume=2,
        )
    )
    position = settlement.position_manager.account.get_positions()[0]

    order: FuturesOrder = settlement.build_exit_order(position, DAY_2, 17900.0)

    assert (order.product, order.expiry) == ("TX", "202601")
    assert (order.action, order.position_type, order.volume) == (
        Action.BUY,
        PositionType.SHORT,
        2,
    )


def test_both_markets_pay_the_same_kind_of_slippage_on_forced_exits() -> None:
    """強制出場都經 `apply_fill_price()`：台股多單賣出往下滑、期貨空單回補往上滑"""

    stock: TwStockSettlementModel = make_stock_settlement(slippage_bps=100.0)
    long_position: StockPosition = open_stock(stock, PositionType.LONG)
    stock.force_close_position(
        long_position, DAY_2, 100.0, build_stock_quote(cur_price=100.0)
    )
    assert stock.position_manager.account.trade_records[-1].sell_price < 100.0

    futures: TwFuturesSettlementModel = make_futures_settlement(slippage_ticks=1.0)
    futures.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202601",
            date=DAY_1,
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=18000.0,
            volume=1,
        )
    )
    position = futures.position_manager.account.get_positions()[0]
    futures.force_close_position(
        position, DAY_2, 18000.0, build_futures_quote(expiry="202601", close=18000.0)
    )
    assert futures.position_manager.account.trade_records[-1].buy_price == 18001.0


def test_long_no_quote_exit_closes_every_position_of_the_symbol_in_order() -> None:
    """
    台股多單改走同標的 FIFO：同一檔加碼的兩筆在同一天一起達到門檻，依開倉順序全部出場

    舊版指定部位出場；無報價是標的層級的狀態，兩種寫法平掉的集合一樣，
    只是這裡要釘住「全部出場、順序不變」。
    """

    settlement: TwStockSettlementModel = make_stock_settlement(max_no_quote_days=1)
    settlement.reference_prices["2330"] = 100.0
    first: StockPosition = open_stock(settlement, PositionType.LONG, volume=1)
    second: StockPosition = open_stock(settlement, PositionType.LONG, volume=3)
    account: StockAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = new_event_counts()

    settlement.update_no_quote_days({}, account.get_positions())
    settlement.check_long_no_quote_exit(DAY_2, {}, account, event_counts)

    assert account.get_positions() == []
    assert event_counts["forced_exit_no_quote"] == 2
    assert [record.id for record in account.trade_records] == [first.id, second.id]
    assert [record.sell_volume for record in account.trade_records] == [1, 3]


def test_futures_expiry_exit_goes_through_the_shared_path() -> None:
    """期貨到期兜底同樣走基底那一支（無報價所以 quote 為 None、不做區間檢查）"""

    settlement: TwFuturesSettlementModel = make_futures_settlement(slippage_ticks=1.0)
    settlement.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry="202601",
            date=DAY_1,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=18000.0,
            volume=1,
        )
    )
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)

    settlement.settle_day(DAY_2, [], account, event_counts)

    records: List = account.trade_records
    assert account.get_positions() == []
    assert event_counts["forced_exit_no_quote"] == 1
    assert records[-1].sell_price == 17999.0
    assert event_counts["close_price_out_of_range"] == 0
