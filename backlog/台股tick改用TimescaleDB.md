# 台股 tick 改用 TimescaleDB

## Abstract

- **背景／問題**：台股 tick 原本（**main 上至今仍是**）的流程是 Shioaji 爬取 → `data/downloads/tw_stock/tick/{stock_id}.csv` → `loadTextEx` 寫入 DolphinDB（`dfs://tickDB`）→ `StockTickAPI` 以 DolphinDB script 查詢。DolphinDB 主機 2026-09-14 已裁示不再使用（台股 tick 不回補、期貨 tick 不做），main 上的 tick 級回測因此**沒有可用的資料源**。
- **目標**：落地改成 TimescaleDB（PostgreSQL extension）——`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json` 當續跑依據、`StockTickAPI` 改用 ConnectorX（公開方法與欄位不變）、2020-04-01～2024-05-10 的歷史 CSV 全量匯入並壓縮、移除台股與期貨 tick 的 DolphinDB 程式。
- **目前狀態（2026-10-18 實查）**：**18 / 18 步都在 `feature/tick-timescaledb` 完成，資料也已入庫；剩下的只有〈合併部署收尾〉**。合併前 main 仍是 DolphinDB 版程式（`tasks/update_db.py`、`core/pipeline/tw/loaders/stock_tick_loader.py` 仍 import `dolphindb`）。
- **範圍界線**：
  - **不做期貨 tick**（2026-09-15 裁示）；期貨 tick 的 DolphinDB 程式依 Phase5-2（2026-10-09 選項 A）一併移除，日後要做另立 backlog。
  - **不做**台股日頻資料的 SQLite → PostgreSQL 遷移（[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 的範圍，沿用本文件建立的 `postgres` service、`[tick]` extra 與 `core/dao/timescale.py`）。
  - **不改**爬蟲與清洗器的欄位格式，**不做** tick 回補續跑到今天（本文件只保證 updater 在新儲存上可以續跑）。
  - **不自動對正式資料表跑更新**（2026-09-18 裁示）：歷史全量匯入已於 2026-10-09 依使用者指令執行、2026-10-10 完成；`python -m apps.update_db --target tick` 對正式表的第一次執行**仍要等使用者下指令**，不屬於本文件的收尾。
  - **不修補歷史資料本身**：異常列只依 Phase4-1 裁示的規則在入庫時排除並計數。
- **驗收標準**（四項都已在功能分支驗過；整份要等〈合併部署收尾〉做完才結案）：
  1. 歷史 CSV 全數入庫，每個「股票 × 交易日」DB 列數＝CSV 列數－排除列數。**已驗**（Phase4-3：1,781,133 組全數相符）。
  2. `StockTickAPI` 四個公開方法在 TimescaleDB 上回傳相同欄位，全市場單日 `get_ordered_ticks()` ≤ 10 秒。**已驗**（正式資料 0.7～1.5 秒）。
  3. `--target tick` 可寫入 TimescaleDB、重跑不重複。**已驗**（以替身與暫存 schema，`test_updater_resumes_from_load_log`）。
  4. 台股 tick 相關程式不再 import `dolphindb`，`pytest` 全數通過。**已驗**（Phase5-2）。

---

## 合併部署收尾（剩下的工作）

> 本節是整份文件唯一還沒做的事。路徑以合併後的程式為準（`apps/update_db.py`、頂層 `strategies/`、根目錄 `Dockerfile`），
> 這些搬家來自 `feature/post-rehearsal`（`回測與實盤入口拆分及架構收斂.md` Phase1-8、`core目錄邊界收斂.md` Phase2-2～2-4）。

### 時機與合併順序

- 分支鏈：`feature/post-rehearsal` → `feature/tick-timescaledb` → `feature/tick-backtest` → `feature/framework-symmetry`，每一條都包含前一條的全部內容（2026-10-18 實查四條都不落後 main）。
- **要等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1（模擬環境端到端演練）結束**，`feature/post-rehearsal` 先進 main，再依序合併本分支。
  合併進 main 等於部署到演練主目錄（launchd 跑的是主目錄的程式），合併動作避開 launchd 段落執行時間。

### 合併後的動作清單（在主目錄 `~/program_project/AlphaEdge/alphaedge` 執行）

| # | 動作 | 驗證 | 2026-10-18 實查的現況 |
|:-:|------|------|----------------------|
| 1 | `uv sync --extra frontend --extra lab --extra tick`（**三個 extra 都要寫**：只寫 `--extra tick` 會拔掉另外兩個；不帶 `--no-sync` 的 `uv run` 也會 exact sync） | `.venv/bin/python -c "import psycopg, connectorx, pyarrow"` 成功 | 主目錄 `.venv` 尚未安裝 psycopg |
| 2 | 確認 `.env` 有 `TICK_DATABASE_URL`；`POSTGRES_*` 沿用預設 `alphaedge` 時不必寫（`.env.example` 為註解） | `pytest tests/test_config_consistency.py` 通過 | 主目錄 `.env` 已有 `TICK_DATABASE_URL`，沒有 `POSTGRES_*`（用預設值） |
| 3 | 確認 TimescaleDB 在跑：`docker compose up -d postgres`。**資料庫不必重灌**：compose 寫死 `name: alphaedge`，worktree 匯入的資料就在同一個 volume | `docker compose ps postgres` 為 healthy；`StockTickAPI().get_ordered_ticks(datetime.date(2024, 5, 10), datetime.date(2024, 5, 10))` 回傳百萬列級的非空表 | 容器 `alphaedge-postgres` 執行中（`restart: unless-stopped`），volume `alphaedge_alphaedge_pgdata` 存在 |
| 4 | 要用容器跑回測時重建映像：`docker compose build`（`Dockerfile` 兩行 `uv sync` 已加 `--extra tick`） | `docker compose run --rm core --help` 可啟動 | launchd 走主目錄 `.venv`（`uv run --no-sync`），不經映像；不用容器可略過 |
| 5 | 刪除主目錄的 DolphinDB 時期殘留：`data/downloads/tw_stock/meta/tick/`（`tick_metadata.json`）與 `data/downloads/tw_futures/tick/`（1 個 CSV）。合併前主目錄仍跑舊程式，所以留到這時才刪 | 兩個資料夾不存在；`grep -rn "tick_metadata" core apps scripts` 只剩說明性註解 | `tick_metadata.json` 1 檔（沒有 backup）、`TX202612_20260828.csv` 1 檔 |
| 6 | 主目錄跑完整檢查：`pytest`（設了 `TICK_DATABASE_URL`，`tests/test_stock_tick_timescale.py` 要是 passed 不是 skipped）、`python scripts/check_layer_deps.py`、`scripts/run_regression.sh`（結束碼 3 不算通過） | 全綠；main 的 CI「tick 整合測試（TimescaleDB，不得 skip）」步驟通過 | — |

- **不需要做的事**：launchd 的 `update-db` 跑 `python -m apps.update_db` 不帶 `--target`，預設是 `no_tick`，合併後**不會**自動爬 tick 寫進正式表（2026-10-18 實查 `scripts/launchd/rehearsal_schedule.py`）。
- **資料備份**（不屬收尾，記下去向）：歷史 CSV 的本機副本 `data/tick_history/` 已於 2026-10-10 依使用者指示刪除；資料現存三份——TimescaleDB 本身、`data/backups/alphaedge_tick_20261010.dump`（`pg_dump`，約 7.8 GiB，做過還原測試，使用者已另存雲端）、Google 雲端的原始 CSV。

### 整份移出的條件

上表 6 項做完即整份移出 `backlog/`，依 `manage-backlog` skill §5：

1. 現行說明已在 `docs/pipeline/etl-ingestion.md`〈台股 tick（TimescaleDB）〉（Phase5-3 寫好，含 schema 設計理由、寫入與排除規則、續跑、讀取、歷史匯入），不必另寫 `docs/`。
2. 刪除 `backlog/index.md` 對應列。
3. 其他 backlog 連到本文件的地方改成純文字或指向 `docs/pipeline/etl-ingestion.md`（2026-10-18 有連結的：`PostgreSQL遷移計畫.md`、`core目錄邊界收斂.md`、`滑價模型後續優化.md`、`台股tick級回測框架.md`、`美股ETL與回測架構規劃.md`、`暫緩工作彙整.md`、`重構後全專案健檢.md`），跑 `scripts/check_doc_paths.py` 確認沒有斷鏈。

### 合併後解鎖的其他工作

- [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1（`core/pipeline/` 搬到頂層 `etl/`）：本文件的 pipeline 改動進 main 後，這一半前提才成立；**本分支進 main 前不要先搬**，否則合併時整批路徑衝突。
- [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)：以本分支合併後的 main 為基底；`stock_tick`／`stock_tick_load_log` 表名已佔用。
- [滑價模型後續優化.md](滑價模型後續優化.md) S2：「實際買賣價差」這條依據的資料面在 main 上才可用。

---

## 進度追蹤表

全部 18 步在 `feature/tick-timescaledb` 完成；Phase5-4、Phase5-5 是 2026-10-12 健檢立項、2026-10-13 完成。各步驟的偏離原規格與理由見步驟章節，施作細節在 git 歷史。

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | `docker-compose.yml` 新增 TimescaleDB service | `docker-compose.yml`、`.env.example` | `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` 有值 | ✅ | 2026-10-09（`12a0a47`）：`timescale/timescaledb:2.29.2-pg17`；另加 `name: alphaedge`（偏離原規格） |
| Phase0-2 | 新增 `TICK_DATABASE_URL` 設定 | `core/config/settings.py`、`.env.example`、`tests/test_config_consistency.py` | `pytest tests/test_config_consistency.py` 通過 | ✅ | 2026-10-09（`12a0a47`）：`require_tick_database_url()` |
| Phase0-3 | `[tick]` 選用相依改為 `psycopg`＋`connectorx` | `pyproject.toml`、`uv.lock` | `uv sync --extra tick` 後可 import 兩者 | ✅ | 2026-10-09（`12a0a47`）：Phase3-1 再補 `pyarrow`；`Dockerfile` 補 `--extra tick`（`390408b`） |
| Phase1-1 | 在 `core/dao/` 建立 TimescaleDB 連線入口 | `core/dao/timescale.py`、`scripts/check_layer_deps.py` | 分層檢查通過；`SELECT 1` 可執行 | ✅ | 2026-10-09（`1638f87`） |
| Phase1-2 | 建表：`stock_tick` hypertable 與 `stock_tick_load_log` | `core/dao/tw/stock_tick_dao.py`、`core/config/schema.py` | 建表重跑不報錯；hypertable 查得到 | ✅ | 2026-10-09（`1638f87`）：loader 改寫併入 Phase2-1（偏離原規格） |
| Phase1-3 | 設定壓縮與壓縮 policy | `core/dao/tw/stock_tick_dao.py` | compression job 存在；`compress_chunk` 成功 | ✅ | 2026-10-09（`1638f87`） |
| Phase2-1 | 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等＋兩種 CSV 格式正規化） | `core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py` | 重載不重複；兩種格式都能入庫 | ✅ | 2026-10-09（`b4ad012`）：全量乾跑 1,856 檔 0 錯誤，寫入基準 1,050,896,400 列 |
| Phase2-2 | updater 續跑依據由 `tick_metadata.json` 改為 `stock_tick_load_log` | `core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py` | 模擬中斷後重跑只爬缺的日期 | ✅ | 2026-10-09（`44cd667`）：另修「爬取失敗被當成沒成交」 |
| Phase3-1 | 改寫 `StockTickAPI` 讀取路徑 | `core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py` | 符合〈讀取介面契約〉 | ✅ | 2026-10-09（`68eb43e`）：ConnectorX 以 Arrow 回傳（偏離原規格） |
| Phase3-2 | DataFeed／Adapter 文字與連線生命週期收尾 | `core/backtest/datafeed/tw/stock_datafeed.py`、`core/api/` | DataFeed 的 TickQuote 數＝DB 列數 | ✅ | 2026-10-09（`68eb43e`） |
| Phase4-1 | 盤點本機歷史 CSV、定案排除規則並搬離 updater 資料夾 | 本文件 | 盤點 5 項、裁示 2 項 | ✅ | 2026-10-09 |
| Phase4-2 | 試點：本機**抽樣**檔案入庫與效能量測 | 本文件 | 寫入速度、壓縮率、查詢耗時 | ✅ | 2026-10-09：壓縮約 12 倍、全市場單日查詢換算約 1.2～1.4 秒 |
| Phase4-3 | 歷史 CSV 全量匯入與完整性比對 | `scripts/manual/manual_tick_history_import.py` | 逐「股票 × 交易日」列數比對 | ✅ | 2026-10-10 匯入完成：1,050,896,400 列、1,781,133 組全數相符、壓縮後 12 GB |
| Phase5-1 | 測試改寫與新增 | `tests/test_stock_tick_timescale.py` 等 | `pytest` 全數通過 | ✅ | 2026-10-09 |
| Phase5-2 | 移除台股 tick 的 DolphinDB 程式與設定 | 見步驟詳述 | `grep dolphindb` 只剩說明性註解 | ✅ | 2026-10-09（`a288df4`，期貨 tick 選項 A）；本機資料留到〈合併部署收尾〉第 5 項 |
| Phase5-3 | 更新文件 | `README.md`、`README_en.md`、`docs/` 相關頁 | 文件不再描述 tick 存在 DolphinDB | ✅ | 2026-10-09（`a288df4`）：`docs/pipeline/etl-ingestion.md` 新增 tick 小節 |
| Phase5-4 | `get_chunk_ranges()` 的 chunk 邊界改以 `AT TIME ZONE 'UTC'` 取值 | `core/dao/tw/stock_tick_dao.py`、`tests/test_stock_tick_timescale.py` | UTC 與 Asia/Taipei 兩種 session 結果相同 | ✅ | 2026-10-13（`e47a236`） |
| Phase5-5 | CI 加 TimescaleDB service，讓整合測試在 CI 執行 | `.github/workflows/ci.yml` | CI 中該檔為 passed 而非 skipped | ✅ | 2026-10-13（`e47a236`）：CI run 38347064704 該步驟 19 passed |

---

## 資料與設計摘要

現行設計的完整說明在 `docs/pipeline/etl-ingestion.md`〈台股 tick（TimescaleDB）〉；這裡只留驗收用的基準數字。

- **歷史 CSV（2026-10-09 全量盤點）**：1,856 檔、56 GB、1,061,227,084 列、2020-04-01～2024-05-10 共 1,001 個交易日（與 `price` 表逐日一致）、「股票 × 交易日」1,781,133 組。
  歷史格式沒有 `stock_id` 欄、時間欄叫 `ts`、欄序有 31 種、部分毫秒時間與浮點字串；往後每日更新產生的是 cleaner 格式。loader 兩種都讀。
- **排除規則**（Phase4-1 裁示）：興櫃時期（當天 `price` 表沒有該股）整天排除 10,322,770 列、全零列 7,901 列、負量列 13 列；`price` 表整天沒資料時該日失敗、不登記。排除後寫入 **1,050,896,400 列**。
- **資料表**：`stock_tick`（hypertable，`TIMESTAMP` 無時區、`DOUBLE PRECISION` 價格、`seq` 原始列序、不設主鍵、7 天 chunk、`segmentby = stock_id` 壓縮）與 `stock_tick_load_log`（主鍵 `stock_id, trade_date`，記 `source_rows` 與 `row_count`）。
- **容量與效能**：壓縮後約 12 GB（約 12 倍）；全市場單日 `get_ordered_ticks()` 0.7～1.5 秒。

### 讀取介面契約

| 項目 | 規格 |
|------|------|
| 回傳欄位 | `stock_id, time, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type`（不含 `seq`）；`台股tick級回測框架.md` Phase2-1 另加 `cum_volume`（當日累計量） |
| dtype | `stock_id`: object、`time`: `datetime64[ns]`（naive）、價格 `float64`、量與 `tick_type` `int64` |
| 日期區間 | 半開區間 `[start 00:00, end + 1 天 00:00)` |
| 排序 | `get()`：`stock_id, time, seq`；`get_ordered_ticks()`：`time, stock_id, seq`；`get_stock_ticks()`：`time, seq` |
| 空結果 | 同欄位、同 dtype 的空表；`start_date > end_date` 不發查詢 |

---

## Phase 0：環境

### Phase0-1. `docker-compose.yml` 新增 TimescaleDB service ✅

- **做法與結果（2026-10-09，`12a0a47`）**：service `postgres`，image 鎖 `timescale/timescaledb:2.29.2-pg17`（不可用 `-oss` tag：沒有壓縮），`timescaledb.license = timescale`；named volume、`pg_isready` healthcheck、port 5432；`core` 以 `depends_on: service_healthy` 相依，`live` 不相依。
  service 取名 `postgres` 而不是 `timescaledb`：[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase0-1 沿用同一個 service。2026-09-18 裁示維持 Docker、不走 Homebrew 本機安裝；全量匯入已在 Docker 內完成。
- **偏離原規格**：另加 `name: alphaedge`，讓 worktree 與主目錄共用同一個 volume `alphaedge_alphaedge_pgdata`。2026-10-13 另加 `restart: unless-stopped`（`2ddc26d`）。
- **驗證**：extension 2.29.2、PostgreSQL 17.11。
- **相依**：無。

### Phase0-2. 新增 `TICK_DATABASE_URL` 設定 ✅

- **做法與結果（2026-10-09，`12a0a47`）**：`core/config/settings.py` 新增 `TICK_DATABASE_URL` 與 `require_tick_database_url()`（未設定與空字串都拋出）。本機是 `localhost`，`core` 容器內由 compose 的 `environment` 覆寫成 `postgres`。
- **刻意不用 `DATABASE_URL`**：`PostgreSQL遷移計畫.md` Phase1-2 規劃「設了 `DATABASE_URL` 就把日頻資料切到 PostgreSQL」，共用一個鍵會讓日頻資料在遷移前被切走。那份計畫完成後，可讓 `TICK_DATABASE_URL` 沒設定時退回 `DATABASE_URL`（要不要做由 `PostgreSQL遷移計畫.md` Phase0-2 決定）。
- **驗證**：`pytest tests/test_config_consistency.py` 通過。
- **相依**：無。

### Phase0-3. `[tick]` 選用相依改為 `psycopg`＋`connectorx` ✅

- **做法與結果（2026-10-09，`12a0a47`）**：`[tick]` = psycopg 3.3.6、connectorx 0.4.6，Phase3-1 再補 `pyarrow>=18`；只在 `core/dao/` 內惰性 import。
  `psycopg` 目前在 `[tick]` extra；`PostgreSQL遷移計畫.md` Phase0-3 要把它搬到主 `dependencies`，`[tick]` 只留 `connectorx`／`pyarrow`。
- **偏離原規格**：`dolphindb` 先拆成獨立的 `[dolphindb]` extra（`import dolphindb` 會把自己的目錄塞進 `sys.path`、蓋掉 `frontend/config.py`，不能和其他 extra 裝在一起），Phase5-2 移除後 `uv sync --all-extras` 恢復可用。
- **步驟完成後補修（`390408b`）**：`Dockerfile` 兩行 `uv sync` 都加 `--extra tick`。
- **驗證**：`python -c "import psycopg, connectorx"` 成功。主目錄要連同既有 extra 一起裝（見〈合併部署收尾〉第 1 項）。
- **相依**：無。

---

## Phase 1：連線層與 schema

### Phase1-1. 在 `core/dao/` 建立 TimescaleDB 連線入口 ✅

- **做法與結果（2026-10-09，`1638f87`）**：`core/dao/timescale.py` 提供 `get_tick_database_url()`、`connect_tick_db()`（寫入用 psycopg 連線）、`get_connectorx_uri()`（讀取用）、`tick_db_error_types()`（DAO 以外不 import psycopg 也能精準捕捉）。
  `scripts/check_layer_deps.py` 的資料庫驅動清單擴充為 `sqlite3`／`psycopg`／`connectorx`。採獨立模組、未併入 `core/dao/connection.py`；[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase1-1 完成後可評估是否併入同一入口。
- **驗證**：分層檢查通過、突變驗證過；`SELECT 1` 與 ConnectorX 讀取實連成功。
- **相依**：Phase0-2、Phase0-3。

### Phase1-2. 建表：`stock_tick` hypertable 與 `stock_tick_load_log` ✅

- **做法與結果（2026-10-09，`1638f87`）**：`StockTickDAO.create_tables()`（不繼承綁 SQLite 的 `BaseDAO`）建 extension、兩張表與 `(stock_id, time)` index，全部 `IF NOT EXISTS`；表名帶 schema（`StockTickDAO(schema=...)`），整合測試在暫存 schema 建表。
- **偏離原規格**：`StockTickLoader.create_db()` 改呼叫 DAO、移除 DolphinDB 常數併入 Phase2-1，避免留下半套 DolphinDB 的 loader。
- **驗證**：重跑不報錯、型別符合 DDL、`timescaledb_information.hypertables` 查得到。
- **相依**：Phase1-1。

### Phase1-3. 設定壓縮與壓縮 policy ✅

- **做法與結果（2026-10-09，`1638f87`）**：`segmentby = stock_id`、`orderby = time, seq`，policy 壓 14 天前的 chunk；另提供 `pause_compression_policy()`／`resume_compression_policy()`（policy 以「現在」往回算，匯入歷史時要先停）與 `compress_chunks_before()`。
- **驗證**：policy job 存在、可暫停／恢復、壓縮後列數不變；整合測試 6 條。
- **相依**：Phase1-2。

---

## Phase 2：寫入路徑

### Phase2-1. 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等＋兩種 CSV 格式正規化） ✅

- **做法與結果（2026-10-09，`b4ad012`）**：`normalize_tick_frame()` 依欄名讀兩種格式、`filter_tick_rows()` 套排除規則；每個「股票 × 交易日」一個 transaction 交給 `StockTickDAO.replace_day()`（刪當天 → `COPY ... FROM STDIN (FORMAT csv)` → upsert `load_log`）；逐檔失敗不中斷，最後 `finish_load()` 彙報。判斷上市櫃交易日用新增的 `StockPriceDAO.get_stock_trading_days()`。
- **偏離原規格**：① 寫入改成整塊送出 CSV 文字而非逐列 `write_row()`（十億列級時逐列往返主導耗時）；② 格式差異與排除規則改由 loader 處理而非匯入腳本，日常更新、試點與歷史匯入同一套規則。
- **步驟完成後補修（`5adb56d`）**：`update_db` 的 tick 改排在 price 之後（`price` 表整天沒資料時 tick 該日失敗），`test_targets_that_read_price_run_after_price` 釘住。
- **驗證**：單元 15 條＋整合 4 條，三處突變轉紅；全量乾跑 1,856 檔 0 錯誤，原始 1,061,227,084 列 → 寫入 1,050,896,400 列。
- **相依**：Phase1-2；排除規則相依 Phase4-1。

### Phase2-2. updater 續跑依據改為 `stock_tick_load_log` ✅

- **做法與結果（2026-10-09，`44cd667`）**：`check_date_crawled()` 改吃整次更新只查一次的 `get_loaded_last_dates()`；刪 CSV 的判斷放在 `StockTickLoader.is_fully_loaded()`；metadata 相關函式全數移除。
  **另修一個續跑漏洞**：crawler 原本把 Shioaji 錯誤吞成 `None`、被當成沒成交，改成拋 `ConnectionError`，失敗日只寫之前的日子、下次從失敗日接著爬。`scripts/manual/manual_tick_updater.py` 提前在本步驟刪除（改連 TimescaleDB 後一跑就會寫正式表）。
- **驗證**：`tests/test_stock_tick_updater.py` 單元 17 條、六處突變轉紅；暫存 schema 端到端 `test_updater_resumes_from_load_log` 只重爬缺的那筆。
- **相依**：Phase2-1。

---

## Phase 3：讀取路徑

### Phase3-1. 改寫 `StockTickAPI` 讀取路徑 ✅

- **做法與結果（2026-10-09，`68eb43e`）**：SQL 全在 DAO，只 `SELECT` 契約的欄位並強制 dtype；ConnectorX 不支援佔位符，日期格式化、代號先驗證英數字再嵌入（理由寫在程式註解）；`StockTickAPI(dao=...)` 可注入；連不上時轉成 `ConnectionError` 並提示啟動 TimescaleDB。
- **偏離原規格**：ConnectorX 用 `return_type="arrow"` 再 `.to_pandas()`（直接回 pandas 走 pandas 已棄用的 `make_block`）；空結果改回傳同欄位、同 dtype 的空表。
- **驗證**：契約整合測試 5 條、無 DB 驗證 8 條，五處突變轉紅。
- **相依**：Phase1-1、Phase1-2。

### Phase3-2. DataFeed／Adapter 文字與連線生命週期收尾 ✅

- **做法與結果（2026-10-09，`68eb43e`）**：回測層與 `core/api/` 的 DolphinDB 字樣改掉，README 的 TICK 列改成 `[tick]` 相依與 TimescaleDB 啟動方式。
- **偏離原規格**：驗證改成在暫存 schema 直接比對 DataFeed 的 TickQuote 數與 DB 列數（worktree 沒有日 K 研究庫，跑不了完整回測）。
  （之後 `台股tick級回測框架.md` Phase3-1 把「整天一次給」的 `get_quotes(date, Scale.TICK)` 換成逐筆資料源 `StockTickDataFeed`。）
- **相依**：Phase3-1。

---

## Phase 4：歷史資料遷移

### Phase4-1. 盤點本機歷史 CSV、定案排除規則並搬離 updater 資料夾 ✅

- **結果（2026-10-09）**：全量盤點 1,856 檔（數字見〈資料與設計摘要〉）；`tick_metadata.json` 記 1,920 檔，其中 93 檔沒有 CSV、30 檔 CSV 不在 metadata；原本 541 檔的 2024-05-13～05-15 已不存在，資料庫歷史止於 2024-05-10。
- **裁示（2026-10-09 使用者採用建議）**：① 排除興櫃時期、全零列、負量列，保留盤後列、完全相同的重複列與 bid/ask 為 0 的列；② 歷史搬到 `data/tick_history/`（之後於 2026-10-10 刪除，見〈合併部署收尾〉的資料備份）。
- **相依**：無。

### Phase4-2. 試點：本機抽樣檔案入庫與效能量測 ✅

- **結果（2026-10-09，寫入 `public.stock_tick`，量完已清空）**：19 檔 × 2024-05-08～05-10，`load_log` 與 CSV、DB 列數全數一致；DB 寫入約 12.8 萬列／秒（含讀檔與正規化約 8.9 萬列／秒）；壓縮約 12 倍；全市場單日 `get_ordered_ticks()` 換算約 1.2～1.4 秒（門檻 10 秒）。
  **發現 chunk 邊界是週四**（從 1970-01-01 起算），Phase4-3 改依 chunk 邊界逐 chunk 處理。
- **相依**：Phase1-3、Phase2-1、Phase3-1、Phase4-1。

### Phase4-3. 歷史 CSV 全量匯入與完整性比對 ✅

- **腳本（2026-10-09，`ac42674`）**：`scripts/manual/manual_tick_history_import.py`，預設只列計畫，`--apply` 才匯入、`--verify-only` 只比對、`--resume` 續跑；先依 chunk 切檔，再逐 chunk 載入並壓縮，匯入期間暫停壓縮 policy（`try/finally` 恢復）。
- **匯入紀錄（2026-10-09 18:19～2026-10-10 01:25，台北時間）**：寫入 390 分鐘、213 個 chunk、失敗 0 檔。
  比對：「股票 × 交易日」1,781,133 組全數相符；寫入 1,050,896,400 列（＝乾跑基準）；抽樣 20 組逐列相同；抽樣 5 天成交量÷`price` 表 0.9920～0.9960（盤後零股與定價交易的落差）；14 天前未壓縮的 chunk 0 個，policy 已恢復。大小約 12 GB。
- **相依**：Phase4-1、Phase4-2。

---

## Phase 5：測試與收斂

### Phase5-1. 測試改寫與新增 ✅

- **結果（2026-10-09）**：`tests/test_stock_tick_timescale.py`（未設 `TICK_DATABASE_URL` 時整檔 skip；每個測試用獨立 schema，`search_path` 含 `public` 才建得了 hypertable）；`tests/test_strategy_data_access.py` 禁止策略直接 import `psycopg`／`connectorx`；舊的 `TICK_DB_PATH` 測試隨 Phase5-2 刪除。
- **相依**：Phase2-2、Phase3-1。

### Phase5-2. 移除台股 tick 的 DolphinDB 程式與設定 ✅

- **期貨 tick 裁示（2026-10-09 使用者採選項 A：一併刪除）**：期貨 tick 已裁示不做；舊程式每跑一次就重複寫入一份，不能原樣保留。下游缺口記在 [台期貨平台說明](../docs/futures/tw-futures-platform.md)〈已知限制〉。
- **結果（2026-10-09，`a288df4`）**：期貨 tick 四支程式與測試、`futures_tick` target、`DDB_*`／`TICK_DB_*` 設定、`.env.example` 的 DolphinDB 區塊、`[dolphindb]` extra、`manual_init_tick_metadata.py` 全數移除。
- **偏離原規格**：沒等 Phase4-3 才刪——DolphinDB 主機早已停用、沒有可對照的資料，完整性改以 CSV 本身對帳。
- **驗證**：`grep -rn "dolphindb\|DDB_\|tick_metadata" core apps scripts .env.example` 只剩三處說明「`load_log` 取代了 `tick_metadata.json`」的註解。
- **本機資料**留到〈合併部署收尾〉第 5 項。
- **相依**：Phase5-1。

### Phase5-3. 更新文件 ✅

- **結果（2026-10-09，`a288df4`）**：README 兩語版、`docs/` 相關頁、`core/backtest/README.md`、`strategies/README.md`、`strategy_lab/README.md`、`scripts/manual/README.md` 共 16 份改完；`docs/pipeline/etl-ingestion.md` 新增〈台股 tick（TimescaleDB）〉，收錄 schema 設計理由、寫入與排除規則、續跑、讀取與歷史匯入。
- **驗證**：`grep -rni dolphin` 只剩 etl-ingestion 的語意差異說明與期貨平台文件的「已移除」說明。
- **相依**：Phase5-2。

### Phase5-4. `get_chunk_ranges()` 的 chunk 邊界改以 `AT TIME ZONE 'UTC'` 取值 ✅

- **目的**：chunk 邊界以 `timestamptz` 呈現，session 時區設成 `Asia/Taipei` 時去掉時區會偏 8 小時，歷史匯入的 chunk 邊界核對與壓縮比對會整組偏移。
- **結果（2026-10-13，`e47a236`）**：查詢改為 `range_start／range_end AT TIME ZONE 'UTC'`，Python 端不再 `replace(tzinfo=None)`；`test_chunk_ranges_do_not_depend_on_session_timezone`（UTC、Asia/Taipei 參數化），兩個突變皆紅；正式表 213 個 chunk 在兩種時區下逐筆相同。
- **相依**：無。

### Phase5-5. CI 加 TimescaleDB service，讓整合測試在 CI 執行 ✅

- **目的**：整合測試在沒有 DB 時整檔 skip，CI 對整條 tick 寫入／讀取路徑是盲的。
- **結果（2026-10-13，`e47a236`）**：`check` job 加 `services: postgres`（同版映像）、`TICK_DATABASE_URL`、`uv sync --locked --extra frontend --extra tick`；專屬步驟「tick 整合測試（TimescaleDB，不得 skip）」輸出出現 `skipped` 就 `exit 1`。
  CI run `38347064704` 該步驟 19 passed、整套 2,861 passed；本機模擬連線字串為空時結束碼 1。
- **相依**：無；隨本分支一起合併。

---

## 附：併入的盲捕收斂（原 `暫緩工作彙整.md` S13 的 tick 部分）

2026-10-01 由 `暫緩工作彙整.md` S13 移入（使用者裁示：動到哪個檔案，就在同一步收窄那個檔案）。**2026-10-09 全數處理完（功能分支）：23 處 → 4 處刻意保留**。

| 檔案 | 原處數 | 結果 |
|------|:--:|------|
| `core/pipeline/tw/updaters/stock_tick_updater.py` | 10 | Phase2-2：剩 4 處刻意保留（Shioaji 用量查詢 2 處與登出——Shioaji 1.7 沒有公開例外型別；thread 邊界），理由寫在註解 |
| `core/pipeline/tw/utils/stock_tick_utils.py` | 4 | Phase2-2：隨 metadata 函式移除歸零 |
| `core/pipeline/tw/cleaners/stock_tick_cleaner.py` | 3 | Phase2-2：收窄為具體例外，歸零 |
| `core/pipeline/tw/crawlers/stock_tick_crawler.py` | 1 | Phase2-2：改成轉拋 `ConnectionError` |
| 期貨 tick 的 loader／updater／crawler | 5 | Phase5-2：隨檔案刪除歸零 |

- **驗證**：每處收窄各配一個突變測試（`tests/test_stock_tick_updater.py`）；`tests/test_quality_ratchet.py` 擋住數量回升。

## 風險與對策

| 風險 | 對策 |
|------|------|
| 合併後主目錄缺 `[tick]` 相依 | 〈合併部署收尾〉第 1 項；DAO 內惰性 import，`update_db` 預設 `no_tick` 不碰 tick，未裝之前其他 target 照常 |
| `core/pipeline/` 搬家造成路徑衝突 | `core目錄邊界收斂.md` Phase5-1 暫緩中，本分支進 main 前不要先搬 |
| 回測結果與 DolphinDB 版不同（float32 → float64、同時間戳記跨股票順序固定） | main 上沒有 `Scale.TICK` 的回歸基準，不影響既有回歸；語意差異已寫在 `docs/pipeline/etl-ingestion.md` |
| 讀取效能、磁碟、壓縮 policy、時區、歷史格式、興櫃混入 | **已排除**（Phase4-2、Phase4-3、Phase5-4 的量測與比對） |

---

## 關聯與狀態

- **優先級**：P2（main 上的 tick 級回測沒有資料源；功能分支已完成，剩合併部署）
- **相關程式**（功能分支）：`core/dao/timescale.py`、`core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/{loaders,updaters,utils,cleaners,crawlers}/stock_tick_*.py`、`core/api/tw/stock_tick_api.py`、`core/config/`、`scripts/check_layer_deps.py`、`scripts/manual/manual_tick_history_import.py`、`apps/update_db.py`、`docker-compose.yml`、`Dockerfile`、`.github/workflows/ci.yml`
- **相關 backlog**：
  - [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)：**本文件先做，該計畫沿用本文件建立的基礎設施**——`postgres` service（該計畫 Phase0-1）、`psycopg` 搬到主相依（該計畫 Phase0-3）、`core/dao/timescale.py`（該計畫 Phase1-1）、`TICK_DATABASE_URL` 與 `DATABASE_URL` 分開（該計畫 Phase0-2）；日頻資料遷入同一個 `alphaedge` 資料庫。
  - [台股tick級回測框架.md](台股tick級回測框架.md)：以本文件的資料做逐筆重放，實作分支 `feature/tick-backtest` 從本分支分出，合併順序跟在後面。
  - [滑價模型後續優化.md](滑價模型後續優化.md)：S3（bid/ask 成交）已由 `台股tick級回測框架.md` Phase3-2 完成；S2 的「實際買賣價差」依據相依本文件進 main。
  - [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1：見〈合併後解鎖的其他工作〉。
  - 期貨 tick 不做造成的下游缺口記在 [台期貨平台說明](../docs/futures/tw-futures-platform.md)〈已知限制〉（原 `暫緩工作彙整.md` S9，2026-10-10 移出）。
