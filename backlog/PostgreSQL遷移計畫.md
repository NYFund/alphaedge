# PostgreSQL 遷移計畫

## Abstract

- **背景／問題**：專案以 SQLite3 為主要儲存，分成 `data/db/tw_stock.db`（台股）與 `data/db/tw_futures.db`（台期貨）兩個檔。資料存取層（`core/dao/`）完成後，SQL 與 SQLite 專屬語法已集中在 15 支 DAO 與 `BaseDAO`，`core/dao/` 以外不再 `import sqlite3`（分層檢查強制）；但 DAO 內部仍是 `sqlite3`（`INSERT OR IGNORE／REPLACE`、`SAVEPOINT`、`sqlite_master`、`GLOB`），21 處以 `connect_sqlite()` 開連線，40 個測試檔直接建立 SQLite 連線。
- **目標**：導入 SQLAlchemy Engine 作為統一資料庫介面，分階段把讀取、寫入、測試與部署路徑遷移到 PostgreSQL（兩個 SQLite 檔併入單一 `alphaedge` 資料庫），並保留可回退方案至少一個版本週期。
- **範圍界線**：**先確保功能等價，再做效能優化**；本次**不做**分區／讀寫分離、不改業務邏輯與欄位語意；除〈關聯與狀態〉列出、刻意留到本批的表名與欄名收斂外，不做其他 schema 重新設計。**tick 不在範圍**：2026-09-14 已決定往後不再使用 DolphinDB、tick 不回補，`StockTickAPI` 等 DolphinDB 程式不遷移。
- **驗收標準**：主要流程（資料更新、查詢、回測讀取）在 PostgreSQL 可完整執行；核心 smoke ＋ integration 測試在 PostgreSQL 環境通過；文件與部署配置已更新且可重現；SQLite 依賴已降到可移除或已完全移除。

---

> **2026-09-02：`core/config.py` 已拆為套件，本文件的產出欄位隨之更新。**
> `core/config/settings.py`（營運參數，`DATABASE_URL` 屬此）／`core/config/schema.py`
> （分庫檔名與完整路徑，`TW_STOCK_DB_PATH`／`TW_FUTURES_DB_PATH` 屬此）／`core/config/paths.py`（目錄佈局）；
> 門面 `from core.config import X` 不變，故 Phase1-2 的「各處直連點」改動面不受影響。
> 另有一件對 Phase0-2 有利的既成事實：**環境變數覆寫路徑的模式已經存在**
> （`ALPHAEDGE_DATA_DIR`／`_RESULTS_DIR`／`_LOGS_DIR`，見
> [執行期產物與原始碼的分界](../docs/dev/runtime-artifacts.md)），`DATABASE_URL`
> 沿用同一套寫法即可，不需要另立機制。

