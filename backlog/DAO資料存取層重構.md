# DAO 資料存取層重構

## Abstract

- **背景／問題**：資料庫存取散在 `core/api`、`core/pipeline`、回測 DataFeed 與工具腳本。2026-09-16 全專案掃描的數字：
  - `core/` 內 44 處 `sqlite3.connect`；`core/`、`tasks/`、`scripts/` 共 54 個檔案 `import sqlite3`。
  - `core/api` 有 43 次 `pd.read_sql_query`、16 次 `execute().fetchone()`；pipeline 另有 6 份手寫 `INSERT`、3 份「表是否存在」的實作。
  - 全專案沒有任何 `rollback`。
  
  結構上的後果：
  - 10 支 updater 與自己的 loader 各開一條連線連到同一個 DB，updater 那條從不關閉。
  - 同一個查詢在 API 與 pipeline 各寫一份，語意已經分岔（保證金生效日 `<` 與 `<=`、`get_contract_size` 的 fallback 不同）。
  - `core/api` 為了欄位 Enum 與 `SQLiteUtils` 反向 import `core/pipeline`。
- **目標**：新增 `core/dao/`，一張表（或一組緊密相關的表）一個 DAO，集中建表、寫入、查詢與交易控制。
  - `core/api` 保留公開介面與業務邏輯，只把 SQL 移進 DAO。
  - loader／updater 共用同一個 DAO，一次執行中同一個 DB 只開一條連線。
  - PostgreSQL 遷移之後只需要改 `core/dao/` 內部。
- **範圍界線**：
  - **不改**資料表 schema、欄位語意與 API 公開介面；表名補 `stock_` 前綴、`stock_id → symbol` 仍屬 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)。
  - **不引入** SQLAlchemy，DAO 內部先用 `sqlite3`。
  - **不改**策略與回測引擎；爬蟲、清洗器（`corporate_action_detector` 的 DB 讀取除外）也不動。
  - tick 的 DAO 由 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 實作。
- **驗收標準**：
  1. `core/dao/` 以外的 `core/`、`tasks/` 不再 `import sqlite3`，由分層檢查腳本強制。
  2. `./scripts/run_regression.sh` 回歸雙線逐筆相同。
  3. `pytest -m "not slow"` 全數通過。
  4. 掃描時發現的連線洩漏與吞錯誤問題，在各批次修掉並有測試。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | 欄位 Enum 下沉到 `core/config/schema.py` | `core/config/schema.py`、`core/pipeline/utils/constant.py`、引用端 | `core/api` 不再 import `core.pipeline.utils.constant`；`pytest` 通過 | ✅ | 2026-09-16 完成：三個 Enum 與欄名常數搬進 `schema.py`，12 個引用端（含 6 個測試）改 import，`pipeline/utils/__init__.py` 不再 re-export；原屬 PostgreSQL遷移計畫 Phase2-3，改由本文件提前做 |
