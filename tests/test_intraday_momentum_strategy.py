import datetime
from typing import Any, Dict, List, Optional

import pytest

from core.models import StockAccount, StockOrder, StockPosition, StockQuote
from core.models.fill_config import VolumeCapPolicy
from core.models.stock.trading_list import DayTradeListSnapshot
from core.portfolio.order_rules import get_execution_sequence
from core.strategies.stock.intraday_momentum_strategy import IntradayMomentumStrategy
from core.utils import Action, BarExecutionSequence, PositionType, Scale
from tests.conftest import build_stock_quote

"""盤中動能策略（日 K 近似）的訊號測試：全部為純記憶體物件，不連資料庫"""


# 連假讓前一交易日與當日隔了四個曆日，用來驗營業日平移
DAY_T1: datetime.date = datetime.date(2024, 1, 3)
DAY_T: datetime.date = datetime.date(2024, 1, 8)
DAY_NEXT: datetime.date = datetime.date(2024, 1, 9)
TRADING_DAYS: List[datetime.date] = [DAY_T1, DAY_T, DAY_NEXT]

# 平盤價 100：觸發價 109、停損價為嚴格低於 108 的最高檔位 107.5（100 元以上檔位 0.5）
BASE_REFERENCE: float = 100.0
TRIGGER_PRICE: float = 109.0
STOP_PRICE: float = 107.5
BASE_VOLUME_LOTS: int = 5000


class FakeDividendAPI:
    """只回傳腳本給定的除權息開盤競價基準"""

    def __init__(self, basis_by_date: Dict[datetime.date, Dict[str, float]]) -> None:
        self.basis_by_date: Dict[datetime.date, Dict[str, float]] = basis_by_date

    def get_opening_reference_price_map(self, date: datetime.date) -> Dict[str, float]:
        """除權息日的開盤競價基準；非除權息日為空 dict"""

        return dict(self.basis_by_date.get(date, {}))


class FakePriceAPI:
    """只回傳腳本給定的收盤價與交易日"""

    def __init__(
        self,
        close_map_by_date: Dict[datetime.date, Dict[str, Any]],
        opening_basis: Optional[Dict[datetime.date, Dict[str, float]]] = None,
    ) -> None:
        self.close_map_by_date: Dict[datetime.date, Dict[str, Any]] = close_map_by_date
        self.dividend_api: FakeDividendAPI = FakeDividendAPI(opening_basis or {})

    def get_dividend_api(self) -> FakeDividendAPI:
        """除權息 API"""

        return self.dividend_api

    def get_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """回傳固定的交易日"""

        return [day for day in TRADING_DAYS if start_date <= day <= end_date]

    def get_close_map(self, date: datetime.date) -> Dict[str, Any]:
        """原始收盤價；回傳副本，呼叫端覆寫不會汙染腳本"""

        return dict(self.close_map_by_date.get(date, {}))


class FakeFeed:
    """只提供現股當沖名單的資料源"""

    def __init__(self, day_tradable: Optional[List[str]]) -> None:
        self.day_tradable: Optional[List[str]] = day_tradable

    def get_day_trade_list(self, date: datetime.date) -> Optional[DayTradeListSnapshot]:
        """當日名單；`None` 代表資料源沒有名單"""

        if self.day_tradable is None:
            return None
        return DayTradeListSnapshot(day_tradable=frozenset(self.day_tradable))


def make_strategy(
    reference: Any = BASE_REFERENCE,
    day_tradable: Optional[List[str]] = None,
    opening_basis: Optional[Dict[str, float]] = None,
) -> IntradayMomentumStrategy:
    """建立已接好假資料源與空帳戶的策略"""

    strategy: IntradayMomentumStrategy = IntradayMomentumStrategy()
    strategy.start_date = DAY_T1
    strategy.end_date = DAY_NEXT
    strategy.price = FakePriceAPI(
        {DAY_T1: {"2330": reference}, DAY_T: {"2330": 110.0}},
        {DAY_T: opening_basis} if opening_basis else None,
    )
    strategy.feed = FakeFeed(["2330"] if day_tradable is None else day_tradable)
    strategy.trading_days = strategy.build_trading_days()
    strategy.setup_account(StockAccount(init_capital=strategy.init_capital))
    return strategy


