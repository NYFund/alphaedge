import datetime
import itertools
from typing import Callable, List, Tuple

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.models import StockOrder, StockTradeRecord
from core.utils import Action, BarExecutionOrder, PositionType

"""單根 bar 的委託決定性排序與同標的開平倉並存規則

委託的到達順序完全繼承自報價順序，而報價來自沒有 ORDER BY 的 SQL，
實際列順序是查詢計畫的副產物。這裡的測試釘住「引擎自己排序」這件事，
讓結果不再依賴任何上游容器的迭代順序。
"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)
DAY_2: datetime.date = datetime.date(2024, 1, 3)


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester（setup 只負責建立目錄與載入 API）"""

    def _make_backtester(strategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


def buy_order(
    stock_id: str,
    date: datetime.date = DAY_1,
    price: float = 100.0,
    volume: int = 1,
) -> StockOrder:
    """做多開倉單"""

    return StockOrder(
        stock_id=stock_id,
        date=date,
        action=Action.BUY,
        position_type=PositionType.LONG,
        price=price,
        volume=volume,
    )


def sell_order(
    stock_id: str,
    date: datetime.date = DAY_1,
    price: float = 100.0,
    volume: int = 1,
) -> StockOrder:
    """做多平倉單"""

    return StockOrder(
        stock_id=stock_id,
        date=date,
        action=Action.SELL,
        position_type=PositionType.LONG,
        price=price,
        volume=volume,
    )


# === 決定性排序 ===
def test_open_orders_are_sorted_by_symbol(
    make_strategy, make_backtester, make_quote
) -> None:
    """開倉委託依代號排序後才進倉位管理器，被上限截斷的一定是排序最後那檔"""

    stock_ids: List[str] = ["2454", "2330", "2317"]  # 刻意亂序
    strategy = make_strategy(
        max_holdings=2,
        open_script={DAY_1: [buy_order(stock_id) for stock_id in stock_ids]},
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    backtester.execute_open_signal(quotes)

    # 若沿用策略給定的順序，成交的會是 2454 與 2330
    assert [p.stock_id for p in backtester.account.positions] == ["2317", "2330"]
    assert backtester.event_counts["rejected_max_holdings"] == 1


def test_same_symbol_orders_keep_arrival_order(
    make_strategy, make_backtester, make_quote
) -> None:
    """排序是穩定的：同一標的的多筆委託維持策略給定的先後（分批建倉不被打散）"""

    strategy = make_strategy(
        max_holdings=5,
        open_script={
            DAY_1: [
                buy_order("2330", volume=1),
                buy_order("2330", volume=2),
                buy_order("2317", volume=3),
            ]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in ("2330", "2317")
    ]
    backtester.execute_open_signal(quotes)

    actual: List[Tuple[str, int]] = [
        (p.stock_id, p.volume) for p in backtester.account.positions
    ]
    assert actual == [("2317", 3), ("2330", 1), ("2330", 2)]


def test_close_orders_are_sorted_by_symbol(
    make_strategy, make_backtester, make_quote
) -> None:
    """平倉委託同樣依代號排序，交易紀錄的產生順序可重現"""

    stock_ids: List[str] = ["2454", "2330", "2317"]
    strategy = make_strategy(
        max_holdings=5,
        open_script={DAY_1: [buy_order(stock_id) for stock_id in stock_ids]},
        close_script={
            DAY_2: [sell_order(stock_id, date=DAY_2) for stock_id in stock_ids]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    day_1_quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    day_2_quotes = [
        make_quote(stock_id=stock_id, date=DAY_2, cur_price=100.0)
        for stock_id in stock_ids
    ]

    backtester.execute_open_signal(day_1_quotes)
    backtester.execute_close_signal(day_2_quotes)

    records: List[StockTradeRecord] = backtester.account.trade_records
    assert [record.stock_id for record in records] == ["2317", "2330", "2454"]


def test_result_is_independent_of_signal_order(
    make_strategy, make_backtester, make_quote
) -> None:
    """同一組訊號無論以什麼順序回傳，成交結果逐筆相同"""

    stock_ids: Tuple[str, ...] = ("2330", "2317", "2454")
    results: List[List[Tuple[str, int]]] = []

    for permutation in itertools.permutations(stock_ids):
        strategy = make_strategy(
            max_holdings=2,  # 故意讓上限截斷生效，否則排序不影響結果
            open_script={DAY_1: [buy_order(stock_id) for stock_id in permutation]},
        )
        backtester: Backtester = make_backtester(strategy)

        quotes = [
            make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
            for stock_id in permutation
        ]
        backtester.execute_open_signal(quotes)

        results.append([(p.stock_id, p.volume) for p in backtester.account.positions])

    assert all(result == results[0] for result in results)
    assert results[0] == [("2317", 1), ("2330", 1)]


# === 停損與一般平倉的優先級 ===
def test_stop_loss_precedes_normal_close(
    make_strategy, make_backtester, make_quote
) -> None:
    """停損是風控，必須先於一般平倉；已被停損掉的標的不會再被平一次"""

    stock_ids: List[str] = ["2330", "2317"]
    strategy = make_strategy(
        max_holdings=5,
        open_script={DAY_1: [buy_order(stock_id) for stock_id in stock_ids]},
        # 代號排序上 2317 在 2330 之前，若沒有優先級，2317 的一般平倉會先成交
        stop_loss_script={DAY_2: [sell_order("2330", date=DAY_2)]},
        close_script={
            DAY_2: [sell_order(stock_id, date=DAY_2) for stock_id in stock_ids]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    day_1_quotes = [
        make_quote(stock_id=stock_id, date=DAY_1, cur_price=100.0)
        for stock_id in stock_ids
    ]
    day_2_quotes = [
        make_quote(stock_id=stock_id, date=DAY_2, cur_price=100.0)
        for stock_id in stock_ids
    ]

    backtester.execute_open_signal(day_1_quotes)
    backtester.execute_close_signal(day_2_quotes)

    records: List[StockTradeRecord] = backtester.account.trade_records
    assert [record.stock_id for record in records] == ["2330", "2317"]


# === 同標的開平倉並存 ===
def test_same_bar_open_and_close_are_not_netted(
    make_strategy, make_backtester, make_quote
) -> None:
    """OPEN_THEN_CLOSE 下同標的當日來回：兩腿分別成交，不合併成淨額委託"""

    strategy = make_strategy(
        max_holdings=5,
        bar_execution_order=BarExecutionOrder.OPEN_THEN_CLOSE,
        open_script={DAY_1: [buy_order("2330", price=100.0)]},
        close_script={DAY_1: [sell_order("2330", price=105.0)]},
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(
        DAY_1,
        [
            make_quote(
                stock_id="2330", date=DAY_1, cur_price=105.0, high=106.0, low=99.0
            )
        ],
    )

    records: List[StockTradeRecord] = backtester.account.trade_records
    assert len(records) == 1

    # 兩腿各自記帳的證據：買賣同日、成交價分別是兩腿的價格，
    # 且證交稅只課在賣出腿（105 * 1000 * 0.3% = 315）
    record: StockTradeRecord = records[0]
    assert record.buy_date == DAY_1 and record.sell_date == DAY_1
    assert record.buy_price == 100.0 and record.sell_price == 105.0
    assert record.tax == 315
    assert backtester.account.positions == []


def test_close_then_open_reopens_same_symbol_in_one_bar(
    make_strategy, make_backtester, make_quote
) -> None:
    """CLOSE_THEN_OPEN 下同標的的並存訊號是「先出清舊倉再重新建倉」"""

    strategy = make_strategy(
        max_holdings=5,
        open_script={
            DAY_1: [buy_order("2330", price=100.0)],
            DAY_2: [buy_order("2330", date=DAY_2, price=105.0)],
        },
        close_script={DAY_2: [sell_order("2330", date=DAY_2, price=105.0)]},
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(
        DAY_1,
        [make_quote(stock_id="2330", date=DAY_1, cur_price=100.0)],
    )
    backtester.execute_bar(
        DAY_2,
        [
            make_quote(
                stock_id="2330", date=DAY_2, cur_price=105.0, high=106.0, low=99.0
            )
        ],
    )

    # 舊倉平掉產生一筆紀錄，新倉以當日價格重新建立
    records: List[StockTradeRecord] = backtester.account.trade_records
    assert len(records) == 1
    assert records[0].buy_date == DAY_1 and records[0].sell_date == DAY_2

    assert len(backtester.account.positions) == 1
    assert backtester.account.positions[0].date == DAY_2
    assert backtester.account.positions[0].price == 105.0


# === 委託紀錄的日期 ===
def test_submitted_orders_carry_the_bar_date(
    make_strategy, make_backtester, make_quote
) -> None:
    """
    `submitted_orders` 每一筆的日期是它所屬那根 bar 的日期

    `cur_date` 以前只在建構時設成起始日，之後從不前進：多日回測的每一筆
    都記成起始日。parity 比對取這份清單時日期全錯，而且不會報錯。
    """

    day_3: datetime.date = datetime.date(2024, 1, 4)
    days: List[datetime.date] = [DAY_1, DAY_2, day_3]
    strategy = make_strategy(
        open_script={day: [buy_order("2330", date=day)] for day in days},
    )
    backtester: Backtester = make_backtester(strategy)

    for day in days:
        backtester.execute_bar(day, [make_quote(stock_id="2330", date=day)])

    assert [date for date, _, _ in backtester.submitted_orders] == days
