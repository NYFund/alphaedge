from types import SimpleNamespace
from typing import Any, Dict

import pytest

from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.tw.crawlers import financial_statement_crawler as fs_crawler_module
from core.pipeline.tw.crawlers.financial_statement_crawler import (
    FinancialStatementCrawler,
)

"""
權益變動表：「採 IFRSs 前」導流訊息要算查無資料，但不能把其他解不出表格的頁面也吞掉

MOPS 對 1107、1606、2381、2396、3426、6457 的 2013~2014 年季回 HTTP 200，
頁面只有一句「公開發行公司103年(含)以前之財報資料請至採IFRSs前之 個別報表 或
合併報表 查詢！」，沒有任何表格。原本這會走「解不出表格 → None（待重試）」，
於是每輪整段回補都會空打這 19 筆，結果永遠一樣。

三態語意：`None` 待重試、`[]` 確認查無資料、非空 list 有資料。本檔不連網路。
"""


PRE_IFRS_PAGE: str = (
    "<html><body><center>"
    "公開發行公司103年(含)以前之財報資料請至採IFRSs前之 個別報表 或 合併報表 查詢！"
    "</center></body></html>"
)
TABLE_HTML: str = "<table><tr><th>會計項目</th></tr><tr><td>期初餘額</td></tr></table>"


def request(monkeypatch: pytest.MonkeyPatch, text: str) -> CrawlResult:
    """以指定的回應內容呼叫 `_request_equity_changes()` 一次"""

    def fake_post(url: str, data: Dict[str, str]) -> Any:
        return SimpleNamespace(text=text)

    monkeypatch.setattr(fs_crawler_module.RequestUtils, "requests_post", fake_post)

    # 本方法只用到 class 常數，跳過 __init__ 以免建立 downloads 目錄
    crawler = FinancialStatementCrawler.__new__(FinancialStatementCrawler)
    return crawler._request_equity_changes(
        url="https://example.invalid/ajax_t164sb06",
        payload={"co_id": "1107"},
        year=2013,
        season=2,
        stock_id="1107",
    )


def test_pre_ifrs_redirect_is_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """導流訊息回 []，才會被寫進查無資料的永久名單、之後不再重打"""

    assert request(monkeypatch, PRE_IFRS_PAGE).is_no_data


def test_unparseable_page_without_marker_is_still_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    防迴歸：不含任何 marker 又解不出表格時仍回 None

    那代表版面改了或拿到錯誤頁，是要人看的狀況；新增一種分類不能把它也吞成沒資料。
    """

    page: str = "<html><body>系統維護中，請稍後再試</body></html>"

    assert request(monkeypatch, page).is_failed


def test_explicit_no_data_message_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """既有行為不得改變：「查無資料」照樣回 []"""

    assert request(monkeypatch, "<html><body>查無資料！</body></html>").is_no_data


def test_page_with_tables_is_never_swallowed_by_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    頁面有表格時一律回傳表格，即使裡面出現「採IFRSs前」

    marker 只在解不出表格時才看；整頁比對的話，正常報表頁的導覽或註腳出現這幾個字，
    就會把有資料的公司永久記成查無資料，而且不會報錯。
    """

    page: str = f"<html><body><a>採IFRSs前之報表</a>{TABLE_HTML}</body></html>"

    result: CrawlResult = request(monkeypatch, page)

    assert result.is_ok
    assert len(result.tables) == 1
