import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_cost_config
from core.models import StockAccount, StockOrder, StockPosition, StockQuote
from core.models.cost_config import CostConfig
from core.strategies.stock.foreign_sell_short_day_trade_strategy import (
    ForeignSellShortDayTradeStrategy,
)
from core.utils import (
    Action,
    BarExecutionOrder,
    Commission,
    PositionType,
    Scale,
    ShortMethod,
    Units,
)

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

"""外資大賣強勢股當沖放空策略的訊號測試：全部為純記憶體物件，不連資料庫"""


# 三個交易日：連假讓 T−1 與 T 之間隔了四個曆日，用來驗營業日平移
DAY_T2: datetime.date = datetime.date(2024, 1, 2)
DAY_T1: datetime.date = datetime.date(2024, 1, 3)
DAY_T: datetime.date = datetime.date(2024, 1, 8)
TRADING_DAYS: List[datetime.date] = [DAY_T2, DAY_T1, DAY_T]

# T−1 的基準情境：漲幅 10%、外資賣超 2,000 張、成交量 5,000 張，三個條件都過
BASE_T2_CLOSE: float = 100.0
BASE_T1_CLOSE: float = 110.0
BASE_NET_SHARES: int = -2000 * Units.LOT
BASE_VOLUME_LOTS: int = 5000


class FakeDividendAPI:
    """只回傳腳本給定的除權息開盤競價基準，不連資料庫"""

    def __init__(
        self, basis_by_date: Optional[Dict[datetime.date, Dict[str, float]]] = None
    ) -> None:
        self.basis_by_date: Dict[datetime.date, Dict[str, float]] = basis_by_date or {}

    def get_opening_reference_price_map(self, date: datetime.date) -> Dict[str, float]:
        """除權息日的開盤競價基準；非除權息日為空 dict"""

        return self.basis_by_date.get(date, {})


class FakePriceAPI:
    """只回傳腳本給定的價量對照表，不連資料庫"""

    def __init__(
        self,
        close_map_by_date: Dict[datetime.date, Dict[str, Any]],
        volume_map_by_date: Dict[datetime.date, Dict[str, int]],
        dividend_api: Optional[FakeDividendAPI] = None,
    ) -> None:
        self.close_map_by_date: Dict[datetime.date, Dict[str, Any]] = close_map_by_date
        self.volume_map_by_date: Dict[datetime.date, Dict[str, int]] = (
            volume_map_by_date
        )
        self.dividend_api: FakeDividendAPI = dividend_api or FakeDividendAPI()

    def get_dividend_api(self) -> FakeDividendAPI:
        """除權息 API"""

        return self.dividend_api

    def get_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """回傳固定的三個交易日"""

        return [day for day in TRADING_DAYS if start_date <= day <= end_date]

    def get_close_map(self, date: datetime.date) -> Dict[str, Any]:
        """原始收盤價"""

        return self.close_map_by_date.get(date, {})

    def get_adjusted_close_map(self, date: datetime.date) -> Dict[str, Any]:
        """還原收盤價；本測試不啟用還原，與原始價相同"""

        return self.close_map_by_date.get(date, {})

    def get_volume_lots_map(self, date: datetime.date) -> Dict[str, int]:
        """成交量（張）"""

        return self.volume_map_by_date.get(date, {})


class FakeChipAPI:
    """只回傳腳本給定的外資買賣超對照表，不連資料庫"""

    def __init__(self, net_shares_by_date: Dict[datetime.date, Dict[str, Any]]) -> None:
        self.net_shares_by_date: Dict[datetime.date, Dict[str, Any]] = (
            net_shares_by_date
        )

    def get_foreign_net_shares_map(self, date: datetime.date) -> Dict[str, Any]:
        """外資買賣超股數（賣超為負）"""

        return self.net_shares_by_date.get(date, {})


def make_quote(
    stock_id: str = "2330",
    date: datetime.date = DAY_T,
    open: float = 108.0,
    close: float = 104.0,
    volume: int = BASE_VOLUME_LOTS,
) -> StockQuote:
    """建立 T 日報價；未指定時開盤 108、收盤 104（開高走低的當沖情境）"""

    return StockQuote(
        stock_id=stock_id,
        scale=Scale.DAY,
        date=date,
        cur_price=close,
        volume=volume,
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
    )