| Phase0-2 | `core/dao` 登記分層 | `scripts/check_layer_deps.py` | `python scripts/check_layer_deps.py` 違規 0 | ✅ | 2026-09-16 完成：違規 0；第 2 層：高於 config／utils，低於 api／pipeline |
| Phase1-1 | 建立 `core/dao/` 底座 | `core/dao/__init__.py`、`core/dao/connection.py`、`core/dao/base.py` | 新增 `tests/test_dao_base.py` 通過 | ✅ | 2026-09-16 完成：`tests/test_dao_base.py` 11 項通過；`_get_latest_value` 設為受保護方法（偏離原規格，見〈設計〉）；含 savepoint（單檔失敗整檔回滾） |
| Phase1-2 | 三份重複實作改委派給 DAO 底座 | `core/api/base.py`、`core/pipeline/utils/sqlite_utils.py`、`core/pipeline/shared/base_loader.py` | 既有 `tests/test_sqlite_error_semantics.py` 等通過 | ✅ | 2026-09-16 完成：四個方法改一行委派，既有測試全過；表存在檢查、`sql_params`、`insert_dataframe`、`create_symbol_date_index` |
| Phase2-1 | 試點：`StockPriceDAO` | `core/dao/tw/stock_price_dao.py` | `tests/test_dao_stock_price.py` 通過 | ✅ | 2026-09-16 完成：`tests/test_dao_stock_price.py` 內 DAO 部分 5 項通過；相依 Phase1-1 |
| Phase2-2 | 試點：`StockPriceAPI` 改走 DAO | `core/api/tw/stock_price_api.py` | `tests/test_stock_data_api.py`、`tests/test_adjusted_price_api.py` 通過 | ✅ | 2026-09-16 完成：連線仍由 API 持有、以 `conn=` 交給 DAO（見〈設計〉）；相關 70 項測試通過；相依 Phase2-1 |
| Phase2-3 | 試點：`StockPriceLoader`／`StockPriceUpdater` 改走 DAO | `core/pipeline/tw/loaders/stock_price_loader.py`、`core/pipeline/tw/updaters/stock_price_updater.py`、`tasks/update_db.py` | 新增單一連線、壞檔回滾測試；`tests/test_loader_failure_reporting.py` 等通過 | ✅ | 2026-09-16 完成：新增共用連線、共用 DAO 不被關閉、寫到一半失敗整檔回滾 3 項測試；相依 Phase2-1 |
| Phase2-4 | 試點：`corporate_action_detector` 的 price 讀取改走 DAO | `core/pipeline/tw/cleaners/corporate_action_detector.py` | `tests/test_corporate_action.py` 通過 | ✅ | 2026-09-16 完成：唯讀連線改走 `connect_sqlite`；相依 Phase2-1；dividend／corporate_action 的讀取留到 Phase3-2 |
| Phase2-5 | 試點回歸驗證 | — | `./scripts/run_regression.sh` 雙線通過；`pytest -m "not slow"` 通過 | ✅ | 2026-09-16 完成：回歸雙線通過（SHORT 6、LONG 1）、`pytest -m "not slow"` 1061 passed、`-m slow` 19 passed、分層違規 0；相依 Phase2-2~Phase2-4；**通過後才推廣** |
| Phase3-1 | 推廣：chip、margin（含 `DatePlanner` 改吃 DAO） | `core/dao/tw/`、對應 API／loader／updater、`core/pipeline/shared/date_planner.py` | 對應測試＋回歸 | ✅ | 2026-09-16 完成：新增 `StockChipDAO`／`StockMarginDAO`；`DatePlanner` 改收 DAO，三支日頻 updater 以共用連線建日曆 DAO；`tests/test_dao_stock_chip_margin.py` 11 項、`pytest -m "not slow"` 1072 passed、回歸雙線通過、分層違規 0；相依 Phase2-5 |
| Phase3-2 | 推廣：dividend、corporate_action | 同上 | 對應測試＋回歸 | ✅ | 2026-09-16 完成：新增 `StockDividendDAO`／`CorporateActionDAO` 與底座 `insert_or_replace`；去重收斂到 `core/pipeline/shared/source_priority.py`；`tests/test_dao_stock_dividend_corporate_action.py` 9 項、`not slow` 1081 passed、`slow` 19 passed、回歸雙線通過、分層違規 0 |
| Phase3-3 | 推廣：monthly_revenue | 同上 | 對應測試 | ⬜ | 相依 Phase2-5；修 `get_range` 跨年查詢錯誤 |
| Phase3-4 | 推廣：財報四表 | 同上 | 對應測試 | ⬜ | 相依 Phase2-5；股票清單查詢與 FinMind 共用 |
| Phase4-1 | 推廣：FinMind 四表 | `core/dao/tw/`、`core/api/tw/finmind_api.py`、`core/pipeline/tw/loaders/finmind/**`、FinMind updater | 對應測試 | ⬜ | 相依 Phase2-5；驗證 `commit=False` 是否被 `to_sql` 蓋掉；`common.py` 吞錯誤 |
| Phase5-1 | 推廣：futures_price | `core/dao/tw/`、對應 API／loader／updater | 對應測試＋期貨回測測試 | ⬜ | 相依 Phase2-5；修 `with sqlite3.connect` 洩漏與 `sqlite3.Error` 被吞 |
| Phase5-2 | 推廣：futures_stock_universe | 同上 | 對應測試 | ⬜ | 相依 Phase5-1；收斂重複的快照查詢、`get_contract_size` |
| Phase5-3 | 推廣：futures_margin 兩表 | 同上、`core/managers/futures/position_manager.py` | 對應測試 | ⬜ | 相依 Phase5-2；統一 `<`／`<=`；修 `FuturesMarginConfig.from_api()` 暗開連線 |
| Phase5-4 | 推廣：futures_chip 三表、futures_continuous | 同上 | 對應測試 | ⬜ | 相依 Phase5-1；`FuturesChipAPI` 的 `table=` 補白名單 |
| Phase6-1 | 測試共用 DAO fixture | `tests/conftest.py`、直接 `sqlite3.connect` 的測試檔 | `pytest` 通過 | ⬜ | 相依 Phase3~Phase5 |
| Phase6-2 | 收斂：刪除舊工具、分層檢查禁止 DAO 以外 `import sqlite3` | `core/pipeline/utils/sqlite_utils.py`、`core/api/base.py`、`scripts/check_layer_deps.py`、`tasks/delete_price_data.py`、`strategy_lab/**`、`scripts/manual/*` | 分層檢查違規 0；全域 `grep "import sqlite3"` 只剩 `core/dao/` 與測試 | ⬜ | 相依 Phase6-1 |
| Phase6-3 | 更新文件 | `docs/backtest/module-map.md`、`docs/pipeline/etl-ingestion.md`、`docs/dev/naming-axes.md` | 文件描述與程式一致 | ⬜ | 相依 Phase6-2 |

---

## 掃描發現（2026-09-16）

### 連線與交易

