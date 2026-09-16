# PostgreSQL 遷移計畫

## Abstract

- **背景／問題**：專案以 SQLite3 為主要儲存，分成 `data/db/tw_stock.db`（台股）與 `data/db/tw_futures.db`（台期貨）兩個檔，且是「直接耦合」——`core/api/`、`core/pipeline/`、`core/backtest/datafeed/` 各自 `import sqlite3` 並持有 `sqlite3.Connection`、SQLite 專屬檢查（`sqlite_master`、`PRAGMA table_info`）散在各 loader、測試大量依賴 SQLite（檔案或 in-memory）。這不是改連線字串就能解決的問題。
- **目標**：導入 SQLAlchemy Engine 作為統一資料庫介面，分階段把讀取、寫入、測試與部署路徑遷移到 PostgreSQL（兩個 SQLite 檔併入單一 `alphaedge` 資料庫），並保留可回退方案至少一個版本週期。
- **範圍界線**：**先確保功能等價，再做效能優化**；本次**不做**分區／讀寫分離、不改業務邏輯與欄位語意；除〈關聯與狀態〉列出、刻意留到本批的表名與欄名收斂外，不做其他 schema 重新設計。**tick 不在範圍**：2026-09-14 已決定往後不再使用 DolphinDB、tick 不回補（見 [爬蟲缺口回補與非交易日批次清理.md](爬蟲缺口回補與非交易日批次清理.md) S7），`StockTickAPI` 等 DolphinDB 程式不遷移。
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

> **2026-09-15：重新盤點改動面**（原文件寫於期貨庫、`core/pipeline/shared/` 與回測 DataFeed 成形之前，範圍低估）。
> 以非測試程式計（含 `scripts/manual/`）：
>
> | 項目 | 數量 | 分布 |
> |------|-----:|------|
> | `import sqlite3` | 55 檔 | `core/pipeline/` 34、`core/api/` 12、`scripts/` 6、`core/backtest/datafeed/tw/` 2、`tasks/` 1 |
> | `sqlite3.connect` | 66 處 | 同上，另含 loader 與 updater 各自開連線 |
> | `PRAGMA table_info` | 14 處 | 10 支 loader 的欄位檢查 ＋ `core/pipeline/tw/loaders/finmind/schema.py` 4 處 |
> | `sqlite_master` | 3 處（`core/`） | `sqlite_utils.py`、`core/api/base.py`、`futures_stock_universe_updater.py` |
> | 測試 `import sqlite3` | 30 檔 | 多為 in-memory 或暫存檔灌樣本；`tests/conftest.py` 本身不連 DB |
>
> 各步驟的產出欄已依此更新。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | `docker-compose.yml` 新增 `postgres` service | `docker-compose.yml` | 本機可連線到 PostgreSQL | ⬜ | 含 volume、healthcheck、port；現有 `core` 服務以唯讀掛載 `./data` 讀 SQLite |
| Phase0-2 | 新增環境變數 `DATABASE_URL` / `DB_BACKEND` | `.env.example`、`core/config/settings.py`、`tests/test_config_consistency.py` | `DATABASE_URL` 可由 `.env` 載入 | ⬜ | `.env.example` 與程式讀取的環境變數由該測試雙向核對，須同批改 |
| Phase0-3 | 新增 Python 依賴（`sqlalchemy`、`psycopg`） | `pyproject.toml` / `requirements.txt` | 安裝後可建立 engine | ⬜ | `psycopg[binary]` 與 `psycopg2-binary` 二擇一；driver 屬執行期才載入的相依，須加註解 |
| Phase1-1 | 建立 DB 抽象層單一入口 | `core/db/connection.py` | 提供 `get_engine()` / `get_connection()` / `db_dialect()` | ⬜ | **關鍵步驟**，後續所有改造的支點；分層位置須通過 `scripts/check_layer_deps.py` |
| Phase1-2 | `TW_STOCK_DB_PATH`／`TW_FUTURES_DB_PATH` 直連改為經由 engine（含 SQLite fallback） | `core/config/schema.py`、各直連點 | 不改業務邏輯前提下 API 可讀到資料 | ⬜ | 相依 Phase1-1；優先讀 `DATABASE_URL`，未設定則 fallback SQLite |
| Phase2-1 | 改造 SQLite 專屬語法（`sqlite_master` / `PRAGMA`） | `core/pipeline/utils/sqlite_utils.py`、`core/api/base.py`、10 支 loader、`core/pipeline/tw/loaders/finmind/schema.py`、`futures_stock_universe_updater.py` | 改用 Inspector 後行為等價 | ⬜ | 相依 Phase1-1；高影響優先改 |
| Phase2-2 | 改造 Loader／Updater | `core/pipeline/shared/`（`base_loader.py`、`date_planner.py`）、`core/pipeline/tw/loaders/**`、`core/pipeline/tw/updaters/**`、`core/pipeline/tw/cleaners/corporate_action_detector.py` | 核心 update task 可在 PostgreSQL 跑完 | ⬜ | 相依 Phase2-1 |
| Phase2-3 | 改造查詢 API 與回測 DataFeed | `core/api/base.py`、`core/api/tw/*.py`、`core/backtest/datafeed/tw/*.py`、`core/config/schema.py`（欄位 Enum 下沉） | 各 API 查詢結果與 SQLite 一致；回測回歸雙線逐筆相同 | ⬜ | 相依 Phase2-1；含 `constant.py` 欄位 Enum 下沉（見〈關聯與狀態〉） |
| Phase2-4 | 改造 tasks 與 scripts | `tasks/delete_price_data.py`、`scripts/manual/*` | 可在 PostgreSQL 正常執行 | ⬜ | 相依 Phase2-1；`scripts/manual/` 可改可刪，逐支判斷 |
| Phase3-1 | 選定資料遷移方案（pgloader 或 Python ETL） | 本文件（決策紀錄） | 決策與理由寫入本文件 | ⬜ | 相依 Phase2-1~Phase2-4；中文欄位名稱需特別驗證 |
| Phase3-2 | 執行一次性資料遷移與完整性比對 | 遷移腳本／指令紀錄 | 每張表 row count 比對、主鍵完整性、抽樣 20 筆查詢一致 | ⬜ | 相依 Phase3-1 |
| Phase4-1 | 測試 fixture 支援 PostgreSQL 測試資料庫 | `tests/conftest.py`、直接 `import sqlite3` 的 30 個測試檔 | 不再直接建立 SQLite 連線灌樣本 | ⬜ | 相依 Phase2-1~Phase2-4 |
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
- 與 pandas `read_sql_query` / `to_sql` 搭配成熟。
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

