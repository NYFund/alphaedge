import datetime
from typing import Callable, List, Tuple

import pytest

from core.models import StockAccount, StockPosition, StockQuote
from core.portfolio.sizing import EqualWeightSizer

"""等權資金切分的單元測試

公式的取整規則直接決定 LONG 回歸 baseline 的 915 筆結果，任何一項改動都會破線。
"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)


@pytest.fixture
def sizer() -> EqualWeightSizer:
    """等權切分器"""

    return EqualWeightSizer()


@pytest.fixture
def make_candidates(
    make_quote,
) -> Callable[..., List[Tuple[StockQuote, float]]]:
    """由 (股票代號, 參考價) 清單建立候選"""

    def _make_candidates(
        pairs: List[Tuple[str, float]],
    ) -> List[Tuple[StockQuote, float]]:
        return [
            (make_quote(stock_id=stock_id, date=DAY_1, cur_price=price), price)
            for stock_id, price in pairs
        ]

    return _make_candidates


def test_equal_split_across_candidates(sizer, make_candidates) -> None:
    """餘額均分到可開檔數後逐檔換算張數（無條件捨去）"""

    account: StockAccount = StockAccount(1000000.0)
    candidates = make_candidates([("2330", 100.0), ("2317", 50.0)])

    sized = sizer.size(account, candidates, max_holdings=2)

    # 每檔資金 500000：100 元 → 5 張；50 元 → 10 張
    assert [(quote.stock_id, volume) for quote, _, volume in sized] == [
        ("2330", 5),
        ("2317", 10),
    ]


def test_truncation_not_rounding(sizer, make_candidates) -> None:
    """張數一律無條件捨去，不可四捨五入"""

    account: StockAccount = StockAccount(199000.0)
    candidates = make_candidates([("2330", 100.0)])

    sized = sizer.size(account, candidates, max_holdings=1)

    # 199000 / 100000 = 1.99 → 1 張（四捨五入會變 2 張，直接破 baseline）
    assert sized[0][2] == 1


def test_stops_at_available_slots(sizer, make_candidates) -> None:
    """候選多於可開檔數時，只下滿名額就停止"""

    account: StockAccount = StockAccount(1000000.0)
    candidates = make_candidates([("2330", 100.0), ("2317", 100.0), ("2454", 100.0)])

    sized = sizer.size(account, candidates, max_holdings=2)

    assert len(sized) == 2
    assert [quote.stock_id for quote, _, _ in sized] == ["2330", "2317"]


def test_no_slots_returns_empty(sizer, make_candidates) -> None:
    """持倉已達上限時不回傳任何部位"""

    account: StockAccount = StockAccount(1000000.0)
    account.positions.append(
        StockPosition(id=1, stock_id="2330", date=DAY_1, price=100.0, volume=1)
    )

    candidates = make_candidates([("2317", 100.0)])

    assert sizer.size(account, candidates, max_holdings=1) == []


def test_below_one_lot_is_skipped(sizer, make_candidates) -> None:
    """資金不足 1 張者不下單，且不佔用名額"""

    account: StockAccount = StockAccount(100000.0)
    candidates = make_candidates([("2454", 1000.0), ("2317", 20.0)])

    sized = sizer.size(account, candidates, max_holdings=2)

    # 每檔資金 50000：1000 元 → 0 張（跳過）；20 元 → 2 張
    assert [(quote.stock_id, volume) for quote, _, volume in sized] == [("2317", 2)]


def test_invalid_reference_price_is_skipped(sizer, make_candidates) -> None:
    """參考價為 0 時跳過，而不是以 0 相除中斷整場回測"""

    account: StockAccount = StockAccount(1000000.0)
    candidates = make_candidates([("2330", 0.0), ("2317", 100.0)])

    sized = sizer.size(account, candidates, max_holdings=2)

    assert [quote.stock_id for quote, _, _ in sized] == ["2317"]


def test_none_max_holdings_means_unlimited(sizer, make_candidates) -> None:
    """max_holdings 為 None 時不限制檔數（五支策略的多數派語意）"""

    account: StockAccount = StockAccount(1000000.0)
    candidates = make_candidates([("2330", 100.0), ("2317", 100.0)])

    sized = sizer.size(account, candidates, max_holdings=None)

    # 以候選檔數為上限：每檔 500000 → 各 5 張
    assert [volume for _, _, volume in sized] == [5, 5]


def test_empty_candidates(sizer) -> None:
    """無候選時回傳空清單，不得除以零"""

    account: StockAccount = StockAccount(1000000.0)

    assert sizer.size(account, [], max_holdings=None) == []