| 問題 | 位置 | 處理步驟 |
|------|------|----------|
| updater 自開一條連線、loader 再開一條到同一個 DB；updater 那條從不關閉 | `stock_price／chip／margin／dividend／corporate_action／monthly_revenue_report／financial_statement／finmind／futures_price／futures_margin_updater.py` 的 `setup()` | Phase2-3、Phase3、Phase4、Phase5 |
| `with sqlite3.connect(...)` 只 commit 不關閉，連線洩漏 | `futures_price_updater.py:166` | Phase5-1 |
| 沒傳 `api` 時暗開一條 `FuturesMarginAPI()` 連線，沒人關 | `core/managers/futures/position_manager.py:104` | Phase5-3 |
| `StockTickAPI` 的 DolphinDB session 從不關閉 | `core/api/tw/stock_tick_api.py` | 由 TimescaleDB 計畫處理 |
| 全專案無 `rollback`：單檔寫到一半失敗時，前面已寫入的列會被最後的 `commit()` 一起寫進去 | 各檔案型 loader 的 `add_to_db()` | Phase1-1 提供 savepoint，各批次套用 |
| `to_sql` 可能自行 commit，使 `commit=False` 失效（未驗證） | `broker_trading_loader.py` | Phase4-1 |

### 語意分岔與錯誤處理

| 問題 | 位置 | 處理步驟 |
|------|------|----------|
| 查最新日期失敗時吞掉 `sqlite3.Error`，改從預設起日重跑 | `futures_price_updater.py:130` | Phase5-1 |
| 查股票清單失敗時 `except Exception` 回空集合 | `financial_statement_updater.py:1126,1147`、`finmind/common.py:202,215` | Phase3-4、Phase4-1 |
| 「當日生效保證金」updater 用 `<`、API 用 `<=` | `futures_margin_updater.py:393` vs `futures_margin_api.py:106` | Phase5-3 |
| `get_contract_size` 兩份實作，fallback 不同 | `futures_margin_api.py:221` vs `futures_stock_universe_api.py:140` | Phase5-2 |
| 年、月各自 `BETWEEN`，跨年區間查錯（例如 2023-11～2024-02） | `monthly_revenue_report_api.py` `get_range` | Phase3-3 |
| `table=` 參數沒有白名單就組進 SQL | `futures_chip_api.py` | Phase5-4 |
| 交易日查詢寫了四份 | `stock_price_api.py:143`、`date_planner.py:215`、`futures_price_updater.py:168`、`corporate_action_detector.py:92` | Phase2、Phase3-1、Phase5-1 |
| `dedup_by_source_priority` 兩份實作 | `stock_dividend_loader.py`、`corporate_action_loader.py` | Phase3-2 |
| 無呼叫端的方法 | `futures_stock_universe_updater.get_active_products`、`SQLiteUtils.get_table_earliest_value` | Phase5-2、Phase6-2 |

### 分層

- `core/api` → `core/pipeline`：5 處，分別是 `PriceColumn`／`ChipColumn`／`FuturesPriceColumn`／`SQLiteUtils`×2。另有 `core/adapters/tw/futures_quote_adapter.py` 與 `core/backtest/report/futures_reporter.py` import 欄位 Enum。
- `core/pipeline` → `core/api`：4 支 updater 持有 API 物件。
- 兩個方向在 `check_layer_deps.py` 都屬「同層不同套件」，只列出、不擋。

---

## 設計

### 分層位置

```text
core.config (0) ← core.utils (1) ← core.dao (2) ← core.api / core.pipeline (3) ← ...
```

- `core/dao` 只 import `core.config`，**不 import `core.utils`**（`core.utils.instrument` 已知會反向拉進回測層）、`core.models`、`core.api`、`core.pipeline`。
- 目錄比照 `core/api`：市場軸 `core/dao/tw/`，登記進分層檢查腳本的 `_MARKET_AXIS_PACKAGES`。
- 本文件取代 PostgreSQL 遷移計畫原定的 `core/db/`。那份計畫的 Phase1-1（連線入口）改在 `core/dao/connection.py` 實作；TimescaleDB 計畫的 `core/db/timescale.py` 同樣改放 `core/dao/`。

### 底座 API

```python
# core/dao/connection.py
def connect_sqlite(db_path: Union[str, Path], read_only: bool = False) -> sqlite3.Connection

# core/dao/base.py
def to_sql_params(*values: Any) -> Tuple[Any, ...]
def table_exists(conn: sqlite3.Connection, table_name: str) -> bool
def insert_or_ignore(conn: sqlite3.Connection, table_name: str, df: pd.DataFrame) -> Tuple[int, int]
def create_symbol_date_index(conn: sqlite3.Connection, table_name: str) -> None

class BaseDAO:
    TABLE_NAME: str
    DEFAULT_DB_PATH: Path

    def __init__(self, conn: Optional[sqlite3.Connection] = None, db_path: Optional[Union[str, Path]] = None) -> None
    def table_exists(self) -> bool
    def query_df(self, sql: str, params: Tuple[Any, ...] = ()) -> pd.DataFrame
    def fetch_one(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Tuple[Any, ...]]
    def insert_or_ignore(self, df: pd.DataFrame) -> Tuple[int, int]
    def get_distinct_dates(self, start_date, end_date) -> List[datetime.date]
    def _get_latest_value(self, col_name: str) -> Optional[Any]            # 受保護：只給子類別以自宣告欄名呼叫
    @contextmanager
    def savepoint(self, name: str = "dao_write") -> Iterator[None]   # 例外時 ROLLBACK TO，不影響同一交易內先前已完成的檔案
    def commit(self) -> None
    def close(self) -> None                                          # 只關自己開的連線（owns_conn）
```

