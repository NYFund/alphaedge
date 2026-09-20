import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

"""
爬取範圍、預設區間與外部服務設定

與 `paths`／`schema` 的差別：這裡的每一個值都是**可調的營運參數**
（要抓哪些商品、從哪一年開始、連哪一台 DolphinDB），
不是系統的結構。改這裡不會動到任何路徑或資料表定義。
"""


# -----------------------------------------------------------------------
# === 台期貨爬取範圍 ===
# -----------------------------------------------------------------------
#
# 「有哪些商品」定義在 `core/utils/constant.py` 的 `FuturesProduct`；
# 「這次要抓哪些」則是本清單——兩者刻意分開，否則想暫時只跑一檔就得改 Enum。
#
# **此處刻意寫字面值而不 import `FuturesProduct`**：`core.config` 是最底層模組，
# 而 `core.utils` 反過來相依它（`core/utils/account.py` → `core.config`），
# import 進來會造成循環。值本身是 TAIFEX 的外部代碼、不會改名，風險低；
# 拼錯的防呆由 `FuturesPriceCrawler.validate_product()` 在送出請求前比對
# `FuturesProduct` 擋下，並有 `test_configured_targets_are_all_valid` 釘住本清單。
#
# **每加一檔的成本是乘出來的**：這個來源一次只能查一個商品，且日盤與夜盤要分開查，
# 故請求數 = 商品數 × 2 × 交易日數（以 2013 年起算約 3,300 日 → 每檔約 6,600 次）。
# 目前 1 檔 ≈ 13,800 次請求（1998-07 起算約 6,900 個交易日）。
#
# **先跑通單檔再擴充**：TX 於 2026-08-29 驗證無誤，其餘六檔於 2026-09-02
# 逐一實測可爬可清可入庫後加入，**crawler／updater 一行都沒改**
# ——商品代碼本來就是查詢參數，這正是當初分層的目的。
#
# ⚠️ **加入本清單不等於資料已回補**：`--target futures_price` 會逐一補齊，
# 六檔的歷史回補約需 40 小時。要確認某商品補到哪一天，
# 查 `SELECT product, MIN(date), MAX(date), COUNT(*) FROM futures_price_daily GROUP BY product`。
#
# 收錄門檻：**契約乘數已查證**。乘數未登錄的商品即使爬回來也算不出 PnL，
# 會拖到回測階段才 KeyError，不如一開始就不收。
FUTURES_TARGET_PRODUCTS: List[str] = [
    "TX",  # 臺股期貨（大台）    乘數 200
    "MTX",  # 小型臺指           乘數 50
    "TMF",  # 微型臺指           乘數 10
    "TE",  # 電子期貨            乘數 4000
    "ZEF",  # 小型電子期貨       乘數 500
    "TF",  # 金融期貨            乘數 1000
    "ZFF",  # 小型金融期貨       乘數 250
]


# -----------------------------------------------------------------------
# === Default dates for update_db / pipeline（資料更新預設區間）===
# -----------------------------------------------------------------------
#
DEFAULT_CHIP_START_DATE: datetime.date = datetime.date(2013, 1, 1)
DEFAULT_MARGIN_START_DATE: datetime.date = datetime.date(2013, 1, 1)
DEFAULT_DIVIDEND_START_DATE: datetime.date = datetime.date(2013, 1, 1)
# 公司行動（減資／面額變更）：與除權息同起點。S1 實測 2013 年起兩個端點的
# 欄位結構未再改制，更早的年份未驗證
DEFAULT_CORPORATE_ACTION_START_DATE: datetime.date = DEFAULT_DIVIDEND_START_DATE
DEFAULT_PRICE_START_DATE: datetime.date = datetime.date(2013, 1, 1)

# 台期貨回補起點（2026-08-29 由使用者決定）。
#
# **來源能給的更早**：TX 臺股期貨可回溯到 1998-07-21（其上市日，逐日實測確認
# 07/20 無資料、07/21 起有），亦即 TAIFEX 提供完整歷史、沒有截斷。
# 此處取 2015-01-01 是**刻意收窄**，不是資料限制。
#
# 要改回更早的起點，改這一行即可——**已入庫的資料不受影響**（loader 走
# INSERT OR IGNORE），續跑會從表內該商品的最新日接續，
# 故往前擴張需要另行指定區間重跑，見 `FuturesPriceUpdater.update()`。
DEFAULT_FUTURES_START_DATE: datetime.date = datetime.date(2015, 1, 1)

