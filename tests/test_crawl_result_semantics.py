import datetime
from typing import List

import pandas as pd
import pytest

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult, CrawlStatus
from core.pipeline.shared.base_updater import UpdateStats
from core.pipeline.shared.request_utils import FetchResult, FetchStatus, RequestUtils
from core.pipeline.tw.crawlers.stock_price_crawler import StockPriceCrawler

"""
「休市」與「失敗」必須是兩種結果

原本 `crawl_*()` 只有 DataFrame 與 `None` 兩種回傳值，而 `None` 同時代表休市、
站方尚未更新、連線失敗與 IP 被擋。updater 對這四種一律記一行 `is a Holiday!`
就跳過，於是**資料缺一天不會有任何錯誤**，回測把那天當休市靜默跳過。
"""


class _FakeResponse:
    """最小 Response 替身"""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code: int = status_code
        self.text: str = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def make_price_table_html(rows: int = 3) -> str:
    """產生一張看起來像收盤行情的表格"""

    df: pd.DataFrame = pd.DataFrame(
        {"證券代號": [f"233{i}" for i in range(rows)], "收盤價": [100.0] * rows}
    )
    return df.to_html(index=False)


# === looks_like_no_data 的判準 ===
def test_short_official_message_is_no_data() -> None:
    """站方的短訊息頁才算「查無資料」"""

    assert BaseDataCrawler.looks_like_no_data("很抱歉，沒有符合條件的資料!")
    assert BaseDataCrawler.looks_like_no_data("查無資料")


def test_long_page_is_never_no_data() -> None:
    """
    被擋時回的是一整頁 HTML，裡頭夾帶「很抱歉」不稀奇

    只靠關鍵字判斷會把「被擋」誤判成「休市」，那正是要修的坑。
    """

    long_page: str = "很抱歉" + "x" * BaseDataCrawler.NO_DATA_TEXT_MAX_LENGTH

    assert not BaseDataCrawler.looks_like_no_data(long_page)


def test_ordinary_page_is_not_no_data() -> None:
    """沒有任何查無資料字樣就不是休市"""

    assert not BaseDataCrawler.looks_like_no_data(make_price_table_html())


# === judge_fetch 的分流 ===
@pytest.mark.parametrize(
    "fetch_result",
    [
        FetchResult(status=FetchStatus.BLOCKED, error="blocked"),
        FetchResult(status=FetchStatus.UNREACHABLE, error="ReadTimeout"),
        FetchResult(status=FetchStatus.HTTP_ERROR, response=_FakeResponse(404)),
    ],
)
def test_transport_problems_are_failures_not_holidays(
    fetch_result: FetchResult,
) -> None:
    """被擋、逾時、4xx 全部是 FAILED——不可任何一個變成「休市」"""

    judged = BaseDataCrawler.judge_fetch(fetch_result, "TWSE price 2024-01-02")

    assert judged is not None
    assert judged.status is CrawlStatus.FAILED


def test_official_no_data_message_is_no_data() -> None:
    """HTTP 200 ＋ 站方明確訊息才是 NO_DATA"""

    fetch_result: FetchResult = FetchResult(
        status=FetchStatus.OK,
        response=_FakeResponse(200, "很抱歉，沒有符合條件的資料!"),
    )

    judged = BaseDataCrawler.judge_fetch(fetch_result, "TWSE price 2024-01-02")

    assert judged is not None
    assert judged.status is CrawlStatus.NO_DATA


def test_parseable_page_is_left_to_the_caller() -> None:
    """一般頁面交回呼叫端解析，不在這裡定案"""

    fetch_result: FetchResult = FetchResult(
        status=FetchStatus.OK, response=_FakeResponse(200, make_price_table_html())
    )

    assert BaseDataCrawler.judge_fetch(fetch_result, "TWSE price 2024-01-02") is None


def test_unparseable_200_is_failed_not_no_data() -> None:
    """
    HTTP 200 但解析不出表格＝版面改了或拿到錯誤頁，是 FAILED

    舊版在這裡印 `is a Holiday!`，於是改版會靜靜變成「這一年都休市」。
    """

    fetch_result: FetchResult = FetchResult(
        status=FetchStatus.OK,
        response=_FakeResponse(200, "<html><body>nope</body></html>"),
    )

    result: CrawlResult = BaseDataCrawler.parse_html_table(fetch_result, "TWSE price")

    assert result.status is CrawlStatus.FAILED


