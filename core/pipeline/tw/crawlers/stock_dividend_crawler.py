import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.tpex_date_range import check_tpex_date_range
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import TimeUtils

"""
除權除息計算結果表爬蟲資料時間表：
1. TWSE（上市）
    - 來源：TWT49U（除權除息計算結果表），支援 startDate／endDate 區間查詢
    - **一次可取整年**（2024 全年 1184 筆／單一請求），故不採逐日爬取
    - 民國 90 年起提供；本專案實測 2013 年起表格結構未再改制
2. TPEX（上櫃）
    - 來源：櫃買中心「除權除息計算結果表」（`bulletin/exDailyQ`），同樣支援區間查詢
    - **一次可取整年**（2024 全年 1060 筆／單一請求）；官方頁面標示資料自 2008/01/02 起
    - 欄位比證交所更細：`權值`／`息值`／`現金股利`／`每仟股無償配股` 皆分開提供
    - **版面曾於 2016 年改制**：2013~2015 為 22 欄（多一欄 `員工紅利轉增資`），
      2016 起為 21 欄。cleaner 依**欄位名稱**對應而非位置，兩種版面皆可正確清洗
      （已實測 2013／2015／2016 逐年清洗零筆遺漏）

兩個來源皆為交易所官方，不需要第三方資料源。

實測涵蓋（2026-08-15，逐年掃描 2013~2026）：
- 上市每年皆為 15 欄，無改制
- 上櫃 2013~2015 為 22 欄、2016 起 21 欄，`totalCount` 與實際筆數逐年相符
"""


class StockDividendCrawler(BaseDataCrawler):
    """爬取上市、上櫃股票除權除息計算結果表"""

    def __init__(self) -> None:
        super().__init__()

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, start_date: datetime.date, end_date: datetime.date) -> None:
        """Crawl TWSE & TPEX Ex-Rights/Ex-Dividend Data"""

        self.crawl_twse_dividend(start_date, end_date)
        self.crawl_tpex_dividend(start_date, end_date)

    def crawl_twse_dividend(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> CrawlResult:
        """
        - Description:
            TWSE 除權除息計算結果表區間爬蟲

            與其他 TWSE 爬蟲不同，本端點支援日期區間，呼叫端應以「年」為單位切分，
            不要退化成逐日呼叫（一年 250 次請求 vs 1 次）

        - Parameters:
            - start_date: datetime.date
                查詢起日
            - end_date: datetime.date
                查詢迄日

        - Return:
            - CrawlResult
                區間內無除權息為 `NO_DATA`；連線或版面異常為 `FAILED`
        """

        logger.info(f"* Start crawling TWSE dividend: {start_date} ~ {end_date}")

        twse_url: str = URLManager.get_url(
            "TWSE_EX_RIGHT_URL",
            start_date=TimeUtils.format_date(start_date, sep=""),
            end_date=TimeUtils.format_date(end_date, sep=""),
        )
        result: FetchResult = RequestUtils.fetch(twse_url)

        # 股票代號含 ETF（00xxx）會被推斷為 int 而丟失前導 0，以 converters 保留原始字串
        return self.parse_html_table(
            result,
            f"TWSE dividend {start_date}~{end_date}",
            index=0,
            converters={1: str},
        )

    def crawl_tpex_dividend(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> CrawlResult:
        """
        - Description:
            TPEX（櫃買中心）除權除息計算結果表區間爬蟲

            回傳格式為 `{"tables": [{"fields": [...], "data": [[...], ...]}]}`，
            本方法組回帶欄名的 DataFrame 再交給 cleaner。

            **日期必須用斜線格式**：傳 `20240101` 不會報錯，而是靜默退回「近三日」的
            預設區間。因此這裡會比對回傳的 `date` 欄位是否等於送出的區間，
            不符時直接中止——寧可沒有資料，也不要把三天的資料當成一整年入庫。

        - Parameters:
            - start_date: datetime.date
                查詢起日
            - end_date: datetime.date
                查詢迄日

        - Return:
            - CrawlResult
                區間內無除權息為 `NO_DATA`；連線、JSON 解析或區間不符為 `FAILED`
        """

        logger.info(f"* Start crawling TPEX dividend: {start_date} ~ {end_date}")

        label: str = f"TPEX dividend {start_date}~{end_date}"
        tpex_url: str = URLManager.get_url(
            "TPEX_EX_RIGHT_URL",
            start_date=TimeUtils.format_date(start_date, sep="/"),
            end_date=TimeUtils.format_date(end_date, sep="/"),
        )
        result: FetchResult = RequestUtils.fetch(tpex_url)

        judged: Optional[CrawlResult] = self.judge_fetch(result, label)
        if judged is not None:
            return judged

        try:
            payload: Dict[str, Any] = result.response.json()
        except Exception as error:
            logger.warning(f"{label}: JSON 解析失敗（{type(error).__name__}: {error}）")
            return CrawlResult.failed(f"json_error: {type(error).__name__}")

        # 區間不符代表**送出的日期格式被站方靜靜忽略**，拿到的是「近三日」而非整年。
        # 這是取錯資料，不是沒有資料，故為 FAILED——記成 NO_DATA 會讓這一年再也不補。
        if not check_tpex_date_range(payload, start_date, end_date, "TPEX dividend"):
            return CrawlResult.failed("date_range_mismatch")

        tables: List[Dict[str, Any]] = payload.get("tables") or []
        if not tables:
            logger.warning(f"{label}: 回應中沒有 tables 欄位")
            return CrawlResult.failed("no_tables_in_payload")

        fields: List[str] = tables[0].get("fields") or []
        rows: List[List[Any]] = tables[0].get("data") or []

        if not fields:
            logger.warning(f"{label}: 回應中沒有欄位定義")
            return CrawlResult.failed("no_fields_in_payload")

        if not rows:
            logger.info(f"{label}: 區間內無除權息")
            return CrawlResult.no_data("區間內無除權息")

        return CrawlResult.ok(pd.DataFrame(rows, columns=fields))
