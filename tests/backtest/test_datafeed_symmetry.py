import datetime
from typing import Any, List, Set, Tuple

import pytest

from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.datafeed.base import BaseDataFeed
from core.live.datafeed.base import BaseLiveDataFeed
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.market_calendar import MarketCalendar

"""
資料源的對稱性：`setup()` 外框、交易日判定、缺日報告與 `close()` 只有基底一份

原本台股用 `trading_days: Set` 加逐日查詢的 fallback、期貨 lazy build 一份 `FuturesCalendar`；
缺日報告只有台股有；建 DB 目錄只有期貨會做；起訖日的記錄與 `close()` 兩邊重複。
"""

DAY_1: datetime.date = datetime.date(2024, 3, 4)  # 一
DAY_2: datetime.date = datetime.date(2024, 3, 5)
DAY_3: datetime.date = datetime.date(2024, 3, 6)


class _PriceApi:
    """只回固定交易日的行情 API 替身"""

    def __init__(self, days: List[datetime.date]) -> None:
        self.days: List[datetime.date] = days
        self.closed: int = 0

    def get_trading_days(self, start, end, product=None) -> List[datetime.date]:
        return [day for day in self.days if start <= day <= end]

    def close(self) -> None:
        self.closed += 1


class _Strategy:
    """只帶區間、商品與級別的策略替身"""

    start_date: datetime.date = DAY_1
    end_date: datetime.date = DAY_3
    products: List[str] = ["TX"]
    scale: Any = None


def stock_feed(days: List[datetime.date]) -> TwStockDataFeed:
    """不連資料庫的台股資料源"""

    feed: TwStockDataFeed = TwStockDataFeed()
    feed.price = _PriceApi(days)
    feed.start_date, feed.end_date = DAY_1, DAY_3
    return feed


def futures_feed(days: List[datetime.date]) -> TwFuturesDataFeed:
    """不連資料庫的期貨資料源"""

    feed: TwFuturesDataFeed = TwFuturesDataFeed()
    feed.futures_price = _PriceApi(days)
    feed.products = ["TX"]
    feed.start_date, feed.end_date = DAY_1, DAY_3
    return feed


# === 外框只有基底一份 ===
def test_frame_lives_only_in_the_base() -> None:
    """`is_market_open()`、`report_calendar_gaps()` 不可在回測子類再寫一份；子類只提供兩個鉤子"""

    for name in (
        "setup",
        "is_market_open",
        "report_calendar_gaps",
        "close",
        "connect_db",
    ):
        assert name in BaseDataFeed.__dict__
    for feed_cls in (TwStockDataFeed, TwFuturesDataFeed):
        assert "is_market_open" not in feed_cls.__dict__
        assert "report_calendar_gaps" not in feed_cls.__dict__
        assert "open_data_sources" in feed_cls.__dict__
        assert "build_calendar" in feed_cls.__dict__
        assert not hasattr(feed_cls(), "trading_days")

    # 實盤以多來源佐證交易日，覆寫 `is_market_open()`、不預建日曆
    assert "is_market_open" in BaseLiveDataFeed.__dict__
    assert "build_calendar" in BaseLiveDataFeed.__dict__


@pytest.mark.parametrize(
    ("make_feed", "calendar_cls"),
    [(stock_feed, MarketCalendar), (futures_feed, FuturesCalendar)],
    ids=["stock", "futures"],
)
def test_market_open_queries_the_prebuilt_calendar(make_feed, calendar_cls) -> None:
    """兩個市場都查 `setup()` 建好的日曆；尚未建時先建一次，之後不再回頭問資料庫"""

    feed: BaseDataFeed = make_feed([DAY_1, DAY_3])

    assert feed.is_market_open(DAY_1) is True
    assert feed.is_market_open(DAY_2) is False
    assert isinstance(feed.calendar, calendar_cls)

    feed.price = feed.futures_price = None  # 回頭查就會 AttributeError
    assert feed.is_market_open(DAY_3) is True


def test_stock_without_a_range_falls_back_to_day_by_day_queries() -> None:
    """沒設區間的策略建不出日曆：台股逐日向行情表查，不是一律休市"""

    feed: TwStockDataFeed = stock_feed([DAY_1])
    feed.start_date = feed.end_date = None

    assert feed.build_calendar() is None
    assert feed.is_market_open(DAY_1) is True
    assert feed.is_market_open(DAY_2) is False


