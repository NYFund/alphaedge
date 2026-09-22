import datetime

import pytest

from core.backtest.models.cost_model import StockCostModel
from core.models import StockTradeRecord
from core.models.cost_config import CostConfig, ShortConstraint
from core.utils import Action, PositionType, ShortMethod
from core.utils.instrument import StockUtils

"""成本模型與數值處理規則的單元測試"""


# === 手算驗收範例 ===
def test_cost_model_day_trade() -> None:
    """100 元放空 1 張、95 元回補的當沖，各項成本與損益需與手算一致"""

    model: StockCostModel = StockCostModel(
        CostConfig.default(ShortMethod.DAY_TRADE, is_day_trade=True)
    )

    open_commission: int = model.commission(price=100.0, volume=1)
    open_tax: int = model.tax(price=100.0, volume=1, action=Action.SELL)
    close_commission: int = model.commission(price=95.0, volume=1)

    assert open_commission == 42  # 100000 × 0.001425 × 0.3 = 42.75 → 捨去
    assert open_tax == 150  # 100000 × 0.0015（當沖減半）
    assert close_commission == 40  # 95000 × 0.001425 × 0.3 = 40.6125 → 捨去

    # 當沖無保證金、無借券費、無利息
    assert model.margin_required(price=100.0, volume=1) == 0
    assert model.borrow_fee(price=100.0, volume=1, holding_days=0) == 0
    assert model.short_interest(proceeds=100000.0, margin=0.0, holding_days=0) == 0

    entry_cost: int = open_commission + open_tax
    pnl: float = model.realized_pnl(
        position_type=PositionType.SHORT,
        entry_price=100.0,
        exit_price=95.0,
        volume=1,
        entry_cost=entry_cost,
        exit_cost=close_commission,
    )
    assert pnl == 4768.0

    roi: float = model.roi(
        position_type=PositionType.SHORT,
        entry_price=100.0,
        exit_price=95.0,
        volume=1,
        entry_cost=entry_cost,
        exit_cost=close_commission,
    )
    assert roi == 4.76  # 4768 / 100192

    roi_on_capital: float = model.roi_on_capital(
        position_type=PositionType.SHORT,
        entry_price=100.0,
        exit_price=95.0,
        volume=1,
        entry_cost=entry_cost,
        exit_cost=close_commission,
        margin=0.0,
    )
    assert roi_on_capital == 2483.33  # 4768 / 192


def test_cost_model_margin_short() -> None:
    """100 元融券放空 1 張、持有 10 天後 95 元回補"""

    model: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.MARGIN))

    open_commission: int = model.commission(price=100.0, volume=1)
    open_tax: int = model.tax(price=100.0, volume=1, action=Action.SELL)
    borrow_fee: int = model.borrow_fee(price=100.0, volume=1)
    margin: int = model.margin_required(price=100.0, volume=1)
    close_commission: int = model.commission(price=95.0, volume=1)
    interest: int = model.short_interest(
        proceeds=100000.0, margin=90000.0, holding_days=10
    )

    assert open_commission == 42
    assert open_tax == 300  # 非當沖，0.3%
    assert borrow_fee == 80  # 100000 × 0.08%
    assert margin == 90000  # 100000 × 90%
    assert close_commission == 40
    assert interest == 10  # 190000 × 0.002 × 10 / 365 = 10.41 → 捨去

    entry_cost: int = open_commission + open_tax + borrow_fee
    pnl: float = model.realized_pnl(
        position_type=PositionType.SHORT,
        entry_price=100.0,
        exit_price=95.0,
        volume=1,
        entry_cost=entry_cost,
        exit_cost=close_commission,
        carry_cost=-interest,  # 利息為收入，故為負的持有成本
    )
    assert pnl == 4548.0

    assert (
        model.roi(
            position_type=PositionType.SHORT,
            entry_price=100.0,
            exit_price=95.0,
            volume=1,
            entry_cost=entry_cost,
            exit_cost=close_commission,
            carry_cost=-interest,
        )
        == 4.53
    )
    assert (
        model.roi_on_capital(
            position_type=PositionType.SHORT,
            entry_price=100.0,
            exit_price=95.0,
            volume=1,
            entry_cost=entry_cost,
            exit_cost=close_commission,
            carry_cost=-interest,
            margin=margin,
        )
        == 5.03
    )


