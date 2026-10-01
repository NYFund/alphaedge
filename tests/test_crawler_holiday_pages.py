import datetime
import json
from typing import Any, Callable, Dict, List

import pandas as pd
import pytest
import requests

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.crawlers.stock_chip_crawler import StockChipCrawler
from core.pipeline.tw.crawlers.stock_margin_crawler import StockMarginCrawler
from core.pipeline.tw.crawlers.stock_price_crawler import StockPriceCrawler

"""
兩站的 HTML 休市頁：判為「沒有資料」，而不是解析失敗

以前休市頁一律記為失敗，每個平日休市日（2013 起 243 天）在每次更新時都被重抓。
回應的形狀照 2026-10-02 實查：
- 證交所 HTML：沒有表格、沒有任何文字的空白報表；JSON 版 `stat` 會明說查無資料
- 櫃買中心收盤行情與三大法人 HTML：只有表頭、「共0筆」與註解列的空表
- 櫃買中心融資融券 HTML：沒有表格；JSON 版為查詢日的 0 列

**完全不連網路**。另外要釘住反面：JSON 版也有資料時，HTML 解析不出來仍是失敗——
那代表版面改了，不可以被當成休市。
"""

HOLIDAY: datetime.date = datetime.date(2026, 5, 1)

TWSE_BLANK_HTML: str = (
    "<html><head><style>table { border: 1px; }</style></head><body></body></html>"
)
TPEX_PLACEHOLDER_HTML: str = """
<table>
  <thead><tr><th>代號</th><th>名稱</th><th>收盤</th></tr></thead>
  <tbody><tr><td colspan="3">共0筆</td></tr></tbody>
  <tfoot><tr><td colspan="3">註：ETF證券代號第六碼為K、C者……</td></tr></tfoot>
</table>
"""
TPEX_NO_TABLE_HTML: str = "<html><body><div>上櫃融資融券餘額</div></body></html>"
TWSE_NO_DATA_JSON: Dict[str, Any] = {"stat": "很抱歉，沒有符合條件的資料!"}
TPEX_ZERO_ROWS_JSON: Dict[str, Any] = {
    "stat": "ok",
    "date": "20260501",
    "tables": [{"date": "115/05/01", "data": []}],
}


def make_response(body: str) -> requests.Response:
    """建立 HTTP 200 的回應"""

    response: requests.Response = requests.Response()
    response.status_code = 200
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return response


def fake_site(
    monkeypatch: pytest.MonkeyPatch, html: str, json_payload: Any
) -> List[str]:
    """HTML 網址回 `html`、JSON 網址回 `json_payload`；回傳被請求過的網址"""

    requested: List[str] = []

    def fetch(url: str, *args: Any, **kwargs: Any) -> FetchResult:
        requested.append(url)
        body: str = (
            json.dumps(json_payload, ensure_ascii=False)
            if "response=json" in url
            else html
        )
        return FetchResult.succeeded(make_response(body))

    monkeypatch.setattr(RequestUtils, "fetch", fetch)
    return requested


# === 佔位表 ===
def test_placeholder_table_is_detected() -> None:
    """跨欄的「共0筆」與註解列展開後每一格都相同"""

    df: pd.DataFrame = pd.DataFrame([["共0筆"] * 3, ["註：……"] * 3])

    assert BaseDataCrawler.is_placeholder_table(df)


def test_real_rows_are_not_placeholders() -> None:
    """真資料列有代號與名稱兩種不同的值；夾著合計列也一樣不算佔位"""

    df: pd.DataFrame = pd.DataFrame([["共2筆"] * 3, ["6488", "環球晶", "500"]])

    assert not BaseDataCrawler.is_placeholder_table(df)


def test_single_column_table_is_never_a_placeholder() -> None:
    """單欄的表每一列本來就只有一種值，不可被當成佔位"""

    assert not BaseDataCrawler.is_placeholder_table(pd.DataFrame([["2330"], ["2317"]]))