### Phase1-1. 建立單一入口 `core/db/connection.py` ⬜

- **目的**：所有 DB 存取收斂到單一入口，後續改造才不會散落各處。
- **做法**：提供三個函式——`get_engine()`、`get_connection()`（必要時）、`db_dialect()`（判斷 sqlite/postgresql）。
  `core/db/` 會被 `core/api/`、`core/pipeline/`、`core/backtest/datafeed/` 共用，須在 `scripts/check_layer_deps.py` 的 `_LAYER_RULES` 登記為低於資料層的共用層（與 `core.config`／`core.utils` 同級），否則會被判為反向相依。
- **產出**：`core/db/connection.py`、`scripts/check_layer_deps.py`。
- **驗證方式**：兩種 backend 下 `get_engine()` 皆可用，`db_dialect()` 回傳正確；`python scripts/check_layer_deps.py` 通過。
- **相依**：Phase0-1~Phase0-3。

### Phase1-2. `TW_STOCK_DB_PATH`／`TW_FUTURES_DB_PATH` 直連改為經由 engine ⬜

- **目的**：在不改業務邏輯的前提下切換底層連線來源。
- **做法**：優先讀 `DATABASE_URL`；若未設定則 fallback 到兩個 SQLite 檔（過渡期）。期貨線刻意寫 `tw_futures.db`（見 [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)〈期貨線〉），fallback 時兩庫仍需分開。
- **產出**：`core/config/schema.py` 及各處直連點。
- **驗證方式**：不改業務邏輯前提下，API 可透過 engine 讀到資料，結果與改動前一致。
- **相依**：Phase1-1。

---

## Phase 2：替換 SQLite 專屬語法

### Phase2-1. 改造 SQLite 專屬語法 ⬜

- **目的**：SQLite 專屬語法是後續所有檔案改造的支點；原規劃以為集中在 `sqlite_utils`，實際散在多處。
- **做法**：
  - `sqlite_master` → 改為 SQLAlchemy Inspector：`SQLiteUtils.check_table_exist()`、`BaseDataAPI`（`core/api/base.py`）的同名檢查、`futures_stock_universe_updater.py`。
    `core/api/base.py` 那份與 `sqlite_utils` 各自存在是刻意的（`core/api` 不可反向 import `core/pipeline`），改造時仍維持不跨層。
  - `PRAGMA table_info(...)` → 改為 Inspector 欄位檢查：10 支 loader 的建表／補欄邏輯與 `core/pipeline/tw/loaders/finmind/schema.py`。
  - 型別註記 `sqlite3.Connection` → 改為 SQLAlchemy Connection/Engine 或 Protocol。
- **產出**：`core/pipeline/utils/sqlite_utils.py`、`core/api/base.py`、`core/pipeline/tw/loaders/*.py`、`core/pipeline/tw/loaders/finmind/schema.py`、`core/pipeline/tw/updaters/futures_stock_universe_updater.py`。
- **驗證方式**：`grep -rn "sqlite_master\|PRAGMA" core` 無結果；既有呼叫端在 SQLite 下行為等價。
- **相依**：Phase1-1。

### Phase2-2. 改造 Loader／Updater ⬜

- **目的**：讓寫入路徑脫離 `sqlite3.Connection`。
- **做法**：改用 engine／connection 抽象；placeholder 與 transaction 行為交由 SQLAlchemy 處理；`INSERT OR IGNORE` 改為方言中立的寫法（見〈技術路線〉）。
  共用層 `core/pipeline/shared/base_loader.py`、`date_planner.py` 先改，`tw/` 的 loader／updater（含 `finmind/` 子目錄）隨後。
