from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager

"""
市場開休市日期爬蟲（TWSE `holidaySchedule`）

1. **一次請求取一整年**：`date` 參數只看年份，回傳 `{"stat": "ok", "fields":
   ["日期", "名稱", "說明"], "data": [...]}`，日期是西元 `YYYY-MM-DD`。
2. **下一年度的公告通常 12 月才出來**：尚未公告的年度一樣回 `stat: ok`，只是
   `data` 為空（2026-09-22 實測 2027 年即如此），故空表是 `NO_DATA` 而不是失敗。
3. 列表**混有提醒列**（「開始交易日」「最後交易日」），那幾天其實有開市；
   本層原樣回傳，分類在 cleaner。
"""


class MarketHolidayCrawler(BaseDataCrawler):
    """爬取 TWSE 公告的市場開休市日期（一年一次請求）"""

    # 站方回傳的欄位；欄位順序或數量一變就是改版，整年不入庫
    EXPECTED_FIELDS: List[str] = ["日期", "名稱", "說明"]

    def __init__(self) -> None:
        super().__init__()

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, year: int) -> CrawlResult:
        """
        - Description:
            爬取該年度的市場開休市日期
        - Parameters:
            - year: int
                西元年
        - Return:
            - CrawlResult
                尚未公告（空表）為 `NO_DATA`；連線失敗、`stat` 非 ok、
                JSON 或欄位不符皆為 `FAILED`
        """

        label: str = f"TWSE holiday schedule {year}"
        logger.info(f"* Start crawling {label}")

        url: str = URLManager.get_url("TWSE_HOLIDAY_SCHEDULE_URL", date=f"{year}0101")
        result: FetchResult = RequestUtils.fetch(url)

        judged: Optional[CrawlResult] = self.judge_fetch(result, label)
        if judged is not None:
            return judged

        try:
            payload: Dict[str, Any] = result.response.json()
        except ValueError as error:
            # `response.json()` 解析失敗拋 `requests.exceptions.JSONDecodeError`，
            # 它同時是 `ValueError` 與 `OSError` 的子類（經 `RequestException`）——
            # 收 `ValueError` 接得住，而且不會連帶把連線類錯誤一起吞掉
            logger.warning(f"{label}: JSON 解析失敗（{type(error).__name__}: {error}）")
            return CrawlResult.failed("json_error")

        # `stat` 不是 ok 代表站方拒絕了這次查詢，不是「這一年沒有休市日」
        stat: str = str(payload.get("stat", "")).strip().upper()
        if stat != "OK":
            logger.warning(f"{label}: 站方回應 stat={stat!r}")
            return CrawlResult.failed(f"bad_stat: {stat}")

        rows: List[List[Any]] = payload.get("data") or []
        if not rows:
            logger.info(f"{label}: 站方尚未公告該年度")
            return CrawlResult.no_data("empty")

        fields: List[str] = list(payload.get("fields") or [])
        if fields != self.EXPECTED_FIELDS:
            logger.error(
                f"{label}: 欄位為 {fields}，預期 {self.EXPECTED_FIELDS}——"
                "版面可能已改制，本年度不入庫"
            )
            return CrawlResult.failed(f"column_mismatch: {fields}")

        logger.info(f"{label}: 取得 {len(rows)} 筆")
        return CrawlResult.ok(pd.DataFrame(rows, columns=fields))
