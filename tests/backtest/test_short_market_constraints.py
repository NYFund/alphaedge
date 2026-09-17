import datetime
from typing import Dict, List, Optional, Set

import pandas as pd

from core.backtest.backtester import new_event_counts
from core.backtest.datafeed.tw.market_calendar import MarketCalendar
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.backtest.models.cost_model import CostConfig, ShortConstraint, StockCostModel
from core.backtest.models.fill_model import TwStockFillModel
from core.backtest.models.settlement_model import TwStockSettlementModel
from core.managers.stock.position_manager import StockPositionManager
from core.models import StockAccount, StockOrder, StockPosition, StockQuote
from core.utils import Action, PositionType, Scale, ShortMethod

"""
放空的市場約束測試：除權息停券強制回補與股利補償

兩者共用 `dividend` 表這一份資料，但解的是不同問題——停券回補讓留倉放空不再
無限續抱，股利補償讓跨除息日的空單付出該付的現金流。全部為純記憶體物件，不連資料庫。
"""

STOCK_ID: str = "2330"

# 2024-01-02 ~ 2024-01-12 的實際台股交易日（1/6、1/7 為週末）
TRADING_DAYS: List[datetime.date] = [
    datetime.date(2024, 1, 2),
    datetime.date(2024, 1, 3),
    datetime.date(2024, 1, 4),
    datetime.date(2024, 1, 5),
    datetime.date(2024, 1, 8),
    datetime.date(2024, 1, 9),
    datetime.date(2024, 1, 10),
    datetime.date(2024, 1, 11),
    datetime.date(2024, 1, 12),
]


# === 交易日平移 ===
def test_shift_trading_days_skips_weekends() -> None:
    """往前推 4 個營業日必須跳過週末，不可用曆日相減"""

    # 1/10 往前推 4 個營業日 → 1/4（中間的 1/6、1/7 為週末，不計）
    shifted: Optional[datetime.date] = MarketCalendar.shift_trading_days(
        TRADING_DAYS, datetime.date(2024, 1, 10), -4
    )

    assert shifted == datetime.date(2024, 1, 4)


def test_shift_trading_days_returns_none_when_out_of_range() -> None:
    """推算結果早於清單起點時回傳 None，代表交易日資料不足以推算"""

    assert (
        MarketCalendar.shift_trading_days(TRADING_DAYS, datetime.date(2024, 1, 3), -4)
        is None
    )


def test_shift_trading_days_accepts_non_trading_base_date() -> None:
    """基準日為非交易日時，以不早於它的第一個交易日為準（1/6 → 1/8）"""

    shifted: Optional[datetime.date] = MarketCalendar.shift_trading_days(
        TRADING_DAYS, datetime.date(2024, 1, 6), -1
    )

    assert shifted == datetime.date(2024, 1, 5)


# === 停券日推導 ===
class StubPriceAPI:
    """只提供交易日曆的假 price API"""

    def get_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        return [day for day in TRADING_DAYS if start_date <= day <= end_date]