# 各商品在 TAIFEX 第一個有行情的日期（2026-09-02 以 crawler 逐日實測）。
#
# **上市較晚的商品不能沿用 `DEFAULT_FUTURES_START_DATE`**：上市前的每一天都查無
# 資料，累積到 `FuturesPriceUpdater.EMPTY_PRODUCT_ABORT_THRESHOLD`（20 天）就會
# 觸發保險絲中止整檔回補——2026-09-01 的回補就是這樣停在 TMF。故 `update_product()`
# 一律以本表把起點往後夾。
#
# **沒登錄的商品不夾**（例如股期，共 320 檔且會隨掛牌／下市異動，無法逐一實測）：
# 那類商品仍靠保險絲擋代碼拼錯，行為與本表加入前相同。
#
# 量測方式：以月為單位二分搜尋找出第一個有行情的月份後，**再逐個交易日往前回走**，
# 確認其前連續多日皆空才定案。只做前者會漏掉「月底才上市」的商品——ZEF 會被測成
# 2021-07-01（實為 06-28）、TMF 會被測成 2024-08-01（實為 07-29），
# 於是回補少掉開頭數日且不會有任何錯誤訊息。
# **只登錄實測過的日期**。填得比實際上市日「晚」會讓回補靜默跳過開頭那幾天，
# 比觸發保險絲更難發現，所以寧可不登錄——不登錄只是回到本表加入前的行為。
# MTX／TE／TF 的上市日尚未實測（僅確認 2015-01-05 就有行情，早於現行回補起點，
# 故實務上不需要夾），要往前回補到 2015 之前時再補測。
FUTURES_PRODUCT_LISTING_DATES: Dict[str, datetime.date] = {
    "TX": datetime.date(1998, 7, 21),  # 臺股期貨（逐日確認 07/20 無資料）
    "ZEF": datetime.date(2021, 6, 28),  # 小型電子期貨
    "ZFF": datetime.date(2021, 12, 6),  # 小型金融期貨
    "TMF": datetime.date(2024, 7, 29),  # 微型臺指
}

# 各商品**夜盤（盤後交易時段）**第一個有行情的日期。
#
# 盤後交易 2017-05-15 晚上上線，而且**不是所有商品同時開放**，是逐批納入的。
# `FuturesPriceUpdater` 對每個日期一律日夜盤各打一次請求，回補早於各自起始日的
# 區間等於白打一半的請求，並產生大量 `No valid futures price rows` warning——
# **資料是對的，雜訊是多的**，而雜訊會淹掉真正該看的那幾行。
#
# ⚠️ **這張表是觀測值不是制度公告**：2026-09-18 以 `futures_price_daily` 逐商品取
# `MIN(date) WHERE session='night'` 得到，故它的語意是「本地資料庫最早有夜盤行情
# 的那天」。若該商品的回補本身就不完整，這個值會比真實開放日晚——那會讓回補靜默
# 跳過開頭幾天，比多打請求嚴重。故 `tests/test_futures_price_updater.py` 有一條
# 測試釘住「表內每個商品的夜盤最早日期 ≥ 本表的值」，回補往前延伸時會變紅。
#
# **沒登錄的商品不跳過**（例如股期）：行為與本表加入前完全相同，照樣日夜盤都查。
FUTURES_PRODUCT_NIGHT_SESSION_START_DATES: Dict[str, datetime.date] = {
    "TX": datetime.date(2017, 5, 16),  # 臺股期貨
    "MTX": datetime.date(2017, 5, 16),  # 小型臺指
    "TE": datetime.date(2018, 11, 20),  # 電子期貨
    "ZEF": datetime.date(2021, 6, 29),  # 小型電子期貨
    "TMF": datetime.date(2024, 7, 30),  # 微型臺指
    "TF": datetime.date(2025, 6, 24),  # 金融期貨
    "ZFF": datetime.date(2025, 6, 24),  # 小型金融期貨
}

# 股票期貨預設只爬**流動性前 N 檔**。
#
# **不要一次爬 320 檔**：那是每天 640 次請求（日夜盤各一），13 年的回補要好幾個月。
# 股期的流動性差距是數量級的——前段幾檔佔了絕大多數成交量，尾端有整批一天只成交
# 個位數口的商品，把它們納入回測只會製造「回測賺錢、實際掛不到單」的假訊號。
#
# 排序依據是**已入庫行情的平均日成交量**，故第一次跑（表內還沒有股期行情）會排不出
# 來而退回整份清單並提醒——那是雞生蛋，不是錯誤。
STOCK_FUTURES_TOP_N: int = 20
DEFAULT_START_YEAR: int = 2013
DEFAULT_END_MONTH: int = 12
TICK_UPDATE_START_DATE: datetime.date = datetime.date(2024, 5, 10)
FINMIND_BROKER_TRADING_START_DATE: datetime.date = datetime.date(2021, 6, 30)
# 結束日不設常數：與 chip／margin／dividend／price 一致，由呼叫端取 today()


