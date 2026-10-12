import datetime
import math
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytest

from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.models.event_counts import FUTURES_EVENT_KEYS, new_event_counts
from core.backtest.models.fill_model import (
    BaseFillModel,
    TwFuturesFillModel,
    TwStockFillModel,
)
from core.backtest.models.settlement_model import TwFuturesSettlementModel
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_roll import FuturesRollConfig, FuturesRollRule
from core.models import FuturesAccount, FuturesOrder, FuturesPosition, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.position.futures.position_manager import FuturesPositionManager
from core.strategies.futures import BaseFuturesStrategy
from core.utils import Action, FuturesSession, PositionType, Scale
from tests.conftest import build_futures_quote

"""
期貨零成交／單筆成交的契約：開倉擋板、單筆成交的區間、策略看不到零成交、盯市用結算價

實測（`tw_futures.db` 2026-10-08 的複本）：`futures_price_daily` 三成的列零成交，
四價是 NULL；另有 600 多列只成交一筆、高低價是 NULL。pandas 把 NULL 讀成 NaN 還是 None，
取決於同一次查詢的其他列——NaN 是 truthy、None 進算術會拋錯，於是三種型態三種行為：
零成交讀成 NaN 時任何價格都拒單、整組 None 時任何價格都成交、單筆成交時委託價等於
實際成交價也被拒。本檔釘住修正後的單一行為；現行策略交易的 TX／MTX／TMF 近月沒有
這類列，修正前後回歸零變動。
"""

DATE: datetime.date = datetime.date(2026, 8, 25)
MULTIPLIER: int = 200

DAY_ROW: dict = {
    "date": "2026-08-25",
    "product": "TX",
    "expiry": "202706",
    "session": "day",
    "開盤價": float("nan"),
    "最高價": float("nan"),
    "最低價": float("nan"),
    "收盤價": float("nan"),
    "成交量": 0,
    "結算價": 46392.0,
    "未沖銷契約量": 12,
    "最後最佳買價": float("nan"),
    "最後最佳賣價": float("nan"),
}


def make_df(rows: List[dict]) -> pd.DataFrame:
    """以 `futures_price_daily` 的欄位組一張查詢結果"""

    return pd.DataFrame(rows)


def make_quote(
    close: float = 18000.0,
    high: Optional[float] = None,
    low: Optional[float] = None,
    volume: int = 1000,
    settlement_price: Optional[float] = None,
    session: FuturesSession = FuturesSession.DAY,
    expiry: str = "202403",
    date: datetime.date = DATE,
) -> FuturesQuote:
    """組一筆 TX 報價；零成交就把 `close` 與 `volume` 都給 0"""

    return build_futures_quote(
        expiry=expiry,
        date=date,
        close=close,
        high=high,
        low=low,
        volume=volume,
        settlement_price=settlement_price,
        session=session,
        multiplier=MULTIPLIER,
    )


def no_trade_quote(
    settlement_price: Optional[float] = None,
    session: FuturesSession = FuturesSession.DAY,
    expiry: str = "202403",
    date: datetime.date = DATE,
) -> FuturesQuote:
    """零成交的 bar：四價 0、量 0；日盤仍有結算價、夜盤沒有"""

    return make_quote(
        close=0.0,
        high=0.0,
        low=0.0,
        volume=0,
        settlement_price=settlement_price,
        session=session,
        expiry=expiry,
        date=date,
    )


def make_order(
    price: float, expiry: str = "202403", action: Action = Action.BUY
) -> FuturesOrder:
    """一口 TX 的開倉單（多單買進、空單賣出）"""

    return FuturesOrder(
        product="TX",
        expiry=expiry,
        date=DATE,
        action=action,
        position_type=(
            PositionType.LONG if action == Action.BUY else PositionType.SHORT
        ),
        price=price,
        volume=1,
    )


def make_position(price: float) -> FuturesPosition:
    """一口 TX202403 多單，保證金固定"""

    return FuturesPosition(
        id=1,
        product="TX",
        expiry="202403",
        position_type=PositionType.LONG,
        date=DATE,
        price=price,
        volume=1,
        multiplier=MULTIPLIER,
        margin=700000.0,
        settled_pnl=0.0,
    )