def test_maintenance_ratio() -> None:
    """維持率 =（擔保價款 + 保證金）/ 市值，146 元時剛好觸及 130%"""

    model: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.MARGIN))

    assert round(model.maintenance_ratio(100000.0, 90000.0, 130.0, 1), 4) == 1.4615
    assert model.check_margin_call(100000.0, 90000.0, 130.0, 1) is False
    assert model.check_margin_call(100000.0, 90000.0, 146.5, 1) is True


def test_long_pnl_direction() -> None:
    """做多方向的損益必須與放空相反，且與既有 StockUtils 結果一致"""

    model: StockCostModel = StockCostModel(CostConfig.default())

    pnl: float = model.realized_pnl(
        position_type=PositionType.LONG,
        entry_price=100.0,
        exit_price=110.0,
        volume=1,
        entry_cost=42,
        exit_cost=47 + 330,
    )
    legacy_pnl: float = StockUtils.calculate_net_profit(
        buy_price=100.0, sell_price=110.0, volume=1
    )

    # 與既有 LONG 公式完全一致（買 42、賣 47 + 稅 330）
    assert pnl == legacy_pnl == 9581.0


# === 數值處理規則 ===
def test_rounding_rules() -> None:
    """費用一律無條件捨去、保證金無條件進位"""

    model: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.MARGIN))

    # 手續費 42.75 → 42（若用 round() 會得到 43）
    assert model.commission(price=100.0, volume=1) == 42

    # 最低手續費 20 元
    assert model.commission(price=10.0, volume=1) == 20

    # 證交稅最低 1 元
    assert model.tax(price=0.1, volume=1, action=Action.SELL) == 1

    # 買進不課稅
    assert model.tax(price=100.0, volume=1, action=Action.BUY) == 0

    # 保證金無條件進位：100.005 × 1000 × 0.9 = 90004.5 → 90005
    assert model.margin_required(price=100.005, volume=1) == 90005
    # 浮點尾數不應造成多進一元：33.33 × 1000 × 0.9 實際為 29996.999999999996
    assert model.margin_required(price=33.33, volume=1) == 29997


def test_sbl_borrow_fee_accrues_by_day() -> None:
    """SBL 借券費依持有天數年化計算，MARGIN 則與天數無關"""

    sbl: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.SBL))
    margin: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.MARGIN))

    # 100000 × 3% × 30 / 365 = 246.57 → 捨去
    assert sbl.borrow_fee(price=100.0, volume=1, holding_days=30) == 246
    assert sbl.borrow_fee(price=100.0, volume=1, holding_days=0) == 0

    # MARGIN 一次性收取，持有天數不影響
    assert margin.borrow_fee(price=100.0, volume=1, holding_days=30) == 80
    assert margin.borrow_fee(price=100.0, volume=1, holding_days=0) == 80

    # SBL 不給付融券保證金利息
    assert sbl.short_interest(proceeds=100000.0, margin=90000.0, holding_days=30) == 0


def test_tax_rate_switch() -> None:
    """is_day_trade 切換 0.15% / 0.3%，且 StockUtils 的預設行為不變"""

    model: StockCostModel = StockCostModel(CostConfig.default(ShortMethod.MARGIN))

    assert model.tax(100.0, 1, Action.SELL, is_day_trade=True) == 150
    assert model.tax(100.0, 1, Action.SELL, is_day_trade=False) == 300

    # 既有函式不帶參數時維持原本的 0.3%
    assert StockUtils.calculate_transaction_tax(100.0, 1) == 300
    assert StockUtils.calculate_transaction_tax(100.0, 1, is_day_trade=True) == 150


@pytest.mark.parametrize(
    "price, direction, expected",
    [
        (9.993, "down", 9.99),
        (9.991, "up", 10.0),
        (10.02, "down", 10.0),
        (10.02, "up", 10.05),
        (49.99, "up", 50.0),
        (50.02, "down", 50.0),
        (99.95, "up", 100.0),
        (100.2, "down", 100.0),
        (100.2, "up", 100.5),
        (499.6, "up", 500.0),
        (500.5, "down", 500.0),
        (999.5, "up", 1000.0),
        (1002.0, "down", 1000.0),
        (1002.0, "up", 1005.0),
        (100.3, "nearest", 100.5),
    ],
)
def test_round_to_tick(price: float, direction: str, expected: float) -> None:
    """六段檔位的邊界取整"""

    assert StockUtils.round_to_tick(price, direction) == expected


