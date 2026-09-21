import datetime
import json
from typing import Any, Dict

import pytest

from core.pipeline.shared.base_crawler import CrawlStatus
from core.pipeline.shared.request_utils import FetchResult, FetchStatus
from core.pipeline.tw.crawlers.corporate_action_crawler import CorporateActionCrawler
from core.pipeline.tw.utils.tpex_date_range import check_tpex_date_range

"""
TPEX 區間查詢的回傳區間比對

日期格式送錯時，TPEX 端點不報錯，而是回傳預設區間或不帶區間；不比對的話，
拿到的錯誤那一段照樣入庫。除權息爬蟲早就有這道檢查，減資爬蟲沒有。
"""

START: datetime.date = datetime.date(2024, 1, 1)
END: datetime.date = datetime.date(2024, 12, 31)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"date": "20240101~20241231"}, True),
        ({"date": "20240918~20240920"}, False),
        ({}, False),
    ],
    ids=["match", "default-range", "no-range"],
)
def test_date_range_check(payload: Dict[str, Any], expected: bool) -> None:
    """回應的 `date` 要與送出的區間逐字相同（`YYYYMMDD~YYYYMMDD`，2026-09-21 實測）"""

    assert check_tpex_date_range(payload, START, END, "test") is expected


class _Response:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload: Dict[str, Any] = payload
        self.status_code: int = 200
        self.text: str = json.dumps(payload)

    def json(self) -> Dict[str, Any]:
        return self.payload


def test_capital_reduction_with_wrong_range_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    減資爬蟲拿到別的區間要回 FAILED，不可入庫、也不可記成 NO_DATA

    記成 NO_DATA 的話，這一年被當成「確認沒有事件」而不再補。
    """

    import core.pipeline.tw.crawlers.corporate_action_crawler as crawler_module

    payload: Dict[str, Any] = {
        "date": None,
        "tables": [{"fields": ["x"], "data": [["1"]]}],
    }
    monkeypatch.setattr(
        crawler_module.RequestUtils,
        "fetch",
        lambda url: FetchResult(FetchStatus.OK, _Response(payload)),
    )

    result = CorporateActionCrawler().crawl_tpex_capital_reduction(START, END)

    assert result.status is CrawlStatus.FAILED
    assert result.reason == "date_range_mismatch"
