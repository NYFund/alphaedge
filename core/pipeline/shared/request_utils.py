import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

import requests
from fake_useragent import UserAgent
from loguru import logger
from requests.exceptions import (
    ChunkedEncodingError,
    ReadTimeout,
)
from requests.exceptions import ConnectionError as RequestsConnectionError

from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.utils.exceptions import IPBlockedError

"""
共用 HTTP 工具：重試、Session 管理，以及**把「取不到」拆成幾種不同的結果**

**不可讓被擋、逾時、404 一律回 `None`**：呼叫端（五支台股 crawler）拿到 `None`
只會記一行「is a Holiday!」並回空表，於是**連線失敗與休市在 updater 眼裡完全相同**，
資料缺一天不會有任何錯誤。

`fetch()` 就是為了拆開這四種結果而存在（見 `FetchStatus`）。
`requests_get()`／`requests_post()` 維持 `Optional[Response]` 介面，
給期貨等尚未改寫的呼叫端使用——它們是 `fetch()` 的薄包裝，不再有第二套重試邏輯。
"""


class FetchStatus(str, Enum):
    """一次 HTTP 請求的結果分類"""

    OK = "ok"  # HTTP 2xx，站方確實回了東西（內容是否為空由呼叫端判斷）
    HTTP_ERROR = "http_error"  # 4xx／5xx：站方有回應，但不是成功
    UNREACHABLE = "unreachable"  # 逾時／連線中斷，重試耗盡
    BLOCKED = "blocked"  # 連 Session 都建不起來，IP 多半已被封鎖


@dataclass
class FetchResult:
    """
    - Description:
        `fetch()` 的回傳值：把狀態、回應本體與錯誤訊息綁在一起

        **不要用 `if result:` 判斷成功**，dataclass 一律為真值；請用 `result.ok`。
    """

    status: FetchStatus
    response: Optional[requests.Response] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """是否為 HTTP 2xx"""

        return self.status is FetchStatus.OK

    @property
    def status_code(self) -> Optional[int]:
        """HTTP 狀態碼；連線根本沒建立時為 None"""

        return self.response.status_code if self.response is not None else None

    @property
    def text(self) -> str:
        """回應內容；沒有回應時回空字串，讓呼叫端不必先判斷 None"""

        return self.response.text if self.response is not None else ""

    @classmethod
    def succeeded(cls, response: requests.Response) -> "FetchResult":
        """建立成功結果"""

        return cls(status=FetchStatus.OK, response=response)

    @classmethod
    def http_error(cls, response: requests.Response) -> "FetchResult":
        """建立 HTTP 錯誤結果（保留回應本體，錯誤頁的內容有時有診斷價值）"""

        return cls(
            status=FetchStatus.HTTP_ERROR,
            response=response,
            error=f"HTTP {response.status_code}",
        )

    @classmethod
    def unreachable(cls, error: str) -> "FetchResult":
        """建立連線失敗結果"""

        return cls(status=FetchStatus.UNREACHABLE, error=error)

    @classmethod
    def blocked(cls, error: str) -> "FetchResult":
        """建立 IP 被封鎖結果"""

        return cls(status=FetchStatus.BLOCKED, error=error)

    def response_if_ok(self) -> Optional[requests.Response]:
        """只在成功時回傳 Response，供舊介面 `requests_get()`／`requests_post()` 使用"""

        return self.response if self.ok else None


