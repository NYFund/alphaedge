import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import TimeUtils

"""
交易所每日公告的兩份資格名單爬蟲（上市＋上櫃，皆為 JSON 端點）：

1. 平盤下得融（借）券賣出之證券名單
    - TWSE `TWT92U`、TPEX `margin/mark`；自 2013-09-23 起提供
    - 名單＝**全部可融資融券的證券**，另附三個當日註記：
      暫停融券賣出、暫停借券賣出、前一交易日收盤跌停本日禁止平盤下融（借）券賣出
2. 現股當日沖銷交易標的
    - TWSE `TWTB4U`（第二張表）、TPEX `intraday/list`；自 2014-01-06 起提供
    - 名單是**完整可當沖名單**而非「有成交的才列」，另附「暫停現股賣出後現款買進當沖」註記

**回應的 `date` 一定要核對**：TPEX 收到非斜線格式的日期時不報錯，而是靜靜回傳
近幾日的資料；拿錯天的名單入庫，回測擋單會整段錯位且沒有任何徵兆。

**交易日回 0 列算失敗，不算查無資料**：兩份名單在任何交易日都有上千檔，
0 列只可能是站方異常或查詢日期早於起點（TPEX 在起點之前回 `ok`、0 列）。
記成查無資料會讓那一天永遠不再補，回測又會把「名單是空的」讀成「全部不得放空」。
"""


class StockTradingListCrawler(BaseDataCrawler):
    """爬取上市、上櫃的平盤下融（借）券名單與現股當沖名單"""

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, date: datetime.date) -> None:
        """Crawl TWSE & TPEX Short Sale and Day Trade Lists"""

        self.crawl_twse_short_sale_list(date)
        self.crawl_tpex_short_sale_list(date)
        self.crawl_twse_day_trade_list(date)
        self.crawl_tpex_day_trade_list(date)

    def crawl_twse_short_sale_list(self, date: datetime.date) -> CrawlResult:
        """TWSE 平盤下得融（借）券賣出名單單日爬蟲（欄位與資料在最外層，沒有 `tables`）"""

        return self.fetch_list(
            "TWSE_SHORT_SALE_LIST_URL",
            date,
            sep="",
            label=f"TWSE short sale list {date}",
            locate=lambda payload: payload,
        )

    def crawl_tpex_short_sale_list(self, date: datetime.date) -> CrawlResult:
        """TPEX 平盤下得融（借）券賣出名單單日爬蟲"""

        return self.fetch_list(
            "TPEX_SHORT_SALE_LIST_URL",
            date,
            sep="/",
            label=f"TPEX short sale list {date}",
            locate=lambda payload: self.table_at(payload, 0),
        )

    def crawl_twse_day_trade_list(self, date: datetime.date) -> CrawlResult:
        """TWSE 現股當沖標的單日爬蟲（第一張表是全市場統計，名單在第二張）"""

        return self.fetch_list(
            "TWSE_DAY_TRADE_LIST_URL",
            date,
            sep="",
            label=f"TWSE day trade list {date}",
            locate=lambda payload: self.table_at(payload, 1),
        )

    def crawl_tpex_day_trade_list(self, date: datetime.date) -> CrawlResult:
        """TPEX 現股當沖標的單日爬蟲"""

        return self.fetch_list(
            "TPEX_DAY_TRADE_LIST_URL",
            date,
            sep="/",
            label=f"TPEX day trade list {date}",
            locate=lambda payload: self.table_at(payload, 0),
        )

    @staticmethod
    def table_at(payload: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
        """取 `tables` 的第幾張表；張數不足時為 None（由呼叫端記為版面異常）"""

        tables: List[Dict[str, Any]] = payload.get("tables") or []
        return tables[index] if len(tables) > index else None

    def fetch_list(
        self,
        url_key: str,
        date: datetime.date,
        sep: str,
        label: str,
        locate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
    ) -> CrawlResult:
        """
        - Description:
            四個端點共用的 `fetch → 判斷 → 解析 JSON → 核對日期` 流程
        - Parameters:
            - url_key: str
                `URLManager` 的端點代號
            - date: datetime.date
                交易日
            - sep: str
                日期分隔字元（TWSE 為空字串、TPEX 為斜線）
            - label: str
                來源與日期的描述，只用於訊息
            - locate: Callable
                從整份 JSON 取出「含 `fields` 與 `data` 的那一層」
        - Return:
            - CrawlResult
                站方明確回覆查無資料為 `NO_DATA`；連線、解析、日期不符或 0 列為 `FAILED`
        """

        logger.info(f"* Start crawling {label}")

        url: str = URLManager.get_url(
            url_key, date=TimeUtils.format_date(date, sep=sep)
        )
        result: FetchResult = RequestUtils.fetch(url)

        judged: Optional[CrawlResult] = self.judge_fetch(result, label)
        if judged is not None:
            return judged

        try:
            payload: Dict[str, Any] = result.response.json()
        except ValueError as error:
            # `response.json()` 解析失敗拋 `requests.exceptions.JSONDecodeError`，
            # 它同時是 `ValueError` 的子類——收 `ValueError` 接得住，又不會吞掉連線類錯誤
            logger.warning(f"{label}: JSON 解析失敗（{type(error).__name__}: {error}）")
            return CrawlResult.failed(f"json_error: {type(error).__name__}")

        expected: str = TimeUtils.format_date(date, sep="")
        actual: str = str(payload.get("date", ""))
        if actual != expected:
            logger.warning(
                f"{label}: 回應日期 {actual or '（無）'} 與查詢日 {expected} 不符，"
                f"不入庫（站方可能忽略了日期參數、回傳別天的名單）"
            )
            return CrawlResult.failed("date_mismatch")

        table: Optional[Dict[str, Any]] = locate(payload)
        if table is None:
            logger.warning(
                f"{label}: 回應中找不到名單表格（stat={payload.get('stat')}）"
            )
            return CrawlResult.failed("no_table_in_payload")

        fields: List[str] = table.get("fields") or []
        rows: List[List[Any]] = table.get("data") or []

        if not fields:
            logger.warning(f"{label}: 回應中沒有欄位定義")
            return CrawlResult.failed("no_fields_in_payload")

        if not rows:
            logger.warning(f"{label}: 交易日的名單為 0 列，記為失敗待重試")
            return CrawlResult.failed("empty_list")

        return CrawlResult.ok(pd.DataFrame(rows, columns=fields))
