import datetime
from typing import Callable, List

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.models import StockOrder
from core.utils import Action, PositionType, StockPriceType
from tests.backtest.conftest import run_bar

"""
查不到報價的開倉單一律拒單

報價為 None 時，成交驗證與成交模型都會被跳過：不查區間、漲跌停、鎖死與成交量上限，
也不吃滑價，直接以策略給的價格建倉。策略依籌碼或財報選股、而標的當天停牌時，
回測就會在一檔不可能成交的標的上以任意價格開倉。
"""

DAY_1: datetime.date = datetime.date(2024, 1, 2)


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester"""

    def _make_backtester(strategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


def make_open_order(stock_id: str) -> StockOrder:
    """建立一張開倉買單（100 元、1 張的限價單）"""

    return StockOrder(
        stock_id=stock_id,
        date=DAY_1,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=100.0,
        volume=1,
        price_type=StockPriceType.LMT,
    )


def test_open_order_without_a_quote_is_rejected(
    make_strategy, make_backtester, make_quote
) -> None:
    """當日報價裡沒有的標的：不建倉、計數 +1；有報價的照常建倉"""

    strategy = make_strategy(
        open_script={DAY_1: [make_open_order("2330"), make_open_order("9999")]},
    )
    backtester: Backtester = make_backtester(strategy)

    run_bar(backtester, DAY_1, [make_quote(stock_id="2330", date=DAY_1)])

    assert [position.symbol for position in backtester.account.positions] == ["2330"]
    assert backtester.event_counts["rejected_no_quote"] == 1


def test_run_skips_market_day_without_any_quote(
    make_strategy, make_backtester, make_quote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    開盤日但全市場都查不到報價（資料缺日）：整天跳過，連每日準備與收盤結算都不做

    每日準備會覆寫漲跌停參考價（除權息日的公告基準），收盤結算會記權益與次日參考價；
    沒有報價的日子照跑的話，前者會留下沒被收盤價蓋掉的基準，後者會多出一筆沒有盯市依據的權益。
    引擎靠 simulator 的 `load_day()` 在準備之前就知道當天沒有報價。
    """

    day_2: datetime.date = DAY_1 + datetime.timedelta(days=1)
    strategy = make_strategy()
    strategy.start_date = DAY_1
    strategy.end_date = day_2
    backtester: Backtester = make_backtester(strategy)
    backtester.write_artifacts = False

    quotes_by_date = {day_2: [make_quote(stock_id="2330", date=day_2)]}
    monkeypatch.setattr(backtester.data_feed, "is_market_open", lambda date: True)
    monkeypatch.setattr(
        backtester.data_feed,
        "get_quotes",
        lambda date, scale, adjusted=False: quotes_by_date.get(date, []),
    )
    monkeypatch.setattr(backtester.data_feed, "close", lambda: None)

    prepared: List[datetime.date] = []
    original_prepare_day = backtester.prepare_day

    def _record_prepare_day(date: datetime.date) -> None:
        prepared.append(date)
        original_prepare_day(date)

    monkeypatch.setattr(backtester, "prepare_day", _record_prepare_day)

    backtester.run()

    assert prepared == [day_2]
    assert [row["Date"] for row in backtester.daily_equity] == [day_2]