class RequestUtils:
    """HTTP 請求工具：Session 建立、重試與結果分類"""

    # Session 建立與 HTTP 請求常數
    SESSION_INIT_MAX_ATTEMPTS: int = 10
    REQUEST_TIMEOUT_SECONDS: int = 10
    SESSION_RETRY_DELAY_SECONDS: int = 10
    HTTP_MAX_RETRIES: int = 3
    HTTP_RETRY_DELAY_SECONDS: int = 60

    # 值得重試的 HTTP 狀態碼：伺服器端暫時性錯誤與流量限制。
    # 其餘 4xx（404、403 等）重試沒有意義，直接回 HTTP_ERROR 讓呼叫端決定。
    RETRYABLE_STATUS_CODES: tuple = (429, 500, 502, 503, 504)

    ses: Optional[requests.Session] = None  # Session

    # 重試要攔的例外。
    #
    # ⚠️ **`requests.exceptions.ConnectionError` 不是內建 `ConnectionError` 的子類**
    # ——兩者是 `OSError` 底下的**兄弟**。只寫 `except ConnectionError` 抓到的是內建
    # 那個，requests 拋的連線中斷完全不會被攔到，直接把行程打死。
    #
    # 2026-09-01 實測：台期貨歷史回補跑到第 9 年（2024-10-29）時因
    # `RemoteDisconnected` 整個中止，前面 23,599 列雖已入庫，但中斷點要人工找。
    # 兩個都列進來，內建那個保留是因為底層 socket 也可能直接拋它。
    RETRYABLE_EXCEPTIONS: tuple = (
        RequestsConnectionError,
        ConnectionError,
        ReadTimeout,
        ChunkedEncodingError,
    )

    @staticmethod
    def generate_random_header() -> Dict[str, str]:
        """產生隨機 headers 避免爬蟲被鎖"""

        ua: UserAgent = UserAgent()
        user_agent: str = ua.random
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "User-Agent": user_agent,
        }
        return headers

    @classmethod
    def find_best_session(cls, url: str) -> requests.Session:
        """
        - Description:
            嘗試建立可用的 requests.Session 連線

            **10 次都失敗時拋出 `IPBlockedError` 而不是回 `None`**：回 `None` 的話
            呼叫端接著就會 `cls.ses.get(...)` 撞 `AttributeError`，或把「IP 被封鎖」
            當成「這天沒資料」。被擋是需要人介入（換 IP）的狀態，必須讓它浮出來。
        - Parameters:
            - url: str
                用來試連的網址
        - Return:
            - requests.Session
                可用的 Session
        - Raise:
            - IPBlockedError
                連續 `SESSION_INIT_MAX_ATTEMPTS` 次都建不起來
        """

        last_error: Optional[str] = None

        for i in range(cls.SESSION_INIT_MAX_ATTEMPTS):
            try:
                logger.info(f"獲取新的Session 第 {i} 回合")
                headers: Dict[str, str] = cls.generate_random_header()
                ses: requests.Session = requests.Session()
                ses.get(url, headers=headers, timeout=cls.REQUEST_TIMEOUT_SECONDS)
                ses.headers.update(headers)
                logger.info("成功！")
                cls.ses = ses

                return ses
            except cls.RETRYABLE_EXCEPTIONS as error:
                last_error = f"{type(error).__name__}: {error}"
                logger.info(error)
                logger.info("失敗,10秒後重試")
                time.sleep(cls.SESSION_RETRY_DELAY_SECONDS)

        logger.error("您的網頁IP已經被證交所封鎖,請更新IP來獲取解鎖")
        logger.error(" 手機:開啟飛航模式,再關閉,即可獲得新的IP")
        logger.error("數據機：關閉然後重新打開數據機的電源")
        raise IPBlockedError(url, cls.SESSION_INIT_MAX_ATTEMPTS, last_error)

    @classmethod
    def fetch(cls, url: str, method: str = "get", **kwargs) -> FetchResult:
        """
        - Description:
            發送 HTTP 請求並把結果分成四類（見 `FetchStatus`）

            **這是給需要分辨「休市」與「失敗」的呼叫端用的入口**。只想拿 Response
            的舊呼叫端請繼續用 `requests_get()`／`requests_post()`。
        - Parameters:
            - url: str
                目標網址
            - method: str
                `"get"` 或 `"post"`
            - kwargs
                原樣轉給 `requests`（`params`、`data`、`headers` 等）
        - Return:
            - FetchResult
                帶狀態的結果物件；不拋出網路層例外
        """

        if cls.ses is None:
            try:
                cls.find_best_session(url)
            except IPBlockedError as error:
                return FetchResult.blocked(str(error))

        last_error: str = "unknown"

        for i in range(cls.HTTP_MAX_RETRIES):
            try:
                send = getattr(cls.ses, method)
                response: requests.Response = send(
                    url, timeout=cls.REQUEST_TIMEOUT_SECONDS, **kwargs
                )
            except cls.RETRYABLE_EXCEPTIONS as error:
                last_error = f"{type(error).__name__}: {error}"
                logger.info(error)
                logger.info(
                    f"retry one more time after 60s {cls.HTTP_MAX_RETRIES - 1 - i} times left"
                )
                time.sleep(cls.HTTP_RETRY_DELAY_SECONDS)
                try:
                    cls.find_best_session(url)
                except IPBlockedError as blocked:
                    return FetchResult.blocked(str(blocked))
                continue

            if response.ok:
                return FetchResult.succeeded(response)

            if response.status_code in cls.RETRYABLE_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    f"{url} 回應 {response.status_code}，"
                    f"{cls.HTTP_RETRY_DELAY_SECONDS} 秒後重試"
                    f"（剩 {cls.HTTP_MAX_RETRIES - 1 - i} 次）"
                )
                time.sleep(cls.HTTP_RETRY_DELAY_SECONDS)
                continue

            # 其餘 4xx：重試沒有意義，直接把狀態碼交給呼叫端判斷
            logger.warning(f"{url} 回應 HTTP {response.status_code}")
            return FetchResult.http_error(response)

        logger.warning(f"{url} 重試耗盡：{last_error}")
        return FetchResult.unreachable(last_error)

    @classmethod
    def requests_get(cls, url: str, *args, **kwargs) -> Optional[requests.Response]:
        """
        - Description:
            使用共用 session 發送 GET 請求，內建重試機制

            **僅在成功（HTTP 2xx）時回傳 Response，其餘一律 None**——與舊行為相容。
            要分辨失敗原因請改用 `fetch()`。
        """

        return cls.fetch(url, method="get", **kwargs).response_if_ok()

    @classmethod
    def requests_post(cls, url: str, *args, **kwargs) -> Optional[requests.Response]:
        """
        - Description:
            使用共用 session 發送 POST 請求，內建重試機制

            回傳語意同 `requests_get()`。
        """

        return cls.fetch(url, method="post", **kwargs).response_if_ok()