def make_strategy(
    net_shares: Optional[int] = None,
    t1_close: float = BASE_T1_CLOSE,
    t1_volume_lots: int = BASE_VOLUME_LOTS,
    opening_basis: Optional[Dict[str, float]] = None,
    **overrides,
) -> ForeignSellShortDayTradeStrategy:
    """建立已接好假資料源與空帳戶的策略；三個門檻的輸入皆可單獨覆寫"""

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()
    strategy.start_date = DAY_T2
    strategy.end_date = DAY_T

    strategy.price = FakePriceAPI(
        close_map_by_date={
            DAY_T2: {"2330": BASE_T2_CLOSE},
            DAY_T1: {"2330": t1_close},
        },
        volume_map_by_date={DAY_T1: {"2330": t1_volume_lots}},
        dividend_api=FakeDividendAPI({DAY_T: opening_basis} if opening_basis else None),
    )
    strategy.chip = FakeChipAPI(
        net_shares_by_date={
            DAY_T1: {
                "2330": BASE_NET_SHARES if net_shares is None else net_shares,
            }
        }
    )
    strategy.setup_account(StockAccount(init_capital=strategy.init_capital))

    for key, value in overrides.items():
        setattr(strategy, key, value)

    return strategy


def make_short_position(
    volume: int = 3, price: float = 108.0, date: datetime.date = DAY_T
) -> StockPosition:
    """建立一筆當日開的空單部位"""

    return StockPosition(
        id=1,
        stock_id="2330",
        position_type=PositionType.SHORT,
        date=date,
        price=price,
        volume=volume,
        short_method=ShortMethod.DAY_TRADE,
        is_day_trade=True,
    )


# === 開倉訊號 ===
def test_open_signal_all_conditions_met() -> None:
    """三個門檻全過時放空開倉：動作為 SELL、方向為 SHORT、成交價為當日開盤價"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    orders: List[StockOrder] = strategy.check_open_signal([make_quote()])

    assert len(orders) == 1
    assert orders[0].action == Action.SELL
    assert orders[0].position_type == PositionType.SHORT
    assert orders[0].price == 108.0  # 開盤價，不是收盤價
    assert orders[0].volume >= 1


def test_open_signal_rejects_insufficient_foreign_sell() -> None:
    """外資賣超未達門檻（999 張 < 1,000 張）不開倉"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(
        net_shares=-999 * Units.LOT
    )

    assert strategy.check_open_signal([make_quote()]) == []


def test_open_signal_rejects_foreign_net_buy() -> None:
    """外資是買超（正值）不開倉——賣超為負值，符號寫反會整組訊號反向"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(
        net_shares=5000 * Units.LOT
    )

    assert strategy.check_open_signal([make_quote()]) == []


def test_open_signal_rejects_insufficient_price_change() -> None:
    """T−1 漲幅未超過門檻（8% 不算「> 8%」）不開倉"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(t1_close=108.0)

    assert strategy.check_open_signal([make_quote()]) == []


def test_open_signal_rejects_insufficient_volume() -> None:
    """T−1 成交量未達流動性門檻不開倉"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(t1_volume_lots=999)

    assert strategy.check_open_signal([make_quote()]) == []


def test_open_signal_skips_existing_position() -> None:
    """已持有該股時不重複開倉"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position())

    assert strategy.check_open_signal([make_quote()]) == []


def test_open_signal_uses_trading_days_not_calendar_days() -> None:
    """
    T−1／T−2 以營業日平移取得

    DAY_T（1/8）的曆日昨天是 1/7，資料只有 1/3 與 1/2。若用曆日相減，
    兩張對照表都會查空而靜默不開倉——這正是連假會踩到的坑。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    assert strategy.get_signal_trading_days(DAY_T) == (DAY_T1, DAY_T2)
    assert strategy.check_open_signal([make_quote()]) != []


def test_open_signal_returns_empty_without_two_prior_trading_days() -> None:
    """回測起始日前不足兩個交易日時不開倉，而不是拿錯日期的資料硬算"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    assert strategy.get_signal_trading_days(DAY_T2) is None
    assert strategy.check_open_signal([make_quote(date=DAY_T2)]) == []


def test_open_signal_ignores_missing_chip_data() -> None:
    """T−1 查無籌碼資料時不開倉（缺資料不等於賣超 0）"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.chip = FakeChipAPI(net_shares_by_date={})

    assert strategy.check_open_signal([make_quote()]) == []


# === 平盤下放空過濾 ===
def test_below_reference_filter_off_by_default() -> None:
    """預設不過濾平盤下放空：開盤 105 低於 T−1 收盤 110 仍然開倉"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    assert strategy.REJECT_BELOW_REFERENCE_OPEN is False
    assert strategy.check_open_signal([make_quote(open=105.0)]) != []