> **2026-09-16：依 DAO 資料存取層完成後的實況重新盤點改動面**（設計見 [資料存取層](../docs/dev/data-access-layer.md)）。
> 2026-09-15 的盤點（`import sqlite3` 55 檔、`PRAGMA table_info` 14 處）已不適用：`sqlite_utils.py`、`finmind/schema.py`
> 已刪除，連線入口改為 `core/dao/connection.py`（不另建 `core/db/`），欄位 Enum 已下沉到 `core/config/schema.py`。
> 以下數字以括號內指令實測（排除 tick：DolphinDB 程式不遷移）：
>
> | 項目 | 數量 | 分布 | 指令 |
> |------|-----:|------|------|
> | 非測試 `import sqlite3` | 5 檔 | 全在 `core/dao/`（`base.py`、`connection.py`、`financial_statement_dao.py`、`futures_chip_dao.py`、`stock_price_dao.py`） | `grep -rlE "^\s*import sqlite3" core tasks scripts strategy_lab --include='*.py'` |
> | `connect_sqlite()` 呼叫點 | 21 檔 | `core/api/tw/` 11、`core/pipeline/` 7、`core/backtest/datafeed/tw/` 2、`scripts/manual/` 1 | `grep -rn "connect_sqlite(" core tasks scripts strategy_lab --include='*.py' \| grep -v "^core/dao"` |
> | `DBConnection` 型別標註 | 48 檔 | `core/pipeline/` 32、`core/api/` 12、回測 2、`scripts/` 2；**只是別名**，改 `core/dao/connection.py` 一處即可 | `grep -rl "DBConnection" core tasks scripts strategy_lab --include='*.py' \| grep -v "^core/dao"` |
> | DAO 外的交易生命週期呼叫（`conn.commit()`／`close()`） | 12 檔 | `core/api/base.py`（各 API 共用的 `close()`）、回測 DataFeed 2、多表 loader／updater 6（財報、FinMind、期貨籌碼）、`corporate_action_detector.py`、`tasks/delete_price_data.py`、`scripts/manual/manual_db_tables.py` | `grep -rn "conn\.commit()\|conn\.close()" core tasks scripts --include='*.py' \| grep -v "^core/dao"` |
> | `sqlite_master` | 1 處 | `core/dao/base.py`（`table_exists()`） | `grep -rn "sqlite_master" core tasks --include='*.py'` |
> | `PRAGMA` | 0 處 | — | `grep -rn "PRAGMA" core tasks --include='*.py'` |
> | `INSERT OR IGNORE／REPLACE`（實際 SQL） | 4 處 | `core/dao/base.py` 2、`futures_chip_dao.py` 1、`futures_margin_dao.py` 1；DAO 外的出現全是註解 | `grep -rn "INSERT OR" core/dao --include='*.py'` |
> | `SAVEPOINT` | 1 處 | `BaseDAO.savepoint()` | `grep -rn "SAVEPOINT" core --include='*.py'` |
> | `GLOB` | 1 處 | `stock_info_dao.py`（四碼代號） | `grep -rn "GLOB" core --include='*.py'` |
> | `cursor.rowcount` | 2 處 | `core/dao/base.py`、`stock_price_dao.py` | `grep -rn "rowcount" core/dao --include='*.py'` |
> | 測試 `import sqlite3` | 40 檔 | `sqlite3.connect` 116 處、手寫 `CREATE TABLE` 14 處（刻意壞掉的 schema）；正常建表已走 `dao_factory` | `grep -rlE "^\s*import sqlite3" tests --include='*.py'` |
>
> 各步驟的產出欄已依此更新；Phase1-2、Phase2-1~Phase2-3 縮減為改寫 `core/dao/` 內部與 21 個連線取得點。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | `docker-compose.yml` 新增 `postgres` service | `docker-compose.yml` | 本機可連線到 PostgreSQL | ⬜ | 含 volume、healthcheck、port；現有 `core` 服務以唯讀掛載 `./data` 讀 SQLite |
| Phase0-2 | 新增環境變數 `DATABASE_URL` / `DB_BACKEND` | `.env.example`、`core/config/settings.py`、`tests/test_config_consistency.py` | `DATABASE_URL` 可由 `.env` 載入 | ⬜ | `.env.example` 與程式讀取的環境變數由該測試雙向核對，須同批改 |
| Phase0-3 | 新增 Python 依賴（`sqlalchemy`、`psycopg`） | `pyproject.toml` / `requirements.txt` | 安裝後可建立 engine | ⬜ | `psycopg[binary]` 與 `psycopg2-binary` 二擇一；driver 屬執行期才載入的相依，須加註解 |
| Phase1-1 | 在 DAO 連線入口加入 engine | `core/dao/connection.py`、`scripts/check_layer_deps.py` | 提供 `get_engine()`／`db_dialect()`，`connect_sqlite()` 保留為 fallback | ⬜ | **關鍵步驟**；入口已存在（`connect_sqlite()`、`DBConnection`／`DBError` 別名），`core.dao` 已登記在分層檢查，不需另建 `core/db/` |
| Phase1-2 | 連線來源改由 `DATABASE_URL` 決定（含 SQLite fallback） | `core/dao/connection.py`、`core/dao/base.py`（`BaseDAO.__init__`）、21 個 `connect_sqlite()` 呼叫點 | 不改業務邏輯前提下 API 可讀到資料 | ⬜ | 相依 Phase1-1；呼叫點分布：`core/api/tw/` 11、`core/pipeline/` 7、回測 DataFeed 2、`scripts/manual/` 1 |
| Phase2-1 | 改造 `core/dao/` 的 SQLite 專屬語法 | `core/dao/base.py`、`core/dao/tw/futures_chip_dao.py`、`futures_margin_dao.py`、`stock_info_dao.py`、`stock_price_dao.py` | 改用 Inspector／`ON CONFLICT` 後 SQLite 下測試全過 | ⬜ | 相依 Phase1-1；`sqlite_master` 1、`INSERT OR` 4、`SAVEPOINT` 1、`GLOB` 1、`rowcount` 2 處，全在 DAO 內 |
| Phase2-2 | 改造 DAO 的連線型別、交易與讀取 | `core/dao/connection.py`（`DBConnection`／`DBError` 別名）、`core/dao/base.py`（`query_df`、`savepoint`、`commit`／`close`）、`core/dao/tw/*.py`（15 支）、DAO 外 12 檔的 `conn.commit()`／`close()` | 核心 update task 可在 PostgreSQL 跑完 | ⬜ | 相依 Phase2-1；loader／updater 的 SQL 已全在 DAO，不必逐檔改 |
| Phase2-3 | 驗證查詢 API 與回測 DataFeed 兩種 backend 結果一致 | `core/api/base.py`、`core/api/tw/*.py`、`core/backtest/datafeed/tw/*.py`（僅連線取得與關閉） | 各 API 查詢結果與 SQLite 一致；回測回歸雙線逐筆相同 | ⬜ | 相依 Phase2-2；欄位 Enum 下沉 `core/config/schema.py` 已於 DAO 重構時完成 |
| Phase2-4 | 改造 scripts | `scripts/manual/manual_db_tables.py` | 可在 PostgreSQL 正常執行 | ⬜ | 相依 Phase1-2；`tasks/delete_price_data.py` 已走 `StockPriceDAO`，不必改 |
| Phase3-1 | 選定資料遷移方案（pgloader 或 Python ETL） | 本文件（決策紀錄） | 決策與理由寫入本文件 | ⬜ | 相依 Phase2-1~Phase2-4；中文欄位名稱需特別驗證 |
| Phase3-2 | 執行一次性資料遷移與完整性比對 | 遷移腳本／指令紀錄 | 每張表 row count 比對、主鍵完整性、抽樣 20 筆查詢一致 | ⬜ | 相依 Phase3-1 |
| Phase4-1 | 測試 fixture 支援 PostgreSQL 測試資料庫 | `tests/conftest.py`（`memory_conn`、`dao_factory`）、直接 `import sqlite3` 的 40 個測試檔 | 不再直接建立 SQLite 連線灌樣本 | ⬜ | 相依 Phase2-1~Phase2-4；`sqlite3.connect` 116 處、手寫 `CREATE TABLE` 14 處 |
| Phase4-2 | 補齊核心路徑測試覆蓋 | `tests/` | `update_db` 各 target、FinMind loader/updater、API 查詢、去重與主鍵衝突 | ⬜ | 相依 Phase4-1 |
| Phase5-1 | 灰度：開發環境全面改 PostgreSQL，保留 SQLite fallback | — | 觀察期內無資料不一致 | ⬜ | 相依 Phase4-2 |
| Phase5-2 | 移除 SQLite 專屬程式碼與舊路徑 | 全專案 | 全域搜尋無 `import sqlite3` 殘留 | ⬜ | 相依 Phase5-1；至少保留一個版本週期後再執行 |
| Phase5-3 | 更新 README 與部署文件 | `README.md`、`README_en.md`、`docs/deployment/`、`docs/setup/dev-setup.md` | 團隊可依文件重現部署 | ⬜ | 相依 Phase5-2 |

