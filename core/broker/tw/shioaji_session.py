import datetime
import re
import time
from typing import Any, Callable, List, Optional, Sequence, Tuple

import shioaji as sj
from loguru import logger

from core.broker.rate_limiter import RateLimiter
from core.config.settings import (
    API_KEY,
    API_SECRET_KEY,
    SHIOAJI_PERSON_ID,
    now_live,
    require_shioaji_ca,
)

"""
ShioajiSession：登入、憑證、模擬旗標、斷線重連與時鐘檢查

取代舊的登入工具 `ShioajiAccount.API_login()`（已刪除）。舊版的三個問題：
用 `print` 而不是 logger、登入失敗回傳 `None`（呼叫端幾乎不會檢查）、
沒有 `simulation` 與 `activate_ca`——也就是它只連得到正式環境，
而且連不上的時候你要自己發現。

**本類別只管連線，不管業務**：下單、查帳、行情各自有元件，
它們共用這裡的 `api` 與 `rate_limiter`。
"""


# 登入失敗的例外訊息會**整串帶著簽章負載**（`payload: {'msg': ..., 'sign': ...}`），
# 那是由 API secret 衍生出來的，而例外一旦沒被接住就會連著 traceback 印到終端與 log，
# 之後還會被告警推播（`core/live/notify/`）原樣送出去。
#
# 附帶的好處是可讀性：實測一次逾時的訊息有 400 多字，真正的原因
# （`Info: Session connect timeout`）被埋在負載後面，肉眼根本掃不到。
_LOGIN_PAYLOAD_PATTERN: re.Pattern = re.compile(r"payload:\s*\{.*?\}", re.DOTALL)
_CLIENT_NAME_PATTERN: re.Pattern = re.compile(r"(Client:\s*)\S+")
_LONG_TOKEN_PATTERN: re.Pattern = re.compile(r"[A-Za-z0-9+/=]{40,}")
_REDACTED: str = "<已略去>"


