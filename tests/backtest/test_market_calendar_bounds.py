import datetime
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set

import pandas as pd
import pytest

from core.market.tw.market_calendar import MarketCalendar

if TYPE_CHECKING:
    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed

"""
交易日曆的兩條防線

1. `get_last_trading_date()` 是無界 `while`，起始日落在資料涵蓋範圍之前時
   會一天一天往回查到 1970 年也不會停，而且沒有任何錯誤訊息——看起來就是「卡住了」。
2. `is_market_open()` 每個曆日都對 `price` 表做一次 `SELECT *` 只為了判斷空不空。
"""


class _FakePriceAPI:
    """只回答「這天有沒有資料」的最小 StockPriceAPI 替身"""

    def __init__(self, trading_days: List[datetime.date]) -> None:
        self.trading_days: List[datetime.date] = trading_days
        self.calls: List[datetime.date] = []

    def get(self, date: datetime.date) -> pd.DataFrame:
        self.calls.append(date)
        if date in self.trading_days:
            return pd.DataFrame([{"stock_id": "2330", "收盤價": 600.0}])
        return pd.DataFrame()


def test_lookback_raises_after_max_days() -> None:
    """回推上界內找不到交易日即 `LookupError`，訊息要指出可能原因"""

    # 以 has_data 恆為 False 的路徑模擬「起始日早於資料涵蓋範圍」
    checked_dates: List[datetime.date] = []

    def always_missing(_api, date: datetime.date) -> bool:
        checked_dates.append(date)
        return False

    original = MarketCalendar.check_price_api_has_data
    MarketCalendar.check_price_api_has_data = staticmethod(always_missing)
    try:
        # **api 只是被原封傳給 `check_price_api_has_data()`**，本身不被檢查型別
        # （舊版有 `isinstance` 分派，Shioaji 那支走掉之後就不需要真的 API 物件了）
        with pytest.raises(LookupError, match="找不到交易日"):
            MarketCalendar.get_last_trading_date(object(), datetime.date(2013, 1, 2))
    finally:
        MarketCalendar.check_price_api_has_data = original

    # 有界：恰好查滿上界天數就停，不會一路查到 1970 年
    assert len(checked_dates) == MarketCalendar.MAX_LOOKBACK_DAYS


def test_lookback_returns_the_previous_trading_day() -> None:
    """正常情況：跨週末往前找到週五"""

    monday: datetime.date = datetime.date(2024, 1, 8)
    friday: datetime.date = datetime.date(2024, 1, 5)

    def has_data(_api, date: datetime.date) -> bool:
        return date == friday

    original = MarketCalendar.check_price_api_has_data
    MarketCalendar.check_price_api_has_data = staticmethod(has_data)
    try:
        assert MarketCalendar.get_last_trading_date(object(), monday) == friday
    finally:
        MarketCalendar.check_price_api_has_data = original


def test_max_lookback_covers_the_longest_holiday() -> None:
    """上界要涵蓋台股史上最長的休市（2023 年春節 12 天）"""

    assert MarketCalendar.MAX_LOOKBACK_DAYS >= 12


# === (stock_id, date) 索引 ===
def test_loader_creates_symbol_date_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    建表時要一併建 `(stock_id, date)` 索引

    四張日更表的主鍵都是 `(date, stock_id, ...)`，date 在前，於是
    「某一檔的整段歷史」要掃過整個 date 範圍——而策略研究問的幾乎都是後者。
    """

    import core.pipeline.tw.loaders.stock_price_loader as loader_module

    downloads: Path = tmp_path / "price"
    downloads.mkdir()
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "PRICE_DOWNLOADS_PATH", downloads)

    loader_module.StockPriceLoader()

    conn = sqlite3.connect(tmp_path / "test.db")
    plan: List[tuple] = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM price "
        "WHERE stock_id = ? AND date BETWEEN ? AND ?",
        ("2330", "2024-01-01", "2024-12-31"),
    ).fetchall()
    conn.close()

    detail: str = " ".join(str(row[-1]) for row in plan)
    assert "SEARCH" in detail
    assert "idx_price_stock_id_date" in detail


def test_index_creation_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`IF NOT EXISTS`：既有資料庫再跑一次不會出錯"""

    import core.pipeline.tw.loaders.stock_price_loader as loader_module

    downloads: Path = tmp_path / "price"
    downloads.mkdir()
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "PRICE_DOWNLOADS_PATH", downloads)

    loader_module.StockPriceLoader()
    loader_module.StockPriceLoader()  # 不得拋出


# === 缺日要被看見 ===
class _HolidayStub:
    """官方休市行事曆：只涵蓋 `covered_years`，休市日為 `closures`"""

    def __init__(self, covered_years: Set[int], closures: Set[datetime.date]) -> None:
        self.covered_years: Set[int] = covered_years
        self.closures: Set[datetime.date] = closures

    def get_covered_years(self) -> Set[int]:
        """已入庫的年度"""

        return self.covered_years

    def get_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """區間內的休市日"""

        return {day for day in self.closures if start_date <= day <= end_date}


