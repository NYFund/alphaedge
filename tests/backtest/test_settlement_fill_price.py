import datetime
from typing import Dict, List, Optional

from core.backtest.backtester import new_event_counts
from core.backtest.models.cost_model import (
    CostConfig,
    FuturesCostConfig,
    StockCostModel,
    TwFuturesCostModel,
)
from core.backtest.models.fill_model import (
    FillConfig,
    FuturesFillConfig,
    TwFuturesFillModel,
    TwStockFillModel,
)
from core.backtest.models.settlement_model import (
    TwFuturesSettlementModel,
    TwStockSettlementModel,
)
from core.managers.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.managers.stock.position_manager import StockPositionManager
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesQuote,
    StockAccount,
    StockOrder,
    StockPosition,
    StockQuote,
)
from core.utils import Action, PositionType, Scale, ShortMethod

"""
引擎強制出場的成交價測試：滑價與當日區間檢查

**缺口的共同特徵是錯了不會報錯**：強制出場不吃滑價只會讓成本靜默偏低，
超出當日區間的成交價則完全無聲。本檔逐一釘住：

1. `apply_fill_price()` 在基底，台股與期貨共用同一份口徑（各寫一份必然漂移）。
2. 未注入 `fill_model` 或未設滑價時**回傳原物件**，既有回測逐筆不變。
3. 有滑價時回傳**副本**，傳入的訂單不被改動。
4. 期貨的追繳平倉、到期兜底出場與換月轉倉兩腿都吃滑價。
5. 成交價被滑價推出當日區間時**只警告並計數，不夾回**（`close_price_out_of_range`）。

全部為純記憶體物件，不連資料庫。
"""

STOCK_ID: str = "2330"
STOCK_DATE: datetime.date = datetime.date(2024, 1, 4)

MULTIPLIER: int = 200
FUTURES_DAY_1: datetime.date = datetime.date(2024, 3, 20)
FUTURES_DAY_2: datetime.date = datetime.date(2024, 3, 21)
TRADING_DAYS: List[datetime.date] = [
    datetime.date(2024, 3, 18),
    datetime.date(2024, 3, 19),
    FUTURES_DAY_1,
    FUTURES_DAY_2,
    datetime.date(2024, 3, 22),
]


# === 台股：共用的組裝 ===
def make_stock_settlement(
    account: StockAccount,
    fill_config: Optional[FillConfig] = None,
    event_counts: Optional[Dict[str, int]] = None,
) -> TwStockSettlementModel:
    """組出台股結算模型；`fill_config` 為 None 時不注入 fill_model"""

    cost_model: StockCostModel = StockCostModel(CostConfig.default())
    fill_model: Optional[TwStockFillModel] = None
    if fill_config is not None:
        fill_model = TwStockFillModel(
            event_counts=event_counts if event_counts is not None else {},
            config=fill_config,
        )

    return TwStockSettlementModel(
        position_manager=StockPositionManager(account, cost_model),
        cost_model=cost_model,
        prev_close={},
        fill_model=fill_model,
    )


def make_stock_order(price: float = 100.0) -> StockOrder:
    """組一張引擎強制回補用的買進單"""

    return StockOrder(
        stock_id=STOCK_ID,
        date=STOCK_DATE,
        action=Action.BUY,
        position_type=PositionType.SHORT,
        price=price,
        volume=2,
        short_method=ShortMethod.DAY_TRADE,
        is_day_trade=True,
    )


def make_stock_quote(close: float = 100.0, high: float = 100.0) -> StockQuote:
    """當日報價；預設收盤即當日最高，滑價一定會把回補價推出區間"""

    return StockQuote(
        stock_id=STOCK_ID,
        scale=Scale.DAY,
        date=STOCK_DATE,
        cur_price=close,
        volume=10_000,
        open=close,
        high=high,
        low=95.0,
        close=close,
    )


def make_day_trade_position() -> StockPosition:
    """建立當沖放空部位（尚未回補）"""

    return StockPosition(
        id=1,
        stock_id=STOCK_ID,
        position_type=PositionType.SHORT,
        date=STOCK_DATE,
        price=100.0,
        volume=2,
        short_method=ShortMethod.DAY_TRADE,
        is_day_trade=True,
        margin=0.0,
        short_proceeds=100.0 * 2 * 1000,
    )


