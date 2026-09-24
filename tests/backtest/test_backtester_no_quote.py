import datetime
from typing import Callable

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.models import StockOrder
from core.utils import Action, PositionType, StockPriceType

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

    backtester.execute_bar(DAY_1, [make_quote(stock_id="2330", date=DAY_1)])

    assert [position.symbol for position in backtester.account.positions] == ["2330"]
    assert backtester.event_counts["rejected_no_quote"] == 1
