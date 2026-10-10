# 台股 tick 改用 TimescaleDB

## Abstract

- **背景／問題**：台股 tick 原本（**main 上至今仍是**）的流程是：Shioaji 爬取 → `data/downloads/tw_stock/tick/{stock_id}.csv` → `loadTextEx` 寫入 DolphinDB（`dfs://tickDB`，TSDB 引擎）→ 回測用 `StockTickAPI` 以 DolphinDB script 查詢。DolphinDB 放在另一台 Windows 主機，2026-09-14 已裁示往後不再使用（台股 tick 不回補、期貨 tick 不做）。於是 main 上的 tick 級回測**沒有可用的資料源**。
  **2026-10-09 使用者把完整歷史 CSV 補回本機**（1,856 檔、56 GB、2020-04-01～2024-05-10、約 10.6 億列，曾放 `data/tick_history/`；全量匯入並比對通過後，2026-10-10 依使用者指示刪除本機這份），並確認**這批就是要入庫的全部資料**；
  **Google 雲端仍保有同一份**，本機這份不是唯一備份。原本那 541 檔（2024-05-13～05-15）已不存在，資料庫的歷史止於 2024-05-10。全量盤點結果見〈CSV 樣式〉與 Phase4-1 的盤點紀錄。
- **目前狀態（2026-10-10）**：**16 步全數在 `feature/tick-timescaledb` 完成，尚未 merge 進 main**。該分支從 `feature/post-rehearsal` 分出，要等後者在模擬演練結束後部署進 main 才能合併；合併前 main 仍是 DolphinDB 程式。合併與部署時剩下的收尾見下方〈現況與合併部署〉。
- **目標**：落地目標改成 TimescaleDB（PostgreSQL extension）：
  - 寫入路徑改用 `COPY`，以「股票 × 交易日」為單位做到冪等寫入。
  - 讀取路徑改用 ConnectorX 直接產生 pandas DataFrame。
  - `StockTickAPI` 的公開方法與回傳欄位不變，`StockDataFeed`／`StockQuoteAdapter`／策略都不用改。
  - 把上述歷史 CSV 一次匯入並壓縮。
- **範圍界線**：
  - **不做期貨 tick**：2026-09-15 已裁示不做；期貨 tick 的 DolphinDB 程式依 Phase5-2 的裁示（2026-10-09 選項 A）與台股部分一併移除，日後要做時另立 backlog。
  - **DolphinDB 殘留**（程式、設定、測試、文件）已由 Phase5-2、Phase5-3 在功能分支清完；本機資料的刪除留到合併部署（見〈現況與合併部署〉）。
  - **不做**台股日頻資料的 SQLite → PostgreSQL 遷移，那是 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 的範圍；本文件建立的 `postgres` service、`[tick]` extra 與 `core/dao/timescale.py` 由該計畫沿用（關係見〈關聯與狀態〉）。
  - **不改**爬蟲（`StockTickCrawler`）與清洗邏輯（`StockTickCleaner` 的欄位格式），也不改 tick 回測引擎的成交語意。
  - **不做** tick 回補續跑到今天。回補要不要做、做多少是另一個決策，本文件只保證 updater 在新儲存上可以續跑。
  - **實作完成後不自動把資料寫進正式資料庫**（2026-09-18 使用者裁示）。以下兩件事都**等使用者明確下指令才執行**，程式與腳本寫好、以抽樣或測試 schema 驗證過即為該步驟完成：
    1. 歷史 CSV 的全量匯入（Phase4-3）。開發過程一律只取少量抽樣（Phase4-2）。**2026-10-09 使用者已下指令，2026-10-10 匯入完成。**
    2. 跑 `python -m apps.update_db --target tick`（main 上是 `tasks.update_db`）——它會實際爬取並寫入正式資料表（Phase2-2 的驗證改以替身取代 crawler）。**尚未執行。**
  - **不修補歷史資料本身**：盤點發現的異常列（興櫃時期、全零列、負成交量等）只依 Phase4-1 裁示的規則在入庫時排除並計數，不回頭改 CSV。
- **驗收標準**（四項都已在功能分支驗過，進 main 前不算整份結案）：
  1. 歷史 CSV 全數入庫，每個「股票 × 交易日」的 DB 列數＝CSV 列數－依規則排除的列數。**2026-10-10 已驗**（Phase4-3 匯入紀錄：1,781,133 組全數相符）。
  2. `StockTickAPI` 四個公開方法在 TimescaleDB 上回傳相同欄位；Mac 本機查一天全市場 `get_ordered_ticks()` 的耗時已記錄，並符合 Phase4-2 定下的門檻。**已驗**（讀取契約整合測試；正式資料上 0.7～1.5 秒，門檻 10 秒）。
  3. `python -m apps.update_db --target tick` 可寫入 TimescaleDB，重跑同一區間不產生重複列。**這一條以替身或測試 schema 驗證**；真的對正式資料表跑更新要等使用者指令。**已驗**（Phase2-2 的 `test_updater_resumes_from_load_log`）。
  4. 台股 tick 相關程式不再 import `dolphindb`，`pytest` 全數通過。**已驗**（Phase5-2 的 `grep` 只剩說明性註解）。

---

> **分層前提（2026-09-16 定案，2026-09-25 併入本文）：** 資料存取層統一放在 `core/dao/`（設計見 [資料存取層](../docs/dev/data-access-layer.md)），
> 不另建 `core/db/`。tick 的連線放 `core/dao/timescale.py`，DDL 與所有 SQL 收進 `core/dao/tw/stock_tick_dao.py`；
> `StockTickLoader`／`StockTickUtils`／`StockTickAPI` 只呼叫 DAO，不直接 import `psycopg`／`connectorx`（分層檢查會擋）。

> **現況與合併部署（2026-10-10 依三個分支實查）**：
> - **分支位置**：`feature/tick-timescaledb` 包含 `feature/post-rehearsal` 的全部內容（`tasks/` 併入 `apps/`、具體策略搬到頂層 `strategies/`，見 `回測與實盤入口拆分及架構收斂.md` Phase1-8、`core目錄邊界收斂.md` Phase2-2～2-4），再加上本文件的工作。
>   所以本文件的路徑一律以功能分支為準（`apps/update_db.py`、`strategies/README.md`）；**main 上仍是 `tasks/update_db.py`、DolphinDB 版的 tick 程式與只有 `core`／`live`／`frontend` 三個 service 的 compose**。
> - **合併順序**：`feature/post-rehearsal` 演練後部署進 main → 再合併 `feature/tick-timescaledb`。合併後依 `manage-backlog` skill §5 整份移出（`docs/pipeline/etl-ingestion.md` 的 tick 小節已在 Phase5-3 寫好）。
> - **合併後的收尾**（都是既有步驟留下的，不是新步驟）：
>   1. 主目錄 `uv sync --extra frontend --extra lab --extra tick`（Phase0-3：只寫 `--extra tick` 會拔掉另外兩個）；`.env` 補 `TICK_DATABASE_URL` 與 `POSTGRES_*`（Phase0-1、Phase0-2）；`docker compose build` 重建映像。
>   2. **資料庫不必重灌**：compose 寫死專案名稱（Phase0-1），worktree 匯入的資料就在主目錄會用的同一個 volume `alphaedge_alphaedge_pgdata`。
>   3. 刪除主目錄的 `tick_metadata.json`（含 backup）與期貨 tick 的 1 個 CSV（Phase5-2）。主目錄在合併前仍跑舊程式，所以留到這時才刪。
>   4. ~~`data/tick_history/` 去留~~：**2026-10-10 已刪除**（使用者指示，連同匯入暫存 `data/tick_import_work/`）。這批資料現存三份：TimescaleDB 本身、
>      `data/backups/alphaedge_tick_20261010.dump`（`pg_dump`，約 7.8 GB，已做過還原測試）、Google 雲端的原始 CSV。
>      **該 dump 仍與資料庫在同一台 Mac 上，須由使用者另複製到雲端或外接硬碟**（尚未確認完成）。
>   5. 第一次對正式資料表跑 `--target tick` 仍要等使用者下指令（〈範圍界線〉）。
> - **步驟完成後在功能分支另修的兩件事**（2026-10-09）：
>   - `5adb56d`：`update_db` 的 tick 改排在 price 之後。tick 入庫以 `price` 表判斷上市櫃交易日（Phase2-1），排在前面的話 `--target all` 收盤後跑當天的 tick 一律失敗；`test_targets_that_read_price_run_after_price` 釘住順序。
>   - `390408b`：`Dockerfile` 兩行 `uv sync` 都加 `--extra tick`。compose 已讓 `core` 連 TimescaleDB，映像卻沒有 psycopg／ConnectorX；`test_image_installs_tick_extra_when_core_uses_timescaledb` 釘住。
> - **與 `core目錄邊界收斂.md` Phase5-1 的先後**：該步驟把 `core/pipeline/` 搬到頂層 `etl/`，暫緩到本文件與 `PostgreSQL遷移計畫.md` 涉及 pipeline 的步驟完成。本文件的 pipeline 改動已寫完，但還沒進 main；那一步要等本分支合併後再做，避免搬兩次。
> - **與盤中動能策略的關係**：`盤中動能策略.md` S2 的日 K 近似（成交量、停損先後）要靠逐筆資料校準；但宣告 `is_tick_triggered` 的策略跑 `Scale.TICK`
>   回測會被引擎拒絕（`IntradayScaleMismatchError`，tick 回測一次給整天、實盤一次一筆，語意不同）。本文件讓逐筆資料可查，校準要另寫研究腳本，不在本文件範圍。

## 進度追蹤表

全部步驟都在 `feature/tick-timescaledb` 完成、**尚未 merge 進 main**；「備註」欄只留結論，細節與偏離原規格的理由在各步驟的實作紀錄。

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | `docker-compose.yml` 新增 TimescaleDB service | `docker-compose.yml`、`.env.example` | `psql` 連線後 `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` 有值 | ✅ | 2026-10-09（`12a0a47`）：`timescale/timescaledb:2.29.2-pg17`，license `timescale`；**另加 `name: alphaedge`（偏離原規格）**讓 worktree 與主目錄共用 volume。`PostgreSQL遷移計畫.md` Phase0-1 沿用這個 service |
| Phase0-2 | 新增 `TICK_DATABASE_URL` 設定 | `core/config/settings.py`、`.env.example`、`tests/test_config_consistency.py` | `pytest tests/test_config_consistency.py` 通過 | ✅ | 2026-10-09（`12a0a47`）：`require_tick_database_url()` 對未設定與空字串都拋出（新測試 2 條）。刻意不用 `DATABASE_URL`，理由見步驟詳述 |
| Phase0-3 | `[tick]` 選用相依改為 `psycopg`＋`connectorx` | `pyproject.toml`、`uv.lock` | `uv sync --extra tick` 後可 import 兩者 | ✅ | 2026-10-09（`12a0a47`）：psycopg 3.3.6、connectorx 0.4.6，Phase3-1 再補 `pyarrow`；`dolphindb` 先拆成獨立 extra（偏離原規格），Phase5-2 移除 |
| Phase1-1 | 在 `core/dao/` 建立 TimescaleDB 連線入口 | `core/dao/timescale.py`、`scripts/check_layer_deps.py` | `python scripts/check_layer_deps.py` 通過；`SELECT 1` 可執行 | ✅ | 2026-10-09（`1638f87`）：`SELECT 1` 與 ConnectorX 讀取實連成功；驅動檢查擴充為 `sqlite3`／`psycopg`／`connectorx`，突變驗證過 |
| Phase1-2 | 建表：`stock_tick` hypertable 與 `stock_tick_load_log` | `core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py`、`core/config/schema.py` | 建表函式重跑兩次不報錯；`timescaledb_information.hypertables` 查得到 | ✅ | 2026-10-09（`1638f87`）：`StockTickDAO.create_tables()` 重跑不報錯、型別符合 DDL；loader 的 `create_db()` 改寫併入 Phase2-1（偏離原規格） |
| Phase1-3 | 設定壓縮與壓縮 policy | `core/dao/tw/stock_tick_dao.py` | `timescaledb_information.jobs` 有 compression job；手動 `compress_chunk` 成功 | ✅ | 2026-10-09（`1638f87`）：policy job 存在、可暫停／恢復、壓縮後列數不變；整合測試 6 條 |
| Phase2-1 | 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等＋兩種 CSV 格式正規化） | `core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py` | 新增的整合測試：同一份 CSV 載入兩次，列數不變；歷史格式與 cleaner 格式各一份樣本都能入庫 | ✅ | 2026-10-09（`b4ad012`）：單元 15 條＋整合 4 條，三處突變轉紅；全量乾跑 1,856 檔 0 錯誤，寫入基準 1,050,896,400 列 |
| Phase2-2 | updater 續跑依據由 `tick_metadata.json` 改為 `stock_tick_load_log` | `core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py` | 以樣本資料模擬中斷後重跑，只爬缺的日期 | ✅ | 2026-10-09（`44cd667`）：續跑改查 `load_log`；另修「爬取失敗被當成沒成交」；台股 tick 盲捕 18 → 4（刻意保留）；單元 17 條、六處突變轉紅，端到端重跑只重爬缺的那筆 |
| Phase3-1 | 改寫 `StockTickAPI` 讀取路徑 | `core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py` | 四個方法的欄位、dtype、排序符合〈讀取介面契約〉；`tests/test_api_public_interfaces.py` 通過 | ✅ | 2026-10-09（`68eb43e`）：ConnectorX 以 Arrow 回傳再轉 pandas（偏離原規格）；契約整合測試 5 條、無 DB 驗證 8 條，五處突變轉紅 |
| Phase3-2 | DataFeed／Adapter 文字與連線生命週期收尾 | `core/backtest/datafeed/tw/stock_datafeed.py`、`core/api/base.py`、`core/api/__init__.py`、`core/api/tw/__init__.py` | tick 級回測跑完後連線有關閉（log 可見） | ✅ | 2026-10-09（`68eb43e`）：DolphinDB 字樣改掉；驗證改以 DataFeed `get_quotes()` 比對 TickQuote 數（偏離原規格） |
| Phase4-1 | 盤點本機歷史 CSV、定案排除規則並搬離 updater 資料夾 | 本文件（盤點紀錄） | 盤點紀錄涵蓋 5 項；兩項裁示已記錄；搬移後檔數 1,856、總大小不變 | ✅ | 2026-10-09：全量盤點完成；使用者採用兩項建議；歷史已搬到 `data/tick_history/`（1,856 檔、56 GB） |
| Phase4-2 | 試點：本機**抽樣**檔案入庫與效能量測 | 本文件（量測紀錄） | 記錄入庫耗時、壓縮前後大小、單日全市場查詢耗時（以樣本列數換算） | ✅ | 2026-10-09：19 檔 × 3 天；寫入約 8.9 萬列／秒、壓縮約 12 倍、全市場單日查詢換算約 1.2～1.4 秒（門檻 10 秒）；發現 chunk 邊界是週四；量完已清空 |
| Phase4-3 | 歷史 CSV 全量匯入與完整性比對 | `scripts/manual/manual_tick_history_import.py` | 每個「股票 × 交易日」：`load_log.source_rows`＝CSV 列數、DB 列數＝`load_log.row_count`＝`source_rows`－排除列數 | ✅ | 腳本 2026-10-09（`ac42674`）；2026-10-09 使用者下指令，**2026-10-10 匯入完成**：213 個 chunk 0 失敗、1,050,896,400 列（＝乾跑基準）、1,781,133 組全數相符、壓縮後 12 GB、正式資料單日查詢 0.7～1.5 秒 |
| Phase5-1 | 測試改寫與新增 | `tests/test_api_public_interfaces.py`、`tests/test_strategy_data_access.py`、`tests/test_entrypoint_and_logging.py`、`tests/test_stock_tick_timescale.py` | `pytest` 全數通過；無 DB 的環境整合測試自動 skip | ✅ | 2026-10-09（分散在 `b4ad012`～`a288df4`）：最後兩條 `TICK_DB_PATH` 測試隨 Phase5-2 刪除 |
| Phase5-2 | 移除台股 tick 的 DolphinDB 程式與設定 | 見步驟詳述 | `grep -rn "dolphindb\|DDB_" core/api core/pipeline/tw/*/stock_tick* apps` 無結果 | ✅ | 2026-10-09（`a288df4`，期貨 tick 選項 A）：台股與期貨 tick 的 DolphinDB 程式、設定、extra 全數移除；沒等 Phase4-3（偏離原規格）；本機資料留到合併部署時刪 |
| Phase5-3 | 更新文件 | `README.md`、`README_en.md`、`docs/` 相關頁、`core/backtest/README.md`、`strategies/README.md` | 文件中不再描述 tick 存在 DolphinDB | ✅ | 2026-10-09（`a288df4`）：16 份文件改完，`etl-ingestion.md` 新增 tick 小節（含 schema 設計理由） |

