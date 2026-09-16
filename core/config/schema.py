from enum import Enum
from pathlib import Path
from typing import Optional

from .paths import DATABASE_DIR_PATH, get_static_resolved_path
from .settings import DDB_PATH

"""資料庫結構：分庫檔名、完整路徑與資料表名稱"""


# -----------------------------------------------------------------------
# === Database Files Full Paths ===
# -----------------------------------------------------------------------
# 常數名與檔名皆帶市場軸（TW_）：`core/database/` 是按市場分庫，
# 美股進來時會是 us_stock.db，泛用的檔名（stock.db）會失去指向
TW_STOCK_DB_NAME: str = "tw_stock.db"
TICK_DB_NAME: str = "tickDB"

TW_STOCK_DB_PATH: Path = get_static_resolved_path(
    base_dir=DATABASE_DIR_PATH, dir_name=TW_STOCK_DB_NAME
)
# **`DDB_PATH` 沒設定時不可拼成 `"NonetickDB"`**：
# 那是一個看起來像路徑的字串，DolphinDB 會拿它去建一個名字很怪的資料庫，
# 或是查一個永遠不存在的路徑，錯誤訊息完全指不到真正的原因。
#
# 但也**不能在 import 時就 raise**——`core.config` 是全專案的共用入口，
# 沒有 DolphinDB 的機器（CI、容器、只跑回測的開發機）連 import 都會失敗。
# 故缺值時為 `None`，真正要用的地方呼叫 `require_tick_db_path()`。
#
# `DDB_PATH` 取自 `settings`，不在這裡再讀一次環境變數：兩處各讀一次時，
# 「路徑到底從哪來」要查兩個地方，日後改其中一處（例如加預設值）就會分岔
TICK_DB_PATH: Optional[str] = f"{DDB_PATH}{TICK_DB_NAME}" if DDB_PATH else None


def require_tick_db_path() -> str:
    """
    - Description:
        取得 tick DB 路徑；`DDB_PATH` 未設定時當場拋出
    - Return:
        - str
            DolphinDB 的資料庫路徑
    - Raise:
        - RuntimeError
            `.env` 缺少 `DDB_PATH`
    """

    if TICK_DB_PATH is None:
        raise RuntimeError(
            "環境變數 DDB_PATH 未設定，無法決定 tick DB 路徑；"
            "請在 .env 補上（例如 DDB_PATH=dfs://）"
        )
    return TICK_DB_PATH


# 期貨與股票分庫：合約碼（contract_id）與 stock_id 語意不同，混在同一個 DB
# 會讓「這張表的主鍵到底是什麼」失去單一答案
TW_FUTURES_DB_NAME: str = "tw_futures.db"

TW_FUTURES_DB_PATH: Path = get_static_resolved_path(
    base_dir=DATABASE_DIR_PATH, dir_name=TW_FUTURES_DB_NAME
)


# -----------------------------------------------------------------------
# === Database Table names ===
# -----------------------------------------------------------------------
#
PRICE_TABLE_NAME: str = "price"
CHIP_TABLE_NAME: str = "chip"
MARGIN_TABLE_NAME: str = "margin"
DIVIDEND_TABLE_NAME: str = "dividend"
# 非除權息的公司行動（減資／面額變更／分割／反向分割）。
# **刻意與 `dividend` 分表**：`dividend.還原係數` 的語意是「除權息參考價 ÷ 除權息前
# 收盤價」（恆 < 1），而減資的倍率是**大於 1** 的（價格上調），塞進同一欄會讓所有
# 既有讀取端的假設反向。表名不叫 `stock_split`：分割只有 6 筆、減資有 600 多筆
CORPORATE_ACTION_TABLE_NAME: str = "corporate_action"
TICK_TABLE_NAME: str = "tick"
# 期貨 tick **與股票分表**：主鍵不同（期貨要 product ＋ expiry ＋
# session 才能定位一筆成交，股票只要 stock_id），且期貨有夜盤。
# 兩者塞同一張表會讓分割鍵（partition key）失去意義，查詢一律掃全表
FUTURES_TICK_TABLE_NAME: str = "futures_tick"
MONTHLY_REVENUE_TABLE_NAME: str = "monthly_revenue"
BALANCE_SHEET_TABLE_NAME: str = "balance_sheet"
COMPREHENSIVE_INCOME_TABLE_NAME: str = "comprehensive_income"
CASH_FLOW_TABLE_NAME: str = "cash_flow"
EQUITY_CHANGE_TABLE_NAME: str = "equity_change"
STOCK_INFO_TABLE_NAME: str = "taiwan_stock_info"
STOCK_INFO_WITH_WARRANT_TABLE_NAME: str = "taiwan_stock_info_with_warrant"
SECURITIES_TRADER_INFO_TABLE_NAME: str = "taiwan_securities_trader_info"
# 台期貨（皆位於 tw_futures.db）
# **`futures_contract` 的表名常數已刪除**：規劃時預留給股票期貨乘數，
# 後來改走 `futures_stock_universe.contract_size`，於是這個常數變成有宣告、無建表、
# 無 loader、無讀取端——留著只會讓人以為 DB 裡有這張表。
# 指數期貨乘數見程式碼常數 `FUTURES_MULTIPLIER`，股票期貨乘數見
# `futures_stock_universe.contract_size`
FUTURES_PRICE_DAILY_TABLE_NAME: str = "futures_price_daily"  # 各月份合約日 K
FUTURES_CONTINUOUS_TABLE_NAME: str = "futures_continuous"  # 連續合約（換月接續後）
FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME: str = (
    "futures_institutional_chip"  # 三大法人（逐商品、逐身份別）
)
# 大額交易人與 PCR **各自成表**，不與三大法人共用：三者的主鍵不同
# （前者多了到期月份與交易人類別、PCR 一天只有一列），塞同一張表會讓多數欄位
# 永遠是 NULL，且下游得先判斷「這是哪一種籌碼」才知道該讀哪組欄位
FUTURES_LARGE_TRADER_TABLE_NAME: str = "futures_large_trader"  # 大額交易人
FUTURES_PUT_CALL_RATIO_TABLE_NAME: str = "futures_put_call_ratio"  # 選擇權 PCR
# 保證金**分兩張表**：指數類等商品給的是「每口固定金額」，股票期貨給的是
# 「適用比例 ＋ 級距」（每檔標的股價不同，固定金額沒有意義）。硬塞同一張表會讓
# 一半欄位永遠是 NULL，且下游得先判斷「這是哪一類」才知道讀哪一組欄位。
FUTURES_MARGIN_HISTORY_TABLE_NAME: str = (
    "futures_margin_history"  # 保證金歷史序列（每口金額）
)
STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME: str = (
    "stock_futures_margin_rate_history"  # 股票期貨保證金歷史序列（適用比例）
)
FUTURES_STOCK_UNIVERSE_TABLE_NAME: str = "futures_stock_universe"  # 股票期貨標的池