# === 解析失敗時改問 JSON 版 ===
@pytest.mark.parametrize(
    ("json_payload", "expected_no_data"),
    [
        (TWSE_NO_DATA_JSON, True),
        (TPEX_ZERO_ROWS_JSON, True),
        ({"stat": "OK", "date": "20260501", "tables": [{"data": [["2330"]]}]}, False),
        ({"stat": "ok", "date": "20260430", "tables": [{"data": []}]}, False),
        (["unexpected", "list"], False),
    ],
    ids=[
        "twse-no-data",
        "tpex-zero-rows",
        "json-has-data",
        "json-other-date",
        "json-not-an-object",
    ],
)
def test_parse_failure_is_confirmed_by_json(
    monkeypatch: pytest.MonkeyPatch,
    json_payload: Any,
    expected_no_data: bool,
) -> None:
    """
    只有站方明確表示當天沒有資料才判休市

    JSON 也有資料，代表 HTML 版面改了；JSON 是別天的 0 列，代表沒問到這天——兩者都仍是失敗。
    """

    fake_site(monkeypatch, TWSE_BLANK_HTML, json_payload)
    url: str = "https://example.com/report?date=20260501&response=html"

    result: CrawlResult = BaseDataCrawler.parse_html_table(
        RequestUtils.fetch(url),
        "test",
        no_data_probe=(BaseDataCrawler.json_variant(url), HOLIDAY),
    )

    assert result.is_no_data is expected_no_data
    assert result.is_failed is not expected_no_data


def test_parse_failure_without_probe_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沒給確認網址的呼叫端維持原本的語意：解析不出來就是失敗"""

    requested: List[str] = fake_site(monkeypatch, TWSE_BLANK_HTML, TWSE_NO_DATA_JSON)

    result: CrawlResult = BaseDataCrawler.parse_html_table(
        RequestUtils.fetch("https://example.com/r?response=html"), "test"
    )

    assert result.is_failed
    assert len(requested) == 1


def test_json_variant_requires_an_html_url() -> None:
    """網址裡沒有 `response=html` 時明確拋錯，不要默默打到同一個 HTML 網址"""

    with pytest.raises(ValueError):
        BaseDataCrawler.json_variant("https://example.com/r?date=20260501")


# === 六個端點的休市日 ===
@pytest.mark.parametrize(
    ("crawl", "html", "json_payload"),
    [
        (
            lambda: StockPriceCrawler().crawl_twse_price,
            TWSE_BLANK_HTML,
            TWSE_NO_DATA_JSON,
        ),
        (
            lambda: StockChipCrawler().crawl_twse_chip,
            TWSE_BLANK_HTML,
            TWSE_NO_DATA_JSON,
        ),
        (
            lambda: StockMarginCrawler().crawl_twse_margin,
            TWSE_BLANK_HTML,
            TWSE_NO_DATA_JSON,
        ),
        (
            lambda: StockPriceCrawler().crawl_tpex_price,
            TPEX_PLACEHOLDER_HTML,
            TPEX_ZERO_ROWS_JSON,
        ),
        (
            lambda: StockChipCrawler().crawl_tpex_chip,
            TPEX_PLACEHOLDER_HTML,
            TPEX_ZERO_ROWS_JSON,
        ),
        (
            lambda: StockMarginCrawler().crawl_tpex_margin,
            TPEX_NO_TABLE_HTML,
            TPEX_ZERO_ROWS_JSON,
        ),
    ],
    ids=[
        "twse-price",
        "twse-chip",
        "twse-margin",
        "tpex-price",
        "tpex-chip",
        "tpex-margin",
    ],
)
def test_holiday_pages_are_no_data(
    monkeypatch: pytest.MonkeyPatch,
    crawl: Callable[[], Callable[[datetime.date], CrawlResult]],
    html: str,
    json_payload: Dict[str, Any],
) -> None:
    """三條台股日頻線、兩個市場的休市頁都判為沒有資料"""

    fake_site(monkeypatch, html, json_payload)

    result: CrawlResult = crawl()(HOLIDAY)

    assert result.is_no_data