class StubDividendAPI:
    """只提供除權息日的假 dividend API"""

    def __init__(self, rows: List[Dict[str, object]]) -> None:
        self.rows: List[Dict[str, object]] = rows

    def get_range(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def make_data_feed(rows: List[Dict[str, object]]) -> TwStockDataFeed:
    """組出只掛了兩個 stub API 的 DataFeed（不呼叫 setup()，不連任何資料庫）"""

    data_feed: TwStockDataFeed = TwStockDataFeed()
    data_feed.price = StubPriceAPI()
    data_feed.dividend = StubDividendAPI(rows)
    data_feed.start_date = TRADING_DAYS[0]
    data_feed.end_date = TRADING_DAYS[-1]
    return data_feed


def test_force_cover_date_is_four_trading_days_before_ex_date() -> None:
    """融券最後回補日 = 除權息交易日前 4 個營業日（停止過戶日前 6 個營業日）"""

    data_feed: TwStockDataFeed = make_data_feed(
        [{"date": "2024-01-10", "stock_id": STOCK_ID}]
    )

    assert data_feed.get_force_cover_symbols(datetime.date(2024, 1, 4)) == {STOCK_ID}
    # 除權息交易日當天已經不是回補日
    assert data_feed.get_force_cover_symbols(datetime.date(2024, 1, 10)) == set()


def test_force_cover_map_skips_ex_dates_too_close_to_start() -> None:
    """回補日早於回測起點的除權息不納入：那段停券發生在回測開始之前"""

    data_feed: TwStockDataFeed = make_data_feed(
        [{"date": "2024-01-03", "stock_id": STOCK_ID}]
    )

    assert data_feed.build_force_cover_map() == {}


def test_force_cover_map_is_built_once() -> None:
    """整場回測只推導一次；dividend 與 price 表在回測期間不會變動"""

    data_feed: TwStockDataFeed = make_data_feed(
        [{"date": "2024-01-10", "stock_id": STOCK_ID}]
    )

    data_feed.get_force_cover_symbols(datetime.date(2024, 1, 4))
    cached = data_feed.force_cover_map

    data_feed.get_force_cover_symbols(datetime.date(2024, 1, 5))

    assert data_feed.force_cover_map is cached


def test_force_cover_symbols_empty_without_setup() -> None:
    """未跑 setup() 時回傳空集合——沒有資料源可查，不是「今日無標的停券」"""

    assert TwStockDataFeed().get_force_cover_symbols(datetime.date(2024, 1, 4)) == set()


# === 停券強制回補的適用範圍 ===
def make_settlement(
    account: StockAccount,
    short_constraint: Optional[ShortConstraint] = None,
    compensate_cash_dividend: bool = True,
) -> TwStockSettlementModel:
    """組出台股結算模型（成本設定可逐項覆寫）"""

    config: CostConfig = CostConfig.default()
    config.compensate_cash_dividend = compensate_cash_dividend
    if short_constraint is not None:
        config.short_constraint = short_constraint

    cost_model: StockCostModel = StockCostModel(config)
    return TwStockSettlementModel(
        position_manager=StockPositionManager(account, cost_model),
        cost_model=cost_model,
        prev_close={},
    )


def make_short_position(
    date: datetime.date = datetime.date(2024, 1, 2),
    volume: int = 2,
    short_method: ShortMethod = ShortMethod.MARGIN,
) -> StockPosition:
    """建立留倉放空部位"""

    return StockPosition(
        id=1,
        stock_id=STOCK_ID,
        position_type=PositionType.SHORT,
        date=date,
        price=100.0,
        volume=volume,
        short_method=short_method,
        margin=100.0 * volume * 1000 * 0.9,
        short_proceeds=100.0 * volume * 1000,
    )


def test_derived_force_cover_only_applies_to_margin() -> None:
    """行事曆推導的回補日屬融券制度，只對 MARGIN 生效"""

    settlement: TwStockSettlementModel = make_settlement(StockAccount(1000000.0))
    settlement.apply_force_cover_symbols({STOCK_ID})
    date: datetime.date = datetime.date(2024, 1, 4)

    assert settlement.check_force_cover(
        date, make_short_position(short_method=ShortMethod.MARGIN)
    )
    # SBL 借券不受強制回補約束，改以除息日的股利補償反映成本
    assert not settlement.check_force_cover(
        date, make_short_position(short_method=ShortMethod.SBL)
    )


def test_manual_force_cover_dates_apply_to_every_short_method() -> None:
    """使用者明示指定的回補日不分放空管道一律適用，引擎不再加條件"""

    settlement: TwStockSettlementModel = make_settlement(
        StockAccount(1000000.0),
        short_constraint=ShortConstraint(
            force_cover_dates={STOCK_ID: [datetime.date(2024, 1, 4)]}
        ),
    )

    assert settlement.check_force_cover(
        datetime.date(2024, 1, 4),
        make_short_position(short_method=ShortMethod.SBL),
    )


def test_auto_force_cover_can_be_disabled() -> None:
    """關掉自動推導後只認手動指定的日期"""

    settlement: TwStockSettlementModel = make_settlement(
        StockAccount(1000000.0),
        short_constraint=ShortConstraint(auto_force_cover_on_ex_dividend=False),
    )
    settlement.apply_force_cover_symbols({STOCK_ID})

    assert not settlement.check_force_cover(
        datetime.date(2024, 1, 4), make_short_position()
    )


# === 股利補償 ===
def make_account_with_short(
    open_date: datetime.date = datetime.date(2024, 1, 2),
    volume: int = 2,
    short_method: ShortMethod = ShortMethod.SBL,
) -> StockAccount:
    """帳戶內掛一筆留倉空單"""

    account: StockAccount = StockAccount(1000000.0)
    account.positions.append(
        make_short_position(date=open_date, volume=volume, short_method=short_method)
    )
    return account


def test_cash_dividend_is_charged_to_short_position() -> None:
    """跨除息日的空單補償出借方股利，並同額扣減帳戶餘額"""

    account: StockAccount = make_account_with_short()
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 2.0})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(datetime.date(2024, 1, 4), account, event_counts)

    # 2 元／股 × 2 張 × 1000 股
    assert account.positions[0].dividend_compensation == 4000
    assert account.balance == 1000000.0 - 4000
    assert event_counts["dividend_compensation_paid"] == 1