def test_below_reference_filter_rejects_gap_down_open() -> None:
    """開啟過濾後，開盤價低於 T−1 收盤的標的被排除"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(
        REJECT_BELOW_REFERENCE_OPEN=True
    )

    assert strategy.check_open_signal([make_quote(open=105.0)]) == []


def test_below_reference_filter_keeps_gap_up_open() -> None:
    """開啟過濾後，開盤價不低於 T−1 收盤的標的仍可放空"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(
        REJECT_BELOW_REFERENCE_OPEN=True
    )

    assert strategy.check_open_signal([make_quote(open=112.0)]) != []


# === 平倉訊號 ===
def test_close_signal_covers_all_short_positions() -> None:
    """回補：動作為 BUY、方向為 SHORT、以當日收盤價近似尾盤、張數與部位相同"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(volume=3))

    orders: List[StockOrder] = strategy.check_close_signal([make_quote()])

    assert len(orders) == 1
    assert orders[0].action == Action.BUY
    assert orders[0].position_type == PositionType.SHORT
    assert orders[0].price == 104.0  # 收盤價
    assert orders[0].volume == 3


def test_close_signal_covers_position_opened_on_earlier_day() -> None:
    """
    留倉的空單一律當日回補

    當沖單若遇鎖漲停無法回補會轉為融券留倉（`limit_up_cover_failed`），
    此時平倉訊號必須繼續回補，否則部位會一路留到回測結束。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(date=DAY_T1))

    assert len(strategy.check_close_signal([make_quote()])) == 1


def test_close_signal_skips_limit_up_locked_bar() -> None:
    """
    一價到底且上漲時不送回補單，把部位留給引擎的鎖漲停判定

    照送等於用買不到的漲停價記一筆回補，`limit_up_cover_failed` 會永遠是 0。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position())

    # T−1 收盤 110，當日開高低收皆為 121（漲停）
    locked: StockQuote = make_quote(open=121.0, close=121.0)

    assert strategy.check_limit_up_locked(locked) is True
    assert strategy.check_close_signal([locked]) == []


def test_close_signal_covers_limit_down_locked_bar() -> None:
    """一價到底但下跌（鎖跌停）照樣回補：買方不缺，補得到"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position())

    locked_down: StockQuote = make_quote(open=99.0, close=99.0)

    assert strategy.check_limit_up_locked(locked_down) is False
    assert len(strategy.check_close_signal([locked_down])) == 1


def test_close_signal_covers_normal_bar_with_range() -> None:
    """當日有高低區間就不算鎖住，即使收在漲停價也照常回補"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position())

    ranged: StockQuote = make_quote(open=115.0, close=121.0)

    assert strategy.check_limit_up_locked(ranged) is False
    assert len(strategy.check_close_signal([ranged])) == 1


def test_limit_up_check_uses_ex_dividend_opening_basis() -> None:
    """
    除權息日的平盤價須取「開盤競價基準」，不可沿用 T−1 收盤

    T−1 收盤 110、開盤競價基準 95（大額配息）。當日一價到底收在 100：
    以基準判定是漲停鎖死（100 > 95），沿用 T−1 收盤則誤判為下跌（100 < 110），
    會照送一張買不到的回補單。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy(
        opening_basis={"2330": 95.0}
    )
    strategy.account.positions.append(make_short_position())

    locked: StockQuote = make_quote(open=100.0, close=100.0)

    assert strategy.get_reference_price_map(DAY_T)["2330"] == 95.0
    assert strategy.check_limit_up_locked(locked) is True
    assert strategy.check_close_signal([locked]) == []


def test_reference_price_falls_back_to_previous_close() -> None:
    """非除權息日沿用 T−1 收盤，不因為覆蓋邏輯而被清空"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    assert strategy.get_reference_price_map(DAY_T)["2330"] == BASE_T1_CLOSE


def test_close_signal_merges_multiple_positions_into_one_order() -> None:
    """
    同一標的的多筆部位合併成一張回補單

    `close_position()` 會 FIFO 掃過該標的所有同向部位，逐筆送單會讓第一張
    就吃掉後面那筆的張數，後續訂單再以「持倉不足」警告收場。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(volume=3))
    second: StockPosition = make_short_position(volume=2)
    second.id = 2
    strategy.account.positions.append(second)

    orders: List[StockOrder] = strategy.check_close_signal([make_quote()])

    assert len(orders) == 1
    assert orders[0].volume == 5