- **連線所有權沿用 `owns_conn` 慣例**：傳入 `conn` 就不擁有、`close()` 不關。和 `BaseDataAPI`、DataFeed 的現行約定一致。
- **`db_path` 參數保留在建構子**：既有測試以 `monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", ...)` 改寫路徑，loader 在呼叫時把模組層級的路徑傳進 DAO，測試不用改。
- **表名與欄名不從參數組 SQL**：`TABLE_NAME` 是類別常數；以欄名為參數的 `_get_latest_value()` 設為受保護方法，只由子類別以自己宣告的欄名呼叫（例如 `StockPriceDAO.get_latest_date()`）。
- **欄名不加雙引號**（2026-09-16 試點時由測試發現）：SQLite 遇到雙引號包住、卻不存在的欄名，會退回當成字串字面值，欄名打錯時查詢不報錯、回傳欄名字串本身。組 SQL 時欄名一律不加雙引號；`INSERT` 的欄位清單例外（欄位清單中的雙引號只能是識別字，不會退化）。
- **API 持有連線、DAO 不持有**（2026-09-16 試點定案）：API 仍依 `owns_conn` 自行開連線，再以 `conn=` 交給 DAO。這樣 `BaseDataAPI.close()`、DataFeed 的共用連線與 reporter 的關閉邏輯都不用改；loader／updater 則由 updater 持有 DAO、loader 共用。

### 各層分工

| 層 | 負責 | 不負責 |
|----|------|--------|
| DAO | 建表與索引、寫入、查詢、交易（savepoint／commit）、表不存在時的回傳約定 | 業務規則（還原係數、保證金公式、流動性排名）、日誌檔設定 |
| API | 公開查詢介面、業務規則、組 map／Series | SQL、連線生命週期細節 |
| loader | 讀 CSV、檔內去重、逐檔彙報（`finish_load`） | SQL、建表 DDL |
| updater | 決定日期、爬取、清洗、分批入庫、持有並關閉 DAO | SQL |

---

## Phase 0：前置

### Phase0-1. 欄位 Enum 下沉到 `core/config/schema.py` ✅

- **目的**：DAO 與 API 都要用欄名，但不能 import `core/pipeline`。
- **做法**：
  - 把 `core/pipeline/utils/constant.py` 的 `PRICE_COL_*`／`FUTURES_PRICE_COL_*`／`CHIP_COL_*` 與 `PriceColumn`／`FuturesPriceColumn`／`ChipColumn` 搬到 `core/config/schema.py`，放在新的 `=== Table Column Names ===` 區塊。
  - 引用端一律改成 `from core.config.schema import ...`（`core/config/__init__.py` 已 star import，`from core.config import PriceColumn` 也可用）。
  - `core/pipeline/utils/__init__.py` 不再 re-export 這三個 Enum。
  - `PriceColumn` docstring 裡的「只有 `core/api/` 可以引用」改為「只有 `core/dao`、`core/api`、`core/adapters` 可以引用」，與 `tests/test_strategy_data_access.py` 的規則一致。
- **產出**：`core/config/schema.py`、`core/pipeline/utils/constant.py`、`core/pipeline/utils/__init__.py`、`core/api/tw/stock_price_api.py`、`stock_chip_api.py`、`futures_price_api.py`、`core/adapters/tw/futures_quote_adapter.py`、`core/backtest/report/futures_reporter.py`、`core/pipeline/tw/updaters/futures_continuous_updater.py`，以及引用這些 Enum 的測試。
- **驗證方式**：`grep -rn "pipeline.utils.constant import.*Column\|pipeline.utils import.*Column" core tests` 無結果；`pytest -m "not slow"` 通過。
- **相依**：無。

### Phase0-2. `core/dao` 登記分層 ✅

- **目的**：讓分層檢查從第一天起就擋住 DAO 往上 import。
- **做法**：`_LAYER_RULES` 新增 `("core.dao", 2, "資料存取層（DAO）", False)`；`_MARKET_AXIS_PACKAGES` 加入 `"core/dao"`。
- **產出**：`scripts/check_layer_deps.py`。
- **驗證方式**：`python scripts/check_layer_deps.py` 違規總數 0。
- **相依**：無。

---

## Phase 1：底座

### Phase1-1. 建立 `core/dao/` 底座 ✅

- **目的**：提供所有 DAO 共用的連線、查詢、寫入與交易控制。
- **做法**：依〈底座 API〉實作。`savepoint()` 的做法：
  1. 若連線不在交易中，先執行 `BEGIN`。否則 Python sqlite3 在執行 `SAVEPOINT` 時不會自動開交易，最外層的 `RELEASE` 會直接 commit。
  2. 執行 `SAVEPOINT {name}`。
  3. 區塊正常結束時 `RELEASE {name}`；拋出例外時先 `ROLLBACK TO {name}`、再 `RELEASE {name}`，然後把例外往外拋。
- **產出**：`core/dao/__init__.py`、`core/dao/connection.py`、`core/dao/base.py`、`tests/test_dao_base.py`。
- **驗證方式**：`tests/test_dao_base.py` 涵蓋：
  - `owns_conn` 語意（傳入的連線 `close()` 不關）
  - `table_exists` 分辨表存在與不存在
  - `insert_or_ignore` 的寫入與跳過列數
  - savepoint 中途拋錯時，該區塊寫入的列全部回滾，但同一交易內先前區塊的列 commit 後仍在
  - `get_latest_value` 在表不存在或表為空時回 `None`，查詢錯誤時往外拋
