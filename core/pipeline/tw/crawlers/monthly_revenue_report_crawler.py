from io import StringIO
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import (
    MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH,
)
from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.pipeline.utils import IssuerOrigin
from core.utils import FileEncoding, TimeUtils

"""
月營收報表爬蟲

**一個「年月」要打四次請求**（TWSE／TPEX × 國內／外國發行人），四張表欄位不同、
無法先合併，故用 `CrawlResult.tables` 承載。

四次之中只要有**任何一次失敗**，整個年月就是 `FAILED`：拿到一半的表會讓
cleaner 產出一份「少了外國發行人」的月營收，數字看起來正常、實際短少數百檔，
而且不會有任何錯誤。寧可整個年月重來。
"""


class MonthlyRevenueReportCrawler(BaseDataCrawler):
    """爬取上市、上櫃的月營收報表（國內與外國發行人分開查詢）"""

    CRAWL_DELAY_MIN: float = 1.0
    CRAWL_DELAY_MAX: float = 3.0

    def __init__(self) -> None:
        self.mrr_dir: Path = MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH

        # 發行人國別；TWSE／TPEX 的區分由各自的爬取函式決定，故兩邊共用同一份清單
        self.issuer_origins: List[IssuerOrigin] = [
            IssuerOrigin.DOMESTIC,
            IssuerOrigin.FOREIGN,
        ]

    def setup(self) -> None:
        """Set Up the Config of Crawler"""

        self.mrr_dir.mkdir(parents=True, exist_ok=True)

    def crawl(self, year: int, month: int) -> CrawlResult:
        """
        - Description:
            爬取指定年月的上市＋上櫃月營收報表

            兩邊任一為 `FAILED` 即整體 `FAILED`：只入庫其中一個市場，
            會得到一份數字看起來正常、實際短少數百檔的月營收。
        - Parameters:
            - year: int
                西元年
            - month: int
                月份
        - Return:
            - CrawlResult
                成功時 `tables` 為四張原始表格
        """

        twse: CrawlResult = self.crawl_twse_monthly_revenue(year, month)
        tpex: CrawlResult = self.crawl_tpex_monthly_revenue(year, month)
        label: str = f"MRR {year}/{month}"

        # **一邊查無資料、另一邊有資料 → 整個年月失敗**，與日頻三表的
        # `record_market_day()` 同語意：站方的「查無資料」涵蓋「尚未公布」，
        # 兩個市場的公布時間不同，先公布的那一邊若照常入庫，該月就只有半個市場
        # （2026/04 補回時「只有 26 檔」即此成因）。兩邊皆查無資料時維持 NO_DATA，
        # 那是當月還沒公布的正常狀態
        if twse.is_no_data != tpex.is_no_data:
            missing: str = "TWSE" if twse.is_no_data else "TPEX"
            logger.warning(
                f"{label}: {missing} 查無資料、另一個市場有資料，"
                f"整個年月視為失敗、不入庫，下次執行會重試"
            )
            return CrawlResult.failed(f"partial_market: {missing} no_data")

        return self.combine_results([twse, tpex], label)

    @staticmethod
    def combine_results(results: List[CrawlResult], label: str) -> CrawlResult:
        """
        - Description:
            合併多次請求的結果：任一失敗即整體失敗，全部沒資料才是沒資料
        - Parameters:
            - results: List[CrawlResult]
                各次請求的結果
            - label: str
                描述文字，只用於訊息
        - Return:
            - CrawlResult
        """

        failed: List[CrawlResult] = [r for r in results if r.is_failed]
        if failed:
            reasons: str = "; ".join(r.reason for r in failed)
            logger.warning(
                f"{label}: 有 {len(failed)} 次請求失敗（{reasons}），整批視為失敗"
            )
            return CrawlResult.failed(reasons)

        tables: List[pd.DataFrame] = []
        for result in results:
            tables.extend(result.tables)

        if not tables:
            return CrawlResult.no_data("站方回覆查無資料")

        return CrawlResult.ok_tables(tables)

    def crawl_exchange_monthly_revenue(
        self,
        url_key: str,
        exchange: str,
        year: int,
        month: int,
    ) -> CrawlResult:
        """
        - Description:
            爬取單一交易所的月營收報表（國內＋外國發行人各一次請求）

            上市櫃兩支的差別只有 URL key 與訊息中的名稱，故共用同一份實作。
        - Parameters:
            - url_key: str
                `URLManager` 的 URL 名稱
            - exchange: str
                交易所名稱，只用於訊息
            - year: int
                西元年
            - month: int
                月份
        - Return:
            - CrawlResult
        """

        logger.info(f"* Start crawling {exchange} MRR: {year}/{month}")

        results: List[CrawlResult] = []

        for issuer_origin in self.issuer_origins:
            label: str = f"{exchange} MRR {year}/{month} {issuer_origin.name}"
            url: str = URLManager.get_url(
                url_key,
                roc_year=TimeUtils.convert_ad_to_roc_year(year),
                month=month,
                issuer_origin=issuer_origin.value,
            )

            fetched: FetchResult = RequestUtils.fetch(url)
            if fetched.ok and fetched.response is not None:
                # 站方以 BIG5 回傳，未指定編碼時中文欄名會變成亂碼
                fetched.response.encoding = FileEncoding.BIG5.value

            judged: Optional[CrawlResult] = self.judge_fetch(fetched, label)
            if judged is not None:
                results.append(judged)
                continue

            try:
                dfs: List[pd.DataFrame] = pd.read_html(StringIO(fetched.text))
            except ValueError as error:
                # `read_html()` 找不到表格時拋 `ValueError`。單月失敗只記一筆，
                # 其餘月份照跑——一個月的版面異常不該讓整批回補停下
                logger.warning(
                    f"{label}: 版面解析失敗（{type(error).__name__}: {error}）"
                )
                results.append(
                    CrawlResult.failed(f"parse_error: {type(error).__name__}")
                )
                continue

            if not dfs:
                results.append(CrawlResult.no_data("沒有任何表格"))
                continue

            results.append(CrawlResult.ok_tables(dfs))

        return self.combine_results(results, f"{exchange} MRR {year}/{month}")

    def crawl_twse_monthly_revenue(self, year: int, month: int) -> CrawlResult:
        """
        - Description:
            爬取上市月營收報表（102／2013 年前不區分國內外，故由該年起爬）
        - Parameters:
            - year: int
                西元年
            - month: int
                月份
        - Return:
            - CrawlResult
        """

        return self.crawl_exchange_monthly_revenue(
            "TWSE_MONTHLY_REVENUE_REPORT_URL", "TWSE", year, month
        )

    def crawl_tpex_monthly_revenue(self, year: int, month: int) -> CrawlResult:
        """
        - Description:
            爬取上櫃月營收報表（102／2013 年前不區分國內外，故由該年起爬）
        - Parameters:
            - year: int
                西元年
            - month: int
                月份
        - Return:
            - CrawlResult
        """

        return self.crawl_exchange_monthly_revenue(
            "TPEX_MONTHLY_REVENUE_REPORT_URL", "TPEX", year, month
        )