---

## 改寫前的現況盤點（2026-09-16；main 上至今仍是這套）

### DolphinDB 的存檔方式

`StockTickLoader.create_db()`（`core/pipeline/tw/loaders/stock_tick_loader.py`）：

```text
database "dfs://tickDB"
  partitioned by VALUE(2020.03.01..2030.12.31), HASH([SYMBOL, 25])
  engine='TSDB'
table "tick"
  stock_id SYMBOL, time NANOTIMESTAMP, close FLOAT, volume INT,
  bid_price FLOAT, bid_volume INT, ask_price FLOAT, ask_volume INT, tick_type INT
  partitioned by time, stock_id
  sortColumns=[`stock_id, `time]
  keepDuplicates=ALL
```

寫入：`add_to_db()` → `append_all_csv_to_dolphinDB()`。用一段 DolphinDB script 對整個資料夾的 CSV 逐一 `loadTextEx`，整批只有一個 try。**DB 層沒有冪等保護**（`keepDuplicates=ALL`），同一份 CSV 載入兩次就會重複。重複載入只靠 updater 開頭的邏輯避免：比對 `tick_metadata.json` 與 CSV 最後日期，已入庫的 CSV 先刪掉。

### DolphinDB 的取資料方式

`StockTickAPI`（`core/api/tw/stock_tick_api.py`）：

| 方法 | 查詢 | 使用端 |
|------|------|--------|
| `get(start, end)` | `select * where time between nanotimestamp(start):nanotimestamp(end+1)` | 無呼叫端 |
| `get_ordered_ticks(start, end)` | 同上，加 `order by time` | `StockDataFeed.get_quotes()`，**回測每個交易日呼叫一次、start＝end**，結果交給 `StockQuoteAdapter.from_tick_rows(ticks, date)` |
| `get_stock_ticks(stock_id, start, end)` | 同上，加 `stock_id=` 條件 | `get_last_tick()` |
| `get_last_tick(stock_id, date)` | `get_stock_ticks(...).iloc[-1:]` | 無呼叫端（有單元測試） |

- `between a:b` 兩端都包含，會多含到隔天 00:00:00 那一個瞬間。台股沒有這個時間點的 tick，實際上沒影響，改寫時直接換成半開區間。
- `get()` 的 docstring 說「個股各自排序好」，但 query 沒有 `order by`，順序其實是分區掃描順序，沒有保證。

使用端：只有 `StockDataFeed.setup()` 在 `strategy.scale == Scale.TICK` 時建立 `StockTickAPI()`。`StockQuoteAdapter.from_tick_rows()`（`core/adapters/tw/stock_quote_adapter.py`）以 `itertuples()` 逐列交給 `from_tick_row()`，讀 `stock_id`／`time`／`close`／`volume`／`bid_*`／`ask_*`／`tick_type` 建立 `TickQuote`；adapter 是純轉換，查詢由 DataFeed 負責。**目前沒有任何策略使用 `Scale.TICK`。**

### ETL 流程

```text
StockTickUpdater.update()
  ├─ 讀 tick_metadata.json，刪除「最後日期 ≤ metadata last_date」的 CSV
  ├─ update_multithreaded()：多個 Shioaji 帳號各開一條 thread
  │    └─ update_thread()：逐檔 check_date_crawled() 跳過已爬日期 → crawl → StockTickCleaner.clean_stock_tick()
  │         └─ 以暫存檔覆寫 tick/{stock_id}.csv（整段爬取區間一個檔，覆寫不是附加）
  ├─ StockTickLoader.add_to_db()：整個資料夾一次 loadTextEx
  └─ StockTickUtils.update_tick_metadata_from_csv()：掃 CSV 的最後日期寫回 tick_metadata.json
```

`tick_metadata.json` 記的是「每檔股票最後的日期」，但這個日期**是從 CSV 掃出來的，不是從資料庫查出來的**。所以只要入庫失敗但 CSV 還在，metadata 照樣會前進，下次就會跳過這些日期。現況 1,920 檔中：1,379 檔停在 2024-05-10、540 檔停在 2024-05-15、1 檔停在 2024-05-14。

### CSV 樣式（2026-10-09 全量盤點）

**有兩種格式，loader 兩種都要讀**（Phase2-1）：

1. **歷史格式**：本機全部 1,856 檔都是這種，也是要入庫的全部資料。

   ```csv
   ts,close,volume,bid_price,bid_volume,ask_price,ask_volume,tick_type
   2020-04-01 09:00:06.042130,39.5,105,39.5,31,39.55,7,0
   ```

   - **沒有 `stock_id` 欄**，股票代號只在檔名（`1101.csv`）；時間欄叫 `ts`。
   - **欄位順序每檔不一**：8 個欄名的集合都相同，但共有 31 種排列（最常見的 `ts,close,volume,...` 只有 614 檔）。**一律依欄名讀，不可依位置**。
   - 8 檔把整數寫成浮點字串（`105.0`、`tick_type` 為 `1.0`），共 26,335,568 列。
2. **cleaner 格式**：`StockTickCleaner` 現行的輸出，往後每日更新產生的是這種（2026-09-16 以 541 檔實測，那批檔案已不存在）。

   ```csv
   stock_id,time,close,volume,bid_price,bid_volume,ask_price,ask_volume,tick_type
   3605,2024-05-13 09:00:19.577032,42.0,80,41.9,1,42.0,21,1
   ```

歷史格式的實測值域（1,856 檔全掃，無空值、`ts` 全部可解析）：

| 欄位 | 實測值域 | 備註 |
|------|----------|------|
| 檔名代號 | 全為 4 位數字 | 不含 ETF；仍要以字串保存（全市場有 `00878` 這類前導 0 代號，cleaner 格式讀 CSV 時一定要 `dtype=str`） |
| `ts` | 26 字元（microsecond）1,051,746,343 列；**23 字元（millisecond）9,480,741 列** | 毫秒格式只出現在 113 檔、而且全在興櫃時期（見下方〈興櫃時期〉）；無時區，為台北當地時間 |
| 時段 | 09:00:00～16:55:00 | 13:30 之後 1,731,924 列（盤後定價等）；另有 2 列 `00:00:00` |
| `close` | 0～5,490 | **23,215 列為 0**（621 檔，整列全零，屬壞資料）；107,203 列帶浮點誤差（`6.5600000000000005`），要四捨五入到 2 位小數 |
| `volume` | −1,232～1,713,000 | **14 列為負值**（14 檔，全在 2022-01-26 14:17～14:20）；72,314 列為 0；超過 2 萬的 33,982 列主要是興櫃時期以「股」為單位的資料 |
| `bid_price`／`ask_price` | 0～5,495；約 2.8% 為 0 | 0 表示該側無委託（例如鎖漲跌停），不是缺值；兩側同時為 0 的 29,922,092 列 |
| `bid_volume`／`ask_volume` | 0～6,912,897 | 百萬級的極端值同樣出在興櫃時期 |
| `tick_type` | 0／1／2 | `{1: 外盤, 2: 內盤, 0: 無法判定}` |

- **總列數 1,061,227,084**，1,856 檔、56 GB（約 57 bytes／列）；最大 `2303.csv` 16,530,473 列（1.05 GB），最小 `6902.csv` 1,435 列。
- **交易日 1,001 天（2020-04-01～2024-05-10），與 `price` 表同期間的交易日逐日一致**，沒有整天缺漏；1,849 檔的最後一天是 2024-05-10。
- 每日列數最少 551,783（2023-01-16）、中位數 997,013、最多 2,770,526；每天有 1,699～1,850 檔有成交；「股票 × 交易日」共 1,781,133 組。
- **同一天內的列不保證依時間排序**：1,652 檔共 991,457 處時間倒退，例如 14:30:00 的盤後列排在 13:30:00 的收盤列之前（2021-08-03）；跨日則完全依序。所以 `seq` 記原始列序，讀取一律 `ORDER BY time, seq`。
- **`(股票, ts)` 不唯一**：45,992,118 列（4.3%）與其他列共用時間戳記，屬於同一瞬間撮合的多筆成交；整列完全相同的有 1,424,359 列，主要集中在興櫃時期（`6770.csv` 一檔就有 529,484 列）。這兩欄不能當主鍵，也不能用 `DISTINCT` 去重。

#### 興櫃時期

**173 檔、62,264 個「股票 × 交易日」、10,322,770 列（約 1.0%）在 `price` 表找不到對應**，逐檔對照都是上市櫃之前的興櫃時期，例如：

| 檔案 | tick 涵蓋 | `price` 表起日 |
|------|-----------|----------------|
| `6770.csv` | 毫秒格式 2020-12-09～2021-12-03，之後微秒格式 | 2021-12-06（上市日） |
| `6821.csv` | 2021-04-20 起（微秒格式） | 2023-05-22 |
| `1563.csv` | 全段毫秒格式 | 2024-05-13（tick 迄日之後才上市櫃） |

- 興櫃以「股」為單位、交易到 15:00 之後，和上市櫃的「張」、13:30 收盤**語意不同**，混進去會讓成交量差上千倍。
- **不能只靠格式判斷**：113 檔的興櫃時期是毫秒格式，另外 60 檔的興櫃時期已經是微秒格式。可靠的判準是「當天 `price` 表有沒有這檔」。

---

## 資料表設計

### `stock_tick`（hypertable）

