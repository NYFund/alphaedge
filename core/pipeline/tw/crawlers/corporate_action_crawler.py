import datetime
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.tpex_date_range import check_tpex_date_range
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import TimeUtils

"""
非除權息公司行動（減資／面額變更）爬蟲

**為什麼需要這一支**：`dividend` 表只記除權息（`權息別` 只有息／權／權息），
所有不走除權息流程的公司行動都不在裡面。實測上市 2013~2026 共 360 筆減資事件、
涵蓋 243 檔，**沒有任何一筆出現在 `dividend`**；其中倍率偏離 1 超過 20% 的有 200 筆，
最極端的南亞科 2014-09-09 是 8.10 → 80.93（9.99×）。少了它們，還原價在那一天
就是一個假跳空。

兩個來源都直接給「停止買賣前收盤價」與「恢復買賣參考價」，相除即調整倍率，
不需要推算：

1. TWSE（上市）：`reducation/TWTAUU`，支援區間查詢，**一次可取整年**
   （2025 全年 17 筆／單一請求），日期參數為西元 `YYYYMMDD`
2. TPEX（上櫃）：`bulletin/revivt`，同樣支援區間查詢，日期參數為 `YYYY/MM/DD`
   （與 `TPEX_EX_RIGHT_URL` 同一個坑：格式不對不會報錯，會靜默退回預設區間）

兩邊回傳的**日期都是民國年**，但格式不同：TWSE 是 `114/02/12`、TPEX 是 `1140113`。

⚠️ **TPEX 會間歇回 HTTP 520**（S1 實測 2019 與 2024 兩年首輪失敗、重試即成功）。
本模組因此帶重試層，且**重試耗盡一律回 FAILED 而不是 NO_DATA**——把連線失敗
記成「該年沒有事件」會讓那一年再也不會被補。
"""

# TPEX 的 520 重試設定；間隔刻意拉長，520 多半是站方短暫過載而非限流
TPEX_MAX_RETRIES: int = 3
TPEX_RETRY_INTERVAL_SECONDS: int = 5

# 兩邊的欄位數（用於版面改制的早期偵測）
TWSE_COLUMN_COUNT: int = 11
TPEX_COLUMN_COUNT: int = 11


