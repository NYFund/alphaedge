import time
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
from loguru import logger

from core.config import (
    FINANCIAL_STATEMENT_DOWNLOADS_PATH,
)
from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.shared.request_utils import RequestUtils
from core.pipeline.tw.utils.mops_payload import Payload
from core.pipeline.tw.utils.url_manager import URLManager
from core.pipeline.utils import ListingBoard
from core.utils import TimeUtils

"""
MOPS 季報財務報表爬蟲

1. 資產負債表、綜合損益表、現金流量表是「全市場一次查完」，逐市場（上市／上櫃）各一次請求。
2. 權益變動表只能逐檔查詢，故請求數 = 股票檔數 × 年季，且需要區分
   「查無資料」與「站方過載」兩種空結果，見 `crawl_equity_changes()`。
3. 站方過載時會回 HTTP 200 但內容為錯誤訊息，判斷一律看內文標記而非狀態碼。
"""


class FinancialStatementCrawler(BaseDataCrawler):
    """爬取 MOPS 季報的四張財務報表（資產負債表、綜合損益表、現金流量表、權益變動表）"""

    # 起始年份為資料源下界（MOPS 只供得出民國 102 年以後），故寫死；
    # 結束年份不設常數，改由呼叫端取當年——MOPS 一路供到當季，寫死會逐年落後
    DEFAULT_START_YEAR: int = 2013
    CRAWL_DELAY_MIN: float = 1.0
    CRAWL_DELAY_MAX: float = 3.0

    # 權益變動表專用（其餘三張報表不適用，故不放共用常數）
    # MOPS 的 ajax_t164sb06 要 step=2 才會直接回報表：step=1 對金控這類多實體公司
    # 只回子公司選單頁（實測 2891 中信金），step=2 則各類公司一律直接得到報表
    EQUITY_CHANGE_STEP: str = "2"
    # 站方過載時會回 HTTP 200，內容卻只有 "Unreachable Server"。這與「查無資料」
    # 必須分開處理，否則逐檔回補會把暫時性失敗記成「這檔沒有權益變動表」而永久略過
    EQUITY_CHANGE_UNREACHABLE_MARKER: str = "Unreachable Server"
    EQUITY_CHANGE_NO_DATA_MARKER: str = "查無資料"
    # 站方對民國 103 年（含）以前、尚未採 IFRSs 的年季只回導流訊息「…請至採IFRSs前之
    # 個別報表 或 合併報表 查詢！」，頁面裡沒有任何表格。這是永久狀態，重跑不會變。
    # 用片段比對而非整句，站方文案微調時才不會失效；**只在解不出表格時才看它**——
    # 正常報表頁的導覽或註腳若出現這幾個字，整頁比對會把有資料的公司誤記成查無資料
    EQUITY_CHANGE_PRE_IFRS_MARKER: str = "採IFRSs前"
    EQUITY_CHANGE_MAX_RETRIES: int = 3
    EQUITY_CHANGE_RETRY_DELAY_SECONDS: int = 30

    def __init__(self) -> None:
        super().__init__()

        self.fs_dir: Path = FINANCIAL_STATEMENT_DOWNLOADS_PATH

        self.payload: Optional[Payload] = None
        self.listing_boards: List[ListingBoard] = [ListingBoard.SII, ListingBoard.OTC]

        self.setup()

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Crawler"""

        self.fs_dir.mkdir(parents=True, exist_ok=True)

        self.payload = Payload(
            firstin="1",
            step="1",
            TYPEK="sii",
            co_id=None,
            year="102",
            season="1",
        )

    def crawl(self, *args, **kwargs) -> Dict[str, List[pd.DataFrame]]:
        """一次取得同一年季的四張報表"""

        """
        kwargs：
        - year: int（必填，西元年）
        - season: int（必填，季別 1~4）
        - stock_id: str（權益變動表逐檔查詢用）
        """

        stock_id: Optional[str] = kwargs.get("stock_id")
        year: Optional[int] = kwargs.get("year")
        season: Optional[int] = kwargs.get("season")

        if year is None or season is None:
            raise ValueError("Missing required parameters: 'date', or 'season'")

        df_dict: Dict[str, List[pd.DataFrame]] = {
            "balance_sheet": [],
            "comprehensive_income": [],
            "cash_flow": [],
            "equity_changes": [],
        }

        # 三張全市場報表在任一市場失敗時會回 None，不能直接 extend
        df_dict["balance_sheet"].extend(self.crawl_balance_sheet(year, season) or [])
        df_dict["comprehensive_income"].extend(
            self.crawl_comprehensive_income(year, season) or []
        )
        df_dict["cash_flow"].extend(self.crawl_cash_flow(year, season) or [])
        # 權益變動表查無資料或站方過載時會回 None，不能直接 extend
        equity_changes: Optional[List[pd.DataFrame]] = self.crawl_equity_changes(
            year, season, stock_id
        )
        df_dict["equity_changes"].extend(equity_changes or [])

        return df_dict

    def crawl_balance_sheet(
        self,
        year: int,
        season: int,
    ) -> Optional[List[pd.DataFrame]]:
        """爬取資產負債表（全市場，上市與上櫃各一次請求）"""

        """
        申報起始：上市為民國 78（1989）年、上櫃為民國 82（1993）年，
        但本端點只供得出民國 102（2013）年以後的年季。
        """

        logger.info(f"* Start crawling balance sheet: {year}/Q{season}")

        balance_sheet_url: str = URLManager.get_url("BALANCE_SHEET_URL")
        return self._crawl_listing_boards(
            balance_sheet_url, "balance sheet", year, season
        )

    def crawl_comprehensive_income(
        self,
        year: int,
        season: int,
    ) -> Optional[List[pd.DataFrame]]:
        """爬取綜合損益表（全市場，上市與上櫃各一次請求）"""

        """
        申報起始：上市為民國 77（1988）年、上櫃為民國 82（1993）年，
        但本端點只供得出民國 102（2013）年以後的年季。
        """

        logger.info(f"* Start crawling comprehensive income: {year}/Q{season}")

        income_url: str = URLManager.get_url("INCOME_STATEMENT_URL")
        return self._crawl_listing_boards(
            income_url, "comprehensive income", year, season
        )

    def crawl_cash_flow(
        self,
        year: int,
        season: int,
    ) -> Optional[List[pd.DataFrame]]:
        """爬取現金流量表（全市場，上市與上櫃各一次請求）"""

        """
        資料區間：上市與上櫃皆自民國 102（2013）年起至今。
        """

        logger.info(f"* Start crawling cash flow: {year}/Q{season}")

        cash_flow_url: str = URLManager.get_url("CASH_FLOW_STATEMENT_URL")
        return self._crawl_listing_boards(cash_flow_url, "cash flow", year, season)

    def _crawl_listing_boards(
        self,
        url: str,
        label: str,
        year: int,
        season: int,
    ) -> Optional[List[pd.DataFrame]]:
        """
        - Description:
            逐市場（上市、上櫃）查詢同一張全市場報表；**任一市場失敗即回 `None`**

            只回傳問到的那個市場的話，updater 會把半份年季清洗入庫，看起來一切正常
            ——資產負債表 2021Q1 整季缺、綜合損益表 2021Q1 上櫃只剩 3 檔、現金流量表
            2024Q1 上市只剩 1 檔都是這樣來的。三張報表共用本方法，判準只留一份，
            以免各自演化出不同的失敗處理。

            解析不出表格同樣算失敗而不是「沒資料」：兩個市場自 2013 年起都有申報，
            拿不到表格代表拿到了非預期的頁面（尚未公布、站方異常），留待下次重試。
        - Parameters:
            - url: str
                報表端點
            - label: str
                報表名稱，只用於訊息
            - year: int
                西元年
            - season: int
                季別
        - Return:
            - Optional[List[pd.DataFrame]]
                兩個市場的所有表格；任一市場失敗時為 None
        """

        self.payload.year = TimeUtils.convert_ad_to_roc_year(year)
        self.payload.season = season
        df_list: List[pd.DataFrame] = []

        for listing_board in self.listing_boards:
            self.payload.TYPEK = listing_board.value
            market: str = f"{label} {year}Q{season} {listing_board.value}"

            try:
                res: Optional[requests.Response] = RequestUtils.requests_post(
                    url, data=self.payload.convert_to_clean_dict()
                )
            except OSError as error:
                # 只收 I/O 層失敗。`requests` 的例外全繼承 `OSError`，作業系統層的
                # `ConnectionError` 也是，一條就涵蓋所有傳輸失敗而不必逐一列舉。
                # **不放寬成 `except Exception`**：payload 組錯拋的 TypeError 之類
                # 是程式壞了，被當成「整季視為失敗」會每次重跑、每次以警告收場，
                # 資料永遠補不齊而沒有人會發現
                logger.warning(
                    f"Cannot get {market}（{type(error).__name__}: {error}）；"
                    f"整季視為失敗"
                )
                return None

            if res is None:
                logger.warning(f"Cannot get {market}；整季視為失敗")
                return None

            try:
                df_list.extend(pd.read_html(StringIO(res.text)))
            except self.HTML_PARSE_ERRORS as error:
                # 判準與共用的 `parse_html_table()` 同一份（見 `HTML_PARSE_ERRORS`）。
                # 本方法不走那支是因為它只回一張表，而全市場報表要整頁的所有表格
                logger.warning(
                    f"No tables found in {market}（{type(error).__name__}）；"
                    f"整季視為失敗"
                )
                return None

        return df_list

    def crawl_equity_changes(
        self,
        year: int,
        season: int,
        stock_id: str,
    ) -> Optional[List[pd.DataFrame]]:
        """爬取權益變動表（逐檔查詢）"""

        """
        資料區間：上市與上櫃皆自民國 102（2013）年起至今。

        與其他三張報表不同，本端點是「逐檔查詢」（一次一檔股票），
        故回傳值要能分辨三種結果，讓逐檔回補的呼叫端決定要不要重試：
        - None: 暫時性失敗（站方過載或連線失敗），本檔尚未確認有無資料，應留待重跑
        - []:   查無資料（例如 ETF、當季未申報），重跑也不會有結果
        - 非空 list: 正常取得，內容為該頁的所有表格
        """

        logger.debug(f"* Start crawling equity changes: {stock_id} {year}/Q{season}")

        roc_year: str = TimeUtils.convert_ad_to_roc_year(year)

        # step 與 co_id 只在本方法生效，離開前一律還原：payload 由四張報表共用，
        # 其餘三張是「全市場一次查完」，被殘留的 co_id 縮成單一公司會靜默少資料
        original_step: Optional[str] = self.payload.step
        self.payload.step = self.EQUITY_CHANGE_STEP
        self.payload.TYPEK = None
        self.payload.co_id = stock_id
        self.payload.year = roc_year
        self.payload.season = season

        equity_changes_url: str = URLManager.get_url("EQUITY_CHANGE_STATEMENT_URL")

        try:
            return self._request_equity_changes(
                url=equity_changes_url,
                payload=self.payload.convert_to_clean_dict(),
                year=year,
                season=season,
                stock_id=stock_id,
            )
        finally:
            self.payload.step = original_step
            self.payload.co_id = None

    def _request_equity_changes(
        self,
        url: str,
        payload: Dict[str, str],
        year: int,
        season: int,
        stock_id: str,
    ) -> Optional[List[pd.DataFrame]]:
        """送出權益變動表請求，暫時性失敗就地重試；回傳語意見 crawl_equity_changes"""

        for attempt in range(self.EQUITY_CHANGE_MAX_RETRIES):
            res: Optional[requests.Response] = None
            try:
                res = RequestUtils.requests_post(url, data=payload)
            except OSError as error:
                # 只收 I/O 層失敗（`requests` 的例外與作業系統層的連線錯誤都繼承
                # `OSError`），其餘一律往上拋給逐檔隔離與斷路器判斷：
                # 在這裡吞掉的話，「環境或程式壞了」會退化成每一檔都重試三次，
                # 逐檔回補的三檔連續例外斷路器就永遠不會觸發
                logger.warning(
                    f"Request failed on equity changes {stock_id} {year}Q{season}: {error}"
                )

            if res is not None:
                if self.EQUITY_CHANGE_NO_DATA_MARKER in res.text:
                    logger.debug(f"No equity changes data: {stock_id} {year}Q{season}")
                    return []

                if self.EQUITY_CHANGE_UNREACHABLE_MARKER not in res.text:
                    try:
                        return pd.read_html(StringIO(res.text))
                    except self.HTML_PARSE_ERRORS:
                        # 同一份判準（見 `HTML_PARSE_ERRORS`）。這裡漏接的代價特別大：
                        # 逃出去會被算成一次非預期例外，連三檔就把整段回補斷路掉
                        # 導流到「採 IFRSs 前」端點：回 [] 讓它寫進查無資料的永久名單，
                        # 回 None（待重試）的話每輪整段回補都會重打，而結果永遠一樣
                        if self.EQUITY_CHANGE_PRE_IFRS_MARKER in res.text:
                            logger.debug(
                                f"Pre-IFRS equity changes not served here: "
                                f"{stock_id} {year}Q{season}"
                            )
                            return []

                        # 既非「查無資料」也非過載，卻解不出表格：版面可能已改制。
                        # **回 None（待重試）而不是 []（確定沒有資料）**——站方真的
                        # 沒資料時會回明確訊息，上面已經攔下了；解析不出來代表拿到
                        # 非預期的頁面，那是要人看的狀況。回 [] 會讓這檔被寫進
                        # 「查無資料」永久名單，從此不再被嘗試（判準見
                        # `BaseDataCrawler` 的三態說明）
                        logger.warning(
                            f"No tables found on equity changes {stock_id} "
                            f"{year}Q{season}；計為待重試，不當成查無資料"
                        )
                        return None

            # 走到這裡代表站方過載或連線失敗，等一下再試同一檔
            if attempt < self.EQUITY_CHANGE_MAX_RETRIES - 1:
                time.sleep(self.EQUITY_CHANGE_RETRY_DELAY_SECONDS)

        logger.warning(
            f"Equity changes unreachable after {self.EQUITY_CHANGE_MAX_RETRIES} "
            f"retries: {stock_id} {year}Q{season}"
        )
        return None