```sql
CREATE TABLE IF NOT EXISTS stock_tick (
    stock_id    TEXT             NOT NULL,  -- 股票代號（保留前導 0）
    time        TIMESTAMP        NOT NULL,  -- 成交時間（台北當地時間，microsecond）
    seq         INTEGER          NOT NULL,  -- 同一股票、同一交易日內的原始列序（從 0 起算）
    close       DOUBLE PRECISION NOT NULL,  -- 成交價
    volume      INTEGER          NOT NULL,  -- 成交量（Unit: Lot）
    bid_price   DOUBLE PRECISION NOT NULL,  -- 委買價（0 表示無委買）
    bid_volume  INTEGER          NOT NULL,  -- 委買量（Unit: Lot）
    ask_price   DOUBLE PRECISION NOT NULL,  -- 委賣價（0 表示無委賣）
    ask_volume  INTEGER          NOT NULL,  -- 委賣量（Unit: Lot）
    tick_type   SMALLINT         NOT NULL CHECK (tick_type IN (0, 1, 2))  -- 內外盤別
);

SELECT create_hypertable(
    'stock_tick',
    by_range('time', INTERVAL '7 days'),
    if_not_exists => TRUE
);

-- 未壓縮 chunk（最近寫入的資料）按股票查詢用；壓縮後改由 segmentby 定位
CREATE INDEX IF NOT EXISTS stock_tick_stock_id_time_idx
    ON stock_tick (stock_id, time);
```

### `stock_tick_load_log`（一般資料表）

```sql
CREATE TABLE IF NOT EXISTS stock_tick_load_log (
    stock_id    TEXT        NOT NULL,
    trade_date  DATE        NOT NULL,
    source_rows INTEGER     NOT NULL,             -- 來源 CSV 當天的原始列數
    row_count   INTEGER     NOT NULL,             -- 實際寫入的列數；與 source_rows 的差＝依規則排除的列數
    source_file TEXT        NOT NULL,             -- 來源 CSV 檔名，出問題時可回溯
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trade_date)
);
```

### 設計決策

