import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Set

import pandas as pd
import pytest

from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.crawlers.market_holiday_crawler import MarketHolidayCrawler
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader
from core.pipeline.tw.updaters import market_holiday_updater
from core.pipeline.tw.updaters.market_holiday_updater import MarketHolidayUpdater
from core.pipeline.utils.exceptions import DataLoadError

"""
市場開休市日期：分類規則、整年替換、三值查詢語意

fixture 是 2026-09-22 實抓的 TWSE `holidaySchedule` 2026 年原始回應（含 `\\r\\n`）。
**分類錯一列的代價不對稱**：把提醒列當休市頂多少跑一天；把休市當交易日則是在休市日送單。
"""

FIXTURE_PATH: Path = (
    Path(__file__).parent / "fixtures" / "twse_holiday_schedule_2026.json"
)


def load_payload() -> Dict[str, Any]:
    """讀取 2026 年的實抓回應"""

    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FakeResponse:
    """最小 Response 替身：JSON 端點只需要 `text`／`json()`"""

    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self.status_code: int = status_code
        self._payload: Dict[str, Any] = payload
        self.text: str = json.dumps(payload, ensure_ascii=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Dict[str, Any]:
        return self._payload


def patch_fetch(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> None:
    """把 HTTP 層換成固定回應"""

    monkeypatch.setattr(
        RequestUtils,
        "fetch",
        classmethod(
            lambda cls, url, **kw: FetchResult.succeeded(_FakeResponse(payload))
        ),
    )


def cleaned_2026() -> pd.DataFrame:
    """2026 年 fixture 清洗後的結果"""

    raw: pd.DataFrame = pd.DataFrame(
        load_payload()["data"], columns=load_payload()["fields"]
    )
    return MarketHolidayCleaner().clean(raw, 2026)


@pytest.fixture
def loaded_api() -> MarketHolidayAPI:
    """已載入 2026 年官方日曆的 in-memory API"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    MarketHolidayLoader(dao=MarketHolidayDAO(conn=conn)).add_to_db(cleaned_2026(), 2026)
    return MarketHolidayAPI(conn=conn)


# === crawler ===
def test_crawler_returns_the_raw_year(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fetch(monkeypatch, load_payload())

    result: CrawlResult = MarketHolidayCrawler().crawl(2026)

    assert result.is_ok
    assert len(result.data) == 27
    assert list(result.data.columns) == ["日期", "名稱", "說明"]


def test_unpublished_year_is_no_data_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    明年的公告 12 月前還沒出來：站方回 `stat: ok` ＋ 空 `data`（2027 年實測如此）

    這是「還沒公告」，不是失敗——當成失敗的話，每年一到十一月的每晚更新都是紅燈。
    """

    patch_fetch(
        monkeypatch,
        {"stat": "ok", "fields": ["日期", "名稱", "說明"], "data": [], "total": 0},
    )

    assert MarketHolidayCrawler().crawl(2027).is_no_data


def test_bad_stat_and_network_failure_are_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stat` 非 ok 與連線失敗都不能被當成「這一年沒有休市日」"""

    patch_fetch(monkeypatch, {"stat": "系統忙碌中", "data": []})
    assert MarketHolidayCrawler().crawl(2026).is_failed

    monkeypatch.setattr(
        RequestUtils,
        "fetch",
        classmethod(lambda cls, url, **kw: FetchResult.unreachable("ReadTimeout")),
    )
    assert MarketHolidayCrawler().crawl(2026).is_failed


# === cleaner：分類規則 ===
def test_reminder_rows_are_trading_days_and_everything_else_is_closed() -> None:
    """
    只有「開始交易／最後交易／補行交易」是交易日，其餘一律休市

    「市場無交易，僅辦理結算交割作業」字面上有「交易」兩個字，但當天不能下單，
    必須是休市——這正是列舉「交易日字樣」而不是列舉「休市字樣」的理由。
    """

    df: pd.DataFrame = cleaned_2026().set_index("date")

    for trading in ("2026-01-02", "2026-02-11", "2026-02-23"):
        assert df.loc[trading, "is_trading_day"] == 1, trading

    for closed in ("2026-02-12", "2026-02-13", "2026-09-25", "2026-10-09"):
        assert df.loc[closed, "is_trading_day"] == 0, closed

    assert df.loc["2026-02-12", "name"] == "市場無交易，僅辦理結算交割作業"
    assert int((df["is_trading_day"] == 1).sum()) == 3


def test_make_up_trading_day_is_trading() -> None:
    """早年的週六補行交易日也是交易日"""

    assert MarketHolidayCleaner.is_trading_day_name("補行交易日")
    assert not MarketHolidayCleaner.is_trading_day_name("中秋節")


def test_weekend_closure_rows_are_kept_and_text_is_stripped() -> None:
    """和平紀念日落在週六（2026-02-28）照樣保留；說明欄的 `\\r\\n` 去掉"""

    df: pd.DataFrame = cleaned_2026().set_index("date")

    assert df.loc["2026-02-28", "is_trading_day"] == 0
    assert "\r" not in "".join(df["description"])
    assert df.loc["2026-02-11", "description"] == "農曆春節前最後交易。"
    assert set(df["year"]) == {2026}


def test_rows_from_another_year_are_rejected() -> None:
    """站方若回了別的年度（參數被忽略），不可寫進查詢年度讓它看起來已涵蓋"""

    raw: pd.DataFrame = pd.DataFrame(
        [["2025-01-01", "中華民國開國紀念日", ""]], columns=["日期", "名稱", "說明"]
    )

    with pytest.raises(ValueError, match="其他年度"):
        MarketHolidayCleaner().clean(raw, 2026)


# === loader：整年替換 ===
def test_reloading_a_year_replaces_rows_instead_of_duplicating() -> None:
    """
    同一年重跑是整年替換

    站方更正公告（刪掉一天）時，只做 upsert 的寫法會把被刪的那天永遠留在表裡。
    """

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    dao: MarketHolidayDAO = MarketHolidayDAO(conn=conn)
    loader: MarketHolidayLoader = MarketHolidayLoader(dao=dao)
    full: pd.DataFrame = cleaned_2026()

    loader.add_to_db(full, 2026)
    loader.add_to_db(full, 2026)
    assert dao.count_rows() == 27

    corrected: pd.DataFrame = full[full["date"] != "2026-09-25"]
    loader.add_to_db(corrected, 2026)

    assert dao.count_rows() == 26
    assert datetime.date(2026, 9, 25) not in dao.get_closures(
        datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
    )


# === API：三值語意 ===
def test_is_trading_day_three_valued_semantics(loaded_api: MarketHolidayAPI) -> None:
    """已涵蓋年度答得出 True／False，未涵蓋年度一律 None（週末也是）"""

    assert loaded_api.get_covered_years() == {2026}

    assert loaded_api.is_trading_day(datetime.date(2026, 9, 24)) is True
    assert loaded_api.is_trading_day(datetime.date(2026, 9, 25)) is False
    assert loaded_api.is_trading_day(datetime.date(2026, 2, 12)) is False
    # 提醒列當天有開市
    assert loaded_api.is_trading_day(datetime.date(2026, 2, 11)) is True
    # 週末
    assert loaded_api.is_trading_day(datetime.date(2026, 9, 26)) is False

    # 未涵蓋的年度：「表上查不到」不等於「不是假日」
    assert loaded_api.is_trading_day(datetime.date(2027, 1, 1)) is None
    assert loaded_api.is_trading_day(datetime.date(2027, 1, 2)) is None


def test_is_closure_and_get_closures(loaded_api: MarketHolidayAPI) -> None:
    assert loaded_api.is_closure(datetime.date(2026, 10, 9)) is True
    assert loaded_api.is_closure(datetime.date(2026, 2, 23)) is False
    assert loaded_api.is_closure(datetime.date(2027, 10, 11)) is None

    closures: Set[datetime.date] = loaded_api.get_closures(
        datetime.date(2026, 9, 22), datetime.date(2026, 10, 21)
    )
    assert closures == {
        datetime.date(2026, 9, 25),
        datetime.date(2026, 9, 28),
        datetime.date(2026, 10, 9),
        datetime.date(2026, 10, 10),
    }


def test_missing_table_answers_none() -> None:
    """尚未跑過 ETL 的資料庫：一律 None，不拋錯也不假設開市"""

    api: MarketHolidayAPI = MarketHolidayAPI(conn=sqlite3.connect(":memory:"))

    assert api.get_covered_years() == set()
    assert api.is_trading_day(datetime.date(2026, 9, 24)) is None


# === updater ===
def test_updater_skips_unpublished_year_and_loads_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明年尚未公告時只跳過那一年，其餘年度照常入庫、不拋錯"""

    monkeypatch.setattr(market_holiday_updater, "TW_STOCK_DB_PATH", tmp_path / "t.db")
    updater: MarketHolidayUpdater = MarketHolidayUpdater()
    payload: Dict[str, Any] = load_payload()

    def fake_crawl(year: int) -> CrawlResult:
        if year == 2026:
            return CrawlResult.ok(
                pd.DataFrame(payload["data"], columns=payload["fields"])
            )
        if year == 2025:
            rows: List[List[str]] = [["2025-01-01", "中華民國開國紀念日", ""]]
            return CrawlResult.ok(pd.DataFrame(rows, columns=payload["fields"]))
        return CrawlResult.no_data("empty")

    monkeypatch.setattr(updater.crawler, "crawl", fake_crawl)
    try:
        updater.update(today=datetime.date(2026, 9, 22))
        assert updater.dao.get_covered_years() == {2025, 2026}
    finally:
        updater.close()


def test_updater_keeps_going_after_a_failed_year_then_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一年取不到不影響其他年度入庫，但跑完要拋出——實盤靠這張表判定交易日"""

    monkeypatch.setattr(market_holiday_updater, "TW_STOCK_DB_PATH", tmp_path / "t.db")
    updater: MarketHolidayUpdater = MarketHolidayUpdater()
    payload: Dict[str, Any] = load_payload()

    def fake_crawl(year: int) -> CrawlResult:
        if year == 2026:
            return CrawlResult.ok(
                pd.DataFrame(payload["data"], columns=payload["fields"])
            )
        if year == 2025:
            return CrawlResult.failed("unreachable: ReadTimeout")
        return CrawlResult.no_data("empty")

    monkeypatch.setattr(updater.crawler, "crawl", fake_crawl)
    try:
        with pytest.raises(DataLoadError) as excinfo:
            updater.update(today=datetime.date(2026, 9, 22))
        assert excinfo.value.failed_files == ["2025: unreachable: ReadTimeout"]
        assert updater.dao.get_covered_years() == {2026}
    finally:
        updater.close()


def test_update_db_target_is_in_the_default_set() -> None:
    """`market_holiday` 一年三次請求，放進預設的 `no_tick`：實盤每天盤前都要用"""

    from tasks.update_db import expand_targets

    assert "market_holiday" in expand_targets({"no_tick"})
    assert "market_holiday" in expand_targets({"all"})