---

## 遷移原則

- 將目前以 `data/db/tw_stock.db`、`data/db/tw_futures.db` 為主的 SQLite 存取，改為 PostgreSQL 單一資料庫。
- 先確保「功能等價」再做「效能優化」。
- 採用分階段遷移：先讀取、再寫入、最後清理舊路徑。
- 保留可回退方案（至少一個版本週期）。

## 技術路線

建議導入 SQLAlchemy Engine 作為統一資料庫介面，原因：

- 可以同時支援 SQLite 與 PostgreSQL（有利於過渡期）。
- SQL 已集中在 `core/dao/`，只需在 DAO 內以 `Connection.execute(text(...))` 取代 `sqlite3` 的 `execute`；**讀寫都不經 pandas 的 `read_sql_query`／`to_sql`**（前者查詢失敗會 rollback 共用連線、後者寫完自行 commit，見 [資料存取層](../docs/dev/data-access-layer.md)）。
- 可避免不同 DB driver 在 placeholder 與 transaction 行為差異造成的大量 if/else。

連線字串範例：

- 開發環境：`postgresql+psycopg://postgres:postgres@localhost:5432/alphaedge`
- Docker 內部：`postgresql+psycopg://postgres:postgres@postgres:5432/alphaedge`

**寫入語意要特別對照**：現行冪等寫入大量依賴 SQLite 的 `INSERT OR IGNORE`（見 [ETL 入庫約定 §3.1](../docs/pipeline/etl-ingestion.md)），
PostgreSQL 對應的是 `INSERT ... ON CONFLICT DO NOTHING`，且**必須有對應的 unique constraint 才能生效**——
現有表若主鍵只存在 pandas 層的去重邏輯，搬過去會變成靜默重複寫入。