@dataclass
class RateLimitPolicy:
    """
    - Description:
        ETL 請求之間的節流：每次請求後隨機短睡，每 N 次改成長睡一次

        **與券商下單配額無關**：`core/broker/rate_limiter.py` 的 rate limiter 是券商
        API 的硬性配額，超過會被拒絕；這裡是爬公開資料時主動放慢速度，避免被站方
        限流或封 IP，只影響等待時間，不影響寫入的資料。

        **計數的兩種語意不可混用**（`reset_after_long_sleep`）：
        - True（預設）：達到門檻長睡一次後計數歸零。逐日、逐年月的回補用這種。
        - False：計數不歸零，以「累計請求數取模」判斷。權益變動表數的是本季累計
          送出幾個請求，由呼叫端傳入 `count`，中途跳過的股票也算在累計裡。

        **一律可中斷**：傳入 `stop` 時睡眠走 `GracefulStop.sleep()`；裸
        `time.sleep()` 被訊號打斷會自動續睡（PEP 475），長睡那一段按下 Ctrl+C
        要等滿才有反應，等於逼人用 `kill -9`，手上未入庫的那批就會消失。
    """

    # 一般請求之間的隨機延遲（秒）；兩者相同時就是固定間隔
    delay_min: float
    delay_max: float
    # 每幾次請求長睡一次；0 表示不長睡
    long_sleep_every: int = 0
    # 長睡秒數
    long_sleep_seconds: float = 0.0
    # 長睡後計數是否歸零（見 class docstring）
    reset_after_long_sleep: bool = True
    # 自上次歸零以來的請求數；`tick()` 未傳 `count` 時由本物件自己累計
    count: int = field(default=0, init=False)

    def plan_next(self, count: Optional[int] = None) -> Tuple[float, bool]:
        """
        - Description:
            決定這一次要睡多久，並推進計數（不睡；`tick()` 與測試共用）
        - Parameters:
            - count: Optional[int]
                呼叫端自行累計的請求數；None 時由本物件累計
        - Return:
            - Tuple[float, bool]
                (秒數, 是否為長睡)
        """

        if count is None:
            self.count += 1
            count = self.count

        if self.long_sleep_every > 0:
            if self.reset_after_long_sleep:
                is_long: bool = count >= self.long_sleep_every
            else:
                is_long = count > 0 and count % self.long_sleep_every == 0

            if is_long:
                if self.reset_after_long_sleep:
                    self.count = 0
                return float(self.long_sleep_seconds), True

        return random.uniform(self.delay_min, self.delay_max), False

    def tick(
        self, stop: Optional[GracefulStop] = None, count: Optional[int] = None
    ) -> bool:
        """
        - Description:
            一次請求之後的節流：決定秒數並睡，長睡時留一行 log
        - Parameters:
            - stop: Optional[GracefulStop]
                中止旗標；None 時退化成不可中斷的 `time.sleep()`
            - count: Optional[int]
                呼叫端自行累計的請求數（不歸零語意用）；None 時由本物件累計
        - Return:
            - bool
                睡完為 True；中途被要求收工而提早返回為 False
        """

        seconds, is_long = self.plan_next(count)
        if is_long:
            # 訊息帶出實際秒數：長睡秒數由各 updater 設定，寫死字面會與行為對不上
            logger.info(f"Sleep {self.long_sleep_seconds:g} seconds...")

        if stop is not None:
            return stop.sleep(seconds)

        # 沒有旗標可問時只能照睡；這條路徑保留給沒有長迴圈、也就沒有中止點的呼叫端
        time.sleep(seconds)
        return True
