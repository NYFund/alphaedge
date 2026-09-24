from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from io import StringIO
from typing import Any, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.pipeline.shared.request_utils import FetchResult, FetchStatus

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

    @classmethod
    def parse_html_table(
        cls,
        result: FetchResult,
        label: str,
        index: int = 0,
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
        except (ValueError, IndexError) as error:
            # `read_html()` 找不到表格時拋 `ValueError`，表格數量少於預期時
            # 取索引拋 `IndexError`——兩者都是「版面變了」。
            # 其餘例外（例如參數給錯）代表呼叫端寫錯，要讓它現形
            logger.warning(
                f"{label}: 版面解析失敗（{type(error).__name__}: {error}）；"
                f"這**不是**休市，站方沒資料時會回明確訊息"
            )
            return CrawlResult.failed(f"parse_error: {type(error).__name__}")

        if df.empty:
            logger.info(f"{label}: 表格為空（休市或尚未公布）")
            return CrawlResult.no_data("表格為空")

        return CrawlResult.ok(df)