- **相依**：Phase0-2。

### Phase1-2. 三份重複實作改委派給 DAO 底座 ✅

- **目的**：先消除重複，不改呼叫端。
- **做法**：
  - `BaseDataAPI.sql_params` → `core.dao.base.to_sql_params`
  - `BaseDataAPI.check_table_exist`、`SQLiteUtils.check_table_exist` → `core.dao.base.table_exists`
  - `BaseDataLoader.insert_dataframe` → `core.dao.base.insert_or_ignore`
  - `BaseDataLoader.create_symbol_date_index` → `core.dao.base.create_symbol_date_index`
  
  原方法保留成一行委派，到 Phase6-2 呼叫端都改完後再刪除。docstring 裡「兩邊各有一份是刻意的」的說明同步改寫。
- **產出**：`core/api/base.py`、`core/pipeline/utils/sqlite_utils.py`、`core/pipeline/shared/base_loader.py`。
- **驗證方式**：`pytest tests/test_sqlite_error_semantics.py tests/test_loader_failure_reporting.py` 通過。
- **相依**：Phase1-1。

---

## Phase 2：試點（`price` 表）

### Phase2-1. `StockPriceDAO` ✅

- **目的**：`price` 表的所有 SQL 集中一處。
- **做法**：`core/dao/tw/stock_price_dao.py`，`TABLE_NAME = PRICE_TABLE_NAME`、`DEFAULT_DB_PATH = TW_STOCK_DB_PATH`：
  - `ensure_table() -> None`：`CREATE TABLE IF NOT EXISTS`（DDL 從 `StockPriceLoader.create_db()` 原樣搬過來），加上 `(stock_id, date)` 索引，然後 commit。
  - `get_by_date(date) -> pd.DataFrame`、`get_range(start_date, end_date) -> pd.DataFrame`、`get_by_stock(stock_id, start_date, end_date) -> pd.DataFrame`：SQL 與現行 API 相同。
  - `get_trading_days(start_date, end_date) -> List[datetime.date]`
  - `get_close_prices(start_date: Optional[datetime.date] = None) -> pd.DataFrame`：給 `corporate_action_detector` 用。
  - `get_latest_date() -> Optional[str]`
- **產出**：`core/dao/tw/__init__.py`、`core/dao/tw/stock_price_dao.py`、`tests/test_dao_stock_price.py`。
- **驗證方式**：`tests/test_dao_stock_price.py` 用 `:memory:` 驗證建表冪等、各查詢的區間邊界、表不存在時 `get_latest_date()` 回 `None`。
- **相依**：Phase1-1。

### Phase2-2. `StockPriceAPI` 改走 DAO ✅

- **目的**：API 不再持有 SQL。
- **做法**：`setup()` 建立 `self.dao = StockPriceDAO(conn=conn)`，`self.conn = self.dao.conn`。保留 `conn`／`owns_conn` 屬性：DataFeed、reporter、`strategy_lab` 的 `price_api.conn` 都還在用。
  - `get`／`get_range`／`get_stock_price`／`get_trading_days` 改呼叫 DAO，其餘業務方法不動。
  - 移除 `import sqlite3` 以外的 SQL；`conn` 的型別標註仍需要 `sqlite3`，保留 import，到 Phase6-2 改成 DAO 型別。
- **產出**：`core/api/tw/stock_price_api.py`。
- **驗證方式**：`pytest tests/test_stock_data_api.py tests/test_adjusted_price_api.py tests/backtest/test_reporting.py tests/backtest/test_market_calendar_bounds.py` 通過。
- **相依**：Phase2-1。

### Phase2-3. `StockPriceLoader`／`StockPriceUpdater` 改走 DAO ✅

- **目的**：一次 price 更新只開一條連線；壞檔不留下半份資料。
- **做法**：
  - `StockPriceLoader.__init__(self, dao: Optional[StockPriceDAO] = None)`：
    - `connect()`：沒有 DAO 時建立 `StockPriceDAO(db_path=TW_STOCK_DB_PATH)`，由 loader 擁有。
    - `disconnect()`：只關自己擁有的 DAO。
    - `create_db()`／`create_missing_tables()` → `dao.ensure_table()`。
    - `add_to_db()`：每個檔案包在 `with self.dao.savepoint()` 內，迴圈結束後 `dao.commit()`。保留 `self.conn` 屬性。
  - `StockPriceUpdater.setup()`：建立 `self.dao = StockPriceDAO()`，`self.conn = self.dao.conn`，並以 `StockPriceLoader(dao=self.dao)` 共用同一條連線。
    - 最新日期改呼叫 `self.dao.get_latest_date()`；`DatePlanner` 仍收 `self.conn`，Phase3-1 再改。
    - 新增 `close()`，`tasks/update_db.py` 的 price 分支改成 `try/finally: stock_price_updater.close()`。