class CorporateActionCrawler(BaseDataCrawler):
    """爬取上市、上櫃的減資（含面額變更）恢復買賣參考價"""

    def __init__(self) -> None:
        super().__init__()

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, start_date: datetime.date, end_date: datetime.date) -> None:
        """Crawl TWSE & TPEX Capital Reduction Data"""

        self.crawl_twse_capital_reduction(start_date, end_date)
        self.crawl_tpex_capital_reduction(start_date, end_date)

    def crawl_twse_capital_reduction(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> CrawlResult:
        """
        - Description:
            TWSE 減資恢復買賣參考價區間爬蟲

            本端點支援日期區間，呼叫端應以「年」為單位切分，不要退化成逐日呼叫。
        - Parameters:
            - start_date: datetime.date
                查詢起日
            - end_date: datetime.date
                查詢迄日
        - Return:
            - CrawlResult
                區間內無事件為 `NO_DATA`；連線或版面異常為 `FAILED`
        """

        label: str = f"TWSE corporate action {start_date}~{end_date}"
        logger.info(f"* Start crawling {label}")

        url: str = URLManager.get_url(
            "TWSE_CAPITAL_REDUCTION_URL",
            start_date=TimeUtils.format_date(start_date, sep=""),
            end_date=TimeUtils.format_date(end_date, sep=""),
        )
        result: FetchResult = RequestUtils.fetch(url)

        judged: Optional[CrawlResult] = self.judge_fetch(result, label)
        if judged is not None:
            return judged

        payload: Optional[Dict[str, Any]] = self._parse_json(result, label)
        if payload is None:
            return CrawlResult.failed("json_error")

        # `stat` 不是 OK 代表站方拒絕了這次查詢，不是「沒有事件」
        stat: str = str(payload.get("stat", "")).strip().upper()
        if stat != "OK":
            logger.warning(f"{label}: 站方回應 stat={stat!r}")
            return CrawlResult.failed(f"bad_stat: {stat}")

        fields: List[str] = payload.get("fields") or []
        rows: List[List[Any]] = payload.get("data") or []
        if not rows:
            logger.info(f"{label}: 區間內無減資事件")
            return CrawlResult.no_data("empty")

        return self._build_result(rows, fields, label, TWSE_COLUMN_COUNT, "twse")

    def crawl_tpex_capital_reduction(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> CrawlResult:
        """
        - Description:
            TPEX（櫃買中心）減資恢復買賣參考價區間爬蟲

            **日期必須用斜線格式**（與 `TPEX_EX_RIGHT_URL` 同一個坑）：傳
            `20240101` 不會報錯，回應會不帶區間。故以 `check_tpex_date_range()`
            比對回應的 `date`，不符即為 `FAILED`。

            **帶重試層**：本端點會間歇回 HTTP 520。重試耗盡時回 `FAILED`，
            **不可回 `NO_DATA`**——後者會讓這一年被記成「確認沒有事件」而不再補。
        - Parameters:
            - start_date: datetime.date
                查詢起日
            - end_date: datetime.date
                查詢迄日
        - Return:
            - CrawlResult
                區間內無事件為 `NO_DATA`；連線、JSON 解析或重試耗盡為 `FAILED`
        """

        label: str = f"TPEX corporate action {start_date}~{end_date}"
        logger.info(f"* Start crawling {label}")

        url: str = URLManager.get_url(
            "TPEX_CAPITAL_REDUCTION_URL",
            start_date=TimeUtils.format_date(start_date, sep="/"),
            end_date=TimeUtils.format_date(end_date, sep="/"),
        )

        payload: Optional[Dict[str, Any]] = None
        last_reason: str = "unknown"

        for attempt in range(1, TPEX_MAX_RETRIES + 1):
            result: FetchResult = RequestUtils.fetch(url)
            judged: Optional[CrawlResult] = self.judge_fetch(result, label)
            if judged is not None:
                # 連線層就失敗（含 520 會被歸進 UNREACHABLE／FAILED）；還有次數就重試
                last_reason = judged.reason or "fetch_failed"
                if attempt < TPEX_MAX_RETRIES:
                    logger.warning(
                        f"{label}: 第 {attempt} 次失敗（{last_reason}），"
                        f"{TPEX_RETRY_INTERVAL_SECONDS} 秒後重試"
                    )
                    time.sleep(TPEX_RETRY_INTERVAL_SECONDS)
                    continue
                break

            payload = self._parse_json(result, label)
            if payload is not None:
                break

            last_reason = "json_error"
            if attempt < TPEX_MAX_RETRIES:
                logger.warning(
                    f"{label}: 第 {attempt} 次 JSON 解析失敗，"
                    f"{TPEX_RETRY_INTERVAL_SECONDS} 秒後重試"
                )
                time.sleep(TPEX_RETRY_INTERVAL_SECONDS)

        if payload is None:
            # **重試耗盡是 FAILED 不是 NO_DATA**：記成後者這一年就再也不會被補
            logger.error(f"{label}: {TPEX_MAX_RETRIES} 次皆失敗（{last_reason}）")
            return CrawlResult.failed(f"retries_exhausted: {last_reason}")

        # 區間不符代表送出的日期格式被站方靜靜忽略，拿到的不是這一年的資料。
        # 這是取錯資料而不是沒有資料，故為 FAILED——記成 NO_DATA 這一年就再也不補
        if not check_tpex_date_range(
            payload, start_date, end_date, "TPEX corporate action"
        ):
            return CrawlResult.failed("date_range_mismatch")

        tables: List[Dict[str, Any]] = payload.get("tables") or []
        if not tables:
            logger.warning(f"{label}: 回應中沒有 tables 欄位")
            return CrawlResult.failed("no_tables")

        table: Dict[str, Any] = tables[0]
        fields: List[str] = table.get("fields") or []
        rows: List[List[Any]] = table.get("data") or []
        if not rows:
            logger.info(f"{label}: 區間內無減資事件")
            return CrawlResult.no_data("empty")

        return self._build_result(rows, fields, label, TPEX_COLUMN_COUNT, "tpex")

    @staticmethod
    def _parse_json(result: FetchResult, label: str) -> Optional[Dict[str, Any]]:
        """把回應解析成 dict；失敗回 None（由呼叫端決定重試或定案）"""

        try:
            return result.response.json()
        except ValueError as error:
            # 見 `stock_dividend_crawler`：`JSONDecodeError` 是 `ValueError` 的子類
            logger.warning(f"{label}: JSON 解析失敗（{type(error).__name__}: {error}）")
            return None

    @staticmethod
    def _build_result(
        rows: List[List[Any]],
        fields: List[str],
        label: str,
        expected_columns: int,
        source: str,
    ) -> CrawlResult:
        """
        - Description:
            把站方的 `fields` ＋ `data` 組成帶欄名的 DataFrame

            **欄數不符即 FAILED**：版面改制時寧可整年不入庫，也不要把錯位的
            欄位當成正確資料寫進去——後者是靜默的錯，而且會一路錯到還原價。
        - Parameters:
            - rows: List[List[Any]]
                站方回傳的資料列
            - fields: List[str]
                站方回傳的欄位名
            - label: str
                來源與區間的描述，只用於訊息
            - expected_columns: int
                預期欄數
            - source: str
                `twse` 或 `tpex`，寫進 `資料來源` 欄
        - Return:
            - CrawlResult
        """

        if len(fields) != expected_columns:
            logger.error(
                f"{label}: 欄數為 {len(fields)}，預期 {expected_columns}——"
                "版面可能已改制，本區間不入庫"
            )
            return CrawlResult.failed(
                f"column_mismatch: {len(fields)} != {expected_columns}"
            )

        df: pd.DataFrame = pd.DataFrame(rows, columns=fields)
        df["資料來源"] = source

        logger.info(f"{label}: 取得 {len(df)} 筆")
        return CrawlResult.ok(df)