---

## Phase 0：準備環境（低風險）

### Phase0-1. `docker-compose.yml` 新增 `postgres` service ⬜

- **目的**：提供本機與 CI 一致的 PostgreSQL 環境。
- **做法**：新增 `postgres` service，設定 volume（資料持久化）、healthcheck、port mapping。
  現有 `core` 服務以 `./data:/app/data:ro` 唯讀掛載 SQLite；切換後 `core` 要 `depends_on` postgres 的 healthcheck，唯讀掛載在 Phase5-2 前保留。
- **產出**：`docker-compose.yml`。
- **驗證方式**：`docker compose up` 後本機可成功連線到 PostgreSQL。
- **相依**：無。

### Phase0-2. 新增環境變數 ⬜

- **目的**：讓連線設定可由環境決定，不再寫死路徑。
- **做法**：新增 `DATABASE_URL`（主來源）與 `DB_BACKEND`（可選，用於開關 `sqlite` / `postgres`），讀取方式比照 `settings.py` 既有的 `os.getenv` 寫法。
  `.env.example` 的鍵與程式實際讀取的環境變數由 `tests/test_config_consistency.py` 雙向核對，三處要同批改。
- **產出**：`.env.example`、`core/config/settings.py`、（必要時）`tests/test_config_consistency.py`。
- **驗證方式**：`DATABASE_URL` 可由 `.env` 載入並被讀取到；`pytest tests/test_config_consistency.py` 通過。
- **相依**：無。

### Phase0-3. 新增 Python 依賴 ⬜

- **目的**：具備建立 SQLAlchemy engine 的能力。
- **做法**：新增 `sqlalchemy` 與 `psycopg[binary]`（或 `psycopg2-binary`，二擇一）。
  `pyproject.toml` 的 `dependencies` 只列「程式碼實際 import 的套件」、`requirements.txt` 鎖精確版本；
  driver 由 SQLAlchemy 依連線字串載入、程式碼不會 import，**須比照 `kaleido`／`html5lib` 加註解說明是執行期相依**，否則下次依 import 掃描清理時會被刪掉。
- **產出**：`pyproject.toml` / `requirements.txt`。
- **驗證方式**：安裝後可用 `DATABASE_URL` 建立 engine 並執行 `SELECT 1`。
- **相依**：無。

---

## Phase 1：建立 DB 抽象層（關鍵）

