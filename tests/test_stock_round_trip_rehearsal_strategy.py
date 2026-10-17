import datetime
from typing import List

import pytest

from core.live.strategy_guard import inspect_strategy
from core.models import StockAccount, StockOrder, StockPosition, StockQuote
from core.strategies.stock.stock_round_trip_rehearsal_strategy import (
    StockRoundTripRehearsalStrategy,
)
from core.strategies.strategy_loader import StrategyLoader
from core.utils import Action, ExecutionStyle, PositionType
from tests.conftest import build_stock_quote

"""
股票下單路徑演練策略：建倉期間、只買 1 張 0050、隔日平倉

擋的是**演練當天才發現單送不出去或送錯量**：額度切出 0 張或好幾張、
開倉當天就平倉（變成當沖）、建倉期間外照樣下單，都不會有任何錯誤訊息。
"""

# 2026-10 實盤報價量級（0050 收盤約 109 元）
PRICE: float = 109.0
DAY_IN: datetime.date = datetime.date(2026, 10, 22)


def make_quote(
    date: datetime.date, stock_id: str = "0050", price: float = PRICE
) -> StockQuote:
    """組一筆日線報價"""

    return build_stock_quote(stock_id=stock_id, date=date, cur_price=price)


def make_position(date: datetime.date, volume: int = 1) -> StockPosition:
    """建立一筆 0050 多單部位"""

    return StockPosition(
        id=1,
        stock_id="0050",
        position_type=PositionType.LONG,
        date=date,
        price=PRICE,
        volume=volume,
    )


@pytest.fixture
def strategy() -> StockRoundTripRehearsalStrategy:
    """帳戶資金與實盤相同（實盤帳戶以 `live_capital` 建立）的演練策略"""

    instance = StockRoundTripRehearsalStrategy()
    instance.setup_account(StockAccount(init_capital=instance.live_capital))
    return instance


# === 載入與實盤前檢查 ===
def test_strategy_is_auto_loaded_and_passes_live_readiness() -> None:
    """自動載入、通過實盤前檢查，且標的池只有 0050（沒宣告的話實盤會補成全市場）"""

    strategy = StrategyLoader.load_strategies()["StockRoundTripRehearsalStrategy"]()

    assert inspect_strategy(strategy) == []
    assert strategy.live_execution is ExecutionStyle.MARKET
    assert strategy.symbols == ["0050"]


# === 開倉 ===
@pytest.mark.parametrize(
    "date",
    [datetime.date(2026, 10, 22), datetime.date(2026, 10, 30)],
)
def test_opens_exactly_one_lot_inside_the_window(
    strategy: StockRoundTripRehearsalStrategy, date: datetime.date
) -> None:
    """建倉期間內沒有部位時，經部位建構器切出剛好 1 張 0050"""

    orders: List[StockOrder] = strategy.check_open_signal(
        [make_quote(date), make_quote(date, stock_id="2330", price=1000.0)]
    )

    assert [(order.stock_id, order.action, order.volume) for order in orders] == [
        ("0050", Action.BUY, 1)
    ]


@pytest.mark.parametrize("price", [75.5, 150.0])
def test_lot_count_stays_one_across_the_price_band(
    strategy: StockRoundTripRehearsalStrategy, price: float
) -> None:
    """0050 在 75～150 元之間都是 1 張：額度 45 萬切 3 檔、每檔 15 萬"""

    orders: List[StockOrder] = strategy.check_open_signal(
        [make_quote(DAY_IN, price=price)]
    )

    assert [order.volume for order in orders] == [1]


@pytest.mark.parametrize(
    "date",
    [datetime.date(2026, 10, 21), datetime.date(2026, 11, 2)],
)
def test_no_open_outside_the_window(
    strategy: StockRoundTripRehearsalStrategy, date: datetime.date
) -> None:
    """建倉期間外不開倉：提早部署時不得在期貨換月與平倉那幾天就下單"""

    assert strategy.check_open_signal([make_quote(date)]) == []


def test_no_second_lot_while_holding(
    strategy: StockRoundTripRehearsalStrategy,
) -> None:
    """
    已有部位就不再買：平倉日的賣單成交前，開倉鉤子看到的仍是持有中

    檔數上限是 3，部位建構器不會替這支策略擋——持有 1 檔時還剩 2 檔可開
    """

    strategy.account.positions.append(make_position(DAY_IN))

    assert strategy.check_open_signal([make_quote(datetime.date(2026, 10, 23))]) == []


# === 平倉 ===
def test_keeps_the_position_on_the_entry_day(
    strategy: StockRoundTripRehearsalStrategy,
) -> None:
    """開倉當天不平：當天平掉是當沖，不是要驗的次日平倉"""

    strategy.account.positions.append(make_position(DAY_IN))

    assert strategy.generate_close_signals([make_quote(DAY_IN)]) == []


@pytest.mark.parametrize(
    "date",
    [datetime.date(2026, 10, 23), datetime.date(2026, 11, 2)],
)
def test_closes_the_whole_position_from_the_next_day(
    strategy: StockRoundTripRehearsalStrategy, date: datetime.date
) -> None:
    """隔天起全數賣出；過了建倉期間照樣平倉，不留部位"""

    strategy.account.positions.append(make_position(DAY_IN, volume=1))

    orders: List[StockOrder] = strategy.check_close_signal([make_quote(date)])

    assert [(order.stock_id, order.action, order.volume) for order in orders] == [
        ("0050", Action.SELL, 1)
    ]