def _gap_feed(
    start_date: datetime.date,
    end_date: datetime.date,
    trading_days: Set[datetime.date],
    holiday: _HolidayStub,
) -> "TwStockDataFeed":
    """不連資料庫、只帶缺日診斷所需欄位的 datafeed"""

    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed

    feed: TwStockDataFeed = TwStockDataFeed.__new__(TwStockDataFeed)
    feed.start_date = start_date
    feed.end_date = end_date
    feed.trading_days = trading_days
    feed.market_holiday = holiday
    return feed


def test_calendar_gap_report_attributes_official_closures() -> None:
    """
    官方公告的休市日不是缺口

    回測遇到缺日會當成休市靜默跳過，策略少做一天的判斷卻不會有任何跡象；
    但連假若也被報成缺口，這行 log 就會被當成噪音忽略。
    """

    feed = _gap_feed(
        datetime.date(2025, 1, 1),  # 週三，元旦
        datetime.date(2025, 1, 3),
        {datetime.date(2025, 1, 2), datetime.date(2025, 1, 3)},
        _HolidayStub({2025}, {datetime.date(2025, 1, 1)}),
    )

    assert feed.report_calendar_gaps() == 0


def test_calendar_gap_report_flags_a_real_hole() -> None:
    """已涵蓋的年度裡，不在休市清單上的平日缺日要被算出來"""

    feed = _gap_feed(
        datetime.date(2025, 1, 1),
        datetime.date(2025, 1, 3),
        {datetime.date(2025, 1, 3)},  # 1/2 缺
        _HolidayStub({2025}, {datetime.date(2025, 1, 1)}),
    )

    assert feed.report_calendar_gaps() == 1


def test_calendar_gap_report_does_not_judge_uncovered_years() -> None:
    """
    行事曆未入庫的年度只報數字、不下判斷

    表裡查不到休市日，可能是「不是假日」也可能是「那年沒入庫」；
    跨年的區間中，已涵蓋那一段照常歸因，未涵蓋那一段全數列為無法判斷。
    """

    feed = _gap_feed(
        datetime.date(2024, 12, 31),  # 週二；2024 未入庫
        datetime.date(2025, 1, 2),
        {datetime.date(2025, 1, 2)},
        _HolidayStub({2025}, {datetime.date(2025, 1, 1)}),
    )

    assert feed.report_calendar_gaps() == 1


def test_is_market_open_uses_the_prebuilt_set() -> None:
    """交易日集合建好之後不再逐日查資料庫"""

    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed

    feed: TwStockDataFeed = TwStockDataFeed.__new__(TwStockDataFeed)
    feed.trading_days = {datetime.date(2024, 1, 2)}
    feed.price = None  # 一旦回頭查資料庫就會 AttributeError

    assert feed.is_market_open(datetime.date(2024, 1, 2))
    assert not feed.is_market_open(datetime.date(2024, 1, 3))


def test_shift_trading_days_gets_the_previous_day() -> None:
    """`shift_trading_days(-1)` 與逐日往回查等價，但不碰資料庫"""

    trading_days: List[datetime.date] = [
        datetime.date(2024, 1, 4),
        datetime.date(2024, 1, 5),
        datetime.date(2024, 1, 8),
    ]

    previous: Optional[datetime.date] = MarketCalendar.shift_trading_days(
        trading_days, datetime.date(2024, 1, 8), offset=-1
    )

    assert previous == datetime.date(2024, 1, 5)


def test_lookback_bound_covers_a_month_long_data_gap() -> None:
    """
    上界要按「`price` 表可能缺多久」抓，不是按連假長度

    `report_calendar_gaps()` 這條防線存在，正是因為表裡真的會有缺口——
    上界抓 30 天的話，一段一個月的缺漏會讓整場回測以 `LookupError` 中止，
    而舊的無界迴圈反而找得到。
    """

    assert MarketCalendar.MAX_LOOKBACK_DAYS >= 60


def test_strategy_prefetch_window_matches_the_calendar_bound() -> None:
    """
    策略的交易日預抓窗不可小於日曆上界

    小於的話 `get_previous_trading_date()` 會在清單裡查不到而退回逐日查詢，
    等於把「交易日集合一次建立」的優化悄悄關掉——綁住的是策略這一邊。
    """

    from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1

    assert MomentumStrategy1.CALENDAR_LOOKBACK_DAYS >= MarketCalendar.MAX_LOOKBACK_DAYS


def test_datafeed_setup_survives_a_strategy_without_dates() -> None:
    """
    `BaseStrategy` 的 `start_date`／`end_date` 預設是 None

    沒設區間的策略在 `setup()` 查 `get_trading_days(None, None)` 會 TypeError；
    那種策略應退回逐日查詢，而不是讓 `load_datasets()` 當場炸掉。
    """

    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed

    feed: TwStockDataFeed = TwStockDataFeed.__new__(TwStockDataFeed)
    feed.start_date = None
    feed.end_date = None
    feed.trading_days = None

    assert feed.report_calendar_gaps() == 0
