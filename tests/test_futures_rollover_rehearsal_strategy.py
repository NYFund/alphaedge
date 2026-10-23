import datetime
from typing import List

import pytest

from core.live.strategy_guard import inspect_strategy
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.models import FuturesAccount, FuturesOrder, FuturesQuote
from core.position.futures.position_manager import FuturesPositionManager
from core.utils import Action, ExecutionStyle, PositionType
from strategies.futures.futures_rollover_rehearsal_strategy import (
    FuturesRolloverRehearsalStrategy,
)
from strategies.loader import StrategyLoader
from tests.conftest import build_futures_quote

"""
期貨換月演練策略：建倉期間、撐過換月日、換月後平倉

擋的是**演練當天才發現沒有部位可換**：日期差一天（例如換月日當天才建倉）
建出來的就是新月部位，換月不會觸發，而那不會有任何錯誤訊息。
"""

MULTIPLIER: int = 200
# 2026-10 實盤報價量級（TXFJ6 參考價約 50,000 點）
TX_PRICE: float = 50000.0


class StubMarginAPI:
    """固定回傳每口保證金的假 API"""

    def get_initial_margin(self, product, date, fallback_to_earliest=False):
        return 701000

    def get_covered_date_range(self, product):
        return {"earliest": "2020-03-13", "latest": "2026-08-12"}


def make_quote(date: datetime.date, expiry: str = "202610") -> FuturesQuote:
    """組一筆台指期日盤報價"""

    return build_futures_quote(
        product="TX",
        expiry=expiry,
        date=date,
        close=TX_PRICE,
        multiplier=MULTIPLIER,
    )


@pytest.fixture
def strategy() -> FuturesRolloverRehearsalStrategy:
    """已載入帳戶與保證金表的演練策略"""

    instance = FuturesRolloverRehearsalStrategy()
    instance.setup_account(FuturesAccount(init_capital=instance.init_capital))
    instance.margin_config = FuturesMarginConfig(api=StubMarginAPI())
    return instance


def hold_one_lot(
    strategy: FuturesRolloverRehearsalStrategy, date: datetime.date, expiry: str
) -> None:
    """讓帳上持有 1 口指定月份的多單"""

    FuturesPositionManager(
        strategy.account, margin_config=strategy.margin_config
    ).open_position(
        FuturesOrder(
            product="TX",
            expiry=expiry,
            date=date,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=TX_PRICE,
            volume=1,
        )
    )


# === 載入與實盤前檢查 ===
def test_strategy_is_auto_loaded_and_passes_live_readiness() -> None:
    """自動載入，且通過實盤前檢查（宣告了執行方式與段落）"""

    strategy = StrategyLoader.load_strategies()["FuturesRolloverRehearsalStrategy"]()

    assert inspect_strategy(strategy) == []
    assert strategy.live_execution is ExecutionStyle.MARKET


def test_dates_straddle_the_roll_day() -> None:
    """
    建倉期間要整段落在換月日之前、平倉在換月日之後

    10 月契約最後交易日 2026-10-21，實盤於前 1 個交易日（10-20）換月；
    10-20 當天建倉會拿到已切換的 11 月契約，換月就不會觸發。
    """

    roll_day: datetime.date = datetime.date(2026, 10, 20)
    cls = FuturesRolloverRehearsalStrategy

    assert cls.OPEN_FROM_DATE <= cls.OPEN_UNTIL_DATE < roll_day < cls.CLOSE_FROM_DATE


# === 開倉 ===
@pytest.mark.parametrize(
    "date",
    [datetime.date(2026, 10, 14), datetime.date(2026, 10, 19)],
)
def test_opens_one_lot_inside_the_window(
    strategy: FuturesRolloverRehearsalStrategy, date: datetime.date
) -> None:
    """建倉期間內沒有部位時，買 1 口近月"""

    orders = strategy.check_open_signal(
        [make_quote(date, "202610"), make_quote(date, "202611")]
    )

    assert len(orders) == 1
    assert orders[0].action == Action.BUY
    assert orders[0].expiry == "202610"
    assert orders[0].volume == 1


@pytest.mark.parametrize(
    "date",
    [datetime.date(2026, 10, 13), datetime.date(2026, 10, 20)],
)
def test_no_open_outside_the_window(
    strategy: FuturesRolloverRehearsalStrategy, date: datetime.date
) -> None:
    """建倉期間外不開倉；換月日當天尤其不可"""

    assert strategy.check_open_signal([make_quote(date)]) == []


def test_no_second_lot_while_holding(
    strategy: FuturesRolloverRehearsalStrategy,
) -> None:
    """已有部位就不再加碼：前一天成交了，隔天不可再買一口"""

    hold_one_lot(strategy, datetime.date(2026, 10, 14), "202610")

    assert strategy.check_open_signal([make_quote(datetime.date(2026, 10, 15))]) == []


# === 平倉 ===
def test_holds_through_the_roll_day(
    strategy: FuturesRolloverRehearsalStrategy,
) -> None:
    """換月日（含）以前不出場，部位才留得到換月"""

    hold_one_lot(strategy, datetime.date(2026, 10, 14), "202610")

    for day in (15, 16, 19, 20):
        date: datetime.date = datetime.date(2026, 10, day)
        assert strategy.check_close_signal([make_quote(date)]) == []


def test_closes_the_rolled_position_after_the_roll_day(
    strategy: FuturesRolloverRehearsalStrategy,
) -> None:
    """換月後的新月部位在 10-21 起平倉"""

    hold_one_lot(strategy, datetime.date(2026, 10, 20), "202611")
    date: datetime.date = datetime.date(2026, 10, 21)

    orders = strategy.check_close_signal(
        [make_quote(date, "202610"), make_quote(date, "202611")]
    )

    assert len(orders) == 1
    assert orders[0].action == Action.SELL
    assert orders[0].expiry == "202611"
    assert orders[0].volume == 1


def test_stop_loss_is_not_implemented(
    strategy: FuturesRolloverRehearsalStrategy,
) -> None:
    """演練策略不停損，一律回傳空 list"""

    signals: List = strategy.check_stop_loss_signal(
        [make_quote(datetime.date(2026, 10, 15))]
    )

    assert signals == []