def test_position_opened_on_ex_date_is_not_charged() -> None:
    """除權息交易日當天賣出者已不含權，不需補償"""

    date: datetime.date = datetime.date(2024, 1, 4)
    account: StockAccount = make_account_with_short(open_date=date)
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 2.0})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(date, account, event_counts)

    assert account.positions[0].dividend_compensation == 0
    assert event_counts["dividend_compensation_paid"] == 0


def test_unknown_cash_dividend_is_counted_not_guessed() -> None:
    """權息並存拆不出現金股利時不猜 0，改記入 unknown 讓報表看得見"""

    account: StockAccount = make_account_with_short()
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: float("nan")})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(datetime.date(2024, 1, 4), account, event_counts)

    assert account.positions[0].dividend_compensation == 0
    assert account.balance == 1000000.0
    assert event_counts["dividend_compensation_unknown"] == 1


def test_pure_stock_dividend_has_no_cash_flow() -> None:
    """純除權（現金股利為 0）不產生補償現金流"""

    account: StockAccount = make_account_with_short()
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 0.0})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(datetime.date(2024, 1, 4), account, event_counts)

    assert account.positions[0].dividend_compensation == 0
    assert event_counts["dividend_compensation_paid"] == 0


def test_compensation_can_be_disabled() -> None:
    """關掉股利補償後回到「留倉放空不受除息影響」的舊假設"""

    account: StockAccount = make_account_with_short()
    settlement: TwStockSettlementModel = make_settlement(
        account, compensate_cash_dividend=False
    )
    settlement.apply_cash_dividends({STOCK_ID: 2.0})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(datetime.date(2024, 1, 4), account, event_counts)

    assert account.positions[0].dividend_compensation == 0
    assert account.balance == 1000000.0


def test_compensation_is_prorated_on_partial_cover() -> None:
    """部分回補時股利補償依張數攤提，且現金流不重複計算"""

    account: StockAccount = make_account_with_short(volume=4)
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 1.5})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(datetime.date(2024, 1, 4), account, event_counts)
    # 1.5 元／股 × 4 張 × 1000 股
    assert account.positions[0].dividend_compensation == 6000

    close_order: StockOrder = StockOrder(
        stock_id=STOCK_ID,
        date=datetime.date(2024, 1, 5),
        action=Action.BUY,
        position_type=PositionType.SHORT,
        price=100.0,
        volume=1,
        short_method=ShortMethod.SBL,
    )
    record = settlement.position_manager.close_position(close_order)[0]

    assert record.dividend_compensation == 1500
    assert account.positions[0].dividend_compensation == 4500

    # 補償在除息當日已一次扣掉 6,000；平倉時攤提進 realized_pnl 的那 1,500 必須
    # 加回餘額，否則同一筆被扣兩次。剩下的 4,500 仍掛在未平倉的 3 張上
    assert account.balance == round(
        1000000.0 - 6000 + 1500 + 90000.0 + record.realized_pnl, 2
    )
    # 這一張的補償只透過損益扣了一次
    assert record.realized_pnl == round(-1500 - record.commission, 2)


# === 事件計數 ===
def test_new_event_counts_contains_dividend_keys() -> None:
    """報表的事件表必須看得見股利補償，否則這筆成本只會沉在損益裡"""

    counts: Set[str] = set(new_event_counts())

    assert {"dividend_compensation_paid", "dividend_compensation_unknown"} <= counts


# === 當沖轉融券留倉的餘額檢查 ===
def make_day_trade_position(price: float = 500.0, volume: int = 1) -> StockPosition:
    """建立當沖放空部位（尚未回補）"""

    position: StockPosition = make_short_position(
        volume=volume, short_method=ShortMethod.DAY_TRADE
    )
    position.price = price
    position.is_day_trade = True
    return position