- **產出**：`core/pipeline/shared/`、`core/pipeline/tw/loaders/**`、`core/pipeline/tw/updaters/**`、`core/pipeline/tw/cleaners/corporate_action_detector.py`。
- **驗證方式**：核心 update task 可在 PostgreSQL 正常跑完，且中斷後續跑行為不變（`DateProgressStore` 的 `no_data`／`incomplete` 語意不變）。
- **相依**：Phase2-1。

### Phase2-3. 改造查詢 API 與回測 DataFeed ⬜

- **目的**：讓讀取路徑脫離 SQLite 專屬型別。
- **做法**：同 Phase2-2；`pd.read_sql_query` 改吃 engine。回測的 `TwStockDataFeed`／`TwFuturesDataFeed` 也直接開 SQLite 連線，一併改。
  同批把 `core/pipeline/utils/constant.py` 的欄位 Enum（`PriceColumn`／`ChipColumn`／`FuturesPriceColumn` 等）下沉到 `core/config/schema.py`——
  欄位 Enum 是資料表 schema 的一部分，`core/api/` 目前為了它反向相依 `core/pipeline/`，見 [命名軸線](../docs/dev/naming-axes.md)〈`utils/` 是層，不是軸〉。
- **產出**：`core/api/base.py`、`core/api/tw/*.py`、`core/backtest/datafeed/tw/*.py`、`core/config/schema.py`、`core/pipeline/utils/constant.py`。
- **驗證方式**：price／chip／margin／dividend／fs／mrr 與期貨各 API 在兩種 backend 下結果一致；`./scripts/run_regression.sh` 回歸雙線逐筆相同。
- **相依**：Phase2-1。

### Phase2-4. 改造 tasks 與 scripts ⬜

- **目的**：補齊最後的直連殘留。
- **做法**：同上。`scripts/manual/` 是手動除錯腳本，逐支判斷改寫或刪除。
- **產出**：`tasks/delete_price_data.py`、`scripts/manual/*`。
- **驗證方式**：可在 PostgreSQL 正常執行。
- **相依**：Phase2-1。

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

- **目的**：30 個測試檔直接以 SQLite（in-memory 或暫存檔）灌樣本，不改造就無法驗證 PostgreSQL 路徑。
- **做法**：在 `tests/conftest.py` 提供可切換 backend 的 DB fixture（PostgreSQL 用 docker container），把各測試檔的 `sqlite3.connect(...)` 改為取用 fixture，DB 建立／清理自動化。
  `@pytest.mark.slow` 的正式庫護欄（`tests/test_trading_calendar_guard.py` 等）另外處理。
- **產出**：`tests/conftest.py`、直接 `import sqlite3` 的測試檔。
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
| 測試風險 | 30 個測試檔直接依賴 SQLite | 建立可切換 backend 的測試 fixture，DB 建立／清理自動化 |

---

## 關聯與狀態

- **優先級**：P3（影響面廣，建議在其他重構收斂後再動）
- **相關程式**：`core/pipeline/utils/sqlite_utils.py`、`core/pipeline/shared/`、`core/pipeline/tw/loaders/**`、`core/pipeline/tw/updaters/**`、`core/api/`、`core/backtest/datafeed/tw/`、`core/config/`、`tasks/*`、`scripts/`、`tests/`
- **刻意留到本計畫一起做的 schema 收斂**（來源皆為 [命名軸線](../docs/dev/naming-axes.md)，理由見該文件）：
  1. **台股表名補上 `stock_` 前綴**：`price`／`chip`／`margin` 等 13 張表不帶前綴，
     期貨表帶 `futures_` 前綴（後者為刻意決策，不改）。PostgreSQL 的目標是**單一**
     `alphaedge` 資料庫，兩個 SQLite 檔會併進同一個扁平命名空間，屆時前綴是必要的。
  2. **`stock_id` → `symbol` 的資料層改名**：`core/models/base/` 的識別欄位已是 `symbol`，資料表與 API 仍是 `stock_id`；
     `BaseDataLoader.create_symbol_date_index()` 寫死 `stock_id` 欄，隨此項一起改。
  3. **欄位 Enum 下沉到 `core/config/schema.py`**：歸 Phase2-3（見該步驟）。
  4. **`data/downloads/` 的目錄形狀**：現為 `tw_stock/`／`tw_futures/`（市場 ＋ 商品
     壓成單一目錄名），程式碼側已是 `pipeline/tw/`（每層只承載一條軸）。純目錄名的
     `tw/stock/` 才與程式碼側同構，但那是第二次資料搬遷，不值得為一致性單獨做——
     **本計畫或下次動 `downloads/` 時順手收斂**。
- **相關 backlog**：[美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md)（美股資料量較大，建議本計畫先收斂；`us_` 表名前綴同樣以單一資料庫為前提）