# === 資料模型 ===
def test_record_entry_exit_semantics() -> None:
    """SHORT 的 entry 是放空賣出、exit 是回補買進；LONG 相反"""

    short_record: StockTradeRecord = StockTradeRecord(
        position_type=PositionType.SHORT,
        sell_date=datetime.date(2024, 3, 1),
        sell_price=100.0,
        buy_date=datetime.date(2024, 5, 1),
        buy_price=95.0,
    )
    assert short_record.entry_date == datetime.date(2024, 3, 1)
    assert short_record.entry_price == 100.0
    assert short_record.exit_date == datetime.date(2024, 5, 1)
    assert short_record.exit_price == 95.0

    long_record: StockTradeRecord = StockTradeRecord(
        position_type=PositionType.LONG,
        buy_date=datetime.date(2024, 3, 1),
        buy_price=100.0,
        sell_date=datetime.date(2024, 5, 1),
        sell_price=110.0,
    )
    assert long_record.entry_date == long_record.buy_date
    assert long_record.exit_date == long_record.sell_date


def test_short_constraint_defaults() -> None:
    """未提供資料時，各項限制檢查一律放行"""

    constraint: ShortConstraint = ShortConstraint()

    assert constraint.check_day_tradable("2330", datetime.date(2024, 1, 2)) is True
    assert constraint.get_force_cover_dates("2330") == []

    limited: ShortConstraint = ShortConstraint(
        day_trade_whitelist={datetime.date(2024, 1, 2): {"2317"}}
    )
    assert limited.check_day_tradable("2330", datetime.date(2024, 1, 2)) is False
    assert limited.check_day_tradable("2317", datetime.date(2024, 1, 2)) is True


# === 成本與稅率的日期邊界===
def test_day_trade_tax_is_full_before_2017_04_28() -> None:
    """
    當沖證交稅減半自 2017-04-28 起實施，之前一律 0.3%

    不看日期就一律減半的話，2013-01 ~ 2017-04 的每一筆當沖賣出都少算一半的稅
    ——約 4 年 4 個月，而且結果只會偏樂觀。
    """

    model: StockCostModel = StockCostModel()

    assert (
        model.tax(
            100.0, 1, Action.SELL, is_day_trade=True, date=datetime.date(2017, 4, 27)
        )
        == 300
    )


def test_day_trade_tax_is_halved_on_the_start_date() -> None:
    """起始日當天就適用減半（含端點）"""

    model: StockCostModel = StockCostModel()

    assert (
        model.tax(
            100.0, 1, Action.SELL, is_day_trade=True, date=datetime.date(2017, 4, 28)
        )
        == 150
    )


def test_day_trade_tax_is_full_after_expiry() -> None:
    """落日之後回到全額稅率"""

    model: StockCostModel = StockCostModel()

    assert (
        model.tax(
            100.0, 1, Action.SELL, is_day_trade=True, date=datetime.date(2028, 1, 3)
        )
        == 300
    )


def test_tax_without_date_keeps_current_regime() -> None:
    """沒有日期資訊時視為現行制度，維持舊行為（既有呼叫端不受影響）"""

    model: StockCostModel = StockCostModel()

    assert model.tax(100.0, 1, Action.SELL, is_day_trade=True) == 150


def test_non_day_trade_tax_ignores_the_date() -> None:
    """非當沖一律全額，日期不影響"""

    model: StockCostModel = StockCostModel()

    assert (
        model.tax(
            100.0, 1, Action.SELL, is_day_trade=False, date=datetime.date(2016, 1, 4)
        )
        == 300
    )


# === 落日警示要看回測區間，不看真實今天 ===
def make_day_trade_config(start: datetime.date, end: datetime.date) -> CostConfig:
    """建一份帶回測區間的當沖成本設定（factory 平常會這樣注入）"""

    config: CostConfig = CostConfig.default(ShortMethod.DAY_TRADE, is_day_trade=True)
    config.backtest_start_date = start
    config.backtest_end_date = end
    return config