### Phase1-1. 在 DAO 連線入口加入 engine ⬜

- **目的**：所有 DB 存取已收斂到 `core/dao/connection.py`，在同一處加入 PostgreSQL 的連線方式。
- **做法**：在 `core/dao/connection.py` 新增 `get_engine()`、`db_dialect()`（判斷 sqlite/postgresql）；既有 `connect_sqlite()` 保留為過渡期 fallback。
  `core.dao` 已在 `scripts/check_layer_deps.py` 登記為第 2 層，且「`core/dao/` 以外不得 import 資料庫驅動」的檢查已存在——`sqlalchemy`／`psycopg` 的 import 應一併納入該檢查。
- **產出**：`core/dao/connection.py`、`scripts/check_layer_deps.py`（驅動清單加入 `sqlalchemy`、`psycopg`）。
- **驗證方式**：兩種 backend 下 `get_engine()` 皆可用，`db_dialect()` 回傳正確；`python scripts/check_layer_deps.py` 通過。
- **相依**：Phase0-1~Phase0-3。

### Phase1-2. 連線來源改由 `DATABASE_URL` 決定 ⬜

- **目的**：在不改業務邏輯的前提下切換底層連線來源。
- **做法**：優先讀 `DATABASE_URL`；若未設定則 fallback 到兩個 SQLite 檔（過渡期）。期貨線刻意寫 `tw_futures.db`（見 [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)〈期貨線〉），fallback 時兩庫仍需分開。
  連線取得點只有兩類：`BaseDAO.__init__()`（DAO 自己開連線）與 21 個 `connect_sqlite()` 呼叫點（API、回測 DataFeed、多表 updater 開一條連線交給多個 DAO 共用）。後者改為從入口取得連線即可，SQL 不必動。
- **產出**：`core/dao/connection.py`、`core/dao/base.py`、21 個 `connect_sqlite()` 呼叫點（`core/api/tw/` 11、`core/pipeline/` 7、`core/backtest/datafeed/tw/` 2、`scripts/manual/manual_db_tables.py`）。
- **驗證方式**：不改業務邏輯前提下，API 可透過 engine 讀到資料，結果與改動前一致。
- **相依**：Phase1-1。

---

## Phase 2：替換 SQLite 專屬語法

### Phase2-1. 改造 `core/dao/` 的 SQLite 專屬語法 ⬜

- **目的**：SQLite 專屬語法已全部集中在 DAO，逐項換成方言中立或依 `db_dialect()` 分派的寫法。
- **做法**：
  - `sqlite_master` → SQLAlchemy Inspector：`core/dao/base.py` 的 `table_exists()`（模組函式與 `BaseDAO` 方法共用）。
  - `INSERT OR IGNORE`／`INSERT OR REPLACE` → `INSERT ... ON CONFLICT DO NOTHING／DO UPDATE`：`core/dao/base.py` 的 `insert_or_ignore()`／`insert_or_replace()`、`futures_chip_dao.py` 的 `insert_new_rows()`、`futures_margin_dao.py` 的 `insert_rows()`。**`ON CONFLICT` 必須指名衝突欄位**，各表主鍵要能由 DAO 取得（目前只有 `StockPriceDAO`、`BrokerTradingDAO`、`MonthlyRevenueDAO` 有 `PRIMARY_KEY_COLUMNS` 常數，其餘主鍵只寫在建表 DDL 裡）。
  - `SAVEPOINT` → `Connection.begin_nested()`：`BaseDAO.savepoint()`。
  - `GLOB '[0-9][0-9][0-9][0-9]'` → PostgreSQL 的 `~ '^[0-9]{4}$'`：`stock_info_dao.py` 的 `get_listed_common_stock_ids()`。
  - `cursor.rowcount`：`insert_or_ignore()` 以它算實際寫入列數、`StockPriceDAO.delete_by_date()` 以它回傳刪除列數；psycopg 的 `executemany` 回傳的 `rowcount` 語意需實測。
  - `CAST(year AS INTEGER)`（`monthly_revenue_dao.py`、`financial_statement_dao.py`）兩邊皆可用，不必改；遷移後欄位型別若改為整數可一併移除。
