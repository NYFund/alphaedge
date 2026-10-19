import time
from typing import List

import pytest
import requests

from core.pipeline.shared.request_utils import RequestUtils

"""
共用 HTTP 工具的重試行為

**本檔存在的唯一理由是一個會打死長時間爬蟲的坑**：
`requests.exceptions.ConnectionError` **不是**內建 `ConnectionError` 的子類——
兩者是 `OSError` 底下的兄弟。`except ConnectionError` 若抓的是內建那個，
requests 拋的連線中斷就完全沒被攔到，行程當場結束。

2026-09-01 實測：台期貨歷史回補跑到第 9 年（2024-10-29）時因 `RemoteDisconnected`
整個中止，前面 23,599 列雖已入庫，但中斷點要人工找。
"""


def test_requests_connection_error_is_not_a_builtin_connection_error() -> None:
    """
    釘住這個反直覺的事實本身

    日後有人「簡化」成只寫 `except ConnectionError` 時，本測試會說明為什麼不行。
    """

    assert not issubclass(requests.exceptions.ConnectionError, ConnectionError)


def test_retryable_exceptions_cover_requests_connection_error() -> None:
    """重試清單必須涵蓋 requests 版本的連線錯誤"""

    assert issubclass(
        requests.exceptions.ConnectionError, RequestUtils.RETRYABLE_EXCEPTIONS
    )


def test_retryable_exceptions_cover_the_builtin_too() -> None:
    """底層 socket 也可能直接拋內建版本，兩個都要攔"""

    assert issubclass(ConnectionError, RequestUtils.RETRYABLE_EXCEPTIONS)


@pytest.mark.parametrize(
    "exception",
    [
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.ReadTimeout("boom"),
        requests.exceptions.ChunkedEncodingError("boom"),
    ],
)
def test_get_retries_then_returns_none(
    monkeypatch: pytest.MonkeyPatch, exception: Exception
) -> None:
    """
    重試耗盡後回傳 None，**不可讓例外往外炸**

    上層一律以「回傳 None」表示這次取不到，由 updater 決定要跳過還是中止。
    """

    class FailingSession:
        """每次請求都拋出指定例外的 session 替身"""

        def get(self, *args, **kwargs) -> None:
            raise exception

        def post(self, *args, **kwargs) -> None:
            raise exception

    monkeypatch.setattr(RequestUtils, "ses", FailingSession())
    monkeypatch.setattr(
        RequestUtils, "find_best_session", classmethod(lambda cls, url: None)
    )
    monkeypatch.setattr(RequestUtils, "HTTP_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    assert RequestUtils.requests_get("https://example.com") is None
    assert RequestUtils.requests_post("https://example.com") is None


# === fetch()：四種結果各自可辨識 ===
class _FakeResponse:
    """最小 Response 替身，只帶 fetch() 會看的三個屬性"""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code: int = status_code
        self.text: str = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def _install_session(monkeypatch: pytest.MonkeyPatch, send) -> None:
    """把共用 session 換成回傳指定結果的替身，並關掉所有等待"""

    class FakeSession:
        """每次請求都回傳 `send()` 結果的 session 替身"""

        def get(self, *args, **kwargs):
            return send()

        def post(self, *args, **kwargs):
            return send()

    monkeypatch.setattr(RequestUtils, "ses", FakeSession())
    monkeypatch.setattr(RequestUtils, "HTTP_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(RequestUtils, "SESSION_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def test_fetch_ok_carries_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 200：狀態為 OK 且拿得到內容"""

    from core.pipeline.shared.request_utils import FetchStatus

    _install_session(monkeypatch, lambda: _FakeResponse(200, "<table></table>"))

    result = RequestUtils.fetch("https://example.com")

    assert result.status is FetchStatus.OK
    assert result.ok
    assert result.text == "<table></table>"


def test_fetch_client_error_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    4xx 必須與「沒資料」分開

    站方回 404 代表請求本身有問題（網址改制、參數錯誤），不是休市。
    """

    from core.pipeline.shared.request_utils import FetchStatus

    _install_session(monkeypatch, lambda: _FakeResponse(404, "not found"))

    result = RequestUtils.fetch("https://example.com")

    assert result.status is FetchStatus.HTTP_ERROR
    assert not result.ok
    assert result.status_code == 404


def test_fetch_server_error_retries_then_reports_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx 值得重試，耗盡後歸類為 unreachable（而不是「沒資料」）"""

    from core.pipeline.shared.request_utils import FetchStatus

    calls: List[int] = []

    def send() -> _FakeResponse:
        calls.append(1)
        return _FakeResponse(503, "service unavailable")

    _install_session(monkeypatch, send)

    result = RequestUtils.fetch("https://example.com")

    assert result.status is FetchStatus.UNREACHABLE
    assert len(calls) == RequestUtils.HTTP_MAX_RETRIES


def test_fetch_timeout_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """逾時重試耗盡後為 unreachable，且帶出最後一次的錯誤訊息"""

    from core.pipeline.shared.request_utils import FetchStatus

    def send() -> None:
        raise requests.exceptions.ReadTimeout("boom")

    _install_session(monkeypatch, send)
    monkeypatch.setattr(
        RequestUtils, "find_best_session", classmethod(lambda cls, url: None)
    )

    result = RequestUtils.fetch("https://example.com")

    assert result.status is FetchStatus.UNREACHABLE
    assert "ReadTimeout" in (result.error or "")


def test_fetch_blocked_when_session_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    建不起 Session＝IP 被擋，必須與逾時分開

    被擋要人換 IP，逾時再跑一次就好；混在一起會讓整段回補安靜地跳過每一天。
    """

    from core.pipeline.shared.request_utils import FetchStatus

    monkeypatch.setattr(RequestUtils, "ses", None)
    monkeypatch.setattr(RequestUtils, "SESSION_INIT_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(RequestUtils, "SESSION_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: (_ for _ in ()).throw(requests.exceptions.ConnectionError("nope")),
    )

    result = RequestUtils.fetch("https://example.com")

    assert result.status is FetchStatus.BLOCKED
    assert not result.ok


def test_find_best_session_raises_instead_of_returning_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 次都失敗要拋 `IPBlockedError`，不可回 None 讓呼叫端撞 AttributeError"""

    from core.pipeline.utils import IPBlockedError

    monkeypatch.setattr(RequestUtils, "SESSION_INIT_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(RequestUtils, "SESSION_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: (_ for _ in ()).throw(requests.exceptions.ConnectionError("nope")),
    )

    with pytest.raises(IPBlockedError):
        RequestUtils.find_best_session("https://example.com")
