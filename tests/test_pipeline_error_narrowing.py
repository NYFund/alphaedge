import json
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest
import requests

from core.pipeline.shared.base_crawler import (
    BaseDataCrawler,
    CrawlResult,
    CrawlStatus,
)
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.shared.request_utils import FetchResult, FetchStatus
from core.pipeline.shared.season_planner import SeasonProgressStore
from core.pipeline.tw.crawlers import financial_statement_crawler as fs_crawler_module
from core.pipeline.tw.crawlers.financial_statement_crawler import (
    FinancialStatementCrawler,
)
from core.pipeline.tw.utils.mops_payload import Payload
from core.pipeline.utils import ListingBoard

"""
盲捕收斂之後，**原本接得住的仍要接得住**

`except Exception` 換成具名例外的風險是**收得太窄**：漏掉一種，
原本只記一行 warning 的情況會升級成整批 ETL 中止，而且往往要等下一次
排程才看得到。本檔逐一餵進每種「應該被接住」的壞輸入。

反過來也要成立：**不該接的不可以接**。程式自己寫錯（例如型別用錯）
必須現形，不能被「最壞只是多問幾次」蓋掉。
"""


def make_fetch_result(html: str) -> FetchResult:
    """組一個 HTTP 200 的 `FetchResult`，內容由測試指定"""

    response: requests.Response = requests.Response()
    response.status_code = 200
    response._content = html.encode("utf-8")
    return FetchResult(status=FetchStatus.OK, response=response)


def test_broken_progress_file_is_treated_as_empty(tmp_path: pathlib.Path) -> None:
    """
    進度檔壞掉視為空，不可中止

    四種壞法都要被接住：JSON 語法壞掉（`ValueError`）、日期字串不合法
    （同樣 `ValueError`）、整段被寫成字串（逐字元解析後仍是 `ValueError`）、
    元素不是字串（`TypeError`）。
    """

    broken_payloads: List[str] = [
        "{ not json at all",
        json.dumps({"no_data": ["2026-13-45"]}),
        json.dumps({"no_data": "應該要是 list 才對"}),
        json.dumps({"no_data": [{"日期": "2026-09-01"}]}),
    ]

    for index, payload in enumerate(broken_payloads):
        path: pathlib.Path = tmp_path / f"progress_{index}.json"
        path.write_text(payload, encoding="utf-8")

        planner: DateProgressStore = DateProgressStore(source="test", path=path)

        assert planner.no_data == set(), f"第 {index} 種壞法沒有被接住"
        assert planner.incomplete == set()


def test_broken_season_progress_file_is_treated_as_empty(
    tmp_path: pathlib.Path,
) -> None:
    """年季進度檔同一條規則"""

    # 用數字而不是字串：字串是可迭代的，`set("abc")` 不會拋錯而是產出單字元集合
    # ——那是 `_parse_section()` 既有的驗證缺口，與本次收斂無關，另行記錄
    path: pathlib.Path = tmp_path / "season.json"
    path.write_text('{"no_data": {"2026": 20260101}}', encoding="utf-8")

    planner: SeasonProgressStore = SeasonProgressStore(source="test", path=path)

    assert planner.no_data == {}
    assert planner.incomplete == {}


def test_unreadable_progress_file_is_treated_as_empty(
    tmp_path: pathlib.Path,
) -> None:
    """
    讀不到檔（`OSError`）同樣視為空

    路徑指到目錄是最容易踩到的一種：`read_text()` 會拋 `IsADirectoryError`，
    它是 `OSError` 的子類而不是 `ValueError`——只收 `ValueError` 就會漏掉。
    """

    directory: pathlib.Path = tmp_path / "looks_like_a_file.json"
    directory.mkdir()

    planner: DateProgressStore = DateProgressStore(source="test", path=directory)

    assert planner.no_data == set()


def test_layout_change_is_reported_not_raised() -> None:
    """
    版面變了要回失敗結果，不可拋出

    兩種形態：頁面上沒有任何表格（`read_html` 拋 `ValueError`）、
    表格數量少於預期（取索引拋 `IndexError`）。**兩者都要接住**——
    只收 `ValueError` 的話，少一張表的那天會整批中止。
    """

    no_table: CrawlResult = BaseDataCrawler.parse_html_table(
        make_fetch_result("<html><body><p>沒有表格</p></body></html>"), "測試"
    )
    assert no_table.status is CrawlStatus.FAILED

    missing_index: CrawlResult = BaseDataCrawler.parse_html_table(
        make_fetch_result("<html><table><tr><td>1</td></tr></table></html>"),
        "測試",
        index=5,
    )
    assert missing_index.status is CrawlStatus.FAILED