def test_futures_without_a_range_has_no_calendar() -> None:
    """期貨沒有區間時建不出日曆；基底預設視為休市（期貨沒有逐日查詢的退路）"""

    feed: TwFuturesDataFeed = futures_feed([DAY_1])
    feed.start_date = feed.end_date = None

    assert feed.build_calendar() is None
    assert feed.is_market_open(DAY_1) is False


# === 缺日報告：兩個市場都有，差別只在有沒有官方行事曆可對照 ===
class _Holiday:
    """官方休市行事曆替身"""

    def __init__(self, years: Set[int], closures: Set[datetime.date]) -> None:
        self.years: Set[int] = years
        self.closures: Set[datetime.date] = closures

    def get_covered_years(self) -> Set[int]:
        return self.years

    def get_closures(self, start, end) -> Set[datetime.date]:
        return self.closures


def test_gap_report_uses_the_official_closures_hook() -> None:
    """台股扣掉官方休市日；期貨沒有那張表，整段只報數字"""

    stock: TwStockDataFeed = stock_feed([DAY_1, DAY_3])
    stock.calendar = stock.build_calendar()
    stock.market_holiday = _Holiday({2024}, {DAY_2})
    assert stock.report_calendar_gaps() == 0  # 3/5 是官方休市日

    stock.market_holiday = _Holiday({2024}, set())
    assert stock.report_calendar_gaps() == 1  # 3/5 是缺口

    futures: TwFuturesDataFeed = futures_feed([DAY_1, DAY_3])
    futures.calendar = futures.build_calendar()
    assert futures.get_official_closures(DAY_1, DAY_3) is None
    assert futures.report_calendar_gaps() == 1  # 無從對照，只報數字


def test_setup_frame_records_the_range_builds_the_calendar_and_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外框：記區間 → 開資料來源 → 建日曆 → 報缺日；期貨之後再把日曆注入換月設定"""

    from core.market.tw.futures_roll import FuturesRollConfig

    calls: List[str] = []
    feed: TwFuturesDataFeed = TwFuturesDataFeed(roll_config=FuturesRollConfig())

    def fake_open(strategy: Any) -> None:
        calls.append("open")
        feed.futures_price = _PriceApi([DAY_1, DAY_3])
        feed.products = list(strategy.products)

    monkeypatch.setattr(feed, "open_data_sources", fake_open)
    monkeypatch.setattr(feed, "report_calendar_gaps", lambda: calls.append("gaps") or 0)

    feed.setup(_Strategy())

    assert calls == ["open", "gaps"]
    assert (feed.start_date, feed.end_date) == (DAY_1, DAY_3)
    assert isinstance(feed.calendar, FuturesCalendar)
    assert feed.roll_config.calendar is feed.calendar


# === close：基底逐一關 API 與連線 ===
def test_close_shuts_every_api_and_the_connection() -> None:
    """兩個市場的 `close()` 都由基底逐一關 `data_apis()` 與連線；可重複呼叫"""

    class _Conn:
        def __init__(self) -> None:
            self.closed: int = 0

        def close(self) -> None:
            self.closed += 1

    stock: TwStockDataFeed = stock_feed([DAY_1])
    stock.conn = _Conn()
    stock.chip = _PriceApi([])
    stock.close()

    assert stock.price.closed == 1 and stock.chip.closed == 1
    assert stock.conn is None
    stock.close()  # 連線已關，再呼叫一次不可拋錯

    futures: TwFuturesDataFeed = futures_feed([DAY_1])
    conn: _Conn = _Conn()
    futures.conn = conn
    futures.stock_conn = _Conn()
    stock_conn = futures.stock_conn
    futures.close()

    assert futures.futures_price.closed == 1
    assert conn.closed == 1 and stock_conn.closed == 1
    assert futures.conn is None and futures.stock_conn is None


def test_connect_db_creates_the_directory_only_for_writable_connections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全新環境沒有 `data/db/`：可寫連線先建目錄；唯讀連線讓「檔案不存在」現形"""

    opened: List[Tuple[Any, bool]] = []
    monkeypatch.setattr(
        "core.datafeed.base.connect_sqlite",
        lambda path, read_only=False: opened.append((path, read_only)) or object(),
    )
    target = tmp_path / "data" / "db" / "x.db"

    BaseDataFeed.connect_db(target)
    assert target.parent.is_dir()
    assert opened[-1] == (target, False)

    missing = tmp_path / "nowhere" / "y.db"
    BaseDataFeed.connect_db(missing, read_only=True)
    assert not missing.parent.exists()
    assert opened[-1] == (missing, True)