class ShioajiSession:
    """
    Shioaji 連線 session

    **一個帳戶只開一個 session**：憑證是一組的，多重登入的行為未定義；
    而且呼叫額度是帳戶級的，多個 session 各自持有限流器時，
    每個都以為自己還有額度，合起來必然超額。
    """

    # 簽章有效視窗（毫秒）。**這就是伺服器端的時鐘偏差檢查**：本機時鐘與券商相差
    # 超過這個範圍時，`login()` 直接失敗。明寫而不用預設值，是為了讓「登入失敗」
    # 與「時鐘飄掉」在程式碼裡看得出關聯
    RECEIVE_WINDOW_MS: int = 30000

    # 回報時戳與本機時鐘的容許偏差（秒）。段落判定、報價過期偵測、日終強制動作
    # 全都建立在本機時鐘上，飄掉的症狀是「訊號時點整個偏移」，很難從結果反推
    MAX_CLOCK_SKEW_SECONDS: float = 5.0

    # 合約檔更新日與本機日期的容許落差（天）。合約檔每個交易日更新一次，
    # 故只擋得住「差了一天以上」這種等級的錯誤——但那正是最致命的一種
    MAX_CONTRACT_DATE_SKEW_DAYS: int = 3

    # 重連退避：2、4、8…秒，上限 5 分鐘
    RECONNECT_BASE_SECONDS: float = 2.0
    RECONNECT_MAX_SECONDS: float = 300.0

    # 單日重連次數上限。官方限制是每日登入 1,000 次，但會需要重連 20 次的那一天
    # 本來就不該繼續交易——無限重試只會把額度用完，然後明天也登不進去
    MAX_DAILY_RECONNECTS: int = 20

    def __init__(
        self,
        simulation: bool = True,
        activate_ca: Optional[bool] = None,
        rate_limiter: Optional[RateLimiter] = None,
        api_factory: Optional[Callable[[bool], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        now_provider: Callable[[], datetime.datetime] = now_live,
        on_degrade: Optional[Callable[[str], None]] = None,
        credentials: Optional[Tuple[str, str]] = None,
        verify_contract_date: bool = True,
    ) -> None:
        """
        - Description:
            建立 session（此時尚未連線）
        - Parameters:
            - simulation: bool
                是否連模擬環境。**預設為 True**：預設值錯的方向若是「連到正式環境」，
                代價是真的下單
            - activate_ca: Optional[bool]
                是否啟用下單憑證。`None`（預設）時依環境決定——正式環境一律啟用，
                模擬環境不啟用。模擬環境只有官方要求的 API 測試流程需要明確傳 True；
                平時不啟用是因為開發機與 CI 不該接觸正式憑證
            - rate_limiter: Optional[RateLimiter]
                共用的限流器；未提供時自建一個
            - api_factory: Optional[Callable[[bool], Any]]
                建立 API 物件的工廠（收 simulation 旗標）；測試注入假 API 用
            - sleep: Callable[[float], None]
                等待函式；測試注入假時鐘用
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
            - on_degrade: Optional[Callable[[str], None]]
                降級回呼。**session 不自己改交易模式**，只送事件給風控——
                散在各元件各自切換的話，沒有任何一處知道「現在到底能不能送單」
            - credentials: Optional[Tuple[str, str]]
                `(api_key, secret_key)`；None 時讀環境變數 `API_KEY`／`API_SECRET_KEY`。
                tick 爬蟲以多組帳號輪替時逐組傳入
            - verify_contract_date: bool
                是否以合約檔更新日粗略檢查本機日期。實盤一律要；只抓歷史資料的
                ETL 可關掉——長連假期間合約檔可能超過容許天數沒更新，會擋住回補
        """

        self.simulation: bool = simulation
        self.activate_ca: bool = (
            (not simulation) if activate_ca is None else activate_ca
        )
        self.rate_limiter: RateLimiter = (
            rate_limiter if rate_limiter is not None else RateLimiter()
        )

        self._api_factory: Callable[[bool], Any] = (
            api_factory if api_factory is not None else self._default_api_factory
        )
        self._sleep: Callable[[float], None] = sleep
        self._now: Callable[[], datetime.datetime] = now_provider
        self._on_degrade: Optional[Callable[[str], None]] = on_degrade
        self._credentials: Optional[Tuple[str, str]] = credentials
        self._verify_contract_date_enabled: bool = verify_contract_date

        self.api: Optional[Any] = None
        self.connected: bool = False
        self.reconnect_count: int = 0

    @staticmethod
    def redact_credentials(message: str) -> str:
        """
        - Description:
            刮掉訊息中的簽章負載與長字串憑證

            **在進入 log 之前就刮**，不是在輸出端過濾：訊息一旦被 `logger` 收下，
            它同時進了終端、檔案 sink，之後還會被推播端原樣轉送，
            那時要一處一處補過濾已經來不及了。
        - Parameters:
            - message: str
                原始訊息
        - Return:
            - str
                可安全記錄的訊息
        """

        cleaned: str = _LOGIN_PAYLOAD_PATTERN.sub(f"payload: {_REDACTED}", message)
        cleaned = _CLIENT_NAME_PATTERN.sub(rf"\1{_REDACTED}", cleaned)
        cleaned = _LONG_TOKEN_PATTERN.sub(_REDACTED, cleaned)
        return cleaned

    @staticmethod
    def _default_api_factory(simulation: bool) -> Any:
        """預設工廠：建立真正的 Shioaji API 物件"""

        return sj.Shioaji(simulation=simulation)

    # === 連線 ===
    def connect(self) -> None:
        """
        - Description:
            登入、啟用憑證、確認帳號與時鐘，全部通過才算連線成功

            **失敗一律拋出**，不回傳 `None` 或旗標：舊版回 `None` 的寫法讓呼叫端
            可以不檢查就往下走，於是「沒登入」會一路走到下單才爆，
            而那時的錯誤訊息完全指不到原因。
        - Raise:
            - RuntimeError
                金鑰未設定、憑證啟用失敗、缺少必要帳號，或時鐘偏差過大
        """

        api_key, secret_key = self._credentials or (API_KEY, API_SECRET_KEY)
        if not api_key or not secret_key:
            raise RuntimeError(
                "環境變數 API_KEY／API_SECRET_KEY 未設定，無法登入 Shioaji"
            )

        environment: str = "模擬" if self.simulation else "**正式**"
        logger.info(f"Shioaji 登入中（{environment}環境）…")

        self.api = self._api_factory(self.simulation)
        try:
            accounts: List[Any] = self.api.login(
                api_key=api_key,
                secret_key=secret_key,
                receive_window=self.RECEIVE_WINDOW_MS,
            )
        except Exception as exc:
            # **一律轉成 `ConnectionError` 並切斷例外鏈（`from None`）**：
            # 原始例外的訊息帶著簽章負載，保留鏈結的話它照樣會被印出來。
            # 原例外的型別名留在訊息裡，追查時不會少掉資訊
            reason: str = self.redact_credentials(str(exc))
            message: str = f"Shioaji 登入失敗（{type(exc).__name__}）：{reason}"
            logger.error(message)
            raise ConnectionError(message) from None

        logger.info(f"Shioaji 登入成功，取得 {len(accounts or [])} 個帳號")

        if self.activate_ca:
            self._activate_ca()

        self._verify_accounts()
        if self._verify_contract_date_enabled:
            self._verify_contract_date()
        self._register_session_callbacks()

        self.connected = True

    def _activate_ca(self) -> None:
        """啟用下單憑證；失敗時拋出（**密碼不得進入任何 log 或例外訊息**）"""

        ca_path, ca_password = require_shioaji_ca()
        activated: bool = self.api.activate_ca(
            ca_path=str(ca_path),
            ca_passwd=ca_password,
            person_id=SHIOAJI_PERSON_ID or "",
        )
        if not activated:
            raise RuntimeError(f"CA 憑證啟用失敗：{ca_path}（密碼不在此顯示）")

        logger.info(f"CA 憑證已啟用：{ca_path.name}")

    def _verify_accounts(self) -> None:
        """
        確認股票與期貨帳號都在

        **缺哪一個要在登入時就講清楚**，不要等到下單才 `AttributeError`：
        那個例外訊息只會說某個屬性不存在，看不出是權限沒開。
        """

        missing: List[str] = [
            label
            for label, attribute in (
                ("股票", "stock_account"),
                ("期貨", "futopt_account"),
            )
            if getattr(self.api, attribute, None) is None
        ]
        if missing:
            logger.warning(
                f"Shioaji 帳號缺少：{'、'.join(missing)}。"
                "該類商品無法下單，請確認已開通對應的 API 權限"
            )

    def _verify_contract_date(self) -> None:
        """
        - Description:
            以合約檔的更新日期粗略檢查本機時鐘

            **只擋得住「差了一天以上」**：合約檔一個交易日才更新一次，拿不到秒級時戳。
            但那正是最致命的一種——本機日期錯一天，整天的段落判定、交易日計算與
            帳務歸屬全部落在錯的日子上，而且不會有任何錯誤訊息。

            秒級的偏差由伺服器端把關：`login()` 的 `receive_window` 超過就直接失敗。
            回報進來之後，另以 `check_report_clock_skew()` 做精確檢查。

            合約檔取不到更新日期時**只記 warning 不阻擋**：那代表欄位語意與這裡的
            假設不同，阻擋會讓整套系統因為一個輔助檢查而啟動不了。
        - Raise:
            - RuntimeError
                本機日期與合約檔更新日相差超過 `MAX_CONTRACT_DATE_SKEW_DAYS` 天
        """

        update_date: Optional[datetime.date] = self._fetch_contract_update_date()
        if update_date is None:
            logger.warning("取不到合約檔更新日期，略過本機日期檢查")
            return

        today: datetime.date = self._now().date()
        skew_days: int = abs((today - update_date).days)
        if skew_days > self.MAX_CONTRACT_DATE_SKEW_DAYS:
            raise RuntimeError(
                f"本機日期（{today}）與券商合約檔更新日（{update_date}）"
                f"相差 {skew_days} 天，超過容許的 {self.MAX_CONTRACT_DATE_SKEW_DAYS} 天；"
                "請先校正系統時間與時區（實盤一律使用 Asia/Taipei）"
            )

    # 合約檔更新日為字串時可能的格式。**實測 Shioaji 1.3.3 回的是 `YYYY/MM/DD`**
    # （2026-09-21 模擬環境實連），而不是 ISO 的 `YYYY-MM-DD`；
    # 1.7 起改回 `datetime.date`，由 `_fetch_contract_update_date()` 直接採用
    CONTRACT_DATE_FORMATS: Tuple[str, ...] = ("%Y/%m/%d", "%Y-%m-%d")

    def _fetch_contract_update_date(self) -> Optional[datetime.date]:
        """
        取一張上市股票合約的 `update_date`

        取不到就回 `None`，不拋出：這是輔助檢查，不該成為啟動的硬條件。

        **格式不只一種，而且猜錯的代價是整道檢查靜默失效**：欄位原本只以
        `fromisoformat()` 解析，實際拿到 `'2026/09/21'` 時解析失敗被吞成 None，
        於是「本機日期差一天以上」那道守門從來沒有生效過，只留下一行
        「略過本機日期檢查」的 warning——看起來像環境問題，其實是程式問題。
        """

        contract: Any = None
        try:
            contract = self.api.Contracts.Stocks.get("2330")
        except Exception as exc:
            logger.opt(exception=True).debug(f"取不到合約：{exc}")
            return None

        raw: Any = getattr(contract, "update_date", None)
        if isinstance(raw, datetime.date):
            return raw
        if raw is None:
            return None

        text: str = str(raw)
        for pattern in self.CONTRACT_DATE_FORMATS:
            try:
                return datetime.datetime.strptime(text, pattern).date()
            except ValueError:
                continue

        # 欄位在、但格式不認得 → 這是**程式該更新**的訊號，不是環境問題，
        # 故記 warning 而不是 debug：debug 等於沒有人會看到
        logger.warning(
            f"合約檔更新日 {text!r} 的格式不在已知清單 {self.CONTRACT_DATE_FORMATS} 內，"
            "本機日期檢查將被略過；請補上該格式"
        )
        return None

    def check_report_clock_skew(self, report_ts: datetime.datetime) -> float:
        """
        - Description:
            以券商回報的時戳精確檢查本機時鐘偏差

            這是登入時做不到的那一半：合約檔只有日期，回報才有秒級時戳。
            由回報正規化的元件在收到第一筆回報時呼叫一次。
        - Parameters:
            - report_ts: datetime.datetime
                券商回報的時戳（台北時區 aware）
        - Return:
            - float
                偏差秒數（絕對值）
        """

        skew: float = abs((self._now() - report_ts).total_seconds())
        if skew > self.MAX_CLOCK_SKEW_SECONDS:
            message: str = (
                f"本機時鐘與券商回報相差 {skew:.1f} 秒，"
                f"超過容許的 {self.MAX_CLOCK_SKEW_SECONDS} 秒；"
                "段落判定與報價過期偵測都建立在本機時鐘上"
            )
            logger.error(message)
            self._degrade(message)
        return skew

    def _register_session_callbacks(self) -> None:
        """註冊斷線回呼；回呼只更新旗標與記 log，重連由主流程決定時機"""

        setter: Optional[Callable[..., Any]] = getattr(
            self.api, "set_session_down_callback", None
        )
        if setter is None:
            logger.warning(
                "此 Shioaji 版本沒有 set_session_down_callback，斷線只能靠輪詢察覺"
            )
            return

        setter(self._on_session_down)

    def _on_session_down(self, *args: Any, **kwargs: Any) -> None:
        """
        行情／交易連線中斷

        **不在回呼裡重連**：回呼跑在券商的執行緒上，在裡面登入會拖住整條回報路徑，
        而重連期間進來的回報正是最不能丟的。
        """

        self.connected = False
        logger.error("Shioaji session 中斷")
        self._degrade("Shioaji session 中斷")

    # === 重連 ===
    def reconnect(self) -> bool:
        """
        - Description:
            以指數退避重新登入

            **次數用完就降級而不是繼續試**：官方限制每日登入 1,000 次，
            而需要重連 20 次的那一天本來就不該繼續交易。無限重試只會把額度用完，
            然後明天也登不進去。
        - Return:
            - bool
                是否重連成功
        """

        if self.reconnect_count >= self.MAX_DAILY_RECONNECTS:
            message: str = (
                f"重連次數已達單日上限 {self.MAX_DAILY_RECONNECTS} 次，停止重試"
            )
            logger.error(message)
            self._degrade(message)
            return False

        self.reconnect_count += 1
        backoff: float = min(
            self.RECONNECT_BASE_SECONDS * (2 ** (self.reconnect_count - 1)),
            self.RECONNECT_MAX_SECONDS,
        )
        logger.warning(f"第 {self.reconnect_count} 次重連，退避 {backoff:.0f} 秒後嘗試")
        self._sleep(backoff)

        self.close()
        try:
            self.connect()
        except Exception as exc:
            # 同 `close()`：traceback 的區域變數含有登入負載，一律只記刮過的訊息
            logger.error(
                f"重連失敗（{type(exc).__name__}）：{self.redact_credentials(str(exc))}"
            )
            return False

        logger.info("重連成功")
        return True

    def reset_daily_counters(self) -> None:
        """換日時重設重連計數；登入額度是以日為單位的"""

        self.reconnect_count = 0

    # === 關閉 ===
    def close(self) -> None:
        """
        登出並關閉

        **必須可重複呼叫**：引擎以 `try/finally` 保證它一定跑到，
        而異常路徑上它可能已經被呼叫過一次。登出本身失敗只記 log——
        這時候再拋一個例外只會蓋掉真正的錯誤。
        """

        if self.api is None:
            self.connected = False
            return

        try:
            self.api.logout()
            logger.info("Shioaji 已登出")
        except Exception as exc:
            # **不印 traceback、訊息要先刮過**：登出失敗的例外訊息帶著 session token
            # 與含身分證字號的 client 名稱，而 `opt(exception=True)` 會讓 loguru
            # 連 traceback 裡每一層的區域變數都印出來——token 因此出現好幾次，
            # 並一路寫進檔案 sink。與 `connect()` 的處理方式一致
            logger.warning(
                f"Shioaji 登出失敗（{type(exc).__name__}，忽略）："
                f"{self.redact_credentials(str(exc))}"
            )
        finally:
            self.connected = False
            self.api = None

    def is_connected(self) -> bool:
        """目前是否連線中"""

        return self.connected and self.api is not None

    def _degrade(self, reason: str) -> None:
        """送降級事件給風控；沒有註冊回呼時只記 log"""

        if self._on_degrade is None:
            return
        self._on_degrade(reason)


def login_read_only_sessions(
    credentials: Sequence[Tuple[str, str]],
) -> List[ShioajiSession]:
    """
    - Description:
        以多組金鑰各登入一次正式環境（**不啟用憑證、不檢查合約檔日期**），供 tick 爬蟲輪替

        不啟用憑證就送不出委託，這組連線只能查資料。登入失敗的帳號記錄後跳過，
        其餘照常——與舊版「登入失敗回 None、呼叫端略過」的行為相同，
        但失敗訊息經 `ShioajiSession` 刮掉憑證後才進 log。
    - Parameters:
        - credentials: Sequence[Tuple[str, str]]
            `[(api_key, secret_key)]`
    - Return:
        - List[ShioajiSession]
            登入成功的 session；呼叫端用完要逐一 `close()`
    """

    sessions: List[ShioajiSession] = []
    for index, pair in enumerate(credentials, start=1):
        session: ShioajiSession = ShioajiSession(
            simulation=False,
            activate_ca=False,
            credentials=pair,
            verify_contract_date=False,
        )
        try:
            session.connect()
        except Exception as exc:
            logger.warning(f"第 {index} 組 Shioaji 帳號登入失敗，略過：{exc}")
            continue
        sessions.append(session)
    return sessions