def test_no_warning_when_backtest_stays_inside_the_tax_window() -> None:
    """回測區間完整落在 [2017-04-28, 2027-12-31] 內時不該有任何提示"""

    from loguru import logger

    messages: list = []
    handler_id: int = logger.add(messages.append, level="WARNING")
    try:
        StockCostModel(
            make_day_trade_config(
                datetime.date(2020, 1, 2), datetime.date(2024, 12, 31)
            )
        )
    finally:
        logger.remove(handler_id)

    assert not [m for m in messages if "當沖證交稅減半" in str(m)]


def test_warns_when_backtest_runs_past_the_expiry_date() -> None:
    """
    回測跑過落日日期就要警示，且要指出是**哪一段**

    舊版看的是 `datetime.date.today()`，而落日日期是 2027-12-31——
    這條警告在 2028 年之前永遠不會觸發；反過來，拿當沖策略回測到 2028 年時
    稅率假設早已失效卻沒有任何提示。
    """

    from loguru import logger

    messages: list = []
    handler_id: int = logger.add(messages.append, level="WARNING")
    try:
        StockCostModel(
            make_day_trade_config(datetime.date(2026, 1, 5), datetime.date(2028, 6, 30))
        )
    finally:
        logger.remove(handler_id)

    warnings: list = [str(m) for m in messages if "當沖證交稅減半" in str(m)]
    assert len(warnings) == 1
    assert "2028-01-01 ~ 2028-06-30" in warnings[0]
    assert "落日日期" in warnings[0]


def test_warns_when_backtest_starts_before_the_halving_took_effect() -> None:
    """區間跨到 2017-04-28 之前同樣要警示——那段一律課全額 0.3%"""

    from loguru import logger

    messages: list = []
    handler_id: int = logger.add(messages.append, level="WARNING")
    try:
        StockCostModel(
            make_day_trade_config(
                datetime.date(2015, 1, 5), datetime.date(2020, 12, 31)
            )
        )
    finally:
        logger.remove(handler_id)

    warnings: list = [str(m) for m in messages if "當沖證交稅減半" in str(m)]
    assert len(warnings) == 1
    assert "2015-01-05 ~ 2017-04-27" in warnings[0]
    assert "減半實施日" in warnings[0]


def test_stays_silent_when_the_backtest_range_is_unknown() -> None:
    """
    拿不到回測區間時維持靜默，不退回看 `today()`

    寧可不說，也不要說錯：退回看真實今天就會回到舊版的錯誤方向。
    """

    from loguru import logger

    messages: list = []
    handler_id: int = logger.add(messages.append, level="WARNING")
    try:
        StockCostModel(CostConfig.default(ShortMethod.DAY_TRADE, is_day_trade=True))
    finally:
        logger.remove(handler_id)

    assert not [m for m in messages if "當沖證交稅減半" in str(m)]


def test_non_day_trade_never_warns_about_the_tax_window() -> None:
    """非當沖策略與這條稅制邊界無關，跨多遠都不該提示"""

    from loguru import logger

    config: CostConfig = CostConfig.default(ShortMethod.MARGIN, is_day_trade=False)
    config.backtest_start_date = datetime.date(2015, 1, 5)
    config.backtest_end_date = datetime.date(2028, 6, 30)

    messages: list = []
    handler_id: int = logger.add(messages.append, level="WARNING")
    try:
        StockCostModel(config)
    finally:
        logger.remove(handler_id)

    assert not [m for m in messages if "當沖證交稅減半" in str(m)]


def test_factory_injects_the_backtest_range_into_the_cost_config() -> None:
    """
    警示要成立，前提是 factory 真的把回測區間交給 CostModel

    `CostConfig` 多兩個欄位而沒有人填，等於這條警告換一種方式繼續不會觸發。
    """

    from core.backtest.factory import build_cost_config

    class _Strategy:
        position_type = PositionType.SHORT
        enable_intraday = True
        short_method = ShortMethod.DAY_TRADE
        cost_config = None
        short_constraint = None
        start_date = datetime.date(2026, 1, 5)
        end_date = datetime.date(2028, 6, 30)

    config: CostConfig = build_cost_config(_Strategy())

    assert config.backtest_start_date == datetime.date(2026, 1, 5)
    assert config.backtest_end_date == datetime.date(2028, 6, 30)
