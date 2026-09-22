import datetime
import inspect
from typing import Any, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import pytest
import shioaji as sj

from core.broker.rate_limiter import RateLimiter
from core.broker.tw import shioaji_session as session_module
from core.broker.tw.shioaji_session import ShioajiSession

"""
`ShioajiSession`：登入、憑證、帳號與時鐘檢查，以及斷線重連

全部以假 API 物件驗證，不連網。真正要連上模擬環境才能確認的事項由
`scripts/manual/manual_shioaji_login.py` 手動冒煙——本檔驗的是
「程式在各種回應下的行為」，不是「券商會回什麼」。

擋的事故：
- 預設值錯成正式環境 → 真的下單。
- 登入失敗回傳 None 而呼叫端不檢查 → 一路走到下單才爆（舊版 `ShioajiAccount` 就是）。
- 本機時鐘差一天 → 整天的段落判定與帳務歸屬落在錯的日子上，且不會報錯。
- 無限重連 → 把每日 1,000 次登入額度用完，明天也登不進去。
"""

TAIPEI: ZoneInfo = ZoneInfo("Asia/Taipei")


class FakeContract:
    def __init__(self, update_date: Any = "2026-09-19") -> None:
        self.update_date: Any = update_date


class FakeStockCategory:
    """對應 shioaji 1.7 的 `api.Contracts.Stocks`：以 `get(code)` 查，查不到回 None"""

    def __init__(self, contract: Optional[FakeContract]) -> None:
        self._contract: Optional[FakeContract] = contract

    def get(self, code: str) -> Optional[FakeContract]:
        return self._contract


class FakeContracts:
    def __init__(self, contract: Optional[FakeContract]) -> None:
        self.Stocks: FakeStockCategory = FakeStockCategory(contract)


class FakeShioaji:
    """假 API 物件：記錄呼叫參數，並可腳本化各種失敗"""

    def __init__(
        self,
        simulation: bool = False,
        ca_result: bool = True,
        has_stock_account: bool = True,
        has_futopt_account: bool = True,
        contract: Optional[FakeContract] = None,
        login_error: Optional[Exception] = None,
    ) -> None:
        self.simulation: bool = simulation
        self.ca_result: bool = ca_result
        self.login_error: Optional[Exception] = login_error

        self.login_kwargs: Dict[str, Any] = {}
        self.activate_ca_kwargs: Dict[str, Any] = {}
        self.logout_count: int = 0
        self.session_down_callback: Optional[Callable[..., None]] = None

        self.stock_account: Optional[str] = "S" if has_stock_account else None
        self.futopt_account: Optional[str] = "F" if has_futopt_account else None
        self.Contracts: FakeContracts = FakeContracts(
            contract if contract is not None else FakeContract()
        )

    def login(self, **kwargs: Any) -> List[str]:
        if self.login_error is not None:
            raise self.login_error
        self.login_kwargs = kwargs
        return ["stock", "futopt"]

    def activate_ca(self, **kwargs: Any) -> bool:
        self.activate_ca_kwargs = kwargs
        return self.ca_result

    def logout(self) -> None:
        self.logout_count += 1

    def set_session_down_callback(self, callback: Callable[..., None]) -> None:
        self.session_down_callback = callback


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有測試都假設金鑰已設定；要驗缺值的那條自行覆寫"""

    monkeypatch.setattr(session_module, "API_KEY", "key")
    monkeypatch.setattr(session_module, "API_SECRET_KEY", "secret")
    monkeypatch.setattr(session_module, "SHIOAJI_PERSON_ID", None)


@pytest.fixture
def fixed_now() -> Callable[[], datetime.datetime]:
    """固定在 2026-09-19 13:25（台北）"""

    def _now() -> datetime.datetime:
        return datetime.datetime(2026, 9, 19, 13, 25, tzinfo=TAIPEI)

    return _now


def make_session(
    fixed_now: Callable[[], datetime.datetime],
    api: Optional[FakeShioaji] = None,
    slept: Optional[List[float]] = None,
    **kwargs: Any,
) -> ShioajiSession:
    """以假 API 建立 session"""

    created: FakeShioaji = api if api is not None else FakeShioaji()

    def factory(simulation: bool) -> FakeShioaji:
        created.simulation = simulation
        return created

    return ShioajiSession(
        api_factory=factory,
        now_provider=fixed_now,
        sleep=(slept.append if slept is not None else (lambda seconds: None)),
        **kwargs,
    )


# === 預設值與環境 ===
def test_simulation_is_the_default(fixed_now: Callable[[], datetime.datetime]) -> None:
    """
    預設連模擬環境

    預設值錯的方向若是「連到正式環境」，代價是真的下單。
    """

    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(fixed_now, api=api)
    session.connect()

    assert session.simulation is True
    assert api.simulation is True


def test_production_always_activates_ca(
    fixed_now: Callable[[], datetime.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式環境一律啟用憑證，不給選"""

    monkeypatch.setattr(
        session_module,
        "require_shioaji_ca",
        lambda: (__import__("pathlib").Path("/tmp/ca.pfx"), "pw"),
    )
    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(fixed_now, api=api, simulation=False)

    assert session.activate_ca is True

    session.connect()
    assert api.activate_ca_kwargs["ca_path"] == "/tmp/ca.pfx"