- **產出**：`core/pipeline/tw/loaders/stock_price_loader.py`、`core/pipeline/tw/updaters/stock_price_updater.py`、`tasks/update_db.py`。
- **驗證方式**：
  - 新增測試：updater 與其 loader 持有同一條連線；loader 在共用 DAO 下 `add_to_db()` 後連線仍可用；一個中途才出錯的 CSV 不留下任何列，其他檔案照常入庫並拋 `DataLoadError`。
  - `pytest tests/test_loader_failure_reporting.py tests/test_batched_loading.py tests/test_partial_market_guard.py tests/test_date_gap_backfill.py tests/backtest/test_market_calendar_bounds.py` 通過。
- **相依**：Phase2-1。

### Phase2-4. `corporate_action_detector` 的 price 讀取改走 DAO ✅

- **目的**：交易日相關的 price 查詢少一份。
- **做法**：`detect_unexplained_moves()` 讀 price 的那段改成 `StockPriceDAO(conn=conn).get_close_prices(start_date)`；唯讀連線改用 `connect_sqlite(TW_STOCK_DB_PATH, read_only=True)`。`_drop_explained()` 讀 dividend／corporate_action 的部分留到 Phase3-2。
- **產出**：`core/pipeline/tw/cleaners/corporate_action_detector.py`。
- **驗證方式**：`pytest tests/test_corporate_action.py` 通過。
- **相依**：Phase2-1。

### Phase2-5. 試點回歸驗證 ✅

- **目的**：確認試點沒有改變任何行為，再決定推廣。
- **做法**：跑 `./scripts/run_regression.sh`、`pytest -m "not slow"`、`python scripts/check_layer_deps.py`。另外人工檢查試點後的樣式是否順手，若要調整底座 API 在這一步定案，並回寫〈設計〉。
- **產出**：本步驟末尾的驗證紀錄。
- **驗證方式**：三者皆通過。
- **相依**：Phase2-2~Phase2-4。

> **驗證紀錄（2026-09-16）**
> - `./scripts/run_regression.sh`：SHORT 6 passed、LONG 1 passed，無 skip。
> - `pytest -m "not slow"`：1061 passed（改動前 1042，新增 `test_dao_base.py` 11 項、`test_dao_stock_price.py` 8 項）。
> - `pytest -m slow`：19 passed（含連正式 DB 的交易日曆與公司行動護欄）。
> - `python scripts/check_layer_deps.py`：違規 0。
> - 樣式檢討：底座 API 大致順手，定案兩點寫回〈設計〉：欄名不加雙引號、API 持有連線再交給 DAO。
>   另一個已知的過渡寫法是 `StockPriceUpdater.update()` 就地以 `self.conn` 建 DAO 查最新日期，
>   原因是既有測試用 `__new__` 只注入 `conn`；Phase3-1 改寫 `DatePlanner` 時一併把這些測試改成注入 DAO。

---

## Phase 3～5：推廣

各步驟的做法與 Phase2 相同：新增 DAO → API 改走 DAO → loader／updater 共用 DAO 並加 savepoint → 修該批次列在〈掃描發現〉的問題。以下只列各批次特有的事項。

### Phase3-1. chip、margin ✅

- **做法**：新增 `StockChipDAO`、`StockMarginDAO`。`DatePlanner` 的 `get_existing_dates`／`get_trading_dates`／`get_weekend_dates`／`plan` 改為接收 DAO（`BaseDAO.get_distinct_dates()`），不再接收 `conn` 與表名，`StockPriceUpdater` 同步改寫。`stock_margin_api.py` 對 `SQLiteUtils` 的 import 移除。
- **產出**：`core/dao/tw/stock_chip_dao.py`、`stock_margin_dao.py`、`core/api/tw/stock_chip_api.py`、`stock_margin_api.py`、兩支 loader、兩支 updater、`core/pipeline/shared/date_planner.py`。
- **驗證方式**：`pytest tests/test_date_gap_backfill.py tests/test_partial_market_guard.py tests/test_batched_loading.py tests/test_api_public_interfaces.py` 通過；回歸雙線通過。
- **相依**：Phase2-5。

> **完成紀錄（2026-09-16）**
> - `DatePlanner.get_existing_dates`／`get_trading_dates`／`get_weekend_dates`／`plan` 改收 DAO；
>   日曆來源（`price`）與週末來源（`chip`／`margin`）由 updater 以自己的連線就地建 DAO，不另開連線。
> - Phase2-5 記下的過渡寫法已移除：`StockPriceUpdater.update()` 改用 `self.dao`，
>   `test_date_gap_backfill.py`、`test_partial_market_guard.py` 以 `__new__` 建 updater 時改注入 DAO。
> - chip／margin loader 保留原本的 `partial_files` 回報（price 試點已拿掉），維持行為不變。
> - `tasks/update_db.py` 的 chip／margin 分支補上 `try/finally: close()`。

### Phase3-2. dividend、corporate_action ✅

- **做法**：新增 `StockDividendDAO`、`CorporateActionDAO`。兩份 `dedup_by_source_priority` 比對差異後收斂成一份，放在 `core/pipeline/shared/`（屬清洗規則，不放 DAO）。`StockDividendAPI` 的係數快取留在 API。`corporate_action_detector._drop_explained()` 改走 DAO。
- **產出**：對應 DAO、API、loader、updater、detector。
- **驗證方式**：`pytest tests/test_adjusted_price_api.py tests/test_corporate_action.py` 通過；回歸雙線通過。
- **相依**：Phase2-5。