def make_quote(
    open: float = 105.0,
    high: float = 110.0,
    low: float = 104.0,
    close: float = 109.5,
    volume: int = BASE_VOLUME_LOTS,
    date: datetime.date = DAY_T,
) -> StockQuote:
    """建立 2330 的日 K；預設盤中觸及 109、最低 104"""

    return build_stock_quote(
        stock_id="2330",
        date=date,
        cur_price=close,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def make_position(date: datetime.date = DAY_T, volume: int = 2) -> StockPosition:
    """建立一筆多單部位"""

    return StockPosition(
        id=1,
        stock_id="2330",
        position_type=PositionType.LONG,
        date=date,
        price=TRIGGER_PRICE,
        volume=volume,
    )


# === 價位換算 ===
def test_trigger_and_stop_prices_align_to_ticks() -> None:
    """觸發價往上對齊、停損價嚴格低於門檻：門檻 108 剛好在檔位上時退一檔到 107.5"""

    strategy: IntradayMomentumStrategy = make_strategy()

    assert strategy.get_entry_trigger_price(100.0, "2330") == TRIGGER_PRICE
    assert strategy.get_stop_loss_price(100.0, "2330") == STOP_PRICE
    # 平盤價 50.3：門檻 54.827 → 觸發價 54.9；停損門檻 54.324 → 54.3（不在檔位上，不退）
    assert strategy.get_entry_trigger_price(50.3, "2330") == 54.9
    assert strategy.get_stop_loss_price(50.3, "2330") == 54.3


# === 開倉訊號 ===
def test_open_signal_enters_at_trigger_price() -> None:
    """最高價觸及觸發價時以觸發價買進，算張數也用觸發價"""

    strategy: IntradayMomentumStrategy = make_strategy()

    signals = strategy.generate_open_signals([make_quote()])

    assert len(signals) == 1
    assert signals[0].action == Action.BUY
    assert signals[0].position_type == PositionType.LONG
    assert signals[0].order_price == TRIGGER_PRICE
    assert signals[0].sizing_price == TRIGGER_PRICE


def test_open_signal_gap_up_enters_at_open() -> None:
    """開盤就跳空在觸發價之上時，第一筆能買到的是開盤價"""

    strategy: IntradayMomentumStrategy = make_strategy()

    signals = strategy.generate_open_signals([make_quote(open=109.5, low=109.0)])

    assert signals[0].order_price == 109.5


def test_open_signal_rejects_high_below_trigger() -> None:
    """最高價差一檔沒碰到觸發價就不進場"""

    strategy: IntradayMomentumStrategy = make_strategy()

    assert strategy.generate_open_signals([make_quote(high=108.5)]) == []


def test_open_signal_rejects_insufficient_volume() -> None:
    """當日總量未達 5,000 張不進場"""

    strategy: IntradayMomentumStrategy = make_strategy()

    assert strategy.generate_open_signals([make_quote(volume=4999)]) == []


def test_open_signal_rejects_symbol_not_in_day_trade_list() -> None:
    """不在現股當沖名單的標的不進場（當天停損會賣不掉）"""

    strategy: IntradayMomentumStrategy = make_strategy(day_tradable=["2317"])

    assert strategy.generate_open_signals([make_quote()]) == []


def test_open_signal_skips_day_without_day_trade_list() -> None:
    """整天沒有當沖名單時不進場，而不是當作沒有限制"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.feed = FakeFeed(None)

    assert strategy.generate_open_signals([make_quote()]) == []


def test_open_signal_skips_existing_position() -> None:
    """帳上已有該檔部位時不再進場"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position(date=DAY_T1))

    assert strategy.generate_open_signals([make_quote()]) == []


@pytest.mark.parametrize("reference", [None, float("nan"), 0.0])
def test_open_signal_skips_invalid_reference(reference: Any) -> None:
    """平盤價缺漏、NULL（NaN）或 0 時不進場——NaN 不擋會一路走成買進候選"""

    strategy: IntradayMomentumStrategy = make_strategy(reference=reference)

    assert strategy.generate_open_signals([make_quote()]) == []


def test_open_signal_uses_ex_dividend_opening_basis() -> None:
    """除權息日以開盤競價基準算觸發價：基準 95 時觸發價 103.55→104，不是前收 100 的 109"""

    strategy: IntradayMomentumStrategy = make_strategy(opening_basis={"2330": 95.0})

    signals = strategy.generate_open_signals(
        [make_quote(open=100.0, high=104.0, low=99.0, close=103.0)]
    )

    assert signals[0].order_price == 104.0


def test_check_open_signal_builds_order() -> None:
    """經基底契約與部位建構層後產生買單，張數 ≥ 1（只開一檔，100 萬夠買 109 元的股票）"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.max_holdings = 1

    orders: List[StockOrder] = strategy.check_open_signal([make_quote()])

    assert len(orders) == 1
    assert orders[0].price == TRIGGER_PRICE
    assert orders[0].volume >= 1


# === 停損訊號 ===
def test_stop_loss_bearish_bar_uses_low() -> None:
    """陰線（開 → 高 → 低 → 收）的最低價在進場之後：跌破停損價就以停損價賣出全部張數"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position())

    signals = strategy.generate_stop_loss_signals(
        [make_quote(open=109.5, high=110.0, low=107.0, close=109.0)]
    )

    assert len(signals) == 1
    assert signals[0].action == Action.SELL
    assert signals[0].order_price == STOP_PRICE
    assert signals[0].volume == 2


def test_stop_loss_not_triggered_at_threshold() -> None:
    """陰線最低價剛好是 108（＝8%，沒有跌破）不停損"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position())

    assert (
        strategy.generate_stop_loss_signals(
            [make_quote(open=109.5, high=110.0, low=108.0, close=109.0)]
        )
        == []
    )


def test_stop_loss_bullish_bar_ignores_low_before_entry() -> None:
    """陽線（開 → 低 → 高 → 收）的最低價在拉漲、進場之前：收盤沒跌破就不停損"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position())

    assert (
        strategy.generate_stop_loss_signals(
            [make_quote(open=101.0, high=110.0, low=100.0, close=109.5)]
        )
        == []
    )


def test_stop_loss_bullish_bar_closing_below_stop() -> None:
    """陽線收盤跌破停損價：進場後確定跌到過停損價，停損"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position())

    signals = strategy.generate_stop_loss_signals(
        [make_quote(open=101.0, high=110.0, low=100.0, close=107.5)]
    )

    assert signals[0].order_price == STOP_PRICE


def test_fill_config_caps_volume_share() -> None:
    """單筆最多買當日成交量的 10%，超過截斷而不是整筆拒絕"""

    strategy: IntradayMomentumStrategy = IntradayMomentumStrategy()

    assert strategy.fill_config.max_volume_share == 0.1
    assert strategy.fill_config.volume_cap_policy == VolumeCapPolicy.TRUNCATE
    assert strategy.fill_config.slippage_bps_buy == 0.0


def test_stop_loss_ignores_positions_from_earlier_days() -> None:
    """停損只看進場當天：前一天的部位今天收陰線、跌再深也交給開盤出場，不停損"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position(date=DAY_T1))
    # 同一根陰線換成當天進場的部位就會停損，確認不停損是因為進場日而不是 K 棒形狀
    bearish_quote: StockQuote = make_quote(open=109.5, high=110.0, low=90.0, close=95.0)

    assert strategy.generate_stop_loss_signals([bearish_quote]) == []


# === 平倉訊號 ===
def test_close_signal_exits_next_day_at_open() -> None:
    """進場隔天以開盤價賣出，同一檔多筆部位合併成一張單"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position(date=DAY_T1, volume=2))
    strategy.account.positions.append(make_position(date=DAY_T1, volume=3))

    signals = strategy.generate_close_signals([make_quote(open=111.0)])

    assert len(signals) == 1
    assert signals[0].order_price == 111.0
    assert signals[0].volume == 5


def test_close_signal_keeps_entry_day_position() -> None:
    """當天進場的部位不在當天平倉（當天只有停損會賣）"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.account.positions.append(make_position(date=DAY_T))

    assert strategy.generate_close_signals([make_quote()]) == []


# === 引擎設定 ===
def test_engine_derives_open_then_close() -> None:
    """可當沖 → 先開後平：當天的新倉同一根 bar 就會檢查停損"""

    strategy: IntradayMomentumStrategy = IntradayMomentumStrategy()

    assert strategy.allow_day_trade is True
    assert (
        get_execution_sequence(strategy.allow_day_trade)
        == BarExecutionSequence.OPEN_THEN_CLOSE
    )


def test_tick_scale_is_rejected() -> None:
    """逐筆分支尚未實作，TICK 級別當場擋下"""

    strategy: IntradayMomentumStrategy = IntradayMomentumStrategy()
    strategy.scale = Scale.TICK

    with pytest.raises(NotImplementedError):
        strategy.setup_apis(FakeFeed([]))
