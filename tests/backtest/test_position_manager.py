import datetime
from typing import List, Optional

from core.backtest.models.cost_model import CostConfig, ShortConstraint, StockCostModel
from core.managers.stock.position_manager import StockPositionManager
from core.models import StockAccount, StockPosition, StockTradeRecord
from core.utils import Action, PositionType, ShortMethod

"""放空開平倉記帳測試"""


def build_manager(
    short_method: ShortMethod = ShortMethod.MARGIN,
    is_day_trade: bool = False,
    constraint: Optional[ShortConstraint] = None,
    init_capital: float = 1000000.0,
) -> StockPositionManager:
    """建立指定放空管道的部位管理器"""

    config: CostConfig = CostConfig.default(short_method, is_day_trade)
    if constraint is not None:
        config.short_constraint = constraint

    return StockPositionManager(StockAccount(init_capital), StockCostModel(config))


def test_short_open_position_margin(make_order) -> None:
    """融券開倉扣「保證金 + 開倉成本」，賣出價款留作擔保品不計入餘額"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)

    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
            short_method=ShortMethod.MARGIN,
        )
    )

    assert position is not None
    assert position.commission == 42
    assert position.tax == 300
    assert position.borrow_fee == 80
    assert position.margin == 90000
    assert position.short_proceeds == 100000.0

    # 開倉時餘額變化 = −(90000 + 42 + 300 + 80)
    assert manager.account.balance == 1000000.0 - 90422
    assert manager.account.margin_used == 90000


def test_short_open_position_day_trade(make_order) -> None:
    """當沖開倉不佔保證金、稅率減半、無借券費"""

    manager: StockPositionManager = build_manager(
        ShortMethod.DAY_TRADE, is_day_trade=True
    )

    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
            short_method=ShortMethod.DAY_TRADE,
            is_day_trade=True,
        )
    )

    assert position is not None
    assert position.tax == 150  # 當沖減半
    assert position.margin == 0
    assert position.borrow_fee == 0
    assert manager.account.balance == 1000000.0 - 192
    assert manager.account.margin_used == 0


def test_short_open_position_rejected_by_balance(make_order) -> None:
    """保證金不足時拒絕開倉並回傳 None，不得靜默失敗"""

    manager: StockPositionManager = build_manager(
        ShortMethod.MARGIN, init_capital=10000.0
    )

    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
        )
    )

    assert position is None
    assert manager.account.positions == []
    assert manager.account.balance == 10000.0


def test_short_open_position_rejected_by_exposure_limit(make_order) -> None:
    """單一標的曝險超過上限時拒絕開倉"""

    manager: StockPositionManager = build_manager(
        ShortMethod.MARGIN,
        constraint=ShortConstraint(max_short_exposure_ratio=0.05),
    )

    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,  # 曝險 100000 > 1000000 × 5%
        )
    )

    assert position is None


def test_reject_opposite_direction_position(make_order) -> None:
    """同一標的已有多單時不得開空單"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)

    manager.open_position(
        make_order(action=Action.BUY, position_type=PositionType.LONG, price=100.0)
    )
    position: Optional[StockPosition] = manager.open_position(
        make_order(action=Action.SELL, position_type=PositionType.SHORT, price=100.0)
    )

    assert position is None
    assert len(manager.account.positions) == 1


