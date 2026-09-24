import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.live.datafeed.calendar import (
    BrokerContractCalendarSource,
    OfficialHolidayCalendarSource,
    TradingCalendarUnavailableError,
    WeekendCalendarSource,
    resolve_trading_day,
)
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.factory import to_live_roll_config
from core.market.tw.futures_roll import FuturesRollConfig
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader

"""
官方開休市日曆接進實盤交易日判定

- 兩個實盤資料源的預設來源都要有它，而且是主來源。
- 年度未入庫時它回 None，交由其他來源；**與券商合約檔衝突時拒絕啟動**，不偏袒任一方。
- 期貨換月日曆的未來交易日要扣掉官方休市日。
"""

FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "twse_holiday_schedule_2026.json"
)

TODAY: datetime.date = datetime.date(2026, 9, 22)
MID_AUTUMN: datetime.date = datetime.date(2026, 9, 25)


def build_stock_db() -> sqlite3.Connection:
    """灌好 2026 年官方日曆的 in-memory `tw_stock.db`"""

    payload: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw: pd.DataFrame = pd.DataFrame(payload["data"], columns=payload["fields"])
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    MarketHolidayLoader(dao=MarketHolidayDAO(conn=conn)).add_to_db(
        MarketHolidayCleaner().clean(raw, 2026), 2026
    )
    return conn


class _Strategy:
    """只需要 `setup_apis()` 的策略替身"""

    def setup_apis(self, feed: Any) -> None:
        pass


@pytest.fixture
def stock_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """
    兩個資料源開 `tw_stock.db` 時都拿到這一條灌好日曆的連線；`tw_futures.db` 給空庫

    以路徑分辨：期貨資料源的 `db_path` 在測試裡設成 `"futures"`。
    """

    conn: sqlite3.Connection = build_stock_db()

    def connect(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:") if db_path == "futures" else conn

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect)
    return conn


# === 來源本身 ===
def test_official_source_answers_only_covered_years() -> None:
    """已入庫年度答得出開休市，未入庫的年度回 None 交給其他來源，不猜"""

    source: OfficialHolidayCalendarSource = OfficialHolidayCalendarSource(
        MarketHolidayAPI(conn=build_stock_db())
    )

    assert source.is_trading_day(TODAY) is True
    assert source.is_trading_day(MID_AUTUMN) is False
    assert source.is_trading_day(datetime.date(2027, 1, 4)) is None


def test_official_holiday_is_resolved_as_closed() -> None:
    """平日的國定假日：週末來源答不出，官方日曆答得出"""

    sources: List[Any] = [
        OfficialHolidayCalendarSource(MarketHolidayAPI(conn=build_stock_db())),
        WeekendCalendarSource(),
    ]

    assert resolve_trading_day(MID_AUTUMN, sources) is False
    assert resolve_trading_day(TODAY, sources) is True


def test_conflict_with_broker_contract_refuses_to_start() -> None:
    """
    休市日券商若因系統作業更新了合約檔：合約檔說開市、官方說休市 → 拒絕啟動

    不偏袒任何一方：衝突代表其中一個來源的語意與我們以為的不同，由人判斷。
    """

    sources: List[Any] = [
        OfficialHolidayCalendarSource(MarketHolidayAPI(conn=build_stock_db())),
        WeekendCalendarSource(),
        BrokerContractCalendarSource(lambda: MID_AUTUMN),
    ]

    with pytest.raises(TradingCalendarUnavailableError, match="衝突"):
        resolve_trading_day(MID_AUTUMN, sources)


# === 接線 ===
def test_stock_feed_uses_official_calendar_as_a_default_source(
    stock_db: sqlite3.Connection,
) -> None:
    """股票資料源的預設日曆來源要有官方日曆，而且排在第一順位"""

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(
        broker=None, now_provider=lambda: datetime.datetime(2026, 9, 22, 8, 30)
    )
    feed.setup(_Strategy())

    names: List[str] = [source.name for source in feed.calendar_sources]
    assert names[0] == "official_holiday"
    # 空庫沒有 `price` 表，不經 `resolve_trading_day()`（`price` 來源會撞表不存在）
    assert feed.calendar_sources[0].is_trading_day(MID_AUTUMN) is False
    assert feed.calendar_sources[0].is_trading_day(TODAY) is True


def test_futures_feed_opens_stock_db_for_the_official_calendar(
    stock_db: sqlite3.Connection,
) -> None:
    """期貨的歷史資料在 `tw_futures.db`，官方日曆要另開 `tw_stock.db` 才讀得到"""

    feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
        broker=None,
        db_path="futures",
        now_provider=lambda: datetime.datetime(2026, 9, 22, 8, 40),
    )
    feed.setup(_Strategy())

    names: List[str] = [source.name for source in feed.calendar_sources]
    assert names[0] == "official_holiday"
    assert resolve_trading_day(MID_AUTUMN, feed.calendar_sources) is False
    assert resolve_trading_day(datetime.date(2026, 10, 9), feed.calendar_sources) is (
        False
    )


# === 換月日曆 ===
def test_roll_calendar_excludes_official_closures(
    stock_db: sqlite3.Connection,
) -> None:
    """
    未來交易日＝平日扣掉官方休市日

    2026-09-22 ~ 2026-10-21 之間的平日休市：9/25 中秋、9/28 教師節、10/9 國慶補假。
    沒扣掉的話，距最後交易日（10/21）的交易日數會多算，換月就晚一天。
    """

    config: FuturesRollConfig = to_live_roll_config(FuturesRollConfig())
    feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
        broker=None,
        db_path="futures",
        now_provider=lambda: datetime.datetime(2026, 9, 22, 8, 40),
        roll_config=config,
    )
    feed.setup(_Strategy())

    days: List[datetime.date] = list(config.calendar.trading_days)
    window: List[datetime.date] = [
        day
        for day in days
        if datetime.date(2026, 9, 22) <= day <= datetime.date(2026, 10, 21)
    ]

    for closure in (MID_AUTUMN, datetime.date(2026, 9, 28), datetime.date(2026, 10, 9)):
        assert closure not in window
    assert datetime.date(2026, 10, 8) in window
    # 30 個曆日裡 22 個平日，扣掉 3 個平日休市
    assert len(window) == 19