# === crawler 端到端 ===
def test_price_crawler_reports_failure_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """連線失敗時 crawler 回 FAILED，而不是空表加一行 Holiday"""

    monkeypatch.setattr(
        RequestUtils,
        "fetch",
        classmethod(lambda cls, url, **kw: FetchResult.unreachable("ReadTimeout")),
    )

    result: CrawlResult = StockPriceCrawler().crawl_twse_price(
        datetime.date(2024, 1, 2)
    )

    assert result.is_failed
    assert result.data is None


def test_price_crawler_reports_no_data_on_official_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """站方明確回覆查無資料時才是 NO_DATA"""

    monkeypatch.setattr(
        RequestUtils,
        "fetch",
        classmethod(
            lambda cls, url, **kw: FetchResult.succeeded(
                _FakeResponse(200, "很抱歉，沒有符合條件的資料!")
            )
        ),
    )

    result: CrawlResult = StockPriceCrawler().crawl_twse_price(
        datetime.date(2024, 1, 2)
    )

    assert result.is_no_data


def test_price_crawler_returns_data_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常情況拿得到表格"""

    monkeypatch.setattr(
        RequestUtils,
        "fetch",
        classmethod(
            lambda cls, url, **kw: FetchResult.succeeded(
                _FakeResponse(200, make_price_table_html(rows=5))
            )
        ),
    )

    result: CrawlResult = StockPriceCrawler().crawl_twse_price(
        datetime.date(2024, 1, 2)
    )

    assert result.is_ok
    assert len(result.data) == 5


# === updater 統計：連線失敗計入 unreachable 而非 no data ===
def test_connection_failure_counts_as_unreachable_not_no_data() -> None:
    """
    連線失敗要讓 `unreachable` +1

    記成 `no_data` 的話這天會被當成休市，之後再也不會補。
    """

    stats: UpdateStats = UpdateStats()
    day_status: CrawlStatus = stats.record(
        CrawlResult.failed("unreachable: ReadTimeout"),
        CrawlResult.no_data("站方回覆查無資料"),
    )

    assert stats.unreachable == 1
    assert stats.no_data == 0
    assert day_status is CrawlStatus.FAILED, (
        "有任一來源失敗時，這天不可被認定為確定沒資料——"
        "另一半的資料已經入庫，差集會把這天當成『已經有了』而永遠不再補"
    )


def test_all_sources_no_data_is_a_confirmed_holiday() -> None:
    """兩邊都明確回覆沒資料，才算確定休市"""

    stats: UpdateStats = UpdateStats()
    day_status: CrawlStatus = stats.record(
        CrawlResult.no_data("站方回覆查無資料"),
        CrawlResult.no_data("站方回覆查無資料"),
    )

    assert stats.no_data == 1
    assert day_status is CrawlStatus.NO_DATA


def test_summary_line_carries_the_three_counters() -> None:
    """統計行必須帶出 requested／no data／unreachable 三個數字"""

    stats: UpdateStats = UpdateStats()
    stats.record(
        CrawlResult.ok(pd.DataFrame({"a": [1]})),
        CrawlResult.ok(pd.DataFrame({"a": [1]})),
    )
    stats.record(CrawlResult.no_data("x"), CrawlResult.no_data("x"))
    stats.record(CrawlResult.failed("x"), CrawlResult.no_data("x"))

    line: str = stats.summary_line("price")

    assert "3 requested" in line
    assert "1 no data" in line
    assert "1 unreachable" in line


def test_multi_request_sources_fail_as_a_whole() -> None:
    """
    月營收一個年月要打四次請求，任一次失敗即整體失敗

    拿到一半的表會產出「少了外國發行人」的月營收，數字看起來正常、實際短少數百檔。
    """

    from core.pipeline.tw.crawlers.monthly_revenue_report_crawler import (
        MonthlyRevenueReportCrawler,
    )

    results: List[CrawlResult] = [
        CrawlResult.ok_tables([pd.DataFrame({"a": [1]})]),
        CrawlResult.failed("unreachable: ReadTimeout"),
    ]

    combined: CrawlResult = MonthlyRevenueReportCrawler.combine_results(
        results, "MRR 2024/1"
    )

    assert combined.is_failed
