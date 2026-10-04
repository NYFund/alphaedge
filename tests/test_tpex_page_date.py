import datetime
from typing import Any

import pytest
import requests

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.crawlers.stock_chip_crawler import StockChipCrawler
from core.pipeline.tw.crawlers.stock_margin_crawler import StockMarginCrawler
from core.pipeline.tw.crawlers.stock_price_crawler import StockPriceCrawler

"""
櫃買中心頁面的資料日期核對

櫃買中心收到不合格式的日期時不報錯，而是靜靜回近幾日的資料，表格照樣解析得出來。
頁面上的日期寫法照 2026-10-04 實查（2013～2026 各年代）：收盤行情與融資融券是
「資料日期:115/09/24」，三大法人是「115年09月24日」。**完全不連網路**。
"""

DATE: datetime.date = datetime.date(2024, 1, 2)


def page(date_text: str, rows: int = 3, columns: int = 4) -> str:
    """帶資料日期與一張表的頁面"""

    body: str = "".join(
        "<tr>" + "".join(f"<td>{r}{c}</td>" for c in range(columns)) + "</tr>"
        for r in range(rows)
    )
    header: str = "".join(f"<th>h{c}</th>" for c in range(columns))
    return f"<div>{date_text}</div><table><tr>{header}</tr>{body}</table>"


def serve(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    """所有請求都回同一頁"""

    response: requests.Response = requests.Response()
    response.status_code = 200
    response._content = html.encode("utf-8")
    response.encoding = "utf-8"
    monkeypatch.setattr(
        RequestUtils, "fetch", lambda url, *a, **k: FetchResult.succeeded(response)
    )


@pytest.mark.parametrize(
    "date_text",
    ["上櫃股票每日收盤行情&nbsp;資料日期:113/01/02", "113年01月02日 三大法人買賣明細"],
    ids=["slash", "chinese"],
)
def test_matching_page_date_passes(date_text: str) -> None:
    """兩種寫法都認得；與查詢日相符時放行"""

    assert BaseDataCrawler.check_page_date(page(date_text), DATE, "t") is None


def test_mismatched_page_date_fails() -> None:
    """頁面是別天的資料：記為失敗，不入庫"""

    result: Any = BaseDataCrawler.check_page_date(page("資料日期:112/12/29"), DATE, "t")

    assert result.is_failed


def test_missing_page_date_fails() -> None:
    """找不到日期：各年代實查都有標示，找不到代表版面改了"""

    assert BaseDataCrawler.check_page_date(page("上櫃股票"), DATE, "t").is_failed


@pytest.mark.parametrize(
    ("crawl", "date_text"),
    [
        (lambda: StockPriceCrawler().crawl_tpex_price, "資料日期:{d}"),
        (lambda: StockMarginCrawler().crawl_tpex_margin, "資料日期:{d}"),
        (lambda: StockChipCrawler().crawl_tpex_chip, "{y}年{m}月{dd}日"),
    ],
    ids=["price", "margin", "chip"],
)
def test_tpex_crawlers_check_the_page_date(
    monkeypatch: pytest.MonkeyPatch, crawl: Any, date_text: str
) -> None:
    """上櫃三支爬蟲都核對日期：相符時取得資料、別天的頁面記為失敗"""

    def render(day: datetime.date) -> str:
        return date_text.format(
            d=f"{day.year - 1911}/{day:%m/%d}",
            y=day.year - 1911,
            m=f"{day:%m}",
            dd=f"{day:%d}",
        )

    serve(monkeypatch, page(render(DATE)))
    assert crawl()(DATE).is_ok

    serve(monkeypatch, page(render(DATE - datetime.timedelta(days=4))))
    result: CrawlResult = crawl()(DATE)
    assert result.is_failed