# === 基底行為：沒開滑價時逐筆不變 ===
def test_returns_the_same_object_when_no_fill_model() -> None:
    """未注入 fill_model（純記憶體測試）時原物件回傳，不是副本"""

    settlement: TwStockSettlementModel = make_stock_settlement(StockAccount(1_000_000))
    order: StockOrder = make_stock_order()

    assert settlement.apply_fill_price(order, make_stock_quote()) is order


def test_returns_the_same_object_when_slippage_is_zero() -> None:
    """有 fill_model 但滑價為 0 時同樣是原物件——既有回測不可能因此改變"""

    settlement: TwStockSettlementModel = make_stock_settlement(
        StockAccount(1_000_000), fill_config=FillConfig()
    )
    order: StockOrder = make_stock_order()

    assert settlement.apply_fill_price(order, make_stock_quote()) is order


def test_returns_a_copy_and_leaves_the_original_untouched() -> None:
    """
    有滑價時回傳副本

    就地改動會讓呼叫端手上的訂單被引擎改過，理由與 `fill()` 的
    「絕不就地修改傳入的 order」相同。
    """

    settlement: TwStockSettlementModel = make_stock_settlement(
        StockAccount(1_000_000),
        fill_config=FillConfig(slippage_bps_buy=100.0),
    )
    order: StockOrder = make_stock_order(price=100.0)

    filled: StockOrder = settlement.apply_fill_price(order, make_stock_quote())

    assert filled is not order
    assert filled.price > 100.0
    assert order.price == 100.0  # 傳入的那一張沒被動過


# === S9：超出當日區間只警告不夾回 ===
def test_forced_cover_price_out_of_range_is_counted_not_clamped() -> None:
    """
    滑價把強制回補價推出當日區間時只計數，不夾回

    **不夾回的理由與策略平倉腿一致**：強制出場的時點是市場規則決定的，
    夾回等於換一個價格假設，而且一律把成交價推向對持有者有利的一側，
    正好抵銷滑價的保守意義。
    """

    account: StockAccount = StockAccount(1_000_000)
    event_counts: Dict[str, int] = new_event_counts()
    settlement: TwStockSettlementModel = make_stock_settlement(
        account,
        fill_config=FillConfig(slippage_bps_buy=100.0),
        event_counts=event_counts,
    )

    # 當日區間 [95, 100]，回補是買進，100 元 ＋ 100 bps ＝ 101 已在高點之上
    filled: StockOrder = settlement.apply_fill_price(
        make_stock_order(price=100.0), make_stock_quote(close=100.0, high=100.0)
    )

    assert filled.price == 101.0  # 沒有被夾回 100
    assert event_counts["close_price_out_of_range"] == 1


def test_price_inside_the_range_is_not_counted() -> None:
    """含滑價後仍在區間內時不計數（防止改過頭，每一筆強制出場都報一次）"""

    event_counts: Dict[str, int] = new_event_counts()
    settlement: TwStockSettlementModel = make_stock_settlement(
        StockAccount(1_000_000),
        fill_config=FillConfig(slippage_bps_buy=100.0),
        event_counts=event_counts,
    )

    settlement.apply_fill_price(
        make_stock_order(price=100.0), make_stock_quote(close=100.0, high=105.0)
    )

    assert event_counts["close_price_out_of_range"] == 0


def test_day_trade_cover_goes_through_the_fill_price() -> None:
    """當沖日終強制回補是完整路徑，成交價含滑價且超區間有計數"""

    account: StockAccount = StockAccount(1_000_000)
    account.positions.append(make_day_trade_position())
    event_counts: Dict[str, int] = new_event_counts()
    settlement: TwStockSettlementModel = make_stock_settlement(
        account,
        fill_config=FillConfig(slippage_bps_buy=100.0),
        event_counts=event_counts,
    )

    settlement.enforce_day_trade_cover(
        STOCK_DATE, [make_stock_quote(close=100.0, high=100.0)], account, event_counts
    )

    assert event_counts["forced_cover_day_trade"] == 1
    assert account.trade_records[-1].buy_price == 101.0
    assert event_counts["close_price_out_of_range"] == 1


def test_no_quote_skips_the_range_check() -> None:
    """停牌無報價時傳 None，不該因此出錯，也不該計數"""

    event_counts: Dict[str, int] = new_event_counts()
    settlement: TwStockSettlementModel = make_stock_settlement(
        StockAccount(1_000_000),
        fill_config=FillConfig(slippage_bps_buy=100.0),
        event_counts=event_counts,
    )

    filled: StockOrder = settlement.apply_fill_price(make_stock_order(100.0), None)

    assert filled.price == 101.0
    assert event_counts["close_price_out_of_range"] == 0