# -----------------------------------------------------------------------
# === DolphinDB server setting ===
# -----------------------------------------------------------------------
#
def get_int_env(name: str, default: int = 0) -> int:
    """
    讀取整數型環境變數；無法轉型時退回預設值

    原本是 `int(os.getenv("DDB_PORT") or "0")`：環境變數被設成任何非數字字串
    （含誤植的空白或註解）都會在 **import 期**拋 ValueError，
    而這個模組被全專案 import，等於整個程式無法啟動且錯誤訊息與設定無關。
    """

    raw: Optional[str] = os.getenv(name)
    if not raw:
        return default

    try:
        return int(raw)
    except ValueError:
        return default


DDB_PATH: str | None = os.getenv("DDB_PATH")
DDB_HOST: str | None = os.getenv("DDB_HOST")
DDB_PORT: int = get_int_env("DDB_PORT")
DDB_USER: str | None = os.getenv("DDB_USER")
DDB_PASSWORD: str | None = os.getenv("DDB_PASSWORD")


# -----------------------------------------------------------------------
# === Shioaji API ===
# -----------------------------------------------------------------------
#
API_KEY: str | None = os.getenv("API_KEY")
API_SECRET_KEY: str | None = os.getenv("API_SECRET_KEY")

# 下單憑證（CA）：正式環境下單前必須 `activate_ca()`，模擬環境預設不啟用。
#
# **憑證檔放專案目錄以外**（例如 `~/.alphaedge/ca/`）：放在 repo 內遲早會被
# `git add -A` 掃進版控，也會被複製進容器映像。這裡只存路徑，檔案本身不歸專案管。
#
# 缺值時為 `None` 而不在 import 期拋出：`core.config` 是全專案的共用入口，
# 沒有憑證的機器（CI、只跑回測的開發機）連 import 都會失敗，
# 錯誤訊息還與憑證無關。真正要下單的地方呼叫 `require_shioaji_ca()`
# （理由同 `schema.require_tick_db_path()`）
SHIOAJI_CA_PATH: Optional[str] = os.getenv("SHIOAJI_CA_PATH")
SHIOAJI_CA_PASSWORD: Optional[str] = os.getenv("SHIOAJI_CA_PASSWORD")

# 身分證字號；一張憑證對應多個帳號時，`activate_ca()` 才需要指定
SHIOAJI_PERSON_ID: Optional[str] = os.getenv("SHIOAJI_PERSON_ID")


def require_shioaji_ca() -> Tuple[Path, str]:
    """
    - Description:
        取得 CA 憑證路徑與密碼；任一未設定、或憑證檔不存在時當場拋出

        **不回傳 None 讓呼叫端自己判斷**：`activate_ca()` 拿到空路徑時的錯誤訊息
        指不到真正的原因，而這是正式下單前的最後一道設定檢查。
    - Return:
        - Tuple[Path, str]
            憑證檔路徑與密碼
    - Raise:
        - RuntimeError
            `.env` 缺少 `SHIOAJI_CA_PATH`／`SHIOAJI_CA_PASSWORD`，或憑證檔不存在
    """

    if not SHIOAJI_CA_PATH or not SHIOAJI_CA_PASSWORD:
        raise RuntimeError(
            "環境變數 SHIOAJI_CA_PATH／SHIOAJI_CA_PASSWORD 未設定，無法啟用下單憑證；"
            "請在 .env 補上（憑證檔請放專案目錄以外，例如 ~/.alphaedge/ca/）"
        )

    ca_path: Path = Path(SHIOAJI_CA_PATH).expanduser().resolve()
    if not ca_path.is_file():
        raise RuntimeError(f"CA 憑證檔不存在：{ca_path}")

    return (ca_path, SHIOAJI_CA_PASSWORD)