def test_short_open_close_roundtrip_margin(make_order) -> None:
    """融券持有 10 天後回補，損益、餘額與保證金三者一致"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)

    manager.open_position(
        make_order(
            date=datetime.date(2024, 1, 2),
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
        )
    )

    records: List[StockTradeRecord] = manager.close_position(
        make_order(
            date=datetime.date(2024, 1, 12),  # 持有 10 個曆日
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=95.0,
            volume=1,
        )
    )

    assert len(records) == 1
    record: StockTradeRecord = records[0]

    assert record.holding_days == 10
    assert record.interest == 10
    assert record.borrow_fee == 80
    assert record.margin == 90000
    assert record.realized_pnl == 4548.0
    assert record.roi == 4.53
    assert record.roi_on_capital == 5.03

    # entry 為放空開倉、exit 為回補
    assert record.entry_date == datetime.date(2024, 1, 2)
    assert record.entry_price == 100.0
    assert record.exit_date == datetime.date(2024, 1, 12)
    assert record.exit_price == 95.0

    # 平倉後保證金釋回，餘額 = 初始 + 已實現損益
    assert manager.account.margin_used == 0
    assert manager.account.balance == 1000000.0 + 4548.0
    assert manager.account.realized_pnl == 4548.0
    assert manager.account.positions == []


def test_short_open_close_roundtrip_day_trade(make_order) -> None:
    """當沖同日開平倉，損益 4768 且不佔保證金"""

    manager: StockPositionManager = build_manager(
        ShortMethod.DAY_TRADE, is_day_trade=True
    )
    trade_date: datetime.date = datetime.date(2024, 1, 2)

    manager.open_position(
        make_order(
            date=trade_date,
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
            is_day_trade=True,
        )
    )
    records: List[StockTradeRecord] = manager.close_position(
        make_order(
            date=trade_date,
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=95.0,
            volume=1,
        )
    )

    record: StockTradeRecord = records[0]
    assert record.holding_days == 0
    assert record.interest == 0
    assert record.realized_pnl == 4768.0
    assert record.roi == 4.76
    assert manager.account.balance == 1000000.0 + 4768.0


def test_short_loss_when_price_rises(make_order) -> None:
    """放空遇股價上漲須為虧損（方向不能寫反）"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)

    manager.open_position(
        make_order(
            date=datetime.date(2024, 1, 2),
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=1,
        )
    )
    records: List[StockTradeRecord] = manager.close_position(
        make_order(
            date=datetime.date(2024, 1, 3),
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=110.0,
            volume=1,
        )
    )

    assert records[0].realized_pnl < 0
    assert manager.account.balance < 1000000.0


def test_short_partial_cover_fifo(make_order) -> None:
    """開兩筆放空、只回補部分，保證金與擔保價款須等比例攤提"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)

    # 第一筆 2 張、第二筆 1 張
    manager.open_position(
        make_order(
            date=datetime.date(2024, 1, 2),
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            volume=2,
        )
    )
    manager.open_position(
        make_order(
            date=datetime.date(2024, 1, 3),
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=105.0,
            volume=1,
        )
    )
    assert manager.account.margin_used == 180000 + 94500

    # 只回補 1 張，應優先回補最早的部位（FIFO）
    records: List[StockTradeRecord] = manager.close_position(
        make_order(
            date=datetime.date(2024, 1, 12),
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=95.0,
            volume=1,
        )
    )

    assert len(records) == 1
    assert records[0].entry_price == 100.0  # FIFO：先回補最早開的那筆
    assert records[0].margin == 90000  # 180000 的一半

    remaining: List[StockPosition] = manager.account.get_positions(
        position_type=PositionType.SHORT
    )
    assert len(remaining) == 2
    assert remaining[0].volume == 1
    assert remaining[0].margin == 90000
    assert remaining[0].short_proceeds == 100000.0
    assert manager.account.margin_used == 90000 + 94500


def test_close_position_ignores_opposite_direction(make_order) -> None:
    """做多的平倉單不得動到同標的的放空部位（FIFO 篩選須含方向）"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)
    manager.account.positions.append(
        StockPosition(
            id=99,
            stock_id="2330",
            position_type=PositionType.SHORT,
            short_method=ShortMethod.MARGIN,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
            margin=90000.0,
            short_proceeds=100000.0,
        )
    )

    # 送出做多的平倉單（SELL），不應影響放空部位
    records: List[StockTradeRecord] = manager.close_position(
        make_order(
            date=datetime.date(2024, 1, 3),
            action=Action.SELL,
            position_type=PositionType.LONG,
            price=110.0,
            volume=1,
        )
    )

    assert records == []
    assert len(manager.account.get_positions(position_type=PositionType.SHORT)) == 1