# === 期貨：共用的組裝 ===
def make_futures_settlement(
    init_capital: float = 10_000_000,
    fill_config: Optional[FuturesFillConfig] = None,
    roll_config: Optional[FuturesRollConfig] = None,
    event_counts: Optional[Dict[str, int]] = None,
) -> TwFuturesSettlementModel:
    """組出零成本、比率保證金的期貨結算模型；滑價由 `fill_config` 決定"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=init_capital),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )

    fill_model: Optional[TwFuturesFillModel] = None
    if fill_config is not None:
        fill_model = TwFuturesFillModel(
            event_counts=event_counts if event_counts is not None else {},
            config=fill_config,
        )

    config: FuturesRollConfig = roll_config or FuturesRollConfig()
    config.calendar = FuturesCalendar(TRADING_DAYS)
    return TwFuturesSettlementModel(manager, roll_config=config, fill_model=fill_model)


def make_futures_quote(
    expiry: str = "202403",
    close: float = 20000.0,
    date: datetime.date = FUTURES_DAY_2,
    high: Optional[float] = None,
    low: Optional[float] = None,
) -> FuturesQuote:
    """組一筆 TX 報價（結算價即收盤價）"""

    return FuturesQuote(
        product="TX",
        expiry=expiry,
        scale=Scale.DAY,
        date=date,
        cur_price=close,
        volume=1000,
        open=close,
        high=high if high is not None else close + 200,
        low=low if low is not None else close - 200,
        close=close,
        settlement_price=close,
        open_interest=100,
        multiplier=MULTIPLIER,
    )


def open_futures_position(
    settlement: TwFuturesSettlementModel,
    expiry: str = "202403",
    price: float = 20000.0,
    volume: int = 1,
    position_type: PositionType = PositionType.LONG,
) -> None:
    """在指定契約開倉"""

    settlement.position_manager.open_position(
        FuturesOrder(
            product="TX",
            expiry=expiry,
            date=FUTURES_DAY_1,
            action=Action.BUY if position_type == PositionType.LONG else Action.SELL,
            position_type=position_type,
            price=price,
            volume=volume,
        )
    )


ONE_TICK: FuturesFillConfig = FuturesFillConfig(
    slippage_ticks_buy=1, slippage_ticks_sell=1
)


# === S3：期貨強制出場 ===
def test_margin_call_close_pays_the_slippage() -> None:
    """
    保證金追繳的強制平倉含滑價，方向對持有者不利

    多單被斷頭是賣出，成交價比結算價**低**一個跳動點——TX 的一檔是 1 點。
    """

    settlement: TwFuturesSettlementModel = make_futures_settlement(
        init_capital=450_000, fill_config=ONE_TICK
    )
    open_futures_position(settlement)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}

    # 結算價跌到 19,000：權益 250,000 低於維持保證金 400,000，觸發追繳
    quote: FuturesQuote = make_futures_quote(close=19000.0, low=18500.0)
    for position in list(account.get_positions()):
        settlement.position_manager.settle_daily(position, 19000.0)

    settlement.check_margin_call(
        FUTURES_DAY_2, {quote.symbol: quote}, account, event_counts
    )

    assert event_counts["forced_cover_margin_call"] == 1
    assert account.trade_records[-1].sell_price == 18999.0


def test_margin_call_close_of_a_short_pays_the_slippage_upward() -> None:
    """空單被斷頭是買進回補，成交價比結算價**高**一個跳動點"""

    settlement: TwFuturesSettlementModel = make_futures_settlement(
        init_capital=450_000, fill_config=ONE_TICK
    )
    open_futures_position(settlement, position_type=PositionType.SHORT)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = {"forced_cover_margin_call": 0}

    quote: FuturesQuote = make_futures_quote(close=21000.0, high=21500.0)
    for position in list(account.get_positions()):
        settlement.position_manager.settle_daily(position, 21000.0)

    settlement.check_margin_call(
        FUTURES_DAY_2, {quote.symbol: quote}, account, event_counts
    )

    assert event_counts["forced_cover_margin_call"] == 1
    assert account.trade_records[-1].buy_price == 21001.0


def test_expired_position_exit_pays_the_slippage() -> None:
    """
    到期兜底出場同樣含滑價（D1）

    這條路徑的價格本來就是估計值（最近一次結算價），滑價讓它偏保守；
    三條強制出場路徑兩種口徑的話，日後看到差異的人無從判斷哪個是刻意的。
    """

    settlement: TwFuturesSettlementModel = make_futures_settlement(fill_config=ONE_TICK)
    open_futures_position(settlement)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = {"forced_cover_no_quote": 0}

    # 連續無報價達 MAX_NO_QUOTE_DAYS 才出場
    for _ in range(settlement.MAX_NO_QUOTE_DAYS):
        settlement.close_expired_positions(FUTURES_DAY_2, {}, account, event_counts)

    assert event_counts["forced_cover_no_quote"] == 1
    assert account.trade_records[-1].sell_price == 19999.0


def test_expired_position_exit_survives_a_missing_quote() -> None:
    """到期出場本來就沒有報價，區間檢查要跳過而不是出錯"""

    event_counts: Dict[str, int] = {
        "forced_cover_no_quote": 0,
        "close_price_out_of_range": 0,
    }
    settlement: TwFuturesSettlementModel = make_futures_settlement(
        fill_config=ONE_TICK, event_counts=event_counts
    )
    open_futures_position(settlement)
    account: FuturesAccount = settlement.position_manager.account

    for _ in range(settlement.MAX_NO_QUOTE_DAYS):
        settlement.close_expired_positions(FUTURES_DAY_2, {}, account, event_counts)

    assert event_counts["close_price_out_of_range"] == 0


def test_futures_forced_exit_without_fill_model_is_unchanged() -> None:
    """未注入 fill_model 時成交價即參考價，既有期貨回測逐筆不變"""

    settlement: TwFuturesSettlementModel = make_futures_settlement()
    open_futures_position(settlement)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = {"forced_cover_no_quote": 0}

    for _ in range(settlement.MAX_NO_QUOTE_DAYS):
        settlement.close_expired_positions(FUTURES_DAY_2, {}, account, event_counts)

    assert account.trade_records[-1].sell_price == 20000.0


# === S4：換月轉倉兩腿 ===
def test_roll_pays_the_slippage_on_both_legs() -> None:
    """
    轉倉的平舊月與開新月**兩腿都吃滑價**（D2）

    實盤換月是真的送兩張單、吃兩次價差；只算平倉腿會讓轉倉成本少一半，
    提前 N 日換月的策略因此系統性低估。
    """

    settlement: TwFuturesSettlementModel = make_futures_settlement(fill_config=ONE_TICK)
    open_futures_position(settlement, expiry="202403", price=20000.0)
    account: FuturesAccount = settlement.position_manager.account
    event_counts: Dict[str, int] = {}

    settlement.on_bar_close(
        FUTURES_DAY_2,
        [make_futures_quote("202404", 20100.0)],
        account,
        event_counts,
    )

    positions = account.get_positions()
    assert len(positions) == 1
    assert positions[0].expiry == "202404"
    # 多單轉倉：舊月賣出往下一檔、新月買進往上一檔
    assert account.trade_records[-1].sell_price == 19999.0
    assert positions[0].price == 20101.0


def test_roll_without_slippage_is_unchanged() -> None:
    """未設滑價時兩腿都是原價，既有轉倉測試的數字不動"""

    settlement: TwFuturesSettlementModel = make_futures_settlement()
    open_futures_position(settlement, expiry="202403", price=20000.0)
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(
        FUTURES_DAY_2, [make_futures_quote("202404", 20100.0)], account, {}
    )

    assert account.get_positions()[0].price == 20100.0


def test_short_roll_pays_the_slippage_on_both_legs() -> None:
    """空單轉倉：舊月買進回補往上、新月賣出開倉往下"""

    settlement: TwFuturesSettlementModel = make_futures_settlement(fill_config=ONE_TICK)
    open_futures_position(
        settlement, expiry="202403", price=20000.0, position_type=PositionType.SHORT
    )
    account: FuturesAccount = settlement.position_manager.account

    settlement.on_bar_close(
        FUTURES_DAY_2, [make_futures_quote("202404", 20100.0)], account, {}
    )

    assert account.trade_records[-1].buy_price == 20001.0
    assert account.get_positions()[0].price == 20099.0