| 決策 | 選擇 | 理由 |
|------|------|------|
| 表名 | `stock_tick`（原 DolphinDB 為 `tick`） | 會和日頻資料共用同一個 PostgreSQL 資料庫；[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 已決定台股表名要補上 `stock_` 前綴，這裡直接照新規則命名 |
| 識別欄名 | 維持 `stock_id` | `TickQuote`、`StockQuoteAdapter` 都讀 `stock_id`；`stock_id → symbol` 的改名在 PostgreSQL 遷移計畫裡跨所有表一起做，tick 單獨先改會讓兩邊不一致 |
| `time` 型別 | `TIMESTAMP`（無時區） | 資料本身沒有時區，DolphinDB 的 `NANOTIMESTAMP` 也沒有。用 `TIMESTAMPTZ` 的話，ConnectorX 讀出來會是 UTC 的 tz-aware 欄位，回測拿去比日期時會差 8 小時。台股只有一個時區，沒有換算需求 |
| 時間精度 | microsecond | PostgreSQL 的上限；cleaner 本來就只保留到 microsecond，DolphinDB 的 nanosecond 精度從未用到 |
| 價格型別 | `DOUBLE PRECISION` | DolphinDB 的 `FLOAT` 是 4 bytes，`33.55` 讀出來是 `33.549999…`，跟 tick size 比對或做等值判斷時容易出錯。`NUMERIC` 雖然精確，但讀進 pandas 會變成 `Decimal` object 欄，轉換慢又不能做向量化運算。壓縮後 double 的空間成本很小 |
| 量的型別 | `INTEGER` | 排除後單筆最大 358,476 張（Phase2-1 全量乾跑），原始資料含興櫃時期以「股」計的值最大到 1,713,000，委買賣量最大 6,912,897，`SMALLINT` 不夠、`INTEGER` 足夠 |
| `bid/ask` 為 0 | 照存 0，不轉 `NULL` | 和現行語意一致（`TickQuote` 預設 0.0），使用端不用多處理 `NaN` |
| `seq` 欄 | 新增 | ① 同一時間戳記有多筆成交，要有 `seq` 才能得到可重現的排序；② 完整性比對時可以逐列對照 CSV |
| 主鍵／唯一鍵 | **不設**，冪等由「刪除後重寫＋`load_log`」保證 | hypertable 的唯一索引一定要含 `time`；`(stock_id, time, seq)` 雖然可以當唯一鍵，但每筆寫入都要檢查唯一性，歷史匯入會明顯變慢，寫入壓縮 chunk 時還要先解壓。寫入單位本來就是「股票 × 交易日」，整段刪除後重寫已經足夠 |
| chunk 間隔 | 7 天 | 實測 2020-04～2024-05 共 1,001 個交易日：1 天一個 chunk 會產生約 1,500 個 chunk（含非交易日），planning 成本偏高；7 天約 220 個（Phase4-3 實際 213 個）。每天查詢靠壓縮 batch 的 min/max `time` 跳過不相關資料，不需要 chunk 剛好切在一天。Phase4-2 量測後維持 7 天（全市場單日查詢遠低於門檻）；chunk 從 1970-01-01（週四）起算，邊界是週四 |
| 壓縮 | `segmentby = stock_id`、`orderby = time, seq` | 按股票查詢時可以直接定位 segment；同一 segment 內依時間排序，delta 編碼壓縮率高 |
| `load_log` 粒度 | 股票 × 交易日 | 取代 `tick_metadata.json` 的「每檔最後日期」，還能逐日核對列數；主鍵天然防止重複登記 |
| `load_log` 記兩個列數 | `source_rows` 與 `row_count` | 入庫時會依規則排除壞列與興櫃時期（Phase4-1 裁示），只記寫入列數就無法和 CSV 對帳；兩者的差就是排除的列數 |
| 異常列 | 入庫時依規則排除，不改 CSV | 原始檔保持原樣才能重跑、重新比對；排除規則寫在 loader，日常更新與歷史匯入共用同一套 |

### 容量推估（列數與每列大小皆為 2026-10-09 實測）

- **實測總列數 1,061,227,084**，平均每天 106 萬列；排除後寫入 **1,050,896,400 列**（Phase2-1 全量乾跑）。
- **未壓縮每列約 167 bytes**（Phase4-2 實測：資料約 104 bytes＋`(stock_id, time)` 索引約 62 bytes）→ 若全部不壓縮約 **175 GB**，比原推估的 85 GB 多一倍，多出來的主要是索引。
- **壓縮後每列約 13.6 bytes**（壓縮率約 12 倍，壓縮後索引幾乎不佔空間）→ 全量壓縮後推估約 14 GB；**Phase4-3 實際約 12 GB**。
- chunk 間隔 7 天約 530 萬列／chunk，未壓縮約 0.9 GB。Phase4-3「載入一個 chunk、壓縮一個 chunk」時峰值只多出約 1 GB。
- Docker Desktop 的磁碟上限：`Docker.raw` 的上限目前是 926 GB（2026-10-09 實查），14 GB 加上 1 GB 峰值遠低於此，**不必再調**。

---

## 讀取介面契約

`StockTickAPI` 改寫後必須符合下表；`StockQuoteAdapter` 依賴這些欄位名稱與型別。

| 項目 | 規格 |
|------|------|
| 回傳欄位與順序 | `stock_id, time, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type`（**不含 `seq`**，避免改動 `TickQuote` 的建構流程） |
| dtype | `stock_id`: object（str）、`time`: `datetime64[ns]`（naive）、價格：`float64`、量與 `tick_type`：`int64` |
| 日期區間 | `time >= start_date 00:00 AND time < (end_date + 1 day) 00:00`（半開區間） |
| `get()` 排序 | `ORDER BY stock_id, time, seq`（補上 docstring 原本承諾、但 DolphinDB 版沒有保證的排序） |
| `get_ordered_ticks()` 排序 | `ORDER BY time, stock_id, seq`（跨股票同一時間戳記的順序固定下來，回測才可重現） |
| `get_stock_ticks()` 排序 | `ORDER BY time, seq` |
| `get_last_tick()` | 維持 `get_stock_ticks(...).iloc[-1:]`，不改寫成 SQL（既有單元測試以替身驗證這段邏輯） |
| 空結果 | 回傳空的 `pd.DataFrame()`，與現行一致 |
| `start_date > end_date` | 直接回空表，不發查詢（維持現行） |

---

## Phase 0：環境

### Phase0-1. `docker-compose.yml` 新增 TimescaleDB service ✅

- **目的**：提供本機可重現的 TimescaleDB。放在 Mac 本機，回測讀取就不用走網路。
- **做法**：
  - 新增 service `postgres`，image 用 `timescale/timescaledb`，**實作時鎖定 `2.x-pg17` 的明確版本**，不要用 `latest`。`by_range()` 需要 TimescaleDB 2.13 以上。
  - **不可選 `-oss` 結尾的 tag**：那是只含 Apache 授權功能的版本，**沒有壓縮**（Phase1-3 的 `compress`、壓縮 policy 都屬 Timescale License）。驗證時一併確認 `SHOW timescaledb.license` 為 `timescale`。
  - 使用 named volume `alphaedge_pgdata`。**不要 bind mount 到專案目錄**：Docker Desktop on Mac 的 bind mount 走檔案共享，大量寫入時非常慢，而且專案目錄以前放在同步資料夾吃過虧。
  - 加上 `healthcheck`（`pg_isready`）、port `5432:5432`，帳密從 `.env` 的 `POSTGRES_USER`／`POSTGRES_PASSWORD`／`POSTGRES_DB=alphaedge` 讀取。
  - compose 現有 `core`（回測與資料更新，`./data` 唯讀掛載）、`live`（實盤）、`frontend` 三個 service。tick 只給回測與資料更新用，**`core` 加 `depends_on: postgres`（`condition: service_healthy`）**；`live` 不讀 tick、不必相依。
  - `.env.example` 補上述三個鍵。
  - service 名稱用 `postgres` 而不是 `timescaledb`：[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase0-1 會沿用同一個 service，TimescaleDB image 本身就是完整的 PostgreSQL。
  - ~~Docker Desktop 的 Disk usage limit 要調到 ≥ 150 GB~~：**2026-10-09 實查上限已是 926 GB**，而 Phase4-2 實測全量壓縮後約 14 GB、逐 chunk 壓縮的峰值只多約 1 GB，不必調（見〈容量推估〉）。
- **2026-09-18 裁示：維持 Docker，不走本機安裝。** 曾評估以 Homebrew 直接裝 `postgresql@17` ＋ `timescale/tap` 的 `timescaledb`（程式面完全不受影響——Phase0-2 之後只認 `TICK_DATABASE_URL`，只有本步驟會變）。本機路線的好處是原生 I/O、沒有 Docker Desktop 的磁碟上限；代價是 brew 只給當下版本、鎖不住 `2.x-pg17`，`brew upgrade` 動到 postgres 時要重跑 `timescaledb-tune`，且 [PostgreSQL遷移計畫](PostgreSQL遷移計畫.md) Phase0-1「沿用同一個 service」的規劃要改寫。**結論是維持 Docker**：全量匯入已改為等使用者要求（見〈範圍界線〉），I/O 的代價在抽樣試點根本不會發生，而版本鎖定與跨計畫共用是現在就受用的。~~真的要做全量匯入時，可再評估是否改走本機 instance~~：**Phase4-3 已在 Docker 內完成全量匯入**（寫入 390 分鐘、壓縮後 12 GB），沒有改走本機的理由。
- **實作紀錄（2026-10-09，`feature/tick-timescaledb`）**：image 鎖 `timescale/timescaledb:2.29.2-pg17`，實測 extension 2.29.2、`timescaledb.license = timescale`、PostgreSQL 17.11。
  **另加 `name: alphaedge`**（偏離原規格）：compose 預設以目錄名當專案名，在 git worktree 裡啟動會變成另一個專案、另建一份 `pgdata` volume；寫死後 volume 固定為 `alphaedge_alphaedge_pgdata`，主目錄與 worktree 共用同一個資料庫。
  帳密以 `POSTGRES_*` 內插、預設皆為 `alphaedge`，登記在 `tests/test_config_consistency.py` 的「範本有列、程式不讀」例外清單。
- **產出**：`docker-compose.yml`、`.env.example`。
- **驗證方式**：`docker compose up -d postgres` 之後，執行 `docker compose exec postgres psql -U $POSTGRES_USER -d alphaedge -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"` 有回傳版本號。
- **相依**：無。

### Phase0-2. 新增 `TICK_DATABASE_URL` 設定 ✅

- **目的**：tick 的連線設定由環境決定。
- **做法**：
  - 在 `core/config/settings.py` 新增 `TICK_DATABASE_URL: Optional[str] = os.getenv("TICK_DATABASE_URL")`，格式為 `postgresql://user:pass@localhost:5432/alphaedge`。
  - **主機名依執行位置不同**：本機直接跑是 `localhost`，在 compose 的 `core` 容器裡是 service 名稱 `postgres`。`.env.example` 兩種都寫出來，`docker-compose.yml` 的 `core` 以 `environment` 覆寫成容器內的那一個，不要讓使用者自己改 `.env`。
  - **不直接用 `DATABASE_URL`**：`PostgreSQL遷移計畫.md` Phase1-2 規劃「設了 `DATABASE_URL` 就把所有日頻資料切到 PostgreSQL」。本文件先上線的話，使用者為了 tick 設定 `DATABASE_URL`，等那邊的程式合進來，日頻資料會在還沒遷移時就被切走。分成兩個鍵最安全；那份計畫完成後，可以讓 `TICK_DATABASE_URL` 沒設定時退回 `DATABASE_URL`（要不要做由 `PostgreSQL遷移計畫.md` Phase0-2 決定）。
  - 比照 `require_tick_db_path()` 新增 `require_tick_database_url()`：未設定時拋出清楚的錯誤訊息。
  - `.env.example` 補 `TICK_DATABASE_URL`；`DDB_*` 先保留，到 Phase5-2 再移除。
- **產出**：`core/config/settings.py`、`core/config/schema.py`（或 `settings.py`，看 `require_*` 放哪裡）、`core/config/__init__.py`、`.env.example`。
- **驗證方式**：`pytest tests/test_config_consistency.py` 通過（`.env.example` 與程式讀取的環境變數雙向核對）。
- **相依**：無。

### Phase0-3. `[tick]` 選用相依改為 `psycopg`＋`connectorx` ✅

- **目的**：寫入要用 psycopg 3 的 `COPY`，讀取要用 ConnectorX。
- **做法**：
  - `pyproject.toml` 的 `[project.optional-dependencies]` 改成 `tick = ["psycopg[binary]>=3.2", "connectorx>=0.4"]`，執行 `uv lock` 鎖定精確版本。
  - **本步驟先完成**（`PostgreSQL遷移計畫.md` Phase0-3 尚未開始），所以 `psycopg` 目前在 `[tick]` extra；
    那一步要把它搬到主 `dependencies`（研究庫遷移後每次存取都要它），`[tick]` 只留 `connectorx`／`pyarrow`。
  - ~~`dolphindb` 先搬到另一個 extra `futures-tick`~~：實作改名 `[dolphindb]`（見下方偏離原規格），Phase5-2 已整個移除。
  - 這兩個套件只在 `core/dao/timescale.py`、`core/dao/tw/stock_tick_dao.py` 內惰性 import（分層檢查只允許資料庫驅動出現在 `core/dao/`），比照現行 `dolphindb` 的寫法加註解；`[tool.ruff.lint.per-file-ignores]` 若需要 F401 例外，改指向這兩支 DAO 模組，舊的 tick 例外到 Phase5-2 再移除。
- **產出**：`pyproject.toml`、`uv.lock`。
- **驗證方式**：`uv sync --extra tick` 後，`python -c "import psycopg, connectorx"` 成功。
  **主目錄要連同既有的 extra 一起裝**：`uv sync --extra frontend --extra lab --extra tick`。只寫 `--extra tick` 會把 `frontend`／`lab` 拔掉；
  不帶 `--no-sync` 的 `uv run` 也會 exact sync 而拔掉沒列的 extra。
- **偏離原規格（2026-10-09）**：`dolphindb` 搬到名為 **`[dolphindb]`** 的 extra，而不是 `futures-tick`——台股 tick 的舊路徑在 Phase3-1 換掉前也還用它，叫 `futures-tick` 名不副實。
  **兩者絕不能放同一個 extra**：實測 `import dolphindb` 會把它自己的套件目錄塞進 `sys.path`，裡面的 `config.py` 蓋過 `frontend/config.py`，裝在一起時前端測試整批 import 失敗。
  Phase5-2 移除 `[dolphindb]` 後 `uv sync --all-extras` 恢復可用，README 與 `docs/setup/dev-setup.md` 已改回。實裝版本：psycopg 3.3.6、connectorx 0.4.6；Phase3-1 另補 `pyarrow>=18`。
- **步驟完成後補修（2026-10-09，`390408b`）**：`Dockerfile` 兩行 `uv sync` 都加 `--extra tick`——compose 讓 `core` 連 TimescaleDB，映像卻沒裝驅動，容器內一碰 tick 就 `ImportError`；`uv sync` 是 exact sync，只帶一行會被另一行拔掉。
- **相依**：無。

---

## Phase 1：連線層與 schema

### Phase1-1. 在 `core/dao/` 建立 TimescaleDB 連線入口 ✅

- **目的**：loader（`core/pipeline`）和 API（`core/api`）都要連 TimescaleDB。連線邏輯不能各寫一份，也不能讓 `core/api` 去 import `core/pipeline`；現行專案的連線單一入口在 `core/dao/`（`core/dao/connection.py`），tick 沿用同一層。
- **做法**：
  - 新增 `core/dao/timescale.py`（實作採獨立模組，未併入 `core/dao/connection.py`），提供：
    - `get_tick_database_url() -> str`：呼叫 `require_tick_database_url()`。
    - `connect_tick_db(autocommit: bool = False) -> psycopg.Connection`：寫入用。
    - `get_connectorx_uri() -> str`：讀取用。ConnectorX 吃的是 `postgresql://` URI，不接受 psycopg 的 connection。
    - （實作另加）`tick_db_error_types()`：讓 DAO 以外的 loader／updater 不 import psycopg 也能精準捕捉資料庫錯誤。
  - **分層登記不用新增**：`scripts/check_layer_deps.py` 已有 `("core.dao", 2, "資料存取層（DAO）", False)`，`core.api` 與 `core.pipeline`（第 3 層）都可以 import。
  - `scripts/check_layer_deps.py` 的 `_DB_DRIVER_MODULES` 由 `{"sqlite3"}` 擴充為 `{"sqlite3", "psycopg", "connectorx"}`，讓「資料庫驅動只能在 `core/dao/` 內 import」的檢查也涵蓋 tick 的新驅動。
  - 與 `core/dao/connection.py` 放在同一個目錄。[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase1-1 之後會在 `core/dao/connection.py` 加 SQLAlchemy engine，兩者不衝突；那份計畫完成後可評估 tick 連線是否併入同一入口。
- **產出**：`core/dao/timescale.py`、`scripts/check_layer_deps.py`。
- **驗證方式**：`python scripts/check_layer_deps.py` 通過；在有 DB 的環境 `connect_tick_db().execute("SELECT 1")` 成功。
- **相依**：Phase0-2、Phase0-3。

### Phase1-2. 建表：`stock_tick` hypertable 與 `stock_tick_load_log` ✅

- **目的**：取代 DolphinDB 的 `create_db()`。
- **做法**：
  - `core/config/schema.py`：新增 `STOCK_TICK_TABLE_NAME: str = "stock_tick"`、`STOCK_TICK_LOAD_LOG_TABLE_NAME: str = "stock_tick_load_log"`。`TICK_DB_NAME`／`TICK_DB_PATH`／`TICK_TABLE_NAME` 留到 Phase5-2 移除。
  - 新增 `core/dao/tw/stock_tick_dao.py` 的 `StockTickDAO`，DDL 全放在它的 `create_tables()`，依序執行：
    1. `CREATE EXTENSION IF NOT EXISTS timescaledb`
    2. 〈資料表設計〉的兩段 DDL
    3. 建立 index
  - `StockTickDAO` **不繼承 `BaseDAO`**：`BaseDAO` 綁 `sqlite3.Connection`。它自行持有 `connect_tick_db()` 的連線與 `get_connectorx_uri()`，連線所有權沿用 `owns_conn` 慣例。
  - `StockTickLoader.create_db()` 只呼叫 `StockTickDAO.create_tables()`，loader 本身不寫 SQL、不 import `psycopg`。
  - 全部用 `IF NOT EXISTS`／`if_not_exists => TRUE`，重跑不報錯。
  - 移除 `StockTickLoader` 的類別常數 `DEFAULT_TICK_DB_START_TIME`／`DEFAULT_TICK_DB_END_TIME`／`TICK_DB_HASH_PARTITIONS`：hypertable 會自動長出 chunk，不需要預先宣告日期範圍。這也順便解決 DolphinDB 分區只開到 2030-12-31 的隱藏上限。
  - 移除 `setup()` 裡的 `setTSDBCacheEngineSize`，那是 DolphinDB 專用設定。
- **產出**：`core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py`、`core/config/schema.py`、`core/config/__init__.py`。
- **偏離原規格（2026-10-09）**：`StockTickLoader.create_db()` 改呼叫 DAO、移除 DolphinDB 常數與 `setTSDBCacheEngineSize` 這幾項**併入 Phase2-1**：loader 的寫入路徑那時才換掉，只換 `create_db()` 會留下一個一半 DolphinDB、一半 TimescaleDB 的 loader。
  本步驟改以 `StockTickDAO.create_tables()` 驗證；所有表名帶 schema（`StockTickDAO(schema=...)`），整合測試在暫存 schema 建表、不靠 `search_path` 切換（讀取端的 ConnectorX 拿不到同一個設定）。
- **驗證方式**：連續呼叫 `create_db()` 兩次不報錯；`SELECT hypertable_name FROM timescaledb_information.hypertables` 回傳 `stock_tick`；`\d stock_tick` 的欄位型別與 DDL 一致。
- **相依**：Phase1-1。

### Phase1-3. 設定壓縮與壓縮 policy ✅

- **目的**：歷史 tick 寫入後不再修改，壓縮能把磁碟用量與全市場查詢的 I/O 降到約十分之一。
- **做法**：`StockTickDAO.create_tables()` 建完 hypertable 後接著執行：

  ```sql
  ALTER TABLE stock_tick SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = 'stock_id',
      timescaledb.compress_orderby   = 'time, seq'
  );
  SELECT add_compression_policy('stock_tick', INTERVAL '14 days', if_not_exists => TRUE);
  ```

  - 14 天讓每日更新寫入的最近兩個 chunk 保持未壓縮，重跑最近幾天時不用先解壓。
  - **policy 以「現在」往回算 14 天，歷史資料（2020～2024）全部符合條件**：背景 job 一跑就會壓縮它們，不分是否還在匯入。
    抽樣試點（Phase4-2）與歷史匯入（Phase4-3）期間要先停用這個 job，否則試點量不到「壓縮前」，匯入中的那一週也會被壓縮，
    之後同一週的寫入都要走壓縮 chunk 的 DML（慢很多），「載入一週、壓縮一週」的設計就失效了。
    `StockTickDAO` 一併提供 `pause_compression_policy()`／`resume_compression_policy()`（以 `alter_job(<job_id>, scheduled => false／true)`，
    job_id 由 `timescaledb_information.jobs` 依 `hypertable_name = 'stock_tick'` 查出）。
  - 另外在 `StockTickDAO` 提供 `compress_chunks_before(older_than: datetime.date) -> int`，包一層 `SELECT compress_chunk(c, if_not_compressed => TRUE) FROM show_chunks('stock_tick', older_than => ...) c`，給 Phase4-3 的匯入腳本逐週呼叫（腳本經 loader 或直接建 DAO 呼叫，不自己寫 SQL）。
- **產出**：`core/dao/tw/stock_tick_dao.py`。
- **驗證方式**：`SELECT * FROM timescaledb_information.jobs WHERE hypertable_name = 'stock_tick'` 有 compression job；寫入樣本後呼叫 `compress_chunks_before()`，`chunk_compression_stats('stock_tick')` 顯示已壓縮；
  `pause_compression_policy()` 之後該 job 的 `scheduled` 為 false，`resume_compression_policy()` 後恢復 true。
- **相依**：Phase1-2。

---

## Phase 2：寫入路徑

### Phase2-1. 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等＋兩種 CSV 格式正規化） ✅

- **目的**：取代 `loadTextEx`，並補上 DolphinDB 版缺少的兩件事：DB 層冪等，以及逐檔回報失敗。
- **做法**：
  - `add_to_db(remove_files: bool = False, dir_path: Optional[Path] = None) -> None`：`dir_path` 預設 `TICK_DOWNLOADS_PATH`，讓 Phase4-3 可以指定其他資料夾。逐檔呼叫 `load_csv()`，單檔失敗不中斷整批，最後呼叫 `BaseDataLoader.finish_load()` 彙報結果。現行 tick 線沒有呼叫 `finish_load()`（`tests/test_loader_failure_reporting.py` 特別註明這個例外），改寫後要補上。
  - `load_csv(csv_path: Path) -> int`：
    1. **正規化成 cleaner 格式**（純函式 `normalize_tick_frame(raw: pd.DataFrame, stock_id: str) -> pd.DataFrame`，可單獨測試）：
       - 全部欄位先以字串讀入（`dtype=str`），**依欄名取欄、不依位置**（歷史格式有 31 種欄序）。
       - 有 `ts` 沒有 `time` 時改名為 `time`；沒有 `stock_id` 欄時以檔名補上（歷史格式）。檢查 9 個欄位齊全，缺欄整檔失敗。
       - `time` 以 `pd.to_datetime(..., format="ISO8601")` 解析，26 字元與 23 字元（millisecond）都要吃得下。
       - 量與 `tick_type` 先轉 float 再轉 int（8 檔寫成 `105.0`）；價格 `round(2)`（去掉 `6.5600000000000005` 這類浮點誤差）。
    2. 加上 `trade_date = time.dt.date`，**在排除之前**依原始列序算出 `seq = groupby("trade_date").cumcount()`（同一天內原始列序不一定依時間排序，`seq` 保留原貌；排除後 `seq` 會有缺號，逐列對照 CSV 時反而能直接定位）。
    3. **依 Phase4-1 裁示的規則排除**，各規則排除的列數記進 log（純函式 `filter_tick_rows()`，可單獨測試）：
       - `close = 0` 的全零列、`volume < 0` 的列。
       - **興櫃時期**：當天 `price` 表沒有這檔股票的列，整天排除。由 loader 經 `core/dao/tw/stock_price_dao.py` 一次查出檔案涵蓋期間「每個交易日有哪些股票」，不逐日查。
       - **`price` 表整天都沒有資料時不可排除**，而是該日失敗、不寫 `load_log`。否則日常更新若 tick 比 `price` 先跑，會把整天當成興櫃排除並登記為已載入，之後再也不會補。
    4. **一個交易日一個 transaction**，逐日交給 `StockTickDAO.replace_day(stock_id, trade_date, day_df, source_rows, source_file) -> int`（`source_rows` 為該日排除前的列數；整天被排除的日子也要登記，`row_count = 0`，續跑時才不會重做）；以下 SQL 全在 DAO 內，loader 不 import `psycopg`：
       - `DELETE FROM stock_tick WHERE stock_id = %s AND time >= %s AND time < %s`（該日 00:00 到隔天 00:00）
       - 用 `cursor.copy("COPY stock_tick (stock_id, time, seq, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type) FROM STDIN")` 以 `write_row()` 寫入
       - `INSERT INTO stock_tick_load_log ... ON CONFLICT (stock_id, trade_date) DO UPDATE SET source_rows = EXCLUDED.source_rows, row_count = EXCLUDED.row_count, source_file = EXCLUDED.source_file, loaded_at = now()`
       - `COMMIT`
    5. 回傳寫入的總列數。
  - **寫入速度**：`write_row()` 逐列由 Python 送出，每日更新（百萬列級）夠用；歷史匯入是十億列級，Phase4-2 要記錄每秒寫入列數，
    推估全量匯入耗時。太慢時改成先把當日資料（含 `seq`）組成 CSV 文字，再以 `copy.write()` 整塊送出，省掉逐列的 Python 往返。
  - **為什麼選「刪除後重寫」而不是 `ON CONFLICT DO NOTHING`**：完全相同的重複列本來就是合法資料（全量盤點 1,424,359 列，主要在興櫃時期，見〈CSV 樣式〉），沒有自然唯一鍵可以用；而且 CSV 是「整段區間覆寫」產生的，同一天再出現時應該以新檔為準。
  - **目標 chunk 已壓縮時**：TimescaleDB 2.11 以上支援對壓縮 chunk 做 `DELETE`／`INSERT`，但很慢。日常更新只會碰到最近 14 天（未壓縮），可以忽略。歷史重灌時，由 Phase4-3 的腳本先 `decompress_chunk` 再寫入。
  - DAO 內的 SQL 參數一律走 `%s` 佔位符（CLAUDE.md §2.10），表名從 `schema.py` 常數以 `psycopg.sql.Identifier` 組合。
  - 移除 `append_csv_to_dolphinDB`／`append_all_csv_to_dolphinDB`／`clear_all_cache`／`delete_dolphinDB`。
- **產出**：`core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py`。
- **驗證方式**：新增整合測試（見 Phase5-1）：
  1. 同一份 CSV 載入兩次，`stock_tick` 列數不變，`load_log.source_rows` 與 CSV 一致。
  2. 載入一份內容較少的同日 CSV，舊列被完整取代。
  3. 一個欄位損壞的 CSV 會讓 `finish_load()` 拋出 `DataLoadError`，其他檔案照常入庫。
  4. 歷史格式（欄序打亂、無 `stock_id`、`105.0`、毫秒時間）與 cleaner 格式各一份手寫樣本，正規化後內容相同。
  5. 排除規則：全零列、負量列、`price` 表當天沒有該股的日子都被排除且 `row_count` 少掉對應列數；`price` 表整天缺資料時該日失敗、`load_log` 沒有紀錄。
- **實作紀錄（2026-10-09）**：
  - **寫入改用 `COPY ... FROM STDIN (FORMAT csv)` 整塊送出**（偏離原規格的 `write_row()`）：歷史匯入是十億列級，逐列往返會主導總耗時；pandas 組 CSV 文字在 C 層完成。
  - 例外隔離只收 `ValueError`、`OSError` 與 `core/dao/timescale.py` 的 `tick_db_error_types()`（DAO 以外不 import psycopg 也能精準捕捉），不盲捕。
  - 指定 `dir_path` 時拒絕 `remove_files`：歷史存檔是原始資料，不該被入庫流程順手刪掉。
  - 判斷上市櫃交易日用新增的 `StockPriceDAO.get_stock_trading_days()`。
  - **全量乾跑**（`data/tick_history/` 1,856 檔，只跑正規化與排除、不寫 DB）：0 個檔案失敗、0 個失敗日；
    原始 1,061,227,084 列 → 寫入 **1,050,896,400 列**；排除興櫃 10,322,770 列（與盤點完全一致）、全零 7,901 列、負量 13 列
    （盤點的 23,215 與 14 列中，其餘落在興櫃日、只計一條規則）；「股票 × 交易日」1,781,133 組。這組數字就是 Phase4-3 全量比對的基準。
    留下的最大單筆量 358,476 張是 `2834` 在 2021-08-31 13:30 的收盤集合競價（當日成交 616,357 張），屬實。
  - **步驟完成後補修（2026-10-09，`5adb56d`）**：`update_db` 的 tick 原本排在 price 之前，而上面「`price` 表整天沒資料 → 該日失敗」的規則會讓 `--target all` 收盤後跑當天的 tick 一律失敗；已改排在 price 之後，`test_targets_that_read_price_run_after_price` 釘住順序。
  - **`load_csv()` 一次讀整個檔**：最大的 `2303.csv`（1 GB、1,653 萬列）讀進來要數 GB 記憶體；日常更新與抽樣不受影響，Phase4-3 依原規劃先把每檔依週切開再交給 loader。
- **偏離原規格（2026-10-09）**：原規劃「格式差異在匯入腳本轉換、不改 loader 的欄位契約」。全量盤點後改為 loader 直接讀兩種格式並套用排除規則：歷史匯入（Phase4-3）、抽樣試點（Phase4-2）與日常更新才會是同一套規則，完整性比對的口徑也才一致。
- **相依**：Phase1-2；排除規則相依 Phase4-1 的裁示。

### Phase2-2. updater 續跑依據改為 `stock_tick_load_log` ✅

- **目的**：`tick_metadata.json` 的日期是從 CSV 掃出來的，入庫失敗時也會前進，改成以資料庫實際寫入的紀錄為準。
- **做法**：
  - `StockTickUtils`：
    - 新增 `get_loaded_last_dates() -> Dict[str, datetime.date]`，轉呼叫 `StockTickDAO.get_loaded_last_dates()`（DAO 內查 `SELECT stock_id, max(trade_date) FROM stock_tick_load_log GROUP BY stock_id`）。
    - `check_date_crawled()` 改用這份結果，語意維持「`date <= last_date` 就跳過」，和現行一致（停牌日不會每次重爬）。
    - `get_table_latest_date()` 改由 DAO 查 `max(trade_date)`。
    - update 開頭查一次，傳給各 thread，**不要每檔股票查一次 DB**。
  - `StockTickUpdater.update()`：
    - 開頭那段「比對 metadata 刪 CSV」改成：CSV 裡每個交易日在 `load_log` 都有紀錄、且 `source_rows` 與 CSV 當日列數一致，才刪除（`load_log` 的查詢同樣由 `StockTickDAO` 提供）。
    - 結尾的 `update_tick_metadata_from_csv()` 移除。
    - `setup()` 裡的 `StockTickUtils.generate_tick_metadata_backup()` 呼叫一併移除（該函式本步驟刪除，留著會在建構時就 `AttributeError`）。
    - 模組說明「資料庫目前的涵蓋範圍以 `tick_metadata.json` 為準」改成以 `stock_tick_load_log` 為準。
  - `generate_tick_metadata_backup()`、`update_tick_metadata_from_csv()`、`load_tick_metadata_stocks()`、`scan_tick_downloads_folder()` 移除。`tick_metadata.json` 檔案本身留到 Phase5-2 再刪，作為對照。
  - `scripts/manual/manual_init_tick_metadata.py` 失去用途，在 Phase5-2 刪除。
  - 模組說明字串（`"""台股 tick 的 DolphinDB 與 metadata 工具"""`）與 class docstring 同步更新。
- **產出**：`core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py`。
- **驗證方式**：以 Phase4-2 的 3 天樣本（2024-05-08～05-10）入庫後，手動刪掉某檔股票最後一天的 `load_log` 與資料，對同一段日期跑 `update()`（**crawler 一律用替身**，不對外連線、也不對正式資料表跑真實更新，見〈範圍界線〉），只有那檔股票被重爬。
- **實作紀錄（2026-10-09）**：
  - `StockTickDAO` 新增 `get_loaded_last_dates()`、`get_latest_trade_date()`、`get_source_rows()`；`check_date_crawled()` 改吃整次更新只查一次的結果。
  - 刪 CSV 的判斷放在 `StockTickLoader.is_fully_loaded()`（loader 才知道兩種 CSV 格式），只比對每日列數。
  - **另修一個續跑漏洞**（原規格沒寫到）：crawler 原本把 Shioaji 的任何錯誤吞成 `None`，updater 當成「這天沒成交」；
    加上「不晚於已入庫最後一天就跳過」，那天就永遠補不回來。改成 crawler 拋 `ConnectionError`，
    updater 遇到失敗日**只寫失敗日之前的日子**、該檔計為失敗（整次更新以 `DataLoadError` 收場），下次從失敗日接著爬。
    代號不在合約表（已下市）仍回 `None`、算沒資料。
  - **`scripts/manual/manual_tick_updater.py` 提前在本步驟刪除**（原排 Phase5-2）：它靠 mock DolphinDB 才不寫 DB，updater 改連 TimescaleDB 後一跑就會寫進正式表。
  - 盲捕的突變測試放在新檔 `tests/test_stock_tick_updater.py`（偏離原規格的 `test_pipeline_error_narrowing.py`：tick 更新流程的測試集中一處比較好找）。
    `pyproject.toml` 的 `BLE001`／`TRY300`／`TRY201` 處數註記回寫為 75／16／2。
  - 驗證改在暫存 schema 以端到端整合測試完成（`test_updater_resumes_from_load_log`），不用 Phase4-2 的真實樣本：測試資料可控，也不碰 `public`。
- **相依**：Phase2-1。

---

## Phase 3：讀取路徑

### Phase3-1. 改寫 `StockTickAPI` 讀取路徑 ✅

- **目的**：讓回測用的查詢在 TimescaleDB 上跑得快，並符合〈讀取介面契約〉。
- **做法**：
  - 比照 `core/api/tw/stock_price_api.py` 的慣例，SQL 全在 DAO，API 只呼叫 DAO。由於 `StockTickDAO` 不收 SQLite 連線，`StockTickAPI` 維持 `DEFAULT_DB_PATH = None`、`DAO_CLASS = None`。
  - **現行 `__init__` 沒有呼叫 `super().__init__()`**（自己設 `session` 後直接呼叫 `setup()`），所以 `conn`／`owns_conn` 根本不存在，直接在 `setup()` 呼叫 `super().setup()` 會 `AttributeError`。
    改寫時刪掉自訂的 `__init__`（`default_stock_id`／`query_start_date`／`query_end_date` 三個欄位全庫無人使用，一併移除），改由 `BaseDataAPI.__init__()` 呼叫 `setup()`：
    `setup()` 先 `super().setup()`（`DEFAULT_DB_PATH` 為 None，不開 SQLite），再建立 `self.dao = StockTickDAO()`。
  - `setup()`：移除 DolphinDB session 與 `setTSDBCacheEngineSize`，改成建立 `StockTickDAO`，並由 DAO 確認資料表存在（查一次 `to_regclass('stock_tick')`，不存在時記 error 並拋出）。現行「資料庫不存在只 `print`」的行為一併修正，避免回測跑完才發現整段沒有報價。
  - 三個查詢方法都轉呼叫 `StockTickDAO` 的查詢；DAO 內共用私有方法 `_query_ticks(where_sql: str, params: Tuple, order_by: str) -> pd.DataFrame`（下方是原規格，實作改成 `return_type="arrow"`，見實作紀錄）：

    ```python
    tick: pd.DataFrame = cx.read_sql(
        self.uri,  # get_connectorx_uri()
        query,  # SELECT <9 欄> FROM stock_tick WHERE ... ORDER BY ...
        return_type="pandas",
    )
    ```

    - **ConnectorX 不支援參數佔位符**。日期由 `datetime.date` 格式化成 `'YYYY-MM-DD'`；`stock_id` 先以 `str.isalnum()` 驗證、不合法就拋 `ValueError`，再嵌入 SQL。這個例外要在程式註解寫清楚理由（CLAUDE.md §2.10 要求參數化，這裡是 driver 限制）。
    - 只 `SELECT` 契約列出的 9 個欄位，不用 `SELECT *`：壓縮 chunk 是按欄位解壓的，而且回傳結果不應帶出 `seq`。
    - 查完之後強制轉 dtype（`astype`），保證空表與非空表的欄位型別一致。
  - `get_last_tick()` 不改。
  - `close()`：ConnectorX 每次查詢自行開關連線，讀取路徑沒有常駐連線；API 的 `close()` 只轉呼叫 `StockTickDAO.close()`（DAO 若沒有常駐連線即為 no-op），並寫註解說明。
  - **不採用 `pd.read_sql`**：它是逐列轉成 Python 物件，百萬列級查詢要慢上數十倍，這是換 TimescaleDB 後最容易踩到的效能陷阱。
- **產出**：`core/dao/tw/stock_tick_dao.py`、`core/api/tw/stock_tick_api.py`。
- **驗證方式**：`pytest tests/test_api_public_interfaces.py -k tick` 通過；Phase5-1 的整合測試驗證三個查詢的排序與 dtype；Phase4-2 記錄查詢耗時。
- **實作紀錄（2026-10-09）**：
  - **ConnectorX 改用 `return_type="arrow"` 再 `.to_pandas()`**（偏離原規格）：直接回 pandas 的路徑用到 pandas 已棄用的內部 API `make_block`，
    實測每次查詢都發 `Pandas4Warning`，pandas 拿掉它那天整條讀取路徑就壞；`[tick]` extra 因此補上 `pyarrow`。
  - 空結果回傳**同欄位、同 dtype 的空表**（`empty` 仍為 True），比原本的 `pd.DataFrame()` 多帶欄位，呼叫端不受影響。
  - `StockTickAPI(dao=...)` 可注入 DAO（整合測試指向暫存 schema）；`connect_tick_db()` 連不上時轉成 `ConnectionError`，訊息說明要開 Docker Desktop 並 `docker compose up -d postgres`。
  - schema 名稱在 `StockTickDAO` 建構時驗證（會嵌進 ConnectorX 的 SQL）。
- **相依**：Phase1-1、Phase1-2。

### Phase3-2. DataFeed／Adapter 文字與連線生命週期收尾 ✅

- **目的**：清掉回測層對 DolphinDB 的描述。行為不變。
- **做法**：
  - `core/backtest/datafeed/tw/stock_datafeed.py`：class docstring 的「DolphinDB 的 Tick」改成「TimescaleDB 的 Tick」，`setup()` 的 docstring 同步修改。
  - `core/api/base.py` 的 `close()` docstring 中「非 SQLite 的資料源（如 DolphinDB）自行覆寫」改成 TimescaleDB。
  - `strategies/stock/foreign_selling_reversal_short_strategy.py` 的註解「tick 資料在 DolphinDB」改掉。
  - `StockQuoteAdapter.from_tick_rows()`／`from_tick_row()` 不用改，但要確認 `TickQuote(time=row.time)` 可以接受 `datetime64[ns]` 經 `itertuples()` 取出的 `pd.Timestamp`。
  - `core/api/__init__.py`、`core/api/tw/__init__.py` 的模組說明字串「不做 eager import 是因為 `stock_tick_api` 相依 DolphinDB」改成相依選用套件 `psycopg`／`connectorx`（經 `core/dao/tw/stock_tick_dao.py`）；若 DAO 改成在函式內惰性 import、這條理由已不成立，就依實際情況改寫說明，不要留下 DolphinDB 字樣。
- **產出**：上列檔案。
- **驗證方式**：寫一支只在測試裡用的 `Scale.TICK` 最小策略，對 Phase4-2 的 3 天樣本跑一次回測，不報錯，而且 `get_quotes()` 每天回傳的 `TickQuote` 數量等於當天的 DB 列數。
  這支策略**定義在測試檔裡、不放 `strategies/`**（放進去會被策略載入器收錄），而且**不可宣告 `is_tick_triggered`**——逐筆觸發的策略跑 `Scale.TICK` 回測會被引擎以 `IntradayScaleMismatchError` 拒絕（`tests/backtest/test_intraday_contract.py` 也只允許 `IntradayMomentumStrategy` 宣告它）。
- **實作紀錄（2026-10-09）**：驗證改成在暫存 schema 寫入資料後，直接以 `TwStockDataFeed.get_quotes(date, Scale.TICK)` 比對
  TickQuote 數與 DB 列數、並確認 `tick_quote.time` 是 `datetime`（偏離原規格的「最小策略跑完整回測」：完整回測還要日 K 的 SQLite，
  worktree 沒有 `data/db`；DataFeed 到 adapter 這一段才是本步驟要驗的）。
  README 兩語版與 `core/backtest/README.md` 的 TICK 列已一併改成 `[tick]` 相依與 TimescaleDB 啟動方式（原屬 Phase5-3）。
- **相依**：Phase3-1。

---

## Phase 4：歷史資料遷移

### Phase4-1. 盤點本機歷史 CSV、定案排除規則並搬離 updater 資料夾 ✅

- **目的**：先弄清楚要入庫的資料長什麼樣子，Phase2-1 的解析和 Phase4-3 的匯入才不會做白工；並把歷史搬到 updater 碰不到的地方。
- **背景**：原規劃從 Google 雲端下載歷史 CSV 再盤點。2026-10-09 使用者已把完整歷史補回 `data/downloads/tw_stock/tick/`，並確認那批就是要入庫的全部資料，所以改成盤點本機這批，不再下載。
- **做法**：
  1. 全量盤點（2026-10-09 已完成，結果見下方盤點紀錄與〈CSV 樣式〉）。
  2. **請使用者裁示兩件事**，結果見下方〈裁示〉。
  3. **把歷史搬離 `data/downloads/tw_stock/tick/`**：那是 tick updater 的工作目錄，`--target tick`（`all` 也含 tick）爬到的股票會整檔覆寫 `{stock_id}.csv`；Phase2-2 之後的 updater 還會在入庫成功後刪 CSV。
     搬到同一顆磁碟的位置用 `mv` 是瞬間完成、不會複製 56 GB；搬完之後 `data/downloads/tw_stock/tick/` 留空，給 updater 用。
- **盤點紀錄（2026-10-09，以 pyarrow 逐檔全掃 1,856 檔）**：
  1. **檔案佈局**：一檔股票一個檔（`{4 位代號}.csv`），沒有分年份資料夾、沒有壓縮；8 個欄名都相同、但有 31 種欄序（〈CSV 樣式〉）。
  2. **日期範圍**：2020-04-01～2024-05-10，1,001 個交易日，與 `price` 表同期間的交易日逐日一致，沒有整天缺漏。
  3. **欄位格式**：時間欄是 `ts` 不是 `time`；沒有 `stock_id` 欄；`ts` 有 26 字元與 23 字元兩種；8 檔整數寫成浮點字串。和 cleaner 格式的差異全部由 Phase2-1 的正規化處理。
  4. **總量**：1,061,227,084 列、56 GB；每日列數中位數 997,013（已回填〈容量推估〉）。
  5. **和 `tick_metadata.json` 的差異**：metadata 記 1,920 檔，**其中 93 檔沒有對應的 CSV**（含 `2603`、`2609` 這類大型股；90 檔在 metadata 停在 2024-05-10、3 檔停在 05-15），另有 30 檔 CSV 不在 metadata 裡。
     原本 541 檔的 2024-05-13～05-15 已不存在，資料庫的歷史止於 2024-05-10。
- **裁示（2026-10-09 使用者採用下列兩項建議）**；歷史已於同日搬到 `data/tick_history/`（1,856 檔、56 GB，`data/downloads/tw_stock/tick/` 已清空給 updater 用）：
  1. **排除規則**。建議：
     - **排除興櫃時期**：「股票 × 交易日」在 `price` 表沒有資料就整天排除（10,322,770 列、約 1.0%；理由見〈CSV 樣式〉的〈興櫃時期〉）。
     - **排除全零列**（`close = 0`，23,215 列）與**負成交量列**（14 列）。
     - **保留**：13:30 之後的盤後列（現行 cleaner 也保留）、整列完全相同的重複列（同一瞬間的多筆成交，見〈設計決策〉）、bid／ask 為 0 的列。
  2. **搬移位置**。建議 `data/tick_history/`：同一顆磁碟、`mv` 瞬間完成；`data/` 已在 `.gitignore`，不會被誤加；也不在任何 updater 的路徑上。
     原規劃的 `~/tick_history/` 也可以，差別只在要不要和專案資料放一起。
- **產出**：本文件的盤點紀錄與裁示紀錄；歷史 CSV 的新位置。
- **驗證方式**：盤點紀錄涵蓋上述 5 項；兩項裁示已記錄；搬移後新位置檔數 1,856、`du` 大小不變，`data/downloads/tw_stock/tick/` 沒有歷史檔。
- **相依**：無，可最先做。

### Phase4-2. 試點：本機抽樣檔案入庫與效能量測 ✅

- **目的**：在全量匯入前驗證 schema 與冪等性，量測壓縮率和查詢速度，決定 chunk 間隔要不要調整。
- **只取抽樣**（2026-09-18 使用者裁示）：全量入庫走 Phase4-3，而且要等使用者明確要求。
- **樣本**：從 Phase4-1 搬移後的歷史位置取**約 20 檔 × 最後 3 個交易日（2024-05-08～05-10）**。這 3 天的全市場列數已由盤點得知（1,262,189／1,169,462／1,213,752），換算倍數有確定的分母。
- **做法**：
  0. **先 `pause_compression_policy()`**（見 Phase1-3）：樣本是 2024 年的資料，policy 一跑就會壓縮，步驟 2 會量不到壓縮前的大小與查詢耗時。量測結束後再 `resume_compression_policy()`。
  1. 挑約 20 檔（涵蓋成交量大小不同的股票，例如 `2330`、`2303`、`1101`、`9958` 與幾檔冷門股，**至少一檔有興櫃時期**、一檔是浮點字串格式），以一次性小腳本截出這 3 天、**原格式不動**地另存到暫存目錄，
     再以 `add_to_db(dir_path=...)` 入庫，記錄耗時與樣本列數。
     - `add_to_db()` 的 `dir_path` 參數（Phase2-1）就是為了這件事：**不要**直接對整個歷史資料夾跑。
     - 截出來的樣本是歷史格式，順便驗證 Phase2-1 的正規化；排除規則在這 3 天不一定觸發，由 Phase2-1 的單元測試保證。
  2. 記錄壓縮前大小：`hypertable_size('stock_tick')`，以及入庫的每秒寫入列數（推估全量匯入耗時，見 Phase2-1〈寫入速度〉）。
  3. `compress_chunks_before(datetime.date(2024, 5, 11))` 之後，記錄 `hypertable_compression_stats('stock_tick')`。
  4. 分別在**壓縮前**與**壓縮後**，各量 3 次：
     - `StockTickAPI().get_ordered_ticks(d, d)`（三天各一次）
     - `get_stock_ticks("2330", d, d)`
  5. 把量測值依「全市場每日列數 ÷ **樣本**每日列數」換算成全市場的推估耗時。
     - **換算基準要寫進量測紀錄**：樣本只有 20 檔時倍數會拉到 50～100 倍，換算誤差不小。門檻踩線（8～12 秒）時不要直接下結論，改為向使用者說明並請求擴大樣本。
- **門檻**：換算後全市場單日 `get_ordered_ticks()` **≤ 10 秒**，以 1,000 個交易日的回測來說約 3 小時。超過時依序檢查：
  - 是否誤用 `SELECT *`
  - `ORDER BY` 能否由壓縮的 `orderby` 直接提供
  - 改用 ConnectorX 的 `return_type="arrow"` 再轉 pandas
- **產出**：本步驟末尾的「量測紀錄」表（含樣本檔數、樣本列數與換算倍數）。
- **驗證方式**：量測紀錄填寫完整；**抽樣**檔案每個「股票 × 交易日」的 `load_log.source_rows` 與樣本 CSV 列數一致、DB 列數＝`row_count`。
- **量測紀錄（2026-10-09，寫入 `public.stock_tick`，量完已清空）**：
  - **樣本**：19 檔 × 2024-05-08～05-10，依當期成交筆數分位挑選（`2618` 最多 54,801 筆、`6655`／`1203` 最少 5 筆），含 `2330`、`2303`（浮點字串格式）、`1101`、`9958`，以及兩檔興櫃（`1294`、`1563`）。
    原格式截出 155,688 列 → 寫入 **152,826 列**，排除的 2,862 列正好是兩檔興櫃；57 組「股票 × 交易日」的 `source_rows` 與 CSV、DB 列數與 `row_count` **全數一致**。
  - **寫入速度**：DB 寫入（`replace_day()` 本身）**約 12.8 萬列／秒**；含讀 CSV、正規化、查日 K 的整體 **約 8.9 萬列／秒**。
    → 全量 10.5 億列推估 **約 3.3 小時**（不含 Phase4-3 依 chunk 切檔與逐 chunk 壓縮的時間）。
  - **大小**：未壓縮那個 chunk（41,412 列）6.9 MB → 壓縮後 0.57 MB，**約 12 倍**；換算見〈容量推估〉。壓縮一個 chunk 0.03 秒。
  - **查詢耗時**（各 3 次取中位數；`get_ordered_ticks` 換算全市場＝樣本耗時 × 全市場列數 ÷ 樣本列數）：

    | 交易日 | 樣本列數 | 倍數 | 壓縮前 | 壓縮後 | 換算全市場 | `get_stock_ticks("2330")` |
    |--------|---------:|-----:|-------:|-------:|-----------:|--------------------------:|
    | 2024-05-08 | 41,412 | 30.5 | 0.046 s | 0.046 s | **約 1.4 s** | 0.017 s |
    | 2024-05-09 | 50,755 | 23.0 | 0.054 s | 0.053 s | **約 1.2 s** | 0.016 s |
    | 2024-05-10 | 60,659 | 20.0 | 0.064 s | 0.063 s | **約 1.3 s** | 0.019 s |

    **遠低於 10 秒門檻**（約 1,000 個交易日的 tick 回測，查詢合計約 20～25 分鐘），不必擴大樣本或改寫查詢。
    5/9、5/10 那一欄「壓縮後」其實沒壓到，原因見下一點；5/8 壓縮前後幾乎相同，壓縮不拖慢查詢。
  - **chunk 邊界不是週一**：TimescaleDB 的 7 天 chunk 從 1970-01-01（週四）起算，本次兩個 chunk 是 05-02～05-09、05-09～05-16。
    `compress_chunks_before(2024-05-11)` 只會壓**整個範圍都早於該日**的 chunk，所以只壓了一個。
    **Phase4-3 的「逐週」要改成依 chunk 邊界（週四到週三）**，否則每一輪都會留一個沒壓的 chunk，下一輪又要往壓縮過的 chunk 寫。
  - 清空方式：`TRUNCATE public.stock_tick`、`TRUNCATE public.stock_tick_load_log`（chunk 一併刪除），壓縮 policy 恢復啟用；兩張空表保留。
- **相依**：Phase1-3、Phase2-1、Phase3-1、Phase4-1（樣本從搬移後的位置取）。

### Phase4-3. 歷史 CSV 全量匯入與完整性比對 ✅

- **目的**：把 Phase4-1 盤點的歷史資料全部搬進 TimescaleDB。
- **執行時機**：**腳本可以先寫好，真正跑匯入要等使用者要求**（2026-09-18 裁示，與 Phase4-2 同一條）。
- **規模（2026-10-09 實測）**：1,856 檔、10.6 億列、1,001 個交易日（約 214 週）。Phase4-2 量到每秒寫入列數後，先估總耗時再請使用者決定何時跑。
- **做法**：新增 `scripts/manual/manual_tick_history_import.py`：
  - 參數：`--source-dir`（預設 Phase4-1 搬移後的位置）、`--start-date`、`--end-date`、`--resume`。
  - **開始前 `pause_compression_policy()`，結束（含中斷）時 `resume_compression_policy()`**（`try/finally`）：policy 會在匯入途中壓縮還沒載完的週（見 Phase1-3）。
  - **逐 chunk 處理**：載入一個 chunk 的範圍 → `compress_chunks_before(chunk 結束日)` → 在 log 記錄進度 → 下一個。峰值磁碟用量只多出一個 chunk 的未壓縮資料（約 1 GB）。
    **範圍要對齊 chunk 邊界（週四到週三，從 1970-01-01 起算的 7 天）**，不是日曆週：Phase4-2 實測 `compress_chunks_before()` 只壓整段都早於該日的 chunk。
    歷史檔是「一檔股票一個檔、涵蓋四年」，逐週處理代表每一週都要從 1,856 個檔各取出該週的列：不要每週重讀整個檔（最大的 `2303.csv` 有 1 GB），
    先把每個檔依週切成暫存檔（一次掃完全部），或改成依 `ts` 前綴串流切分，實作時二擇一並記錄峰值暫存空間。
  - `--resume`：以 `stock_tick_load_log` 判斷已完成的「股票 × 交易日」並跳過，可以隨時中斷後重跑。
  - 要重灌已壓縮的週時，先 `decompress_chunk` 再呼叫 `load_csv()`。
  - **格式正規化與排除規則都在 loader**（Phase2-1），腳本不另做轉換，日常更新與歷史匯入才是同一套規則。
  - 用 `from loguru import logger` 輸出進度，檔名 `tick_history_import.log`。
- **產出**：`scripts/manual/manual_tick_history_import.py`、本步驟末尾的「匯入紀錄」。
- **驗證方式**：
  1. 每個「股票 × 交易日」：CSV 列數＝`load_log.source_rows`，`SELECT count(*) FROM stock_tick WHERE ...`＝`load_log.row_count`。腳本最後跑一次全量比對，列出不一致的組合（應為 0 筆），並彙總各排除規則的列數，與 Phase4-1 盤點紀錄的數字對照。
     全部「股票 × 交易日」應為盤點的 1,781,133 組（含整天被排除、`row_count = 0` 的組合）。
  2. 抽樣 20 個「股票 × 交易日」，`get_stock_ticks()` 的結果與正規化、排除後的 CSV 逐列比對（`time`、價格、量）完全相同。
  3. 抽樣 5 個交易日，每日成交量加總 ≈ `price` 表當日成交股數 ÷ 1000（盤後零股與定價交易會造成小幅落差，差距記進匯入紀錄）。
  4. 全部 chunk 除了最近 14 天之外都已壓縮。
- **實作紀錄（2026-10-09，`feature/tick-timescaledb`）**：腳本完成；2026-10-09 使用者下指令執行，結果見下方「匯入紀錄」。
  - **預設只列計畫**（來源檔數與大小、工作目錄空間、推估耗時、資料庫現況），不建表、不寫入；`--apply` 才匯入，`--verify-only` 只比對。
    對真實資料列計畫：1,856 檔、57.9 GB、推估約 10.2 億列、寫入約 3.2 小時（不含切檔與比對）。
  - **切檔改成一次掃完**：每檔依 chunk 範圍切到 `data/tick_import_work/<chunk 起日>/<代號>.csv`（原格式不動），
    同時記下每個「股票 × 交易日」的來源列數；峰值暫存約等於來源大小（58 GB），成功的 chunk 逐一刪除。
  - 依 chunk 邊界逐 chunk 載入 → `compress_chunks_before(chunk 終日)`；重灌已壓縮的 chunk 先 `decompress_chunks_between()`；
    每個 chunk 寫完核對資料庫實際的 chunk 範圍和切檔一致，不一致當場中止。
  - 比對 4 項照本步驟驗證方式實作；成交量對照只報告不判定（盤後零股與定價交易會有落差），其餘有不符時結束碼 1。
  - 端到端測試（暫存 schema）：列計畫不寫入、跨兩個 chunk 匯入並壓縮、比對通過、`--resume` 不重複、刪掉一列後比對失敗。
  - **執行方式**（在 `feature/tick-timescaledb` 合併後的主目錄，或在 worktree 以 `--source-dir`／`--price-db`／`--work-dir` 指向主目錄的 `data/`）：
    先開 Docker Desktop 並 `docker compose up -d postgres`，再
    `TICK_DATABASE_URL=... uv run --no-sync python -m scripts.manual.manual_tick_history_import --apply`；中斷後加 `--resume` 重跑。
- **匯入紀錄（2026-10-09 18:19～2026-10-10 01:25，台北時間）**：
  - 執行方式：在 worktree 以 `--source-dir`／`--price-db`／`--work-dir` 指向主目錄的 `data/tick_history`、`data/db/tw_stock.db`、`data/tick_import_work`，外層包 `caffeinate -i` 避免睡眠。
  - 耗時：切檔 34 分（1,856 檔，峰值暫存約 58 GB，逐 chunk 刪除，結束後只剩 33 MB 的 `source_counts.csv` 與完成標記）；
    寫入 390 分、213 個 chunk（2020-03-26～2024-05-16），每 chunk 1,711～1,853 檔、**失敗 0 檔**，log 0 筆 ERROR／WARNING。
    實際比 Phase4-2 推估的 3.3 小時慢：2021 年後每週列數由約 400 萬增到 500～700 萬，每 chunk 由約 1.3 分拉長到約 1.8～2.1 分。
  - **比對 1**：來源「股票 × 交易日」1,781,133 組、`load_log` 1,781,133 組；缺登記 0、多登記 0、來源列數不符 0。
  - **比對 2**：來源 1,061,227,084 列，寫入 **1,050,896,400 列**（排除 10,330,684 列，與 Phase2-1 全量乾跑的基準完全相同）；DB 實際列數與 `row_count` 不符 0 組。
  - **比對 3**：抽樣 20 組逐列比對，不符 0 組。
  - **比對 4**（只報告）：抽樣 5 天 tick 成交量加總÷`price` 表 0.9920～0.9960（2021-11-08、2021-12-28、2023-06-07、2023-10-17、2023-12-21），落差符合盤後零股與定價交易的預期。
  - **比對 5**：chunk 213 個，14 天以前仍未壓縮 0 個；壓縮 policy 已恢復啟用（job 1289）。
  - 大小：`hypertable_size` 與整個資料庫都約 **12 GB**（Phase4-2 推估 14 GB），資料範圍 2020-04-01～2024-05-10、1,856 檔。
  - 正式資料上的查詢（三次取中位數）：`get_ordered_ticks()` 全市場單日 2020-04-06 73 萬列 0.66 秒、2022-03-08 114 萬列 1.0 秒、2024-05-08 126 萬列 1.45 秒（門檻 10 秒）；`get_stock_ticks("2330")` 單日約 0.02 秒。
- **相依**：Phase4-1、Phase4-2。

---

## Phase 5：測試與收斂

### Phase5-1. 測試改寫與新增 ✅

- **目的**：讓 CI 在沒有 TimescaleDB 的機器上照常通過，有 DB 的機器上能驗證真實行為。
- **做法**：
  - `tests/test_api_public_interfaces.py`：`get_last_tick` 兩個測試以 `StockTickAPI.__new__` 繞過 `__init__`，改寫後照常可用，只需更新 docstring 裡的「DolphinDB 連線」字樣。
  - `tests/test_strategy_data_access.py`：禁止策略直接連 tick 資料庫的規則，從 `dolphindb|ddb` 擴充為 `dolphindb|ddb|psycopg|connectorx`。
  - `tests/test_entrypoint_and_logging.py`：`test_require_tick_db_path_raises_when_unset` 改成驗證 `require_tick_database_url()`；`test_tick_db_path_is_none_rather_than_none_string` 驗的是 `TICK_DB_PATH`，Phase5-2 移除該常數時一併刪除（或改驗 `TICK_DATABASE_URL` 缺值時為 None）。
  - 新增 `tests/test_stock_tick_timescale.py`：
    - 模組層級 `pytest.mark.skipif(not os.getenv("TICK_DATABASE_URL"), ...)`。
    - 每個測試建立獨立的 schema（`CREATE SCHEMA test_<uuid>`），結束時 `DROP SCHEMA ... CASCADE`，**不碰正式的 `stock_tick`**。
      `search_path` 要設成 `test_<uuid>, public`，**不可只有測試 schema**：`create_hypertable()` 等函式屬於 extension、裝在 `public`，只指向測試 schema 的話建表當場失敗。
    - 涵蓋 Phase2-1 驗證方式的 5 個情境（冪等、取代、失敗彙報、兩種格式正規化、排除規則）、〈讀取介面契約〉的排序與 dtype、同時間戳記多筆成交的 `seq` 排序、`bid_price = 0` 原樣保存。
    - 測試資料用手寫的小 DataFrame，不讀 `data/downloads/`。
- **產出**：上列測試檔。
- **驗證方式**：未設 `TICK_DATABASE_URL` 時 `pytest` 全數通過（新測試被 skip）；設定後 `pytest tests/test_stock_tick_timescale.py` 全數通過。
- **相依**：Phase2-2、Phase3-1。

### Phase5-2. 移除台股 tick 的 DolphinDB 程式與設定 ✅

- **目的**：兩套儲存不長期並存。
- **原規劃**：移除 `DDB_*`、`TICK_DB_*`／`require_tick_db_path()`、`.env.example` 的 DolphinDB 區塊、`manual_init_tick_metadata.py` 與本機的 `tick_metadata.json`；施作前依 2026-09-25 的殘留盤點（程式、設定、測試、文件、本機資料）逐項處理，收尾以下方 `grep` 為準。
- **期貨 tick 裁示（2026-10-09 使用者採選項 A：一併刪除）**。依據：
  - 期貨 tick 已裁示不做（2026-09-15），程式沒有落地目標；日後要做時比照本文件另立 backlog，改寫成 TimescaleDB 的 `futures_tick` 表。
  - 舊程式**每跑一次就重複寫入一份**：`futures_tick_updater.py` 沒有任何已爬紀錄可續跑，DolphinDB 表又設 `keepDuplicates=ALL`（去重會丟掉同時間戳的多筆成交）。選項 B（保留）至少要補「契約 × 日」的載入紀錄，不能原樣留著。
  - 期貨 tick 的下游缺口（期貨逐筆回測做不了）記在 [台期貨平台說明](../docs/futures/tw-futures-platform.md)〈已知限制〉（原 `暫緩工作彙整.md` S9，2026-10-10 改判不做並移出）。
- **產出**：見下方實作紀錄。
- **驗證方式**：`grep -rn "dolphindb\|DDB_\|tick_metadata" core apps scripts .env.example` 只剩說明性註解；`pytest` 全數通過；`python scripts/check_layer_deps.py` 通過。
- **實作紀錄（2026-10-09，`feature/tick-timescaledb`，選項 A）**：
  - **偏離原規格：沒有等 Phase4-3 執行完才刪**。原本相依 Phase4-3 是為了「新儲存資料完整後才刪對照」，
    但 DolphinDB 主機早已停用、裡面沒有可對照的資料；完整性改以歷史 CSV 本身對帳（Phase4-3 的比對）。
  - 期貨 tick 四支程式與測試、`futures_tick` target 與 `DataType.FUTURES_TICK`、`DDB_*` 設定與只剩它在用的 `get_int_env()`、
    `TICK_DB_*`／`require_tick_db_path()`／`TICK_TABLE_NAME`／`FUTURES_TICK_TABLE_NAME`、`FUTURES_TICK_DOWNLOADS_PATH` 與 tick metadata 路徑、
    `.env.example` 的 DolphinDB 區塊、`[dolphindb]` extra、`manual_init_tick_metadata.py` 全數移除；
    `manual_tick_updater.py` 與兩個 F401 例外已在 Phase2-2 移除；`manual_tick_crawler.py` 沒有 DolphinDB 相依，保留。
  - `grep -rn "dolphindb\|DDB_\|tick_metadata" core apps scripts .env.example` 只剩三處說明「`load_log` 取代了 `tick_metadata.json`」的註解，沒有程式相依。
  - **本機資料留到合併部署時再刪**：主目錄的 `data/downloads/tw_stock/meta/tick/tick_metadata.json`（含 backup）與 `data/downloads/tw_futures/tick/` 的 1 個 CSV。
    主目錄在合併前仍跑舊程式，先刪沒有好處。
  - **歷史 CSV（`data/tick_history/`）本步驟不刪**：Phase4-3 比對通過後，2026-10-10 由使用者另行指示刪除（備份去向見〈現況與合併部署〉第 4 點）。
- **相依**：原定 Phase4-3（實際未等，見上方偏離原規格）、Phase5-1。

### Phase5-3. 更新文件 ✅

- **目的**：現行說明文件不再描述 DolphinDB。
- **做法**：更新下列文件中 tick 儲存、環境建立、`.env` 設定的段落：
  - `README.md`／`README_en.md`
  - `docs/setup/dev-setup.md`
  - `docs/deployment/dev-deployment.md`
  - `docs/exchanges/data_coverage.md`（tick 的日期範圍改成 Phase4-3 的實際結果）
  - `docs/pipeline/etl-ingestion.md`
  - `docs/dev/runtime-artifacts.md`
  - `docs/dev/data-access-layer.md`（「tick 不在此列、仍走 DolphinDB」與 `update_db` 收尾說明兩處，改成 tick 已由 `core/dao/tw/stock_tick_dao.py` 管理）
  - `docs/dev/naming-axes.md`（沒有 DolphinDB 字樣，但以「metadata 的鍵是台股代號」說明 `stock_tick_utils.py`，metadata 移除後要改）
  - `docs/futures/tw-futures-platform.md`（期貨 tick 走 DolphinDB 的三處，依 Phase5-2 的期貨裁示更新或保留）
  - `docs/dev/code-quality.md`
  - `docs/backtest/module-map.md`
  - `docs/commands/command-usage.md`／`command-usage.zh-TW.md`
  - `core/backtest/README.md`（`Scale.TICK` 那列寫 DolphinDB）
  - `strategies/README.md`（`StockTickAPI` 小節；目前沒有 DolphinDB 字樣，確認說明與新契約一致即可）
  - `strategy_lab/README.md`
  - `scripts/manual/README.md`

  新的 schema 設計理由（〈資料表設計〉的決策表）寫進 `docs/pipeline/etl-ingestion.md` 的 tick 小節；進度表與量測紀錄不搬過去。
- **產出**：上列文件。
- **驗證方式**：`grep -rni "dolphin" README.md README_en.md docs core/backtest/README.md strategies/README.md strategy_lab/README.md scripts/manual/README.md` 只剩裁示保留的期貨部分；依 `docs/setup/dev-setup.md` 從零啟動 TimescaleDB 並跑通 Phase4-2 的查詢。
- **實作紀錄（2026-10-09）**：清單上的文件全數改完（`docs/dev/naming-axes.md`、`docs/backtest/module-map.md` 等），
  另補 `docs/deployment/dev-deployment.md`；`docs/pipeline/etl-ingestion.md` 新增〈台股 tick（TimescaleDB）〉，收錄〈資料表設計〉的決策理由、
  寫入與排除規則、續跑、讀取與歷史匯入。`dolphindb` 移除後 `uv sync --all-extras` 恢復可用，README 與 dev-setup 一併改回。
  驗證：`grep -rni dolphin` 只剩 etl-ingestion 的〈與 DolphinDB 版的語意差異〉與期貨平台文件的「已移除」說明。
- **相依**：Phase5-2。

---

## 附：併入的盲捕收斂（原 `暫緩工作彙整.md` S13 的 tick 部分）

2026-10-01 由 `暫緩工作彙整.md` S13 移入（使用者裁示）。**2026-10-09 全數處理完（功能分支）：23 處 → 4 處刻意保留**，其餘收窄或隨檔案刪除。那 23 處 `except Exception`（ruff `BLE001`）
都在本文件要改寫或可能刪除的檔案裡：先收窄、改寫時再丟掉是白工，所以改成**動到哪個檔案，就在同一步收窄那個檔案**。

| 檔案 | 處數（2026-10-01 現查；2026-10-09 複查相同） | 在哪一步處理 |
|------|:--:|------|
| `core/pipeline/tw/updaters/stock_tick_updater.py` | 10 | Phase2-2（續跑依據改寫）。**✅ 2026-10-09：剩 4 處刻意保留**（Shioaji 用量查詢 2 處與登出：Shioaji 1.7 沒有公開的例外型別；thread 邊界），理由就地寫在註解 |
| `core/pipeline/tw/utils/stock_tick_utils.py` | 4 | Phase2-2。**✅ 2026-10-09：歸零**（隨 metadata 函式移除） |
| `core/pipeline/tw/cleaners/stock_tick_cleaner.py` | 3 | Phase2-2。**✅ 2026-10-09：歸零**（收窄為 `KeyError`／`ValueError`／`OSError`／`TypeError` 的組合） |
| `core/pipeline/tw/crawlers/stock_tick_crawler.py` | 1 | Phase2-2。**✅ 2026-10-09：改成轉拋 `ConnectionError`**（不再被 ruff 計為盲捕；原本回 None 會讓失敗日被當成沒成交） |
| `core/pipeline/tw/loaders/futures_tick_loader.py` | 3 | Phase5-2。**✅ 2026-10-09：隨檔案刪除歸零**（期貨 tick 選項 A） |
| `core/pipeline/tw/updaters/futures_tick_updater.py` | 1 | Phase5-2。**✅ 2026-10-09：隨檔案刪除歸零** |
| `core/pipeline/tw/crawlers/futures_tick_crawler.py` | 1 | Phase5-2。**✅ 2026-10-09：隨檔案刪除歸零** |

- **判準**沿用 `暫緩工作彙整.md` S13 的做法：只收「該重試的外部失敗」（傳輸用 `OSError`、解析用
  `BaseDataCrawler.HTML_PARSE_ERRORS`、DAO 用 `DBError`），**逐項隔離迴圈不收窄**，理由就地寫在註解裡。
- **驗證**：每處收窄各配一個突變測試（台股 tick 四檔的放在 `tests/test_stock_tick_updater.py`，見 Phase2-2）；處數以
  `uv run ruff check core/pipeline --select BLE001 --statistics` 現查，`tests/test_quality_ratchet.py` 擋住數量回升。

## 風險與對策

| 風險 | 說明 | 對策 |
|------|------|------|
| 讀取效能不如 DolphinDB | 用 `pd.read_sql` 或 `SELECT *` 會慢數十倍 | 讀取固定走 ConnectorX 並明列欄位；Phase4-2 設門檻，量過才全量匯入。**已排除**：正式資料上全市場單日 0.7～1.5 秒（門檻 10 秒） |
| 回測結果與 DolphinDB 版不同 | ① 價格由 float32 改成 float64；② 同一時間戳記跨股票的順序由不確定改成固定 | 目前沒有 `Scale.TICK` 策略，也沒有 tick 回測的回歸基準，不影響既有回歸；在 `docs/pipeline/etl-ingestion.md` 記下這兩點語意 |
| 磁碟寫滿 | Phase4-2 實測：全部不壓縮約 175 GB，壓縮後約 14 GB | 匯入逐 chunk 壓縮，峰值只多約 1 GB；Docker 磁碟上限 926 GB（2026-10-09 實查）不必調。**已排除**：Phase4-3 實際 12 GB |
| 壓縮 policy 在匯入中途壓縮歷史資料 | policy 以「現在」往回算，2020～2024 的 chunk 全部符合條件，背景 job 會壓縮還在載入的週 | 試點與匯入期間停用 policy、結束後恢復（Phase1-3 的 `pause_／resume_compression_policy()`） |
| 時區錯位 | 用 `TIMESTAMPTZ` 時，ConnectorX 會回傳 UTC | schema 固定用 `TIMESTAMP`；整合測試驗證 `time` 為 naive 且等於 CSV 原值 |
| 歷史 CSV 格式不一致 | 歷史格式與 cleaner 格式不同（欄名、欄序、毫秒時間、浮點字串），2026-10-09 已全量盤點 | Phase2-1 的 loader 正規化兩種格式，並以手寫樣本的單元測試涵蓋每一種差異 |
| 興櫃時期混入 | 173 檔有興櫃時期的資料（約 1,032 萬列），以「股」為單位、交易到 15:00 後，混進去成交量會差上千倍 | 依 Phase4-1 裁示的規則，以 `price` 表判斷並整天排除；`price` 表整天缺資料時該日失敗而不是排除 |
| 歷史 CSV 被覆寫或刪除 | 歷史放在 tick updater 的工作目錄 `data/downloads/tw_stock/tick/`：`--target tick`（`all` 也含 tick）會整檔覆寫爬到的股票；Phase2-2 之後的 updater 會在入庫後刪 CSV（Google 雲端另有一份，但重新下載 56 GB 很費事） | **已排除**：2026-10-09 搬到 `data/tick_history/`（Phase4-1）；開發用的 worktree 一律以 `--source-dir` 明確指向新位置，不改 `ALPHAEDGE_DATA_DIR` 去指主目錄的 `data/` |
| `core/pipeline/` 搬家 | `core目錄邊界收斂.md` Phase5-1 會把它搬到頂層 `etl/` | 該步驟暫緩中；本分支合併進 main 前不要先搬，否則合併時整批路徑衝突 |
| 與 PostgreSQL 遷移計畫衝突 | 兩份工作都要動 compose、driver、`core/dao/` 的連線入口 | 本文件先做，已建立 `postgres` service、`[tick]` extra、`core/dao/timescale.py` 與 `TICK_DATABASE_URL`；`PostgreSQL遷移計畫.md` Phase0-1～Phase1-1 已改寫為沿用這些基礎設施 |

---

## 關聯與狀態

- **優先級**：P2（main 上的 tick 級回測沒有資料源；功能分支已完成，剩合併部署）
- **相關程式**（功能分支）：`core/dao/timescale.py`、`core/dao/tw/stock_tick_dao.py`、`core/pipeline/tw/loaders/stock_tick_loader.py`、`core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py`、`core/pipeline/tw/cleaners/stock_tick_cleaner.py`（邏輯不改，是欄位契約的來源）、`core/api/tw/stock_tick_api.py`、`core/adapters/tw/stock_quote_adapter.py`、`core/backtest/datafeed/tw/stock_datafeed.py`、`core/config/`、`scripts/check_layer_deps.py`、`scripts/manual/manual_tick_history_import.py`、`apps/update_db.py`、`docker-compose.yml`、`Dockerfile`
- **相關 backlog**：
  - 2026-09-14／09-15 使用者裁示（原記於已刪除的爬蟲缺口回補文件）：不再使用 DolphinDB、台股 tick 不回補、期貨 tick 不做；本文件是台股 tick 的新落地方式，台股與期貨 tick 的 DolphinDB 殘留都由本文件 Phase5-2 移除。
  - [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)：**本文件先做，該計畫沿用本文件建立的基礎設施**——`postgres` service（TimescaleDB image，`PostgreSQL遷移計畫.md` Phase0-1）、`psycopg`（`PostgreSQL遷移計畫.md` Phase0-3 從 `[tick]` 搬到主相依）、`core/dao/timescale.py` 與 `core/dao/connection.py` 同目錄（`PostgreSQL遷移計畫.md` Phase1-1）、`TICK_DATABASE_URL` 與 `DATABASE_URL` 分開（`PostgreSQL遷移計畫.md` Phase0-2）；日頻資料遷入同一個 `alphaedge` 資料庫，`stock_tick`／`stock_tick_load_log` 已佔用的表名不得重複。該計畫的程式改動以本分支合併後的 main 為基底。
  - [滑價模型後續優化.md](滑價模型後續優化.md) S3（tick 級改用 bid/ask 成交）與 S2 的「實際買賣價差」依據都相依本文件；資料已入庫，本分支合併進 main 後即可開工。
  - [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1（`core/pipeline/` 搬到頂層 `etl/`）暫緩到本文件與 `PostgreSQL遷移計畫.md` 涉及 pipeline 的步驟完成，避免搬兩次；本文件這一半要等合併進 main 才算落地。
  - [盤中動能策略.md](盤中動能策略.md)：S2 日 K 近似的偏差（成交量、停損先後）要靠逐筆資料校準；逐筆觸發策略不能跑 `Scale.TICK` 回測，校準另寫研究腳本（見文首〈現況與合併部署〉）。
  - 期貨 tick 不做造成的下游缺口（期貨逐筆回測做不了）：原 `暫緩工作彙整.md` S9，2026-10-10 改判不做並移出，缺口改記在 [台期貨平台說明](../docs/futures/tw-futures-platform.md)〈已知限制〉；期貨 tick 程式已由本文件 Phase5-2 移除。