# === 同標的雙向持倉：兩個方向都要擋===
def test_short_after_long_is_rejected(make_order) -> None:
    """先做多再放空會被擋（既有行為）"""

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)
    manager.open_position(
        make_order(action=Action.BUY, position_type=PositionType.LONG, price=100.0)
    )

    blocked: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            short_method=ShortMethod.MARGIN,
        )
    )

    assert blocked is None


def test_long_after_short_is_also_rejected(make_order) -> None:
    """
    先放空再做多**同樣**要被擋（反之亦然）

    舊版只在放空端檢查，於是同一檔會同時掛著多空兩個部位——兩邊各自盯市、
    各自計算維持率，帳面曝險與實際完全對不上。
    """

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)
    manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            short_method=ShortMethod.MARGIN,
        )
    )

    blocked: Optional[StockPosition] = manager.open_position(
        make_order(action=Action.BUY, position_type=PositionType.LONG, price=100.0)
    )

    assert blocked is None
    assert len(manager.account.get_positions()) == 1


def test_long_is_allowed_after_the_short_is_covered(make_order) -> None:
    """
    回補之後就該能做多——否則這道防線會變成「一輩子不能再碰這檔」

    這條同時釘住已平倉部位的處理：`check_has_position()` 若不濾 `is_closed`，
    已平倉的部位會讓反向開倉被永久拒絕。
    """

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)
    short_position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.SELL,
            position_type=PositionType.SHORT,
            price=100.0,
            short_method=ShortMethod.MARGIN,
        )
    )
    assert short_position is not None

    manager.close_position(
        make_order(
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=98.0,
            volume=short_position.volume,
        )
    )

    long_position: Optional[StockPosition] = manager.open_position(
        make_order(action=Action.BUY, position_type=PositionType.LONG, price=100.0)
    )

    assert long_position is not None


# === 做多平倉的當沖稅 ===
def test_long_overnight_sell_is_taxed_at_the_full_rate(make_order) -> None:
    """
    做多部位**隔夜**賣出一律是一般稅率

    `cost_model.tax()` 遇 `is_day_trade=None` 會取 `config.is_day_trade`，
    而那是策略層的開關。放空策略開了當沖、`allowed_directions` 又含 LONG 時，
    做多部位持有 7 天後賣出也會吃到減半稅（實測 150，應為 300）。
    """

    manager: StockPositionManager = build_manager(
        ShortMethod.DAY_TRADE, is_day_trade=True
    )
    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.BUY,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
        )
    )
    assert position is not None

    record: StockTradeRecord = manager.close_long_position(
        position=position,
        stock_order=make_order(
            action=Action.SELL,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 9),
            price=100.0,
            volume=1,
        ),
        close_volume=1,
    )

    assert record.tax == 300


def test_long_same_day_sell_keeps_the_day_trade_rate(make_order) -> None:
    """開倉、平倉同一天且策略確實開了當沖時，維持減半稅（防止改過頭）"""

    manager: StockPositionManager = build_manager(
        ShortMethod.DAY_TRADE, is_day_trade=True
    )
    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.BUY,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
        )
    )
    assert position is not None

    record: StockTradeRecord = manager.close_long_position(
        position=position,
        stock_order=make_order(
            action=Action.SELL,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
        ),
        close_volume=1,
    )

    assert record.tax == 150


def test_long_same_day_sell_without_day_trade_config_is_full_rate(make_order) -> None:
    """
    沒開當沖的策略即使當日來回也是一般稅率

    現股當沖要事先簽署同意書，不是「同一天賣掉」就自動成立；
    只看日期會讓從未打算當沖的策略憑空少繳一半稅。
    """

    manager: StockPositionManager = build_manager(ShortMethod.MARGIN)
    position: Optional[StockPosition] = manager.open_position(
        make_order(
            action=Action.BUY,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
        )
    )
    assert position is not None

    record: StockTradeRecord = manager.close_long_position(
        position=position,
        stock_order=make_order(
            action=Action.SELL,
            position_type=PositionType.LONG,
            date=datetime.date(2024, 1, 2),
            price=100.0,
            volume=1,
        ),
        close_volume=1,
    )

    assert record.tax == 300