def test_conversion_is_rejected_when_balance_is_insufficient() -> None:
    """
    餘額不足以支應保證金時不可硬轉留倉

    實跑：帳戶 10,000 元、當沖放空 500 元 × 1 張，舊行為轉留倉後餘額 −442,113
    且沒有任何拒絕或計數；之後的維持率追繳只看單一部位的擔保維持率、
    不看帳戶現金，負餘額會一路留著。
    """

    account: StockAccount = StockAccount(10000.0)
    position: StockPosition = make_day_trade_position()
    account.positions.append(position)
    settlement: TwStockSettlementModel = make_settlement(account)
    event_counts: Dict[str, int] = new_event_counts()

    settlement.convert_to_margin_position(
        position, account, datetime.date(2024, 1, 4), 500.0, event_counts
    )

    assert event_counts["forced_cover_insufficient_margin"] == 1
    assert account.balance >= 0
    assert account.get_positions(position_type=PositionType.SHORT) == []


def test_conversion_proceeds_when_balance_is_enough() -> None:
    """餘額足夠時照常轉為融券留倉（防止改過頭）"""

    account: StockAccount = StockAccount(1000000.0)
    position: StockPosition = make_day_trade_position()
    account.positions.append(position)
    settlement: TwStockSettlementModel = make_settlement(account)
    event_counts: Dict[str, int] = new_event_counts()

    settlement.convert_to_margin_position(
        position, account, datetime.date(2024, 1, 4), 500.0, event_counts
    )

    assert event_counts["forced_cover_insufficient_margin"] == 0
    assert position.is_day_trade is False
    assert position.short_method == ShortMethod.MARGIN
    assert account.balance >= 0


# === 停券期間不得新增融券賣出 ===
def make_stock_quote() -> StockQuote:
    """當日有量的正常報價"""

    return StockQuote(
        stock_id=STOCK_ID,
        scale=Scale.DAY,
        date=datetime.date(2024, 1, 4),
        cur_price=100.0,
        volume=10_000,
        open=100.0,
        high=105.0,
        low=95.0,
        close=100.0,
    )


def make_fill_model(suspended: Set[str]) -> TwStockFillModel:
    """組出帶停券清單的成交模型"""

    fill_model: TwStockFillModel = TwStockFillModel(event_counts=new_event_counts())
    fill_model.apply_short_suspended_symbols(suspended)
    return fill_model


def make_short_order(
    short_method: ShortMethod = ShortMethod.MARGIN,
    action: Action = Action.SELL,
    position_type: PositionType = PositionType.SHORT,
) -> StockOrder:
    """建立放空開倉單"""

    return StockOrder(
        stock_id=STOCK_ID,
        date=datetime.date(2024, 1, 4),
        action=action,
        position_type=position_type,
        price=100.0,
        volume=1,
        short_method=short_method,
    )


def test_margin_short_is_rejected_during_the_suspension_window() -> None:
    """
    停券期間的融券放空開倉被拒並計數

    引擎原本只在「最後回補日」當天強制回補，回補日之後到除權息交易日之間
    沒有任何擋板，留倉放空策略可以在停券期間開新的融券空單並持有跨過除權息。
    """

    fill_model: TwStockFillModel = make_fill_model({STOCK_ID})
    quote: StockQuote = make_stock_quote()

    assert fill_model.fill(make_short_order(), quote) is None
    assert fill_model.event_counts["rejected_short_suspended"] == 1


def test_sbl_short_is_not_affected_by_the_suspension() -> None:
    """SBL 借券不受停券限制（其跨除息的成本由股利補償反映），不擋"""

    fill_model: TwStockFillModel = make_fill_model({STOCK_ID})
    order: StockOrder = make_short_order(short_method=ShortMethod.SBL)

    assert fill_model.fill(order, make_stock_quote()) is order
    assert fill_model.event_counts["rejected_short_suspended"] == 0


def test_other_symbols_are_not_affected() -> None:
    """不在停券清單上的標的照常成交（防止改過頭）"""

    fill_model: TwStockFillModel = make_fill_model({"2317"})
    order: StockOrder = make_short_order()

    assert fill_model.fill(order, make_stock_quote()) is order
    assert fill_model.event_counts["rejected_short_suspended"] == 0


def test_cover_order_is_not_blocked_during_the_suspension() -> None:
    """停券期間仍可回補（買進），否則部位會被鎖死在倉裡"""

    fill_model: TwStockFillModel = make_fill_model({STOCK_ID})
    order: StockOrder = make_short_order(action=Action.BUY)

    assert fill_model.fill(order, make_stock_quote()) is order
    assert fill_model.event_counts["rejected_short_suspended"] == 0