# === adapter：零成交只有一種表示法 ===
def test_null_prices_read_as_nan_become_zero() -> None:
    """同一次查詢有其他成交列時，NULL 讀成 NaN；轉出來一律是 0.0，不是 truthy 的 NaN"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(
        make_df([DAY_ROW, {**DAY_ROW, "expiry": "202609", "收盤價": 46100.0}]), DATE
    )[0]

    assert (quote.open, quote.high, quote.low, quote.close) == (0.0, 0.0, 0.0, 0.0)
    assert quote.cur_price == 0.0
    assert not any(math.isnan(value) for value in (quote.open, quote.close))
    # 結算價不受影響：零成交的日盤列仍有結算價，盯市要靠它
    assert quote.settlement_price == 46392.0


def test_null_prices_read_as_none_become_zero_too() -> None:
    """整欄都是 NULL 時 pandas 讀成 None；轉出來同樣是 0.0，不是 None"""

    df: pd.DataFrame = make_df([DAY_ROW]).astype(object)
    for column in ("開盤價", "最高價", "最低價", "收盤價", "結算價"):
        df[column] = None

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(df, DATE)[0]

    assert (quote.open, quote.high, quote.low, quote.close) == (0.0, 0.0, 0.0, 0.0)
    assert quote.settlement_price is None


def test_single_trade_row_keeps_close_and_zeroes_high_low() -> None:
    """只成交一筆時來源的高低價是 `-`：收盤價保留、高低價為 0"""

    quote: FuturesQuote = FuturesQuoteAdapter.from_day_rows(
        make_df([{**DAY_ROW, "開盤價": 45806.0, "收盤價": 45806.0, "成交量": 1}]),
        DATE,
    )[0]

    assert quote.close == 45806.0
    assert (quote.high, quote.low) == (0.0, 0.0)
    assert quote.volume == 1


def test_to_price_keeps_real_prices() -> None:
    """有值的價格原樣轉成 float；只有 NULL／NaN 才變 0"""

    assert FuturesQuoteAdapter.to_price(46100) == 46100.0
    assert FuturesQuoteAdapter.to_price(float("nan")) == 0.0
    assert FuturesQuoteAdapter.to_price(None) == 0.0


# === 成交模型：零成交擋板與單筆成交的區間 ===
def test_tradable_quote_guard_lives_in_the_base() -> None:
    """判準只有一份：台股與期貨子類都不可再覆寫"""

    assert "has_tradable_quote" in BaseFillModel.__dict__
    for model_cls in (TwStockFillModel, TwFuturesFillModel):
        assert "has_tradable_quote" not in model_cls.__dict__

    # 量與價缺一不可：有價無量（舊資料）與有量無價（整併後日盤零成交）都不算有成交
    assert not BaseFillModel.has_tradable_quote(make_quote(close=18000.0, volume=0))
    assert not BaseFillModel.has_tradable_quote(make_quote(close=0.0, volume=10))
    assert BaseFillModel.has_tradable_quote(make_quote(close=18000.0, volume=10))


@pytest.mark.parametrize(
    "quote",
    [
        no_trade_quote(settlement_price=46392.0),
        no_trade_quote(session=FuturesSession.NIGHT),
    ],
    ids=["day_with_settlement", "night_without_settlement"],
)
def test_open_on_a_no_trade_contract_is_rejected_at_any_price(
    quote: FuturesQuote,
) -> None:
    """零成交的契約：結算價、1 點、天價都拒單，計入 `rejected_fill_price`"""

    event_counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)
    fill_model: TwFuturesFillModel = TwFuturesFillModel(event_counts=event_counts)

    for price in (46392.0, 1.0, 999999.0):
        assert fill_model.validate(make_order(price), quote) is False

    assert event_counts["rejected_fill_price"] == 3


def test_single_trade_bar_only_fills_at_the_traded_price() -> None:
    """只成交一筆的 bar 區間是 `[close, close]`：等於收盤價成交，差一跳就拒"""

    event_counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)
    fill_model: TwFuturesFillModel = TwFuturesFillModel(event_counts=event_counts)
    quote: FuturesQuote = make_quote(close=45806.0, high=0.0, low=0.0, volume=1)

    assert fill_model.get_price_range(quote) == (45806.0, 45806.0)
    assert fill_model.validate(make_order(45806.0), quote) is True
    assert fill_model.validate(make_order(45807.0), quote) is False
    assert event_counts["rejected_fill_price"] == 1


def test_stock_single_trade_bar_uses_the_same_range() -> None:
    """同一條區間規則台股也適用（資料裡目前沒有這種列，釘的是契約不是資料）"""

    from tests.conftest import build_stock_quote

    fill_model: TwStockFillModel = TwStockFillModel()
    quote = build_stock_quote(cur_price=100.0, high=0.0, low=0.0, volume=1)

    assert fill_model.get_price_range(quote) == (100.0, 100.0)


# === simulator：策略只看得到有成交的報價 ===
class RecordingFuturesStrategy(BaseFuturesStrategy):
    """記下每個鉤子收到哪些契約，並依腳本回傳訂單"""

    def __init__(
        self,
        open_script: Optional[List[FuturesOrder]] = None,
        close_script: Optional[List[FuturesOrder]] = None,
    ) -> None:
        super().__init__()

        self.strategy_name: str = "RecordingFutures"
        self.init_capital: float = 3000000.0
        self.products: List[str] = ["TX"]
        self.max_lots: int = 4
        self.scale: Scale = Scale.DAY
        self.start_date: datetime.date = DATE
        self.end_date: datetime.date = datetime.date(2026, 8, 31)
        self.cost_config: FuturesCostConfig = FuturesCostConfig.free()

        self.open_script: List[FuturesOrder] = open_script or []
        self.close_script: List[FuturesOrder] = close_script or []
        self.seen: Dict[str, List[str]] = {"open": [], "close": [], "stop": []}

    def setup_account(self, account: FuturesAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: FuturesAccount = account

    def setup_apis(self, feed=None) -> None:
        """測試不使用任何資料 API"""

        pass

    def check_open_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """記下看到的契約，回傳腳本的開倉單"""

        self.seen["open"].extend(quote.symbol for quote in quotes)
        return list(self.open_script)

    def check_close_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """記下看到的契約，回傳腳本的平倉單"""

        self.seen["close"].extend(quote.symbol for quote in quotes)
        return list(self.close_script)

    def check_stop_loss_signal(self, quotes: List[FuturesQuote]) -> List[FuturesOrder]:
        """記下看到的契約，不停損"""

        self.seen["stop"].extend(quote.symbol for quote in quotes)
        return []

    def calculate_position_size(
        self, quotes: List[FuturesQuote], action: Action
    ) -> List[FuturesOrder]:
        """口數由腳本直接指定"""

        return []


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester"""

    def _make_backtester(strategy: BaseFuturesStrategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


def test_strategy_hooks_never_see_a_no_trade_contract(make_backtester) -> None:
    """三個鉤子都只收到有成交的契約；零成交的那一筆不會出現在任何一個"""

    strategy: RecordingFuturesStrategy = RecordingFuturesStrategy()
    backtester: Backtester = make_backtester(strategy)
    # 兩個契約都持有：平倉與停損鉤子只收持倉契約，零成交的那個仍不能出現
    backtester.position_manager.open_position(make_order(18000.0, expiry="202403"))
    backtester.position_manager.open_position(make_order(18000.0, expiry="202706"))

    quotes: List[FuturesQuote] = [
        make_quote(close=18100.0, expiry="202403"),
        no_trade_quote(settlement_price=18050.0, expiry="202706"),
    ]
    backtester.simulator.process_bar(quotes)

    assert strategy.seen["open"] == ["TX202403"]
    assert strategy.seen["close"] == ["TX202403"]
    assert strategy.seen["stop"] == ["TX202403"]


def test_open_on_a_no_trade_contract_counts_as_no_quote(make_backtester) -> None:
    """零成交契約的開倉單走「查不到報價」那條路徑，與台股無成交日一致"""

    strategy: RecordingFuturesStrategy = RecordingFuturesStrategy(
        open_script=[make_order(18050.0, expiry="202706")]
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.simulator.process_bar(
        [
            make_quote(close=18100.0, expiry="202403"),
            no_trade_quote(settlement_price=18050.0, expiry="202706"),
        ]
    )

    assert backtester.account.get_positions() == []
    assert backtester.event_counts["rejected_no_quote"] == 1
    assert backtester.event_counts["rejected_fill_price"] == 0


def test_position_in_a_no_trade_contract_is_marked_at_settlement(
    make_backtester,
) -> None:
    """
    持倉契約當天零成交：不會以 0 元平倉，而是留倉並以結算價盯市

    修正前夜盤零成交會讓盯市價變 NaN，之後的權益全部變 NaN。
    """

    strategy: RecordingFuturesStrategy = RecordingFuturesStrategy()
    backtester: Backtester = make_backtester(strategy)
    account: FuturesAccount = backtester.account
    backtester.position_manager.open_position(make_order(18000.0, expiry="202706"))

    quotes: List[FuturesQuote] = [
        no_trade_quote(settlement_price=18050.0, expiry="202706")
    ]
    backtester.simulator.process_bar(quotes)
    backtester.finish_day(DATE, quotes)

    assert account.get_positions()
    assert account.get_positions()[0].price == 18050.0
    assert account.trade_records == []
    assert account.balance == 3000000.0 - account.margin_used + 50 * MULTIPLIER


# === 結算模型：盯市與無報價計數 ===
def test_no_trade_day_bar_marks_at_settlement_and_is_not_a_missing_quote() -> None:
    """日盤零成交仍有結算價：以結算價盯市，無報價計數歸零（契約還掛牌，不是到期）"""

    settlement: TwFuturesSettlementModel = TwFuturesSettlementModel(
        FuturesPositionManager(FuturesAccount(3000000.0))
    )
    position = make_position(18000.0)
    quote_map: Dict[str, FuturesQuote] = {
        "TX202403": no_trade_quote(settlement_price=46392.0)
    }
    settlement.no_quote_days["TX202403"] = 2

    assert settlement.get_mark_price(position, quote_map) == 46392.0
    settlement.update_no_quote_days(quote_map, [position])
    assert settlement.no_quote_days["TX202403"] == 0


def test_no_trade_night_bar_keeps_the_last_mark_and_counts_as_missing() -> None:
    """夜盤零成交沒有任何價：沿用最近一次盯市價，無報價計數累加"""

    settlement: TwFuturesSettlementModel = TwFuturesSettlementModel(
        FuturesPositionManager(FuturesAccount(3000000.0))
    )
    position = make_position(18000.0)
    quote_map: Dict[str, FuturesQuote] = {
        "TX202403": no_trade_quote(session=FuturesSession.NIGHT)
    }

    mark: float = settlement.get_mark_price(position, quote_map)
    settlement.update_no_quote_days(quote_map, [position])

    assert mark == 18000.0
    assert not math.isnan(mark)
    assert settlement.no_quote_days["TX202403"] == 1


def test_roll_does_not_move_into_a_no_trade_contract(make_backtester) -> None:
    """應轉倉的新契約當天零成交：不轉，部位留在舊契約（修正前 NaN 是 truthy，會轉進去）"""

    strategy: RecordingFuturesStrategy = RecordingFuturesStrategy()
    # 規劃器要有日曆才會建；未沖銷量交叉規則與日期無關，但到期月要在測試日期之後
    strategy.roll_config = FuturesRollConfig(
        rule=FuturesRollRule.OPEN_INTEREST,
        enabled=True,
        calendar=FuturesCalendar([DATE]),
    )
    backtester: Backtester = make_backtester(strategy)
    account: FuturesAccount = backtester.account
    backtester.position_manager.open_position(make_order(18000.0, expiry="202609"))

    near: FuturesQuote = make_quote(
        close=18000.0, expiry="202609", settlement_price=18000.0
    )
    near.open_interest = 100
    far: FuturesQuote = no_trade_quote(settlement_price=18100.0, expiry="202610")
    far.open_interest = 1000

    backtester.settlement.roll_positions(
        DATE, [near, far], account, backtester.event_counts
    )

    assert [position.expiry for position in account.get_positions()] == ["202609"]
    assert backtester.event_counts["rolled_contract"] == 0

    # 對照組：同一組未沖銷量、新契約有成交，就會轉——證明上面不轉是因為零成交
    far_traded: FuturesQuote = make_quote(
        close=18100.0, expiry="202610", settlement_price=18100.0
    )
    far_traded.open_interest = 1000

    backtester.settlement.roll_positions(
        DATE, [near, far_traded], account, backtester.event_counts
    )

    assert [position.expiry for position in account.get_positions()] == ["202610"]
    assert backtester.event_counts["rolled_contract"] == 1


# === 整併：夜盤零成交不可把 open 帶成 0 ===
def test_combining_with_a_no_trade_night_keeps_the_day_open() -> None:
    """日盤有成交、夜盤零成交（資料裡 1,431 列）：整併結果就是日盤本身"""

    day: FuturesQuote = make_quote(close=51400.0, high=51500.0, low=51300.0)
    day.open = 51314.0
    night: FuturesQuote = no_trade_quote(session=FuturesSession.NIGHT)

    combined: FuturesQuote = FuturesQuoteAdapter.combine_quote(day, night)

    assert combined.open == 51314.0
    assert (combined.high, combined.low, combined.close) == (51500.0, 51300.0, 51400.0)
    assert combined.volume == 1000
    assert combined.session == FuturesSession.COMBINED