def test_close_signal_without_position() -> None:
    """沒有部位就沒有平倉單"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()

    assert strategy.check_close_signal([make_quote()]) == []


def test_stop_loss_signal_not_implemented() -> None:
    """本策略不做停損（理由見 class docstring），固定回傳空列表"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position())

    assert strategy.check_stop_loss_signal([make_quote()]) == []


# === 當沖語意 ===
def test_engine_derives_day_trade_cost_config() -> None:
    """
    SHORT ＋ enable_intraday 必須推導出現股當沖沖賣與減半稅率

    策略若自行填 `cost_config`，factory 會跳過這段推導、稅率退回一般 0.3%，
    故本測試同時是「不要在策略裡設 cost_config」的防護線。
    """

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()
    config: CostConfig = build_cost_config(strategy)

    assert strategy.cost_config is None
    assert config.short_method == ShortMethod.DAY_TRADE
    assert config.is_day_trade is True
    assert config.day_trade_tax_rate == float(Commission.DayTradeTaxRate)


def test_engine_derives_open_then_close_execution_order() -> None:
    """同一根 bar 內要先開後平，否則當日開的空單當日平不掉"""

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()

    # 推導只讀 `self.strategy`，故以最小替身呼叫，避免為了一個判斷去連資料庫
    engine: SimpleNamespace = SimpleNamespace(strategy=strategy)

    assert strategy.bar_execution_order is None  # 交由引擎推導
    assert Backtester.get_execution_order(engine) == BarExecutionOrder.OPEN_THEN_CLOSE


def test_fill_config_is_conservative() -> None:
    """成交假設必須實際啟用：滑價與成交量上限都不可留在預設的關閉狀態"""

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()

    assert strategy.fill_config.slippage_bps_buy > 0
    assert strategy.fill_config.slippage_bps_sell > 0
    assert strategy.fill_config.max_volume_share is not None


def test_carry_over_fuses_are_set() -> None:
    """
    留倉部位必須有強制出場上限

    本策略當日必平，唯一會過夜的是「鎖漲停補不到券而轉融券留倉」的部位——
    那正是放空最致命的尾部風險。沒有上限的話，連續鎖漲停會一路留到回測結束、虧損無界。
    """

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()

    assert strategy.max_holding_days is not None
    assert strategy.max_no_quote_days is not None


def test_borrow_check_stays_disabled() -> None:
    """
    券源檢核維持關閉

    這是選擇而非限制：`TwStockFillModel.check_short_borrowable()` 已會跳過
    `ShortMethod.DAY_TRADE`，開啟也不會誤拒沖賣單（見
    `tests/backtest/test_fill_model.py::test_borrow_check_skips_day_trade_short`）。
    當日必平的沖賣不吃券源，檢核對本策略沒有約束力，故不開。
    """

    strategy: ForeignSellShortDayTradeStrategy = ForeignSellShortDayTradeStrategy()
    config: CostConfig = build_cost_config(strategy)

    assert strategy.short_constraint is None
    assert config.short_constraint.check_borrowable is False


# === 回補價：當日 vs 被迫留倉 ===
def test_same_day_position_covers_at_close() -> None:
    """當日開的部位照計畫等到尾盤，以收盤價回補"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(date=DAY_T))

    orders: List[StockOrder] = strategy.check_close_signal([make_quote()])

    assert len(orders) == 1
    assert orders[0].price == 104.0  # 收盤價


def test_carried_over_position_covers_at_open() -> None:
    """
    被迫留倉的部位以**開盤價**回補，不是收盤價

    這種部位是「現股當沖沖賣沒補回來」才存在的，T+2 交割壓力與券商風控都要求
    盡早了結。用收盤價等於給了它一個沒有的權利——**有權等待盤中跌回來**。
    實測 10 筆留倉部位改用開盤價後合計少賺 267,670。
    """

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(date=DAY_T1))

    # 開高走低：開盤 121（跳空）、收盤 104
    orders: List[StockOrder] = strategy.check_close_signal(
        [make_quote(open=121.0, close=104.0)]
    )

    assert len(orders) == 1
    assert orders[0].price == 121.0  # 開盤價，不是那個「跌回來」的收盤價


def test_carried_over_still_skipped_when_locked() -> None:
    """留倉部位若當日仍鎖漲停，一樣不送單——開盤也買不到"""

    strategy: ForeignSellShortDayTradeStrategy = make_strategy()
    strategy.account.positions.append(make_short_position(date=DAY_T1))

    assert strategy.check_close_signal([make_quote(open=121.0, close=121.0)]) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
