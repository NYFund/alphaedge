import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 從 .env 載入環境變數。三個模組都各自呼叫一次；load_dotenv() 冪等，重複呼叫無副作用
load_dotenv()

"""
檔案系統佈局：原始碼路徑、執行期產物三根、以及掛在它們底下的所有目錄常數

分界只有一條——**`core/` 是被讀的，`data/`／`results/`／`logs/` 是被寫的**。
判準：**人維護、重跑不變、缺檔會改變行為**的是設定（放 `core/`、進版控）；
**程式寫出、重跑會被覆寫**的是產物（放產物根、不進版控）。
"""


def get_static_resolved_path(base_dir: Path, dir_name: str) -> Path:
    """Resolve dir_name under base_dir to an absolute path"""
    return (base_dir / dir_name).resolve()


def get_env_path(env_key: str, default: Path) -> Path:
    """讀取環境變數指定的目錄；未設定時用預設值（供容器掛載 volume 覆寫）"""

    value: Optional[str] = os.getenv(env_key)
    return Path(value).resolve() if value else default


# -----------------------------------------------------------------------
# Root Directory Path
# -----------------------------------------------------------------------
#
# 兩個錨點的分工是本專案的目錄分界：
# - BASE_DIR_PATH（core/）：**被讀的東西**，即原始碼
# - PROJECT_ROOT（專案根）：執行期產物一律掛在它底下的 data/ results/ logs/
#
# 錨點原本只有 BASE_DIR_PATH，導致產物只能往套件內長——`core/` 是
# `pip install -e .` 安裝的套件，卻累積了 6.4 GB 程式寫入的檔案，
# 逼得 pyproject／ruff／coverage 各維護一份排除清單，`.gitignore`
# 更只能全 repo 封鎖副檔名。
#
# ⚠️ 層數跟著本檔案的位置走：`core/config/paths.py` → parents[1] 才是 `core/`。
# 這裡原本是 `.parent`（當時檔案在 `core/config.py`），拆成套件後多了一層目錄，
# 沒改層數的話 `PROJECT_ROOT` 會變成 `core/`，產物全部退回 `core/data/`——
# **而且不會有任何錯誤**，只會安靜地在錯的地方建目錄。
BASE_DIR_PATH: Path = Path(__file__).resolve().parents[1]
PROJECT_ROOT: Path = BASE_DIR_PATH.parent


# -----------------------------------------------------------------------
# === Runtime Artifact Root Path ===
# -----------------------------------------------------------------------
#
# 三個根分開而不收成一個，是因為**備份策略在三者之間不同**：
# data/db 弄丟是災難（權益變動表的回補是幾十小時的爬蟲）、results 可重跑但想留歷史、
# logs 隨時可刪。收成一個目錄會讓備份規則退化成「備份 X 但排除 X/logs……」。
#
# 目錄**不在此建立**：import 一個設定模組不應該有檔案系統副作用，
# 否則任何只是要讀常數的測試都會被動生出這些目錄。
# 實際要寫檔時由呼叫端惰性建立（例如 `LogManager.setup_logger()`，見 log_manager.py）
DATA_DIR_PATH: Path = get_env_path("ALPHAEDGE_DATA_DIR", PROJECT_ROOT / "data")
RESULTS_DIR_PATH: Path = get_env_path("ALPHAEDGE_RESULTS_DIR", PROJECT_ROOT / "results")
LOGS_DIR_PATH: Path = get_env_path("ALPHAEDGE_LOGS_DIR", PROJECT_ROOT / "logs")


# -----------------------------------------------------------------------
# === General Directory Path ===
# -----------------------------------------------------------------------
#
DATABASE_DIR_PATH: Path = get_static_resolved_path(
    base_dir=DATA_DIR_PATH, dir_name="db"
)


# -----------------------------------------------------------------------
# === Log Directory Path ===
# -----------------------------------------------------------------------
#
# 依**產生者**分三桶，而不是全部平鋪（原本 258 個檔擠在同一層）。
# 分三桶而非兩桶的理由：`core/api/` 的查詢日誌檔數最多且是純雜訊，
# 與「會回頭讀」的 pipeline 日誌（例如回補的 N requested／N no data／
# N unreachable 統計行）價值完全不同，隔開才能整桶刪掉 api/ 而不誤傷。
API_LOGS_DIR_PATH: Path = get_static_resolved_path(
    base_dir=LOGS_DIR_PATH, dir_name="api"
)
PIPELINE_LOGS_DIR_PATH: Path = get_static_resolved_path(
    base_dir=LOGS_DIR_PATH, dir_name="pipeline"
)
BACKTEST_LOGS_DIR_PATH: Path = get_static_resolved_path(
    base_dir=LOGS_DIR_PATH, dir_name="backtest"
)


