import datetime
from typing import Callable, Dict, List

import pytest

from core.backtest.backtester import Backtester, new_event_counts
from core.backtest.factory import build_backtester
from core.models import StockOrder
from core.utils import Action, PositionType

"""引擎側 max_holdings 硬上限

`max_holdings` 原本被引擎讀進來卻從未使用，實際上限全靠策略自我約束——
一支不呼叫 sizer 的新策略就能無限開倉且不會有任何警告。
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
    """1 張的做多開倉單"""

    return StockOrder(
        stock_id=stock_id,
        date=DAY_1,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=100.0,
        volume=1,
    )


def test_excess_open_orders_are_rejected(
    make_strategy, make_backtester, make_quote
) -> None:
    """策略故意超額回傳時，超出上限的開倉單被引擎剔除並計數"""

    stock_ids: List[str] = ["2330", "2317", "2454"]
    strategy = make_strategy(
        max_holdings=2,
        open_script={DAY_1: [make_open_order(stock_id) for stock_id in stock_ids]},
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    backtester.execute_open_signal(quotes)

    # 上限 2 檔：第三張單被剔除
    assert backtester.account.get_position_count() == 2
    assert backtester.event_counts["rejected_max_holdings"] == 1


def test_rejection_is_not_silent(make_strategy, make_backtester, make_quote) -> None:
    """被剔除的訂單必須留痕，禁止靜默丟棄"""

    strategy = make_strategy(
        max_holdings=1,
        open_script={DAY_1: [make_open_order("2330"), make_open_order("2317")]},
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in ("2330", "2317")
    ]
    backtester.execute_open_signal(quotes)

    assert backtester.event_counts["rejected_max_holdings"] == 1


def test_within_limit_is_untouched(make_strategy, make_backtester, make_quote) -> None:
    """未超額時不得剔除任何訂單，也不得計數（既有策略走的就是這條路）"""

    stock_ids: List[str] = ["2330", "2317"]
    strategy = make_strategy(
        max_holdings=5,
        open_script={DAY_1: [make_open_order(stock_id) for stock_id in stock_ids]},
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    backtester.execute_open_signal(quotes)

    assert backtester.account.get_position_count() == 2
    assert backtester.event_counts["rejected_max_holdings"] == 0


def test_none_max_holdings_means_unlimited(
    make_strategy, make_backtester, make_quote
) -> None:
    """max_holdings 為 None 時不做任何截斷（與 EqualWeightSizer 語意一致）"""

    stock_ids: List[str] = ["2330", "2317", "2454"]
    strategy = make_strategy(
        max_holdings=None,
        open_script={DAY_1: [make_open_order(stock_id) for stock_id in stock_ids]},
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    backtester.execute_open_signal(quotes)

    assert backtester.account.get_position_count() == 3
    assert backtester.event_counts["rejected_max_holdings"] == 0


def test_new_event_counts_includes_max_holdings_key() -> None:
    """報表的事件清單需納入新 key，否則計數不會出現在 event_report"""

    counts: Dict[str, int] = new_event_counts()

    assert "rejected_max_holdings" in counts
    assert counts["rejected_max_holdings"] == 0


def test_adding_to_the_same_symbol_uses_one_slot(
    make_strategy, make_backtester, make_quote
) -> None:
    """
    同一檔加碼兩次只佔一個名額

    `max_holdings` 的語意在 `BaseStrategy` 與各策略 docstring 都寫「檔數」，
    舊實作回傳的卻是部位筆數：允許加碼的策略（例如 `MomentumStrategy1`）
    同一檔加碼兩次就佔掉兩個名額，實際持有檔數比設定少。
    """

    strategy = make_strategy(
        max_holdings=2,
        open_script={DAY_1: [make_open_order("2330"), make_open_order("2330")]},
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(DAY_1, [make_quote(stock_id="2330", date=DAY_1)])

    assert backtester.account.get_position_count() == 1
    assert backtester.event_counts["rejected_max_holdings"] == 0


def test_adding_to_a_held_symbol_is_allowed_when_full(
    make_strategy, make_backtester, make_quote
) -> None:
    """
    滿額時對已持有標的加碼，回測要放行（與實盤一致）

    上限 2、已持有 2330 與 2317，再開 2330：以前回測只比「持倉數 < 上限」而剔除，
    實盤則因為是加碼而放行——parity 比對每天多一筆 `UNEXPLAINED`。
    """

    strategy = make_strategy(
        max_holdings=2,
        open_script={
            DAY_1: [
                make_open_order("2317"),
                make_open_order("2330"),
                make_open_order("2330"),
            ]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(
        DAY_1,
        [
            make_quote(stock_id="2317", date=DAY_1),
            make_quote(stock_id="2330", date=DAY_1),
        ],
    )

    assert backtester.account.get_position_count() == 2
    assert len(backtester.account.positions) == 3
    assert backtester.event_counts["rejected_max_holdings"] == 0