> **完成紀錄（2026-09-16）**
> - 兩份去重的差異：dividend 版對未列入優先序的來源與缺「資料來源」欄會發警告，corporate_action 版不會；
>   勝出規則（優先序最高、同序取最後出現、結果依鍵排序）兩者相同。收斂後採 dividend 版並以 `label` 區分日誌。
>   loader 上的 `dedup_by_source_priority` 方法刪除，測試改呼叫共用函式。
> - 底座新增 `insert_or_replace()`（偏離〈底座 API〉：原規格只有 `insert_or_ignore`）：兩表的來源是區間查詢，
>   站方更正過的值必須蓋掉舊值。loader 的整批 upsert 包在 savepoint 內，並以 `try/finally` 保證自有連線關閉
>   （舊版寫入拋錯時連線不關）。
> - `StockDividendAPI` 同時持有 `dao` 與 `corporate_action_dao`（共用 API 的連線）；`corporate_action_detector`
>   的 `_drop_explained()` 改走兩個 DAO，全檔不再 import `SQLiteUtils`。

### Phase3-3. monthly_revenue ⬜

- **做法**：新增 `MonthlyRevenueDAO`。`get_range` 改成以 `year * 100 + month BETWEEN ? AND ?` 查詢，修正跨年區間，並新增測試（2023-11～2024-02 應回 4 個月）。loader「每個 CSV 讀一次整表主鍵」改用 `insert_or_ignore`。
- **產出**：對應 DAO、API、loader、updater。
- **驗證方式**：新增的跨年測試通過；既有 MRR 測試通過。
- **相依**：Phase2-5。

### Phase3-4. 財報四表 ⬜

- **做法**：新增 `FinancialStatementDAO`，以表名白名單參數化（沿用 `FinancialStatementAPI.ALLOWED_TABLES`）。`financial_statement_updater.py` 的股票清單查詢移到 Phase4-1 的 `StockInfoDAO`；`except Exception: return []` 改成讓錯誤往外拋，只有表不存在時回空。
- **產出**：對應 DAO、API、loader、updater。
- **驗證方式**：`pytest tests/test_equity_change_interruption.py` 與財報相關測試通過。
- **相依**：Phase2-5；股票清單部分相依 Phase4-1。

### Phase4-1. FinMind 四表 ⬜

- **做法**：新增 `StockInfoDAO`（含 with_warrant）、`SecuritiesTraderInfoDAO`、`BrokerTradingDAO`。先寫測試確認 `to_sql` 在目前的 pandas 版本是否會自行 commit：會的話改用 `insert_or_ignore`，讓 `commit=False` 真正生效。`finmind/common.py` 的吞錯誤比照 Phase3-4。FinMind updater 與 loader 共用 DAO 之後，`broker_trading_updater.py` 先 commit loader 連線來避開鎖的寫法可以移除。
- **產出**：對應 DAO、`core/api/tw/finmind_api.py`、`core/pipeline/tw/loaders/finmind/**`、FinMind updater。
- **驗證方式**：`pytest tests/test_finmind_api.py tests/test_finmind_loader_broker_trading.py` 等 FinMind 測試通過。
- **相依**：Phase2-5。

### Phase5-1. futures_price ⬜

- **做法**：新增 `FuturesPriceDAO`。`futures_price_updater.py`：
  - `:130` 不再吞 `sqlite3.Error`。
  - `:166` 的 `with sqlite3.connect` 洩漏改成使用 `StockPriceDAO.get_trading_days()`，由 updater 持有並在 `close()` 關閉。
  - `:527` 的統計查詢移進 DAO。
- **產出**：對應 DAO、`core/api/tw/futures_price_api.py`、loader、updater。
- **驗證方式**：`pytest tests/test_futures_price_api.py tests/test_futures_price_loader.py tests/backtest/test_futures_backtest.py` 通過；新增「查詢錯誤往外拋」測試。
- **相依**：Phase2-5。

### Phase5-2. futures_stock_universe ⬜

- **做法**：新增 `FuturesStockUniverseDAO`：
  - updater 的五個「每個方法各開一條連線」改用 DAO。
  - 三份 `MAX(snapshot_date)` 收斂成一份。
  - 刪除無呼叫端的 `get_active_products`。
  - 兩份 `get_contract_size` 比對 fallback 語意後收斂到 DAO，差異以參數表達，並把決策寫進 docstring。
  - `table_exists` 私有實作刪除。
- **產出**：對應 DAO、`core/api/tw/futures_stock_universe_api.py`、`futures_margin_api.py`（`get_contract_size`）、loader、updater。
- **驗證方式**：`pytest tests/test_futures_stock_universe.py tests/test_futures_stock_universe_api.py` 通過。
- **相依**：Phase5-1。

### Phase5-3. futures_margin 兩表 ⬜

- **做法**：新增 `FuturesMarginDAO`：
  - 「生效日」查詢收斂成一個方法，以參數 `inclusive: bool` 區分 `<` 與 `<=`，updater 與 API 各自明確傳入；確認 updater 用 `<` 是否刻意，結論寫進 docstring。
  - `FuturesMarginConfig.from_api()` 改成必須傳入 `api`，或由呼叫端（DataFeed）提供共用連線，不再暗開連線。