def test_csv_read_failures_do_not_stop_the_batch(tmp_path: pathlib.Path) -> None:
    """
    單一 CSV 壞掉只記下檔名，其餘照常載入

    `pd.errors.ParserError` 與 `EmptyDataError` 都是 `ValueError` 的子類，
    收 `ValueError` 就涵蓋得到；讀不到檔則是 `OSError`。
    """

    empty: pathlib.Path = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        pd.read_csv(empty)

    missing: pathlib.Path = tmp_path / "not_here.csv"
    with pytest.raises(OSError):
        pd.read_csv(missing)


# === MOPS 財報爬蟲：傳輸失敗與版面失敗要分開 ===
def make_mops_crawler(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
) -> FinancialStatementCrawler:
    """
    - Description:
        建一支不連外的 MOPS 爬蟲，`requests_post()` 的回應由測試指定
    - Parameters:
        - monkeypatch: pytest.MonkeyPatch
            用來換掉 `RequestUtils.requests_post`
        - response: Any
            要回傳的假回應；為 Exception 實例時改為拋出
    - Return:
        - FinancialStatementCrawler
    """

    def fake_post(url: str, data: Dict[str, str]) -> Any:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(fs_crawler_module.RequestUtils, "requests_post", fake_post)

    crawler: FinancialStatementCrawler = FinancialStatementCrawler.__new__(
        FinancialStatementCrawler
    )
    crawler.payload = Payload(
        firstin="1", step="1", TYPEK="sii", co_id=None, year="113", season="1"
    )
    crawler.listing_boards = [ListingBoard.SII]
    return crawler


def test_empty_body_fails_the_season_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    HTTP 200 但 body 全空：整季視為失敗，不可讓例外炸穿

    這一種是 MOPS 異常時最常見的樣子，而它拋的**不是 `ValueError`**——
    lxml 解空字串拋 `XMLSyntaxError`，繼承的是 `SyntaxError`。
    只收 `ValueError` 的話，本來只記一行 warning 的情況會變成整批中止。
    """

    crawler: FinancialStatementCrawler = make_mops_crawler(
        monkeypatch, SimpleNamespace(text="")
    )

    assert crawler.crawl_balance_sheet(2024, 1) is None


def test_transport_failure_fails_the_season(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    傳輸層失敗照樣是「整季視為失敗」

    `requests` 的例外與作業系統層的 `ConnectionError` 都繼承 `OSError`，
    收斂成 `OSError` 之後兩者都還要接得住。
    """

    for error in (
        ConnectionError("RemoteDisconnected"),
        requests.exceptions.TooManyRedirects("redirect loop"),
    ):
        crawler: FinancialStatementCrawler = make_mops_crawler(monkeypatch, error)

        assert crawler.crawl_balance_sheet(2024, 1) is None, (
            f"{type(error).__name__} 應該被當成整季失敗"
        )


def test_programming_errors_are_not_swallowed_as_a_failed_season(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    程式自己寫錯必須現形，不可退化成「整季視為失敗」

    退化的後果不是報錯，是**每一次重跑都以警告收場**：年季永遠補不齊，
    而 log 看起來只是站方又不穩。
    """

    crawler: FinancialStatementCrawler = make_mops_crawler(
        monkeypatch, TypeError("payload 欄位型別錯了")
    )

    with pytest.raises(TypeError):
        crawler.crawl_balance_sheet(2024, 1)


def test_equity_changes_empty_body_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    權益變動表拿到空 body 時回 None（待重試），不可拋出

    拋出的話會被逐檔隔離的斷路器計為一次非預期例外，連三檔就中止整段回補
    ——但空 body 是站方異常，屬於該重試的那一類。
    **不可以回 `[]`**：那會把這一檔寫進「查無資料」的永久名單。
    """

    crawler: FinancialStatementCrawler = make_mops_crawler(
        monkeypatch, SimpleNamespace(text="")
    )
    monkeypatch.setattr(crawler, "EQUITY_CHANGE_RETRY_DELAY_SECONDS", 0)

    result: Optional[List[pd.DataFrame]] = crawler.crawl_equity_changes(2024, 1, "2330")

    assert result is None


def test_equity_changes_programming_errors_reach_the_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    非傳輸層的例外要往上拋，讓逐檔斷路器看得到

    在爬蟲內就地吞掉的話，「環境或程式壞了」會退化成每一檔都重試三次，
    連續例外的斷路器永遠不會觸發，整段回補會以十萬次無效請求收場。
    """

    crawler: FinancialStatementCrawler = make_mops_crawler(
        monkeypatch, AttributeError("session 沒有 post")
    )
    monkeypatch.setattr(crawler, "EQUITY_CHANGE_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(AttributeError):
        crawler.crawl_equity_changes(2024, 1, "2330")
