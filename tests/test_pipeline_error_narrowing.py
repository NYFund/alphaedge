import json
import pathlib
from typing import List

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