- **產出**：`core/dao/base.py`、`core/dao/tw/futures_chip_dao.py`、`futures_margin_dao.py`、`stock_info_dao.py`、`stock_price_dao.py`。
- **驗證方式**：`grep -rn "sqlite_master\|INSERT OR\|SAVEPOINT\|GLOB" core/dao` 只剩 SQLite fallback 分支；SQLite 下 `pytest -m "not slow"` 全過。
- **相依**：Phase1-1。

### Phase2-2. 改造 DAO 的連線型別、交易與讀取 ⬜

- **目的**：讓 DAO 內部脫離 `sqlite3.Connection`，呼叫端（API、loader、updater）不必改。
- **做法**：
  - `DBConnection`／`DBError` 別名（`core/dao/connection.py`）改指 SQLAlchemy 的 `Connection`／`SQLAlchemyError`；DAO 外 48 檔的型別標註只引用別名，不必逐檔改。
  - `BaseDAO.query_df()` 已改用 cursor（查詢失敗不 rollback 共用連線），改為 `conn.execute(text(sql), params)` 後以 `Result.keys()` 與 `fetchall()` 組表；**不要改回 `pd.read_sql_query`**。參數佔位由 `?` 改為具名參數，DAO 內 SQL 要逐支調整。
  - `commit()`／`close()`：DAO 外仍有 12 檔直接呼叫 `conn.commit()`／`conn.close()`（`core/api/base.py`、回測 DataFeed、財報／FinMind／期貨籌碼的多表 loader 與 updater、`corporate_action_detector.py`、`tasks/delete_price_data.py`、`scripts/manual/manual_db_tables.py`），確認 SQLAlchemy `Connection` 下語意相同，或改為經由 DAO。
- **產出**：`core/dao/connection.py`、`core/dao/base.py`、`core/dao/tw/*.py`（15 支）、上述 12 檔。
- **驗證方式**：核心 update task 可在 PostgreSQL 正常跑完，且中斷後續跑行為不變（`DateProgressStore` 的 `no_data`／`incomplete` 語意不變）。
- **相依**：Phase2-1。

### Phase2-3. 驗證查詢 API 與回測 DataFeed ⬜

- **目的**：讀取路徑的 SQL 已在 DAO（Phase2-1、Phase2-2 改完），本步驟確認兩種 backend 結果一致。
- **做法**：API 與回測 `TwStockDataFeed`／`TwFuturesDataFeed` 只在建構時取得連線、結束時關閉，隨 Phase1-2 改完；逐一比對 price／chip／margin／dividend／fs／mrr 與期貨各 API 的查詢結果。
  欄位 Enum（`PriceColumn`、`ChipColumn` 等）已於 DAO 重構時下沉到 `core/config/schema.py`，原列於本步驟的下沉工作不再需要。
- **產出**：`core/api/base.py`、`core/api/tw/*.py`、`core/backtest/datafeed/tw/*.py`（僅連線取得與關閉處）。
- **驗證方式**：各 API 在兩種 backend 下結果一致；`./scripts/run_regression.sh` 回歸雙線逐筆相同。
- **相依**：Phase2-2。

### Phase2-4. 改造 scripts ⬜

- **目的**：補齊最後的直連殘留。
- **做法**：`scripts/manual/manual_db_tables.py` 以 `connect_sqlite()` 唯讀開兩個 DB 列出資料表，改為從入口取得連線。`tasks/delete_price_data.py` 已走 `StockPriceDAO`，不必改。
- **產出**：`scripts/manual/manual_db_tables.py`。
- **驗證方式**：可在 PostgreSQL 正常執行。
- **相依**：Phase1-2。

---

## Phase 3：資料遷移（一次性）

### Phase3-1. 選定遷移方案 ⬜

