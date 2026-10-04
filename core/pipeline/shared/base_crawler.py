import datetime
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.pipeline.shared.request_utils import FetchResult, FetchStatus, RequestUtils

"""
所有「取某一天資料」的 crawler 的共同基底，以及**三種結果的分流**

`crawl_*()` **不可退回「DataFrame 或 `None`」兩種回傳值**：單一個 `None` 同時代表
「休市」「站方還沒更新」「連線失敗」「IP 被擋」，updater 對這四種一律
記一行 `is a Holiday!` 就跳過，於是**資料缺一天不會有任何錯誤**，
回測把那天當休市靜默跳過。

`CrawlResult` 把結果收斂成三種，判準寫在 `BaseDataCrawler` 的兩個共用函式：

| 結果 | 判準 | updater 該怎麼做 |
|------|------|------------------|
| `OK` | 拿到非空表格 | 清洗、入庫 |
| `NO_DATA` | **HTTP 200 ＋ 站方明確說沒有** | 記下這天沒資料，不再重問 |
| `FAILED` | 其餘一切（連線失敗、4xx／5xx、被擋、版面解析不出來） | 計入 `unreachable`，下次重試 |

**「解析不出表格」歸在 FAILED 而不是 NO_DATA** 是刻意的：站方真的沒資料時會回
明確訊息，解析不出來代表版面改了或拿到錯誤頁，那是需要人看的狀況。
寧可多幾次重試，也不要讓改版靜靜變成「這一年都休市」。

**例外是兩站的 HTML 休市頁**（2026-10-02 實查）：證交所回一頁沒有表格、沒有任何文字的空白報表，
櫃買中心融資融券也沒有表格——HTML 本身無從判斷是休市還是改版，以前一律記為失敗，
於是每個平日休市日都在每次更新時被重抓。解法是**同一個端點改問 JSON 版**：
證交所會明說查無資料，櫃買中心會回查詢日的 0 列；JSON 也拿到資料的話仍是 FAILED。
判準來自站方的明確回應，不是猜頁面長相（見 `confirm_no_data_by_json()`）。
"""


class CrawlStatus(str, Enum):
    """單次爬取的結果分類"""

    OK = "ok"  # 拿到資料
    NO_DATA = "no_data"  # 站方明確回覆沒有資料（休市，或盤後尚未公布）
    FAILED = "failed"  # 取不到，且**無法斷定**站方到底有沒有資料


@dataclass
class CrawlResult:
    """
    - Description:
        單次爬取的結果

        **不要用 `if result:` 判斷成功**，dataclass 一律為真值；請用 `result.is_ok`。
    """

    status: CrawlStatus
    data: Optional[pd.DataFrame] = None
    reason: str = ""
    # 少數來源（月營收）一次「爬取」其實是好幾個請求、回好幾張表，
    # 且各表欄位不同無法先合併，故另留一個欄位；單表來源不會用到它。
    tables: List[pd.DataFrame] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        """是否拿到資料"""

        return self.status is CrawlStatus.OK

    @property
    def is_no_data(self) -> bool:
        """站方是否明確回覆沒有資料"""

        return self.status is CrawlStatus.NO_DATA

    @property
    def is_failed(self) -> bool:
        """是否為無法斷定的失敗"""

        return self.status is CrawlStatus.FAILED

    @classmethod
    def ok(cls, data: pd.DataFrame) -> "CrawlResult":
        """建立成功結果"""

        return cls(status=CrawlStatus.OK, data=data)

    @classmethod
    def ok_tables(cls, tables: List[pd.DataFrame]) -> "CrawlResult":
        """建立成功結果（多張表，見 `tables` 欄位說明）"""

        return cls(status=CrawlStatus.OK, tables=tables)

    @classmethod
    def no_data(cls, reason: str) -> "CrawlResult":
        """建立「站方明確沒有資料」的結果"""

        return cls(status=CrawlStatus.NO_DATA, reason=reason)

    @classmethod
    def failed(cls, reason: str) -> "CrawlResult":
        """建立失敗結果"""

        return cls(status=CrawlStatus.FAILED, reason=reason)