- **產出**：對應 DAO、`core/api/tw/futures_margin_api.py`、`core/managers/futures/position_manager.py`、loader、updater。
- **驗證方式**：`pytest tests/test_futures_margin_control.py tests/test_futures_position_manager.py` 通過；`tests/test_futures_margin_control.py:457` 不再連正式 DB。
- **相依**：Phase5-2。

### Phase5-4. futures_chip 三表、futures_continuous ⬜

- **做法**：新增 `FuturesChipDAO`（三表以白名單參數化）、`FuturesContinuousDAO`。`FuturesChipAPI` 的 `table=` 參數不在白名單時拋 `ValueError`。loader 每次呼叫就 commit 的行為改由 updater 控制。
- **產出**：對應 DAO、`core/api/tw/futures_chip_api.py`、兩組 loader／updater。
- **驗證方式**：`pytest tests/test_futures_chip.py tests/test_futures_continuous.py tests/test_sqlite_error_semantics.py` 通過；新增白名單測試。
- **相依**：Phase5-1。

---

## Phase 6：測試與收斂

### Phase6-1. 測試共用 DAO fixture ⬜

- **目的**：31 個測試檔各自 `sqlite3.connect` 建表，schema 改一次要改很多份。
- **做法**：`tests/conftest.py` 新增 fixture：`memory_conn`，以及可指定 DAO 類別、呼叫 `ensure_table()` 建表的 `dao_factory`。測試灌資料改用 DAO 的 `insert_or_ignore`，不再手寫 DDL。需要特殊 schema 的測試（例如故意缺欄位）維持手寫。
- **產出**：`tests/conftest.py`、各測試檔。
- **驗證方式**：`pytest -m "not slow"` 通過；`grep -rln "CREATE TABLE" tests` 只剩刻意手寫 schema 的檔案。
- **相依**：Phase3~Phase5。

### Phase6-2. 收斂 ⬜

- **目的**：把「只有 DAO 能碰 SQLite」從慣例變成檢查。
- **做法**：
  - 刪除 `core/pipeline/utils/sqlite_utils.py` 與 `BaseDataAPI`／`BaseDataLoader` 的委派方法。
  - `check_layer_deps.py` 新增檢查：`core/`、`tasks/` 內 `core/dao/` 以外的檔案 `import sqlite3` 視為違規（型別標註改成從 `core.dao` 取型別別名）。
  - `tasks/delete_price_data.py` 改用 `StockPriceDAO`。
  - `strategy_lab/data_analysis/tech_new_high_continuation/analysis.py` 直接用 `price_api.conn` 下 SQL 的地方改成 DAO 方法。
  - `scripts/manual/` 的 5 支腳本逐支判斷改寫或刪除。
- **產出**：上列檔案。
- **驗證方式**：分層檢查違規 0；`grep -rn "import sqlite3" core tasks` 只剩 `core/dao/`；回歸雙線通過。
- **相依**：Phase6-1。

### Phase6-3. 更新文件 ⬜

- **做法**：`docs/backtest/module-map.md` 的分層圖加入 `core/dao`，並改寫連線所有權的說明；`docs/pipeline/etl-ingestion.md` 補上 DAO、savepoint 與單一連線的寫入約定；`docs/dev/naming-axes.md` 補上 `core/dao/tw/` 的市場軸。
- **產出**：上列文件。
- **驗證方式**：`python scripts/check_doc_paths.py` 通過；文件描述與程式一致。
- **相依**：Phase6-2。

---

## 風險與對策

| 風險 | 說明 | 對策 |
|------|------|------|
| 回測結果改變 | 查詢搬家時順手改了排序或型別 | SQL 原樣搬移；每批跑回歸雙線；語意修正（跨年、`<`／`<=`）獨立成一個 commit 並附測試 |
| 共用連線造成交易交錯 | updater 讀、loader 寫在同一條連線上，未 commit 的寫入對後續讀取可見 | 這正是期望行為（讀到剛寫入的資料）；savepoint 確保失敗的檔案不留下資料 |
| 既有測試大量依賴 `conn` 屬性與路徑 monkeypatch | 改動介面會連帶改一大片測試 | 保留 `conn`／`owns_conn` 屬性與模組層級路徑常數，到 Phase6 再統一 |
| 與 PostgreSQL／TimescaleDB 計畫重疊 | 三份文件都要動連線層 | 連線入口統一在 `core/dao/`，兩份計畫已加註（見〈關聯與狀態〉） |

---

## 關聯與狀態

- **優先級**：P2
- **相關程式**：`core/api/`、`core/pipeline/`、`core/backtest/datafeed/tw/`、`core/managers/futures/position_manager.py`、`tasks/`、`scripts/check_layer_deps.py`、`tests/`
- **相關 backlog**：
  - [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)：本文件完成後，該計畫的 Phase1-2、Phase2-1~Phase2-3 縮減為「改寫 `core/dao/` 內部」；Phase1-1 的連線入口改放 `core/dao/connection.py`；Phase2-3 內含的欄位 Enum 下沉由本文件 Phase0-1 提前完成。
  - [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)：`core/db/timescale.py` 改放 `core/dao/`，`StockTickLoader`／`StockTickAPI` 經由 `StockTickDAO` 存取。
