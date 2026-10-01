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

**日期相符的 0 列算查無資料**：櫃買中心在休市日回 `ok`、查詢日、0 列（2026-10-02 實查），
記成失敗的話每個平日休市日都會在每次更新時被重抓。這樣判是安全的：
- 查無資料的那天**不會入庫**，不會出現「名單是空的、於是全部不得放空」的情況。
- 只有一個市場回 0 列時，那天仍是部分取得，整天記為失敗、下次重試（`DailyTwoMarketUpdater.record_market_day()`）。
- 名單起點之前（TPEX 同樣回 `ok`、0 列）由 updater 把起日夾到起點，不會去請求。
- 當天與未來的查無資料不寫進永久名單，盤後尚未公布不會被永久跳過（`DateProgressStore.record_no_data()`）。
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
                站方明確回覆查無資料、或查詢日的 0 列為 `NO_DATA`；連線、解析或日期不符為 `FAILED`
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

        # 休市日的表格形狀與交易日不同（證交所當沖名單：第一張是 0 列的名單、第二張是 `{}`），
        # 照交易日的位置挑表會挑到空物件；日期相符且每張表都沒有資料，就是當天沒有名單
        tables: List[Any] = payload.get("tables") or []
        if tables and all(
            isinstance(each, dict) and not each.get("data") for each in tables
        ):
            logger.info(f"{label}: 查詢日的每一張表都是 0 列（休市或尚未公布）")
            return CrawlResult.no_data("查詢日的每一張表都是 0 列")

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

        if not rows or self.is_date_placeholder(rows, date):
            logger.info(f"{label}: 查詢日的名單為 0 列（休市或尚未公布）")
            return CrawlResult.no_data("查詢日的名單為 0 列")

        # 欄數對不上就交給 `pd.DataFrame` 的話會拋 `ValueError`，一路炸穿整批更新；
        # 這是版面異常，記為失敗、下次重試
        if any(len(row) != len(fields) for row in rows):
            logger.warning(f"{label}: 資料列欄數與欄位定義（{len(fields)} 欄）不符")
            return CrawlResult.failed("row_width_mismatch")

        return CrawlResult.ok(pd.DataFrame(rows, columns=fields))

    @staticmethod
    def is_date_placeholder(rows: List[List[Any]], date: datetime.date) -> bool:
        """
        - Description:
            是否為櫃買中心「當天沒有名單」的佔位列：只有一列、唯一的值是民國年的查詢日

            櫃買中心平盤下名單在休市日回 `[['1150925']]`（2026-10-02 實查），
            名單起點之前也是同一個形狀。判準刻意寫窄（值必須等於查詢日），
            其他欄數不符的情況仍算版面異常。
        - Parameters:
            - rows: List[List[Any]]
                回應的 `data`
            - date: datetime.date
                查詢日
        - Return:
            - bool
                是佔位列為 True
        """

        roc_date: str = f"{date.year - 1911}{date:%m%d}"
        return len(rows) == 1 and [str(value).strip() for value in rows[0]] == [
            roc_date
        ]