# -----------------------------------------------------------------------
# === Backtest Result Directory Path ===
# -----------------------------------------------------------------------
#
# 只放「要給人看的產出」（CSV ＋ PNG）。回測日誌不放這裡——產出與日誌混放
# 正是原本會長出第二棵日誌樹（backtest/results/logs/）的原因
BACKTEST_RESULT_DIR_PATH: Path = RESULTS_DIR_PATH

# -----------------------------------------------------------------------
# === Crawl Data Downloads Path ===
# -----------------------------------------------------------------------
#
# 名為 downloads 但**不只有下載檔**：FinMind 的 cleaner 也把清洗後的 CSV
# 寫進 finmind/，所以嚴格說是「ETL 中繼檔」。名稱維持不改——這個詞在程式與
# 文件裡已經用開，為精確度改名的擴散成本不划算
PIPELINE_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=DATA_DIR_PATH, dir_name="downloads"
)

# 中繼檔依「市場」分層，與 data/db/ 的 tw_stock.db／tw_futures.db、程式碼的 `tw/` 層
# 同一個維度。用單層 `tw_stock` 而非 `tw/stock` 兩層：市場 × 商品只會出現實際支援的
# 組合，多一層只裝兩個項目純屬導覽成本
TW_STOCK_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=PIPELINE_DOWNLOADS_PATH, dir_name="tw_stock"
)
TW_FUTURES_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=PIPELINE_DOWNLOADS_PATH, dir_name="tw_futures"
)
FINANCIAL_STATEMENT_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="financial_statement"
)
MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="monthly_revenue_report"
)
PRICE_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="price"
)
CHIP_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="chip"
)
MARGIN_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="margin"
)
DIVIDEND_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="dividend"
)
CORPORATE_ACTION_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="corporate_action"
)
TICK_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="tick"
)
FINMIND_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="finmind"
)

# 台期貨中繼檔；目錄與 tw_stock 同構，只是資料類型不同
FUTURES_PRICE_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="price"
)
FUTURES_CHIP_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="chip"
)
FUTURES_CONTINUOUS_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="continuous"
)
FUTURES_UNIVERSE_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="universe"
)
FUTURES_TICK_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="tick"
)
FUTURES_MARGIN_DOWNLOADS_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="margin"
)

# -----------------------------------------------------------------------
# === Cleaner Schema Path（欄位定義：設定，不是產物）===
# -----------------------------------------------------------------------
#
# 欄位對照表由人維護、被 cleaner 讀取，缺檔只會 warning 後**靜默降級清洗**，
# 因此必須進版控。原本混在 `downloads/tw_stock/meta/` 裡，
# 產物目錄一納入 `.gitignore` 就會整批掉出版控——這是搬遷時才暴露出來的既有錯置。
#
# 掛 BASE_DIR_PATH（core/）而不是產物根：小、唯讀、隨套件發佈，屬 package data
CLEANER_SCHEMA_DIR_PATH: Path = get_static_resolved_path(
    base_dir=BASE_DIR_PATH, dir_name="pipeline/tw/cleaners/schema"
)
FINANCIAL_STATEMENT_META_DIR_PATH: Path = get_static_resolved_path(
    base_dir=CLEANER_SCHEMA_DIR_PATH, dir_name="financial_statement"
)
MONTHLY_REVENUE_REPORT_META_DIR_PATH: Path = get_static_resolved_path(
    base_dir=CLEANER_SCHEMA_DIR_PATH, dir_name="monthly_revenue_report"
)


# -----------------------------------------------------------------------
# === Crawler Resume State Path（爬蟲進度：產物）===
# -----------------------------------------------------------------------
#
# 與上面相反：這些是「爬到哪了」的執行期狀態，重跑會被覆寫，不進版控
DOWNLOADS_METADATA_DIR_PATH: Path = get_static_resolved_path(
    base_dir=TW_STOCK_DOWNLOADS_PATH, dir_name="meta"
)
TICK_METADATA_DIR_PATH: Path = get_static_resolved_path(
    base_dir=DOWNLOADS_METADATA_DIR_PATH, dir_name="tick"
)
TICK_METADATA_PATH: Path = get_static_resolved_path(
    base_dir=TICK_METADATA_DIR_PATH, dir_name="tick_metadata.json"
)
BROKER_TRADING_METADATA_DIR_PATH: Path = get_static_resolved_path(
    base_dir=DOWNLOADS_METADATA_DIR_PATH, dir_name="broker_trading"
)
FUTURES_METADATA_DIR_PATH: Path = get_static_resolved_path(
    base_dir=TW_FUTURES_DOWNLOADS_PATH, dir_name="meta"
)
BROKER_TRADING_METADATA_PATH: Path = get_static_resolved_path(
    base_dir=BROKER_TRADING_METADATA_DIR_PATH, dir_name="broker_trading_metadata.json"
)
FUTURES_MARGIN_ANNOUNCEMENT_METADATA_PATH: Path = get_static_resolved_path(
    base_dir=FUTURES_METADATA_DIR_PATH, dir_name="futures_margin_announcements.json"
)
