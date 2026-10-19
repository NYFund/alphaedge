import datetime
from typing import Any, List

import pytest

import apps.backtest as backtest_entry
from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.datafeed.base import BaseDataFeed, EmptyCalendarError
from core.market.tw.futures_roll import FuturesRollConfig

"""
回測區間內沒有交易日就拋出，不讓回測靜默跑完

期貨行情表可以缺（查詢層缺表回空），台股行情表也可能存在但是空的；兩者都會建出空日曆，
`Backtester.run()` 於是每個曆日都判成休市，整場回測照樣跑完、結束碼 0、報表全零。
擋板放在兩市場共用的 `BaseDataFeed.setup()`：查詢層回空仍是對的，能不能跑是資料源的判斷。
"""

START: datetime.date = datetime.date(2024, 3, 4)
END: datetime.date = datetime.date(2024, 3, 8)


class _PriceApi:
    """只回固定交易日的行情 API 替身"""

    def __init__(self, days: List[datetime.date]) -> None:
        self.days: List[datetime.date] = days

    def get_trading_days(self, start, end, product=None) -> List[datetime.date]:
        return [day for day in self.days if start <= day <= end]

    def close(self) -> None:
        pass


class _Strategy:
    """只帶區間、商品與級別的策略替身"""

    def __init__(self, start: Any = START, end: Any = END) -> None:
        self.start_date: Any = start
        self.end_date: Any = end
        self.products: List[str] = ["TX"]
        self.scale: Any = None


def stock_feed(
    days: List[datetime.date], monkeypatch: pytest.MonkeyPatch
) -> TwStockDataFeed:
    """建立只看得到指定交易日的台股資料源，不連真的資料庫"""

    feed: TwStockDataFeed = TwStockDataFeed()

    def fake_open(strategy: Any) -> None:
        feed.price = _PriceApi(days)

    monkeypatch.setattr(feed, "open_data_sources", fake_open)
    monkeypatch.setattr(feed, "report_calendar_gaps", lambda: 0)
    return feed


def futures_feed(
    days: List[datetime.date], monkeypatch: pytest.MonkeyPatch
) -> TwFuturesDataFeed:
    """建立只看得到指定交易日的期貨資料源，不連真的資料庫"""

    feed: TwFuturesDataFeed = TwFuturesDataFeed(roll_config=FuturesRollConfig())

    def fake_open(strategy: Any) -> None:
        feed.futures_price = _PriceApi(days)
        feed.products = list(strategy.products)

    monkeypatch.setattr(feed, "open_data_sources", fake_open)
    monkeypatch.setattr(feed, "report_calendar_gaps", lambda: 0)
    return feed


# === 擋板 ===
@pytest.mark.parametrize(
    "make_feed, target",
    [(stock_feed, "--target price"), (futures_feed, "--target futures_price")],
    ids=["stock", "futures"],
)
def test_empty_calendar_is_refused_with_the_backfill_command(
    make_feed, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空日曆拋出，訊息帶回測區間與該市場的補資料指令"""

    feed: BaseDataFeed = make_feed([], monkeypatch)

    with pytest.raises(EmptyCalendarError) as caught:
        feed.setup(_Strategy())

    assert f"{START}～{END}" in str(caught.value)
    assert target in str(caught.value)


def test_futures_lookahead_days_do_not_count_as_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    期貨日曆會往結束日之後多取幾天給換月用；只有那幾天有資料時，回測區間仍是空的

    拿日曆總長判斷的話，這種情況會被放行，回測照樣全天休市。
    """

    after_end: datetime.date = END + datetime.timedelta(days=3)
    feed: TwFuturesDataFeed = futures_feed([after_end], monkeypatch)

    with pytest.raises(EmptyCalendarError):
        feed.setup(_Strategy())

    assert feed.calendar is not None and feed.calendar.trading_days == [after_end]


@pytest.mark.parametrize(
    "make_feed", [stock_feed, futures_feed], ids=["stock", "futures"]
)
def test_one_trading_day_in_range_is_enough(
    make_feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """區間內只要有一個交易日就照常跑：擋板只擋「整段都沒有」"""

    feed: BaseDataFeed = make_feed([END], monkeypatch)

    feed.setup(_Strategy())

    assert feed.calendar is not None and feed.calendar.trading_days == [END]


def test_strategy_without_a_range_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沒有區間的策略不建日曆，改逐日查詢；擋板走不到，不可誤擋"""

    feed: TwStockDataFeed = stock_feed([], monkeypatch)

    feed.setup(_Strategy(start=None, end=None))

    assert feed.calendar is None


# === 入口 ===
def test_backtest_entry_reports_empty_calendar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """入口把空日曆印到 stderr 並回 1，不丟 traceback、不以 0 結束"""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise EmptyCalendarError("回測區間 2024-03-04～2024-03-08 內沒有任何交易日")

    monkeypatch.setattr(backtest_entry, "build_backtester", refuse)

    code: int = backtest_entry.main(["--strategy", "VolumeBreakoutMomentumStrategy"])

    assert code == 1
    assert "內沒有任何交易日" in capsys.readouterr().err