- **目的**：兩個方案的風險與可控程度不同，須先定案。
- **做法**：二選一——
  - **方案 A：pgloader（推薦先嘗試）**。優點是快速、表結構與資料可一次搬運；缺點是轉型規則需驗證，**中文欄位名稱需特別檢查**。兩個 SQLite 檔各跑一次，匯入同一個目標庫。

    ```bash
    pgloader sqlite:///absolute/path/to/data/db/tw_stock.db postgresql://postgres:postgres@localhost:5432/alphaedge
    pgloader sqlite:///absolute/path/to/data/db/tw_futures.db postgresql://postgres:postgres@localhost:5432/alphaedge
    ```

  - **方案 B：Python ETL（可控）**。流程為：SQLite 逐表 `read_sql_query` → 欄位型別修正（日期、整數、浮點）→ 寫入 PostgreSQL（`to_sql` 或 COPY）→ 建立索引與 constraints。
    若同批做表名補前綴（見〈關聯與狀態〉），方案 B 較好控制改名對照。
- **產出**：本文件補上決策段落。
- **驗證方式**：先以小表試跑，確認中文欄位名稱與型別無誤後再定案。
- **相依**：Phase2-1~Phase2-4。

### Phase3-2. 執行遷移與完整性比對 ⬜

- **目的**：確保資料一筆不漏、型別無誤。
- **做法**：依 Phase3-1 選定的方案執行，並建立索引與 constraints。
- **產出**：遷移腳本或指令紀錄。
- **驗證方式**：至少三項——① 每張表 row count 比對；② 主鍵／唯一鍵完整性；③ 抽樣 20 筆關鍵查詢結果一致。
  另跑一次 `pytest tests/test_trading_calendar_guard.py -m slow` 的等價查詢，確認非交易日與單一市場批次護欄在新庫上仍通過。
- **相依**：Phase3-1。

---

## Phase 4：測試與驗證

### Phase4-1. 測試 fixture 支援 PostgreSQL 測試資料庫 ⬜

- **目的**：40 個測試檔直接以 SQLite（in-memory 或暫存檔）建立連線（`sqlite3.connect` 116 處），不改造就無法驗證 PostgreSQL 路徑。
- **做法**：`tests/conftest.py` 已有 `memory_conn` 與 `dao_factory`（以 DAO 自己的建表方法建正式 schema，測試不再以 `to_sql` 建表），讓 `memory_conn` 可切換 backend（PostgreSQL 用 docker container），再把各測試檔的 `sqlite3.connect(...)` 改為取用 fixture，DB 建立／清理自動化。
  手寫 `CREATE TABLE` 的 14 處是刻意壞掉的 schema（缺欄位），逐處確認在 PostgreSQL 下仍能觸發同樣的錯誤型別。
  `@pytest.mark.slow` 的正式庫護欄（`tests/test_trading_calendar_guard.py` 等）另外處理。
- **產出**：`tests/conftest.py`、直接 `import sqlite3` 的 40 個測試檔。
- **驗證方式**：既有測試在新 fixture 下可執行。
- **相依**：Phase2-1~Phase2-4。

### Phase4-2. 補齊核心路徑測試覆蓋 ⬜

- **目的**：確保功能等價。
- **做法**：至少覆蓋——`tasks.update_db` 各 target 路徑、FinMind 相關 loader/updater、API 查詢（price/chip/fs/mrr 與期貨）、重複資料去重與主鍵衝突行為（`ON CONFLICT` 需要 unique constraint，見〈技術路線〉）。
- **產出**：`tests/`。
- **驗證方式**：核心 smoke ＋ integration 測試在 PostgreSQL 環境全數通過。
- **相依**：Phase4-1。

---

## Phase 5：切換與收斂

### Phase5-1. 灰度切換 ⬜

- **目的**：先在低風險環境驗證，保留回退能力。
- **做法**：開發環境全面改 PostgreSQL，保留 SQLite fallback。
- **產出**：環境設定變更。
- **驗證方式**：觀察期內日更與回測流程無資料不一致。
- **相依**：Phase4-2。

### Phase5-2. 移除 SQLite 專屬程式碼 ⬜