class BaseDataCrawler(ABC):
    """Base Class of Data Crawler"""

    # 站方「查無資料」的明確訊息。**只有這些字串出現才算休市**——
    # TWSE 與 TPEX 兩邊的措辭都收在這裡，五支 crawler 共用同一份判準。
    NO_DATA_MARKERS: Tuple[str, ...] = (
        "很抱歉",
        "沒有符合條件的資料",
        "查無資料",
        "查無所需資料",
        "無符合條件資料",
        "尚無資料",
        "無資料",
        "無交易資訊",
    )

    # 回應內容超過這個長度就不再當成「查無資料」訊息：
    # 正常的資料頁動輒數十 KB，訊息頁只有幾百 bytes。
    # TAIFEX／TPEX 被擋時回的是一整頁 HTML，長度上就與訊息頁不同。
    NO_DATA_TEXT_MAX_LENGTH: int = 4096

    # `pd.read_html()` 解析失敗的兩種形態，**判準只留這一份**：
    # 頁面上沒有表格拋 `ValueError`；body 全空或只有空白則是 lxml 的
    # `XMLSyntaxError`，它繼承 `SyntaxError` 而**不繼承 `ValueError`**。
    # 分開各寫一份必然漂移——漏掉後者時，「HTTP 200 但沒有內容」會炸穿整批，
    # 而那是站方異常、屬於該記為失敗後重試的那一類。
    # 缺解析後端（`ImportError`）刻意不收：重試永遠不會讓套件自己長回來
    HTML_PARSE_ERRORS: tuple = (ValueError, SyntaxError)

    def __init__(self) -> None:
        """建立 crawler；連線與參數一律由子類的 `setup()` 負責"""
        pass

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Crawler"""
        pass

    @abstractmethod
    # 回傳型別由各 crawler 自行決定（`CrawlResult`、`DataFrame`、`None`……），
    # 故此處標 `Any`：來源的形狀差異太大，硬統一只會讓每個子類都得回傳包裝物件
    def crawl(self, *args, **kwargs) -> Any:
        """Crawl Data"""
        pass

    @classmethod
    def looks_like_no_data(cls, text: str) -> bool:
        """
        - Description:
            回應內容是否為站方明確的「查無資料」訊息

            **長度也是判準之一**：被擋時站方回的是一整頁 HTML，裡頭夾帶
            「很抱歉」之類的字並不稀奇；只有短訊息頁才算數。
        - Parameters:
            - text: str
                回應內容
        - Return:
            - bool
                是站方的查無資料訊息為 True
        """

        if not text or len(text) > cls.NO_DATA_TEXT_MAX_LENGTH:
            return False

        return any(marker in text for marker in cls.NO_DATA_MARKERS)

    @classmethod
    def judge_fetch(cls, result: FetchResult, label: str) -> Optional[CrawlResult]:
        """
        - Description:
            把 HTTP 層的結果翻成 `CrawlResult`；**仍需解析表格時回傳 `None`**

            五支 crawler 的第一段判斷完全相同，收在這裡以免各寫一份而漂移。
        - Parameters:
            - result: FetchResult
                `RequestUtils.fetch()` 的回傳值
            - label: str
                來源與日期的描述，只用於訊息（例如 `"TWSE price 2024-01-02"`）
        - Return:
            - Optional[CrawlResult]
                已可定案時回傳結果；需要呼叫端自行解析表格時回傳 `None`
        """

        if result.status is FetchStatus.BLOCKED:
            logger.error(f"{label}: IP 疑似被封鎖，{result.error}")
            return CrawlResult.failed(f"blocked: {result.error}")

        if result.status is FetchStatus.UNREACHABLE:
            logger.warning(f"{label}: 連線失敗，{result.error}")
            return CrawlResult.failed(f"unreachable: {result.error}")

        if result.status is FetchStatus.HTTP_ERROR:
            logger.warning(f"{label}: {result.error}")
            return CrawlResult.failed(f"http_error: {result.error}")

        if cls.looks_like_no_data(result.text):
            logger.info(f"{label}: 站方回覆查無資料（休市或尚未公布）")
            return CrawlResult.no_data("站方回覆查無資料")

        return None

    @staticmethod
    def is_placeholder_table(df: pd.DataFrame) -> bool:
        """
        - Description:
            表格是否只有「一列文字橫跨整列」的佔位列

            櫃買中心休市日的收盤行情與三大法人回一張只有表頭、「共0筆」與註解列的表；
            `pd.read_html()` 會把跨欄的那一列展開成「每一格都是同一句話」。
            真正的資料列一定至少有代號與名稱兩種不同的值，故判準是「每一列都只有一種值」。
            **只用在兩欄以上的表**：單欄的表每一列本來就只有一種值，套用會把真資料當成佔位。
        - Parameters:
            - df: pd.DataFrame
                解析出的表格
        - Return:
            - bool
                全部都是佔位列（或沒有任何列）為 True
        """

        if len(df.columns) < 2:
            return False

        return all(row.astype(str).nunique() <= 1 for _, row in df.iterrows())

    @staticmethod
    def json_variant(url: str) -> str:
        """同一端點的 JSON 版網址：兩站的 HTML 與 JSON 只差 `response` 參數"""

        if "response=html" not in url:
            raise ValueError(f"網址沒有 response=html，無法換成 JSON 版：{url}")
        return url.replace("response=html", "response=json")

    @classmethod
    def confirm_no_data_by_json(cls, url: str, date: datetime.date, label: str) -> bool:
        """
        - Description:
            HTML 解析不出表格時，改問同一端點的 JSON 版確認是不是「當天沒有資料」

            兩站的 JSON 版各有明確的「沒有資料」寫法（2026-10-02 實查）：
            - 證交所：`stat` 為「很抱歉，沒有符合條件的資料!」這類查無資料訊息
            - 櫃買中心：`date` 等於查詢日、每一張表都是 0 列

            **其餘一律回 False**（連線失敗、JSON 有資料、日期不符）：HTML 拿不到表格、
            JSON 卻有資料，代表 HTML 版面改了，那仍是要人看的失敗。
        - Parameters:
            - url: str
                JSON 版的網址
            - date: datetime.date
                查詢日
            - label: str
                來源與日期的描述，只用於訊息
        - Return:
            - bool
                站方明確表示當天沒有資料為 True
        """

        # **每一條「確認不了」的路徑都要留一行**：它們都讓這天維持失敗、下次重試，
        # 安靜回 False 的話，log 只看得到 HTML 解析失敗，分不出是確認請求本身失敗
        # （偶發，下次就好）還是 JSON 真的有資料（版面改了，要人看）
        result: FetchResult = RequestUtils.fetch(url)
        if not result.ok:
            logger.warning(
                f"{label}: JSON 版確認請求失敗（{result.status.value}：{result.error}），"
                f"維持記為失敗、下次重試"
            )
            return False

        try:
            payload: Any = result.response.json()
        except ValueError as error:
            logger.warning(
                f"{label}: JSON 版回應無法解析（{type(error).__name__}），維持記為失敗"
            )
            return False
        # 確認流程的任何意外都只能讓結果維持失敗，不可以拋出去中斷整批更新
        if not isinstance(payload, dict):
            logger.warning(f"{label}: JSON 版回應不是物件，維持記為失敗")
            return False

        if cls.looks_like_no_data(str(payload.get("stat", ""))):
            logger.info(
                f"{label}: JSON 版回覆查無資料（{payload.get('stat')}），判為休市"
            )
            return True

        tables: List[Dict[str, Any]] = payload.get("tables") or []
        expected: str = date.strftime("%Y%m%d")
        if (
            str(payload.get("date", "")) == expected
            and tables
            and all(
                isinstance(table, dict) and not table.get("data") for table in tables
            )
        ):
            logger.info(f"{label}: JSON 版為查詢日的 0 列，判為休市")
            return True

        logger.warning(
            f"{label}: JSON 版不是查無資料（stat={payload.get('stat')}、date={payload.get('date')}），"
            f"HTML 卻解析不出表格——可能是版面改了，維持記為失敗"
        )
        return False

    @classmethod
    def parse_html_table(
        cls,
        result: FetchResult,
        label: str,
        index: int = 0,
        no_data_probe: Optional[Tuple[str, datetime.date]] = None,
        page_date: Optional[datetime.date] = None,
        **read_html_kwargs,
    ) -> CrawlResult:
        """
        - Description:
            `fetch → 判斷 → 解析表格` 的共同流程

            **解析失敗算 FAILED 不算休市**：站方真的沒資料時會回明確訊息
            （已由 `judge_fetch()` 攔下），解析不出來代表版面改了或拿到錯誤頁。
        - Parameters:
            - result: FetchResult
                `RequestUtils.fetch()` 的回傳值
            - label: str
                來源與日期的描述，只用於訊息
            - index: int
                要取第幾張表（`pd.read_html()` 的結果索引，可為負）
            - no_data_probe: Optional[Tuple[str, datetime.date]]
                `(JSON 版網址, 查詢日)`；解析不出表格時用它確認是否休市
                （見 `confirm_no_data_by_json()`），None 時維持記為失敗
            - page_date: Optional[datetime.date]
                查詢日；提供時核對頁面上標示的資料日期（見 `check_page_date()`），
                None 時不核對
            - read_html_kwargs
                原樣轉給 `pd.read_html()`（例如 `converters`）
        - Return:
            - CrawlResult
        """

        judged: Optional[CrawlResult] = cls.judge_fetch(result, label)
        if judged is not None:
            return judged

        try:
            df: pd.DataFrame = pd.read_html(StringIO(result.text), **read_html_kwargs)[
                index
            ]
        except (*cls.HTML_PARSE_ERRORS, IndexError) as error:
            # 解析失敗（見 `HTML_PARSE_ERRORS`）之外多收一個 `IndexError`：
            # 表格數量少於預期時取索引會拋它，同樣是「版面變了」。
            # 其餘例外（例如參數給錯）代表呼叫端寫錯，要讓它現形
            if no_data_probe is not None and cls.confirm_no_data_by_json(
                no_data_probe[0], no_data_probe[1], label
            ):
                return CrawlResult.no_data("JSON 版確認當天沒有資料")
            logger.warning(
                f"{label}: 版面解析失敗（{type(error).__name__}: {error}）；"
                f"這**不是**休市，站方沒資料時會回明確訊息"
            )
            return CrawlResult.failed(f"parse_error: {type(error).__name__}")

        if df.empty or cls.is_placeholder_table(df):
            logger.info(f"{label}: 表格為空（休市或尚未公布）")
            return CrawlResult.no_data("表格為空")

        if page_date is not None:
            mismatch: Optional[CrawlResult] = cls.check_page_date(
                result.text, page_date, label
            )
            if mismatch is not None:
                return mismatch

        return CrawlResult.ok(df)

    # 頁面上的資料日期（民國年）：櫃買中心收盤行情與融資融券寫成「資料日期:115/09/24」，
    # 三大法人寫成「115年09月24日」（2026-10-04 實查 2013～2026 各年代皆如此）
    PAGE_DATE_PATTERNS: Tuple[str, ...] = (
        r"資料日期[:：]?\s*(\d{2,3})/(\d{2})/(\d{2})",
        r"(\d{2,3})年(\d{2})月(\d{2})日",
    )

    @classmethod
    def check_page_date(
        cls, html: str, date: datetime.date, label: str
    ) -> Optional[CrawlResult]:
        """
        - Description:
            核對頁面標示的資料日期與查詢日；不符或找不到時回 `FAILED`

            **櫃買中心收到不合格式的日期時不報錯，而是靜靜回近幾日的資料**
            （2026-10-01 盤點交易所來源時實測），表格照樣解析得出來——不核對的話，
            哪天日期格式寫錯就會把別天的行情存成當天，而且 `INSERT OR IGNORE`
            之後重跑也蓋不掉。

            **找不到日期也算失敗**：各端點、各年代實查都有標示，找不到代表版面改了，
            與「解析不出表格」同一個原則——寧可那天重試，也不要讓改版靜靜放行。
        - Parameters:
            - html: str
                回應內容
            - date: datetime.date
                查詢日
            - label: str
                來源與日期的描述，只用於訊息
        - Return:
            - Optional[CrawlResult]
                相符時為 None；不符或找不到時為 `FAILED`
        """

        text: str = re.sub(r"<[^>]+>", " ", html)
        for pattern in cls.PAGE_DATE_PATTERNS:
            matched: Optional[re.Match] = re.search(pattern, text)
            if matched is None:
                continue

            year, month, day = (int(part) for part in matched.groups())
            try:
                shown: datetime.date = datetime.date(year + 1911, month, day)
            except ValueError:
                break
            if shown == date:
                return None

            logger.warning(
                f"{label}: 頁面資料日期 {shown} 與查詢日 {date} 不符，不入庫"
                f"（站方可能忽略了日期參數、回傳別天的資料）"
            )
            return CrawlResult.failed("page_date_mismatch")

        logger.warning(f"{label}: 頁面上找不到資料日期，版面可能改了，記為失敗")
        return CrawlResult.failed("page_date_not_found")