def test_simulation_does_not_touch_the_certificate(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    模擬環境預設不啟用憑證

    開發機與 CI 不該接觸正式憑證；官方要求的 API 測試流程才明確傳 True。
    """

    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(fixed_now, api=api, simulation=True)
    session.connect()

    assert session.activate_ca is False
    assert api.activate_ca_kwargs == {}


def test_ca_can_be_enabled_in_simulation(
    fixed_now: Callable[[], datetime.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """官方的模擬環境 API 測試流程要求啟用憑證，因此要能明確打開"""

    monkeypatch.setattr(
        session_module,
        "require_shioaji_ca",
        lambda: (__import__("pathlib").Path("/tmp/ca.pfx"), "pw"),
    )
    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(
        fixed_now, api=api, simulation=True, activate_ca=True
    )
    session.connect()

    assert api.activate_ca_kwargs != {}


def test_ca_failure_raises(
    fixed_now: Callable[[], datetime.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `activate_ca()` 回 False 要當場拋出

    它回的是布林值，不檢查就會在「憑證沒啟用」的狀態下一路走到下單，
    而券商那時給的錯誤訊息指不到憑證。
    """

    monkeypatch.setattr(
        session_module,
        "require_shioaji_ca",
        lambda: (__import__("pathlib").Path("/tmp/ca.pfx"), "pw"),
    )
    api: FakeShioaji = FakeShioaji(ca_result=False)
    session: ShioajiSession = make_session(fixed_now, api=api, simulation=False)

    with pytest.raises(RuntimeError, match="CA 憑證啟用失敗"):
        session.connect()
    assert session.is_connected() is False


def test_ca_password_never_appears_in_the_error(
    fixed_now: Callable[[], datetime.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """憑證密碼不得出現在例外訊息裡——例外會被記進 log，而 log 會被貼給別人看"""

    monkeypatch.setattr(
        session_module,
        "require_shioaji_ca",
        lambda: (__import__("pathlib").Path("/tmp/ca.pfx"), "super-secret"),
    )
    session: ShioajiSession = make_session(
        fixed_now, api=FakeShioaji(ca_result=False), simulation=False
    )

    with pytest.raises(RuntimeError) as error:
        session.connect()

    assert "super-secret" not in str(error.value)


def test_missing_credentials_raise_before_building_the_api(
    fixed_now: Callable[[], datetime.datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """金鑰沒設定時在建立 API 物件之前就拋出"""

    monkeypatch.setattr(session_module, "API_KEY", None)
    session: ShioajiSession = make_session(fixed_now)

    with pytest.raises(RuntimeError, match="API_KEY"):
        session.connect()
    assert session.api is None


# === 登入參數 ===
def test_login_passes_receive_window(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    `receive_window` 要明寫

    它就是伺服器端的時鐘偏差檢查：本機時鐘與券商差超過這個範圍時登入直接失敗。
    明寫而不靠預設值，是為了讓「登入失敗」與「時鐘飄掉」在程式碼裡看得出關聯。
    """

    api: FakeShioaji = FakeShioaji()
    make_session(fixed_now, api=api).connect()

    assert api.login_kwargs["receive_window"] == ShioajiSession.RECEIVE_WINDOW_MS


def test_login_kwargs_are_accepted_by_the_installed_shioaji(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    傳給 `login()` 的參數都要是**安裝中的 shioaji** 認得的

    假物件什麼參數都收，真品不是：shioaji 1.7 拿掉了 `contracts_timeout`，
    照舊傳的話登入當場 `TypeError`，而單元測試全程是綠的。
    """

    api: FakeShioaji = FakeShioaji()
    make_session(fixed_now, api=api).connect()

    accepted: Set[str] = set(inspect.signature(sj.Shioaji.login).parameters)
    assert set(api.login_kwargs) - accepted == set()


def test_login_failure_propagates(fixed_now: Callable[[], datetime.datetime]) -> None:
    """
    登入失敗要拋出，**不可回傳 None**

    舊版 `ShioajiAccount.API_login()` 回 None，呼叫端幾乎不會檢查，
    於是「沒登入」一路走到下單才爆。
    """

    api: FakeShioaji = FakeShioaji(login_error=ConnectionError("登入失敗"))
    session: ShioajiSession = make_session(fixed_now, api=api)

    with pytest.raises(ConnectionError):
        session.connect()
    assert session.is_connected() is False


# === 帳號與時鐘 ===
def test_missing_account_warns_but_does_not_block(
    fixed_now: Callable[[], datetime.datetime], caplog: pytest.LogCaptureFixture
) -> None:
    """
    缺帳號要在登入時就講清楚，但不阻擋

    只做股票的人沒開期貨權限是正常的；阻擋會讓他根本啟動不了。
    不講的話則是等到下單才 `AttributeError`，而那個訊息看不出是權限沒開。
    """

    session: ShioajiSession = make_session(
        fixed_now, api=FakeShioaji(has_futopt_account=False)
    )
    session.connect()

    assert session.is_connected() is True


def test_clock_off_by_days_refuses_to_start(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    本機日期與合約檔更新日差太多就拒絕啟動

    日期錯一天，整天的段落判定、交易日計算與帳務歸屬全部落在錯的日子上，
    而且不會有任何錯誤訊息。
    """

    api: FakeShioaji = FakeShioaji(contract=FakeContract(update_date="2026-08-01"))
    session: ShioajiSession = make_session(fixed_now, api=api)

    with pytest.raises(RuntimeError, match="相差"):
        session.connect()


def test_contract_date_accepts_the_real_broker_format(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    Shioaji 1.3.3 實際回的是 `YYYY/MM/DD`，不是 ISO

    **2026-09-21 模擬環境實連確認**。原本只以 `fromisoformat()` 解析，
    拿到 `'2026/09/21'` 解析失敗被吞成 None，於是「本機日期差一天以上」那道守門
    從來沒有生效過，只留下一行「略過本機日期檢查」——看起來像環境問題，
    其實是程式問題。這條測試就是釘住那個格式。
    """

    api: FakeShioaji = FakeShioaji(contract=FakeContract(update_date="2026/08/01"))
    session: ShioajiSession = make_session(fixed_now, api=api)

    with pytest.raises(RuntimeError, match="相差"):
        session.connect()


def test_contract_date_as_date_object_is_checked(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    shioaji 1.7 的 `update_date` 是 `datetime.date`，同樣要做日期檢查

    1.3.3 回的是 `'YYYY/MM/DD'` 字串；兩種型別都要能判斷，否則換版後這道守門
    又會像當初那樣靜默失效。
    """

    stale: datetime.date = fixed_now().date() - datetime.timedelta(days=30)
    api: FakeShioaji = FakeShioaji(contract=FakeContract(update_date=stale))

    with pytest.raises(RuntimeError, match="相差"):
        make_session(fixed_now, api=api).connect()

    current: FakeShioaji = FakeShioaji(
        contract=FakeContract(update_date=fixed_now().date())
    )
    make_session(fixed_now, api=current).connect()


def test_contract_date_in_the_real_format_passes_when_current(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """同一個格式、日期正確時要放行——修完不可以變成一律拒絕啟動"""

    today: str = fixed_now().date().strftime("%Y/%m/%d")
    api: FakeShioaji = FakeShioaji(contract=FakeContract(update_date=today))
    session: ShioajiSession = make_session(fixed_now, api=api)
    session.connect()

    assert session.is_connected() is True


def test_unreadable_contract_date_only_warns(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    取不到合約檔更新日期時只記 warning

    這是輔助檢查（秒級偏差由伺服器的 `receive_window` 把關），
    讓它變成硬條件會使整套系統因為一個欄位語意改變而啟動不了。
    """

    api: FakeShioaji = FakeShioaji(contract=FakeContract(update_date="not-a-date"))
    session: ShioajiSession = make_session(fixed_now, api=api)
    session.connect()

    assert session.is_connected() is True


def test_report_clock_skew_triggers_degrade(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    回報時戳才有秒級精度，超過容許偏差要送降級事件

    這是登入時做不到的那一半：合約檔只有日期。
    """

    reasons: List[str] = []
    session: ShioajiSession = make_session(fixed_now, on_degrade=reasons.append)

    within: float = session.check_report_clock_skew(
        datetime.datetime(2026, 9, 19, 13, 24, 58, tzinfo=TAIPEI)
    )
    assert within == pytest.approx(2.0)
    assert reasons == []

    beyond: float = session.check_report_clock_skew(
        datetime.datetime(2026, 9, 19, 13, 24, 0, tzinfo=TAIPEI)
    )
    assert beyond == pytest.approx(60.0)
    assert len(reasons) == 1


# === 斷線與重連 ===
def test_session_down_callback_marks_disconnected_and_degrades(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    斷線回呼只更新旗標與送降級事件，**不在回呼裡重連**

    回呼跑在券商的執行緒上，在裡面登入會拖住整條回報路徑，
    而重連期間進來的回報正是最不能丟的。
    """

    reasons: List[str] = []
    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(
        fixed_now, api=api, on_degrade=reasons.append
    )
    session.connect()

    assert api.session_down_callback is not None
    api.session_down_callback()

    assert session.is_connected() is False
    assert len(reasons) == 1


def test_reconnect_backoff_is_exponential_and_capped(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """退避要指數成長並封頂，不可無限往上加"""

    slept: List[float] = []
    session: ShioajiSession = make_session(fixed_now, slept=slept)

    for _ in range(12):
        session.reconnect()

    assert slept[:4] == [2.0, 4.0, 8.0, 16.0]
    assert max(slept) == ShioajiSession.RECONNECT_MAX_SECONDS
    assert all(value <= ShioajiSession.RECONNECT_MAX_SECONDS for value in slept)


def test_reconnect_stops_at_the_daily_cap_and_degrades(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    次數用完就降級，不繼續試

    官方限制每日登入 1,000 次。無限重試只會把額度用完，然後明天也登不進去——
    而需要重連 20 次的那一天本來就不該繼續交易。
    """

    reasons: List[str] = []
    session: ShioajiSession = make_session(fixed_now, on_degrade=reasons.append)

    for _ in range(ShioajiSession.MAX_DAILY_RECONNECTS):
        session.reconnect()

    assert session.reconnect() is False
    assert any("單日上限" in reason for reason in reasons)


def test_reset_daily_counters(fixed_now: Callable[[], datetime.datetime]) -> None:
    """換日要能重設：登入額度是以日為單位的"""

    session: ShioajiSession = make_session(fixed_now)
    session.reconnect()
    session.reset_daily_counters()

    assert session.reconnect_count == 0


# === 關閉 ===
def test_close_is_idempotent(fixed_now: Callable[[], datetime.datetime]) -> None:
    """`close()` 要可重複呼叫：引擎以 `try/finally` 保證它跑到，異常路徑上可能跑兩次"""

    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(fixed_now, api=api)
    session.connect()

    session.close()
    session.close()

    assert api.logout_count == 1
    assert session.is_connected() is False


def test_logout_failure_does_not_raise(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    登出失敗只記 log

    `close()` 通常跑在 `finally` 裡，這時候再拋一個例外只會蓋掉真正的錯誤。
    """

    api: FakeShioaji = FakeShioaji()
    api.logout = lambda: (_ for _ in ()).throw(ConnectionError("已斷線"))  # type: ignore[assignment]
    session: ShioajiSession = make_session(fixed_now, api=api)
    session.connect()

    session.close()

    assert session.is_connected() is False


def test_rate_limiter_is_shared_not_recreated(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    限流器可以由外部注入

    額度是**帳戶級**的：多個元件各自持有一個 limiter 時，每個都以為自己還有額度，
    合起來必然超額，而且事後誰都不知道是誰用掉的。
    """

    limiter: RateLimiter = RateLimiter()
    session: ShioajiSession = make_session(fixed_now, rate_limiter=limiter)

    assert session.rate_limiter is limiter


# === 登入失敗訊息的憑證刮除 ===
# 2026-09-20 模擬環境冒煙逾時時，實際印出來的訊息（金鑰衍生值已替換為等長假值）
REAL_LOGIN_TIMEOUT_MESSAGE: str = (
    "Topic: api/v1/auth/token_login, Corr: c1, "
    "Client: PYAPI/6xoa4AEqZ6/0920/045345/268708/LOGINING, "
    "payload: {'msg': '6999iKFbmXDYrMsZmjk2aNH1kdt4o8ssNzrst1Yaib7aXKMBuY7rv9gHjMXjMAGMb"
    "6AkP9jYmC1BfLEAC86gZkiRAmPT8XwaEsAUrAbMxAi2hbiebJ26GJV7jm7adpTzECcgEtQ9E1AsfqEijXUo"
    "atRh6NSWBkGbDazGB8induCLXeoA6EXkuoZgBdGJAcVAa', "
    "'sign': 'Uyj2A4wV2Qhmr3EXx7yJh4dN7d2AirC71jAggKX5tyJ8NrY9q9f9AtvZaZiWhZZ2E9vfGmuR5X"
    "aHBCSgyTst4bB'}\n"
    "Response Code: 0 | Event Code: 2 | Info: Session connect timeout | "
    "Event: Session connection attempt failed"
)


def test_redaction_removes_the_signed_payload() -> None:
    """
    簽章負載不得進入訊息

    它由 API secret 衍生，而例外沒被接住就會連著 traceback 印到終端與 log，
    之後還會被推播端原樣轉送。**要在進入 log 之前就刮掉**，不是在輸出端過濾——
    訊息一旦被 `logger` 收下，就同時進了終端與檔案 sink。
    """

    cleaned: str = ShioajiSession.redact_credentials(REAL_LOGIN_TIMEOUT_MESSAGE)

    assert "'msg'" not in cleaned
    assert "'sign'" not in cleaned
    assert "6999iKFbmXDYrMsZ" not in cleaned
    assert "Uyj2A4wV2Qhmr3EX" not in cleaned
    assert "6xoa4AEqZ6" not in cleaned  # Client 名稱含金鑰前綴


def test_redaction_keeps_the_actual_reason() -> None:
    """
    刮完還要看得出原因

    實測那則訊息有 400 多字，真正的原因（`Session connect timeout`）被埋在負載後面。
    刮太乾淨就變成「登入失敗」四個字，等於沒有訊息。
    """

    cleaned: str = ShioajiSession.redact_credentials(REAL_LOGIN_TIMEOUT_MESSAGE)

    assert "Session connect timeout" in cleaned
    assert "Session connection attempt failed" in cleaned
    assert len(cleaned) < len(REAL_LOGIN_TIMEOUT_MESSAGE) / 2


def test_login_error_is_redacted_before_it_escapes(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """連 `connect()` 拋出的例外訊息本身都不得帶著負載——它會被印進 traceback"""

    api: FakeShioaji = FakeShioaji(login_error=TimeoutError(REAL_LOGIN_TIMEOUT_MESSAGE))
    session: ShioajiSession = make_session(fixed_now, api=api)

    with pytest.raises(ConnectionError) as error:
        session.connect()

    text: str = str(error.value)
    assert "'sign'" not in text
    assert "TimeoutError" in text  # 原例外型別不可在轉換時弄丟
    assert "Session connect timeout" in text


def test_login_error_chain_is_cut(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    例外鏈要切斷

    沒切斷的話，原始例外會以「During handling of the above exception…」跟著印出來，
    刮除就白做了。

    **判準是 `__suppress_context__` 而不是 `__context__`**：`raise ... from None`
    只把 `__cause__` 設成 None 並把 `__suppress_context__` 設成 True，
    `__context__` 仍指著原例外——真正決定「印不印」的是前者。
    """

    api: FakeShioaji = FakeShioaji(login_error=TimeoutError(REAL_LOGIN_TIMEOUT_MESSAGE))
    session: ShioajiSession = make_session(fixed_now, api=api)

    with pytest.raises(ConnectionError) as error:
        session.connect()

    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


# === 登出失敗不可以把 token 寫進 log ===
def test_logout_failure_does_not_leak_the_session_token(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    登出失敗時的 log 不可以含 session token 或身分證字號

    **2026-09-21 模擬環境實測撞到**：登出逾時的例外訊息帶著 token 與含身分證的
    client 名稱，而 `close()` 原本直接 `opt(exception=True)` 印出——loguru 連
    traceback 每一層的區域變數都印了，token 出現三次並一路寫進檔案 sink。
    `connect()` 早就有刮過再記的做法，`close()` 沒跟上。
    """

    from loguru import logger

    fake_token: str = (
        "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJmYWtlIjoidG9rZW4ifQ.SIGNATURESIGNATURE"
    )

    class LeakyLogout(FakeShioaji):
        def logout(self) -> None:
            raise TimeoutError(
                "Topic: api/v1/auth/logout, Corr: c11, "
                "Client: PYAPI/A123456789/0921/050257/787868/1.2.3.4, "
                f"payload: {{'token': '{fake_token}'}}"
            )

    captured: List[str] = []
    handle: int = logger.add(lambda message: captured.append(str(message)))
    try:
        session: ShioajiSession = make_session(fixed_now, api=LeakyLogout())
        session.connect()
        session.close()
    finally:
        logger.remove(handle)

    text: str = "".join(captured)
    assert "登出失敗" in text
    assert fake_token not in text
    assert "A123456789" not in text


# === 多帳號、唯讀登入（tick 爬蟲）===
def test_explicit_credentials_override_the_environment(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """tick 爬蟲以多組金鑰輪替：傳入的金鑰優先於環境變數"""

    api: FakeShioaji = FakeShioaji()
    session: ShioajiSession = make_session(
        fixed_now, api=api, credentials=("other-key", "other-secret")
    )

    session.connect()

    assert api.login_kwargs["api_key"] == "other-key"
    assert api.login_kwargs["secret_key"] == "other-secret"


def test_contract_date_check_can_be_skipped_for_read_only_use(
    fixed_now: Callable[[], datetime.datetime],
) -> None:
    """
    只抓歷史資料時可以不檢查合約檔日期

    長連假期間合約檔可能超過容許天數沒更新，實盤要擋，tick 回補不該被擋。
    """

    stale: FakeShioaji = FakeShioaji(contract=FakeContract(update_date="2026-08-01"))
    session: ShioajiSession = make_session(
        fixed_now, api=stale, verify_contract_date=False
    )

    session.connect()

    assert session.connected


def test_read_only_sessions_skip_failed_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    多組帳號逐一登入正式環境、不啟用憑證；登入失敗的那組略過，其餘照常

    不啟用憑證就送不出委託，這組連線只能查資料。
    """

    created: List[FakeShioaji] = []

    def factory(simulation: bool) -> FakeShioaji:
        api: FakeShioaji = FakeShioaji(
            simulation=simulation,
            login_error=ConnectionError("金鑰失效") if len(created) == 1 else None,
        )
        created.append(api)
        return api

    monkeypatch.setattr(
        session_module.ShioajiSession, "_default_api_factory", staticmethod(factory)
    )

    sessions: List[ShioajiSession] = session_module.login_read_only_sessions(
        [("k1", "s1"), ("k2", "s2"), ("k3", "s3")]
    )

    assert len(sessions) == 2
    assert [api.simulation for api in created] == [False, False, False]
    assert all(api.activate_ca_kwargs == {} for api in created)