# -----------------------------------------------------------------------
# === Live Trading ===
# -----------------------------------------------------------------------
#
# **刻意不提供 `ALPHAEDGE_LIVE_PRODUCTION` 這類環境變數**：正式環境只能由命令列
# `--production --confirm-production` 開啟。放進 `.env` 的旗標會留在機器上，
# 於是某天排程默默連到正式環境下單，而那一刻沒有任何人在看。
#
# 實盤整份流程由時點驅動（08:30 開盤段／13:20 取快照／13:25 送單／15:00 盤後），
# 時間一律用 `Asia/Taipei` 的 aware datetime，**不要用 naive 的 `datetime.now()`**：
# 主機時區是 UTC 而程式讀本地 naive 時間時，尾盤段會整段跑在錯的時刻，
# 而且不會有任何錯誤訊息。容器與排程環境另外設 `TZ=Asia/Taipei`
LIVE_TIMEZONE_NAME: str = "Asia/Taipei"


def get_live_timezone() -> ZoneInfo:
    """
    - Description:
        取得實盤用時區

        **刻意不在模組層級就建好 `ZoneInfo` 物件**：精簡的容器映像沒裝 tzdata 時
        `ZoneInfo()` 會拋 `ZoneInfoNotFoundError`，而本模組被全專案 import，
        等於連回測都啟動不了。
    - Return:
        - ZoneInfo
            台北時區
    """

    return ZoneInfo(LIVE_TIMEZONE_NAME)


def now_live() -> datetime.datetime:
    """取得實盤用的當下時間（台北時區的 aware datetime）"""

    return datetime.datetime.now(tz=get_live_timezone())


# 事件通知管道（Telegram 為第一階段唯一實作的通道）。
#
# 缺值時退化為不推播，但**啟動時要 log 警告並寫進 `live_run`**，不可靜默——
# 「以為有告警其實沒有」比「知道沒有告警」危險：前者會讓人放心把程式丟著跑
LIVE_NOTIFY_CHANNEL: Optional[str] = os.getenv("ALPHAEDGE_LIVE_NOTIFY_CHANNEL")
LIVE_NOTIFY_TOKEN: Optional[str] = os.getenv("ALPHAEDGE_LIVE_NOTIFY_TOKEN")
LIVE_NOTIFY_TARGET: Optional[str] = os.getenv("ALPHAEDGE_LIVE_NOTIFY_TARGET")

# -----------------------------------------------------------------------
# === API list for crawling tick data ===
# -----------------------------------------------------------------------
#
NUM_API: int = 4
API_KEYS: List[Optional[str]] = [os.getenv(f"API_KEY_{i + 1}") for i in range(NUM_API)]
API_SECRET_KEYS: List[Optional[str]] = [
    os.getenv(f"API_SECRET_KEY_{i + 1}") for i in range(NUM_API)
]


# -----------------------------------------------------------------------
# === Logging ===
# -----------------------------------------------------------------------
#
# api 桶的**檔案** sink 只留 WARNING 以上：`core/api/` 每次查詢都寫一行，
# 回測一跑就是數十萬次查詢，`logs/api/` 每天長約 100 MB。
# console 不受影響，開發時照樣看得到 INFO
API_LOG_FILE_LEVEL: str = "WARNING"


# -----------------------------------------------------------------------
# === Backtest report ===
# -----------------------------------------------------------------------
#
# 回測畫完圖要不要在瀏覽器開起來
#
# **預設不開**：`reporter` 有五張圖，舊版寫死 `show=True`，於是每跑一次回測
# 就彈出 5 個分頁；批次跑參數掃描時一次開幾十個，在無頭環境（CI、容器、
# nohup 背景作業）更是直接失敗或卡住。圖本來就會存成 PNG，要看打開檔案即可。
#
# 需要互動式檢視時以 `python run.py --strategy X --show` 或
# `ALPHAEDGE_SHOW_FIGURES=1` 開啟。
SHOW_FIGURES_ENV_VAR: str = "ALPHAEDGE_SHOW_FIGURES"
_TRUTHY_VALUES: frozenset = frozenset({"1", "true", "yes", "on"})


def resolve_show_figures(default: bool = False) -> bool:
    """
    - Description:
        決定回測報表是否在瀏覽器開圖

        只認明確的真值字串，`ALPHAEDGE_SHOW_FIGURES=0`／`false`／空字串一律為否——
        「設了變數就當成開」會讓習慣寫 `VAR=0` 關功能的人踩到反效果。
    - Parameters:
        - default: bool
            環境變數未設定時的預設值
    - Return:
        - bool
            是否開圖
    """

    raw: Optional[str] = os.getenv(SHOW_FIGURES_ENV_VAR)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY_VALUES