STOCK_TRADING_DAILY_REPORT_TABLE_NAME: str = (
    "taiwan_stock_trading_daily_report_secid_agg"
)


# -----------------------------------------------------------------------
# === Table Column Names ===
# -----------------------------------------------------------------------
# 定義 price 資料表欄位常量（schema 的宣告處為 StockPriceDAO 的 CREATE TABLE）
PRICE_COL_OPEN: str = "開盤價"
PRICE_COL_HIGH: str = "最高價"
PRICE_COL_LOW: str = "最低價"
PRICE_COL_CLOSE: str = "收盤價"
PRICE_COL_SHARES: str = "成交股數"

# 定義 futures_price_daily 資料表欄位常量
# （schema 的宣告處為 futures_price_loader.py 的 CREATE TABLE）
FUTURES_PRICE_COL_OPEN: str = "開盤價"
FUTURES_PRICE_COL_HIGH: str = "最高價"
FUTURES_PRICE_COL_LOW: str = "最低價"
FUTURES_PRICE_COL_CLOSE: str = "收盤價"
FUTURES_PRICE_COL_VOLUME: str = "成交量"  # 單位：口（不是股）
FUTURES_PRICE_COL_SETTLEMENT: str = "結算價"
FUTURES_PRICE_COL_OPEN_INTEREST: str = "未沖銷契約量"

# 定義 chip 資料表欄位常量
CHIP_COL_FOREIGN_NET_SHARES: str = "外資買賣超股數"
CHIP_COL_TRUST_NET_SHARES: str = "投信買賣超股數"
CHIP_COL_DEALER_NET_SHARES: str = "自營商買賣超股數"


class PriceColumn(str, Enum):
    """
    price 資料表的中文欄位名

    **只有資料存取與轉接層（`core/dao/`、`core/api/`、`core/adapters/`）可以引用**：
    欄位名是資料庫 schema 的細節，策略層若直接取用中文字面值，換資料源時會靜默少開倉而非報錯。

    放在 `schema.py` 而不是 `core/pipeline/`：讀取層與 ETL 都要用，放在任一邊都會讓另一邊
    為了幾個字串常數反向相依。
    """

    OPEN = PRICE_COL_OPEN
    HIGH = PRICE_COL_HIGH
    LOW = PRICE_COL_LOW
    CLOSE = PRICE_COL_CLOSE
    SHARES = PRICE_COL_SHARES


class FuturesPriceColumn(str, Enum):
    """
    futures_price_daily 資料表的中文欄位名；引用規則同 `PriceColumn`

    **與 `PriceColumn` 不可互換**：期貨的量欄是 `成交量`（單位為口）而非
    `成交股數`，且 `結算價`／`未沖銷契約量` 在夜盤是 NULL（來源就沒有這兩項）。
    """

    OPEN = FUTURES_PRICE_COL_OPEN
    HIGH = FUTURES_PRICE_COL_HIGH
    LOW = FUTURES_PRICE_COL_LOW
    CLOSE = FUTURES_PRICE_COL_CLOSE
    VOLUME = FUTURES_PRICE_COL_VOLUME
    SETTLEMENT = FUTURES_PRICE_COL_SETTLEMENT
    OPEN_INTEREST = FUTURES_PRICE_COL_OPEN_INTEREST


class ChipColumn(str, Enum):
    """chip 資料表的中文欄位名；引用規則同 PriceColumn"""

    FOREIGN_NET_SHARES = CHIP_COL_FOREIGN_NET_SHARES
    TRUST_NET_SHARES = CHIP_COL_TRUST_NET_SHARES
    DEALER_NET_SHARES = CHIP_COL_DEALER_NET_SHARES