- **目的**：收斂維護成本，避免兩套路徑長期並存。
- **做法**：移除 SQLite 專屬程式碼與舊文件；**至少保留一個版本週期的觀察期後再執行**。
- **產出**：全專案。
- **驗證方式**：全域搜尋無 `import sqlite3` 殘留；測試全數通過。
- **相依**：Phase5-1。

### Phase5-3. 更新文件與部署配置 ⬜

- **目的**：讓團隊可重現部署。
- **做法**：更新 `README.md` / `README_en.md`、`docs/deployment/`、`docs/setup/dev-setup.md`、[資料覆蓋範圍](../docs/exchanges/data_coverage.md)的資料表位置。
- **產出**：上述文件。
- **驗證方式**：依文件從零建置一次可成功。
- **相依**：Phase5-2。

---

## 風險與對策

| 風險 | 說明 | 對策 |
|------|------|------|
| 型別風險 | SQLite 寬鬆型別 → PostgreSQL 嚴格型別 | 先做欄位型別盤點，遷移前先清洗 |
| 衝突策略風險 | 冪等寫入依賴 `INSERT OR IGNORE` ＋ 主鍵，部分去重在 pandas 層 | 補上 DB 層 unique/PK，改為 `ON CONFLICT DO NOTHING`；缺 constraint 時會變成靜默重複 |
| 主鍵語意風險 | 財報三表主鍵含 `公司名稱`，同一檔同一年季可能兩列（更名或名稱加註 `*`） | 遷移時不順手改主鍵；若要改屬 schema 變更，需另立工作 |
| 效能風險 | 大表寫入速度變慢 | 批次寫入、COPY、索引延後建立、分批 commit |
| 測試風險 | 40 個測試檔直接依賴 SQLite | `memory_conn`／`dao_factory` 已集中建表，從它們切換 backend，DB 建立／清理自動化 |

---

## 關聯與狀態

- **優先級**：P3（影響面廣，建議在其他重構收斂後再動）
- **相關程式**：`core/dao/`（SQL 與 SQLite 專屬語法的唯一所在）、`connect_sqlite()` 呼叫點（`core/api/tw/`、`core/pipeline/`、`core/backtest/datafeed/tw/`、`scripts/manual/`）、`scripts/check_layer_deps.py`、`tests/conftest.py`、`tests/`
- **刻意留到本計畫一起做的 schema 收斂**（來源皆為 [命名軸線](../docs/dev/naming-axes.md)，理由見該文件）：
  1. **台股表名補上 `stock_` 前綴**：`price`／`chip`／`margin` 等 13 張表不帶前綴，
     期貨表帶 `futures_` 前綴（後者為刻意決策，不改）。PostgreSQL 的目標是**單一**
     `alphaedge` 資料庫，兩個 SQLite 檔會併進同一個扁平命名空間，屆時前綴是必要的。
  2. **`stock_id` → `symbol` 的資料層改名**：`core/models/base/` 的識別欄位已是 `symbol`，資料表與 API 仍是 `stock_id`；
     `core/dao/base.py` 的 `create_symbol_date_index()` 寫死 `stock_id` 欄，隨此項一起改。
  3. **欄位 Enum 下沉到 `core/config/schema.py`**：已於 DAO 重構時完成，不再列入本計畫。
  4. **`data/downloads/` 的目錄形狀**：現為 `tw_stock/`／`tw_futures/`（市場 ＋ 商品
     壓成單一目錄名），程式碼側已是 `pipeline/tw/`（每層只承載一條軸）。純目錄名的
     `tw/stock/` 才與程式碼側同構，但那是第二次資料搬遷，不值得為一致性單獨做——
     **本計畫或下次動 `downloads/` 時順手收斂**。
- **相關 backlog**：[台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)（2026-09-16 立項，tick 改落地 TimescaleDB；與本計畫共用 `postgres` service（TimescaleDB image）、driver 與 `core/dao/` 連線入口，先做的建立、後做的沿用）；[美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md)（美股資料量較大，建議本計畫先收斂；`us_` 表名前綴同樣以單一資料庫為前提）
