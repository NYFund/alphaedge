# 台股 tick 改用 TimescaleDB

## Abstract

- **背景／問題**：台股 tick 目前的流程是：Shioaji 爬取 → `data/downloads/tw_stock/tick/{stock_id}.csv` → `loadTextEx` 寫入 DolphinDB（`dfs://tickDB`，TSDB 引擎）→ 回測用 `StockTickAPI` 以 DolphinDB script 查詢。DolphinDB 放在另一台 Windows 主機，2026-09-14 已裁示往後不再使用（台股 tick 不回補、期貨 tick 不做）。於是 tick 級回測現在**沒有可用的資料源**。完整歷史 CSV 備份在使用者的 Google 雲端；本機只殘留 541 檔（2024-05-13～05-15）。
- **目標**：落地目標改成 TimescaleDB（PostgreSQL extension）：
  - 寫入路徑改用 `COPY`，以「股票 × 交易日」為單位做到冪等寫入。
  - 讀取路徑改用 ConnectorX 直接產生 pandas DataFrame。
  - `StockTickAPI` 的公開方法與回傳欄位不變，`StockDataFeed`／`StockQuoteAdapter`／策略都不用改。
  - 把雲端上的歷史 CSV 一次匯入並壓縮。
- **範圍界線**：
  - **不做期貨 tick**：2026-09-15 已裁示不做，`futures_tick_*` 的 DolphinDB 程式原樣保留，去留見 Phase5-2 的裁示。
  - **DolphinDB 殘留的完整清單**（程式、設定、測試、文件、本機資料）見 Phase5-2，2026-09-17 由健檢第五輪的盤點搬進本文件；施作時照那份走即可，不必重新掃。
  - **不做**台股日頻資料的 SQLite → PostgreSQL 遷移，那是 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 的範圍；本文件只和它共用 PostgreSQL 容器、driver 與連線層。
  - **不改**爬蟲（`StockTickCrawler`）與清洗邏輯（`StockTickCleaner` 的欄位格式），也不改 tick 回測引擎的成交語意。
  - **不做** tick 回補續跑到今天。回補要不要做、做多少是另一個決策，本文件只保證 updater 在新儲存上可以續跑。
- **驗收標準**：
  1. 雲端歷史 CSV 全數入庫，每個「股票 × 交易日」的列數與 CSV 一致。
  2. `StockTickAPI` 四個公開方法在 TimescaleDB 上回傳相同欄位；Mac 本機查一天全市場 `get_ordered_ticks()` 的耗時已記錄，並符合 Phase4-2 定下的門檻。
  3. `python -m tasks.update_db --target tick` 可寫入 TimescaleDB，重跑同一區間不產生重複列。
  4. 台股 tick 相關程式不再 import `dolphindb`，`pytest` 全數通過。

---

> **2026-09-16：連線層位置調整。** 資料存取層已統一放在 `core/dao/`（設計見 [資料存取層](../docs/dev/data-access-layer.md)）。
> 本文件 Phase1-1 的 `core/db/timescale.py` 改為 `core/dao/timescale.py`，
> 分層登記沿用既有的 `("core.dao", 2, ...)`；`StockTickLoader`／`StockTickAPI` 的 SQL
> 改收進 `core/dao/tw/stock_tick_dao.py`，〈資料表設計〉與〈讀取介面契約〉不變。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase0-1 | `docker-compose.yml` 新增 TimescaleDB service | `docker-compose.yml`、`.env.example` | `psql` 連線後 `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` 有值 | ⬜ | 與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase0-1 共用同一個 service；Docker Desktop 磁碟上限要先調大 |
| Phase0-2 | 新增 `TICK_DATABASE_URL` 設定 | `core/config/settings.py`、`.env.example`、`tests/test_config_consistency.py` | `pytest tests/test_config_consistency.py` 通過 | ⬜ | 刻意不用 `DATABASE_URL`，理由見步驟詳述 |
| Phase0-3 | `[tick]` 選用相依改為 `psycopg`＋`connectorx` | `pyproject.toml`、`requirements.txt` | 乾淨 venv 安裝 `.[tick]` 後可 import 兩者 | ⬜ | `dolphindb` 的去留在 Phase5-2 裁示，本步驟先不移除 |
| Phase1-1 | 建立 `core/db/` 連線層 | `core/db/__init__.py`、`core/db/timescale.py`、`scripts/check_layer_deps.py` | `python scripts/check_layer_deps.py` 通過；`SELECT 1` 可執行 | ⬜ | 相依 Phase0-2；PostgreSQL遷移計畫 Phase1-1 之後在同一目錄擴充 |
| Phase1-2 | 建表：`stock_tick` hypertable 與 `stock_tick_load_log` | `core/pipeline/tw/loaders/stock_tick_loader.py`、`core/config/schema.py` | 建表函式重跑兩次不報錯；`timescaledb_information.hypertables` 查得到 | ⬜ | 相依 Phase1-1；schema 詳見〈資料表設計〉 |
| Phase1-3 | 設定壓縮與壓縮 policy | 同上 | `timescaledb_information.jobs` 有 compression job；手動 `compress_chunk` 成功 | ⬜ | 相依 Phase1-2 |
| Phase2-1 | 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等） | `core/pipeline/tw/loaders/stock_tick_loader.py` | 新增的整合測試：同一份 CSV 載入兩次，列數不變 | ⬜ | 相依 Phase1-2 |
| Phase2-2 | updater 續跑依據由 `tick_metadata.json` 改為 `stock_tick_load_log` | `core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py` | 以樣本資料模擬中斷後重跑，只爬缺的日期 | ⬜ | 相依 Phase2-1 |
| Phase3-1 | 改寫 `StockTickAPI` 讀取路徑 | `core/api/tw/stock_tick_api.py` | 四個方法的欄位、dtype、排序符合〈讀取介面契約〉；`tests/test_api_public_interfaces.py` 通過 | ⬜ | 相依 Phase1-2 |
| Phase3-2 | DataFeed／Adapter 文字與連線生命週期收尾 | `core/backtest/datafeed/tw/stock_datafeed.py`、`core/adapters/tw/stock_quote_adapter.py`、`core/api/base.py` | tick 級回測跑完後連線有關閉（log 可見） | ⬜ | 相依 Phase3-1 |
| Phase4-1 | 盤點 Google 雲端的歷史 CSV | 本文件（盤點紀錄） | 檔案佈局、日期範圍、總列數、欄位格式差異寫入本文件 | ⬜ | **無相依，可最先做**；結果可能改變 Phase2-1 的 CSV 解析 |
| Phase4-2 | 試點：本機 541 檔入庫與效能量測 | 本文件（量測紀錄） | 記錄入庫耗時、壓縮前後大小、單日全市場查詢耗時 | ⬜ | 相依 Phase1-3、Phase2-1、Phase3-1 |
| Phase4-3 | 歷史 CSV 全量匯入與完整性比對 | `scripts/manual/manual_tick_history_import.py` | 每個「股票 × 交易日」的 DB 列數＝CSV 列數＝`load_log.row_count` | ⬜ | 相依 Phase4-1、Phase4-2 |
| Phase5-1 | 測試改寫與新增 | `tests/test_api_public_interfaces.py`、`tests/test_strategy_data_access.py`、`tests/test_entrypoint_and_logging.py`、`tests/test_stock_tick_timescale.py` | `pytest` 全數通過；無 DB 的環境整合測試自動 skip | ⬜ | 相依 Phase2-2、Phase3-1 |
| Phase5-2 | 移除台股 tick 的 DolphinDB 程式與設定 | 見步驟詳述 | `grep -rn "dolphindb\|DDB_" core/api core/pipeline/tw/*/stock_tick* tasks` 無結果 | ⬜ | 相依 Phase4-3、Phase5-1；**期貨 tick 的處理需使用者裁示** |
| Phase5-3 | 更新文件 | `README.md`、`README_en.md`、`docs/` 相關頁、`core/strategies/README.md` | 文件中不再描述 tick 存在 DolphinDB | ⬜ | 相依 Phase5-2 |

---

## 現況盤點（2026-09-16）

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
| `get_ordered_ticks(start, end)` | 同上，加 `order by time` | `StockQuoteAdapter.convert_to_tick_quotes()`，**回測每個交易日呼叫一次、start＝end** |
| `get_stock_ticks(stock_id, start, end)` | 同上，加 `stock_id=` 條件 | `get_last_tick()` |
| `get_last_tick(stock_id, date)` | `get_stock_ticks(...).iloc[-1:]` | 無呼叫端（有單元測試） |

- `between a:b` 兩端都包含，會多含到隔天 00:00:00 那一個瞬間。台股沒有這個時間點的 tick，實際上沒影響，改寫時直接換成半開區間。
- `get()` 的 docstring 說「個股各自排序好」，但 query 沒有 `order by`，順序其實是分區掃描順序，沒有保證。

使用端：只有 `StockDataFeed.setup()` 在 `strategy.scale == Scale.TICK` 時建立 `StockTickAPI()`。`generate_stock_quotes()` 對每一列 `itertuples()` 讀 `stock_id`／`time`／`close`／`volume`／`bid_*`／`ask_*`／`tick_type` 建立 `TickQuote`。**目前沒有任何策略使用 `Scale.TICK`。**

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

### CSV 樣式（本機 541 檔實測）

```csv
stock_id,time,close,volume,bid_price,bid_volume,ask_price,ask_volume,tick_type
3605,2024-05-13 09:00:19.577032,42.0,80,41.9,1,42.0,21,1
```

| 欄位 | 實測值域 | 備註 |
|------|----------|------|
| `stock_id` | 全為 4 位數字字串 | 樣本不含 ETF；全市場有 `00878` 這類 5～6 碼、前導 0 的代號，**讀 CSV 一定要指定 `dtype=str`** |
| `time` | `YYYY-MM-DD HH:MM:SS.ffffff`，固定 26 字元；時段 09:00:00～14:31:05 | 無時區，為台北當地時間；精度到 microsecond（cleaner 刻意補齊）；13:30 之後的是盤後資料 |
| `close` | 3.24～2980.0，最多 2 位小數 | 無空值 |
| `volume` | 1～10,000 | 單位：張；無 0 |
| `bid_price`／`ask_price` | 0～2980.0；約 0.9% 為 0 | **0 表示該側無委託**（例如鎖漲跌停），不是缺值 |
| `bid_volume`／`ask_volume` | 0～82,814 | |
| `tick_type` | 0／1／2 | `{1: 外盤, 2: 內盤, 0: 無法判定}` |

- 總列數 957,262（05-13：327,090、05-14：279,942、05-15：350,230），CSV 約 58 bytes／列。
- **`(stock_id, time)` 不唯一**：17,451 列（1.8%）與其他列共用同一個時間戳記，屬於同一瞬間撮合出的多筆成交；其中 32 列連所有欄位都完全相同。所以這兩欄不能當主鍵，也不能用 `DISTINCT` 去重，否則成交量會少算。

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
    row_count   INTEGER     NOT NULL,             -- 本次寫入的列數，供完整性比對
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
| 量的型別 | `INTEGER` | 實測最大 82,814 張，`SMALLINT` 不夠 |
| `bid/ask` 為 0 | 照存 0，不轉 `NULL` | 和現行語意一致（`TickQuote` 預設 0.0），使用端不用多處理 `NaN` |
| `seq` 欄 | 新增 | ① 同一時間戳記有多筆成交，要有 `seq` 才能得到可重現的排序；② 完整性比對時可以逐列對照 CSV |
| 主鍵／唯一鍵 | **不設**，冪等由「刪除後重寫＋`load_log`」保證 | hypertable 的唯一索引一定要含 `time`；`(stock_id, time, seq)` 雖然可以當唯一鍵，但每筆寫入都要檢查唯一性，歷史匯入會明顯變慢，寫入壓縮 chunk 時還要先解壓。寫入單位本來就是「股票 × 交易日」，整段刪除後重寫已經足夠 |
| chunk 間隔 | 7 天 | 推估 2020-04～2024-05 約 1,000 個交易日：1 天一個 chunk 會產生約 1,500 個 chunk（含非交易日），planning 成本偏高；7 天約 220 個。每天查詢靠壓縮 batch 的 min/max `time` 跳過不相關資料，不需要 chunk 剛好切在一天。Phase4-2 量測後可再調整 |
| 壓縮 | `segmentby = stock_id`、`orderby = time, seq` | 按股票查詢時可以直接定位 segment；同一 segment 內依時間排序，delta 編碼壓縮率高 |
| `load_log` 粒度 | 股票 × 交易日 | 取代 `tick_metadata.json` 的「每檔最後日期」，還能逐日核對列數；主鍵天然防止重複登記 |

### 容量推估（Phase4-1／Phase4-2 實測後回填）

- 本機樣本 540 檔平均每天約 32 萬列。全市場約 1,900 檔，但成交集中在熱門股，**推估每天 100～200 萬列**。
- 2020-04～2024-05 約 1,000 個交易日 → **推估總計 10～20 億列**。
- 未壓縮每列約 80 bytes（含 tuple header）→ 80～160 GB；TimescaleDB 對這類資料的壓縮率通常在 10 倍以上 → **推估壓縮後 5～15 GB**。
- 未壓縮的量會超過 Docker Desktop 預設的磁碟上限，所以 Phase4-3 必須「匯入一週、壓縮一週」，控制峰值用量。

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

### Phase0-1. `docker-compose.yml` 新增 TimescaleDB service ⬜

- **目的**：提供本機可重現的 TimescaleDB。放在 Mac 本機，回測讀取就不用走網路。
- **做法**：
  - 新增 service `postgres`，image 用 `timescale/timescaledb`，**實作時鎖定 `2.x-pg17` 的明確版本**，不要用 `latest`。`by_range()` 需要 TimescaleDB 2.13 以上。
  - 使用 named volume `alphaedge_pgdata`。**不要 bind mount 到專案目錄**：Docker Desktop on Mac 的 bind mount 走檔案共享，大量寫入時非常慢，而且專案目錄以前放在同步資料夾吃過虧。
  - 加上 `healthcheck`（`pg_isready`）、port `5432:5432`，帳密從 `.env` 的 `POSTGRES_USER`／`POSTGRES_PASSWORD`／`POSTGRES_DB=alphaedge` 讀取。
  - `.env.example` 補上述三個鍵。
  - service 名稱用 `postgres` 而不是 `timescaledb`：[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase0-1 會沿用同一個 service，TimescaleDB image 本身就是完整的 PostgreSQL。
  - **Docker Desktop 的 Disk usage limit 要調到 ≥ 150 GB**（Settings → Resources），避免 Phase4-3 匯入途中寫滿磁碟。
- **產出**：`docker-compose.yml`、`.env.example`。
- **驗證方式**：`docker compose up -d postgres` 之後，執行 `docker compose exec postgres psql -U $POSTGRES_USER -d alphaedge -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"` 有回傳版本號。
- **相依**：無。

### Phase0-2. 新增 `TICK_DATABASE_URL` 設定 ⬜

- **目的**：tick 的連線設定由環境決定。
- **做法**：
  - 在 `core/config/settings.py` 新增 `TICK_DATABASE_URL: Optional[str] = os.getenv("TICK_DATABASE_URL")`，格式為 `postgresql://user:pass@localhost:5432/alphaedge`。
  - **不直接用 `DATABASE_URL`**：PostgreSQL 遷移計畫 Phase1-2 規劃「設了 `DATABASE_URL` 就把所有日頻資料切到 PostgreSQL」。本文件先上線的話，使用者為了 tick 設定 `DATABASE_URL`，等那邊的程式合進來，日頻資料會在還沒遷移時就被切走。分成兩個鍵最安全；那份計畫完成後，可以讓 `TICK_DATABASE_URL` 沒設定時退回 `DATABASE_URL`。
  - 比照 `require_tick_db_path()` 新增 `require_tick_database_url()`：未設定時拋出清楚的錯誤訊息。
  - `.env.example` 補 `TICK_DATABASE_URL`；`DDB_*` 先保留，到 Phase5-2 再移除。
- **產出**：`core/config/settings.py`、`core/config/schema.py`（或 `settings.py`，看 `require_*` 放哪裡）、`core/config/__init__.py`、`.env.example`。
- **驗證方式**：`pytest tests/test_config_consistency.py` 通過（`.env.example` 與程式讀取的環境變數雙向核對）。
- **相依**：無。

### Phase0-3. `[tick]` 選用相依改為 `psycopg`＋`connectorx` ⬜

- **目的**：寫入要用 psycopg 3 的 `COPY`，讀取要用 ConnectorX。
- **做法**：
  - `pyproject.toml` 的 `[project.optional-dependencies]` 改成 `tick = ["psycopg[binary]>=3.2", "connectorx>=0.4"]`，`requirements.txt` 鎖定精確版本。
  - **`dolphindb` 先搬到另一個 extra `futures-tick`**，不要直接刪。期貨 tick 仍在用它，最終去留在 Phase5-2 裁示。
  - 這兩個套件都是由 tick 模組惰性 import，比照現行 `dolphindb` 的寫法加註解，並把 `[tool.ruff.lint.per-file-ignores]` 那段的 F401 例外換成新模組。
- **產出**：`pyproject.toml`、`requirements.txt`。
- **驗證方式**：乾淨 venv 執行 `pip install -e ".[tick]"` 後，`python -c "import psycopg, connectorx"` 成功。
- **相依**：無。

---

## Phase 1：連線層與 schema

### Phase1-1. 建立 `core/db/` 連線層 ⬜

- **目的**：loader（`core/pipeline`）和 API（`core/api`）都要連 TimescaleDB。連線邏輯不能各寫一份，也不能讓 `core/api` 去 import `core/pipeline`。
- **做法**：
  - 新增 `core/db/timescale.py`，提供：
    - `get_tick_database_url() -> str`：呼叫 `require_tick_database_url()`。
    - `connect_tick_db(autocommit: bool = False) -> psycopg.Connection`：寫入用。
    - `get_connectorx_uri() -> str`：讀取用。ConnectorX 吃的是 `postgresql://` URI，不接受 psycopg 的 connection。
  - `scripts/check_layer_deps.py` 的 `_LAYER_RULES` 新增 `("core.db", 1, "共用層／資料庫連線", False)`，與 `core.utils` 同層，才能被 `core.api` 與 `core.pipeline` 共用。
  - 路徑與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase1-1 的 `core/db/connection.py` 放在同一個目錄。那份計畫之後的 SQLAlchemy engine 會放在隔壁，兩者不衝突。
- **產出**：`core/db/__init__.py`、`core/db/timescale.py`、`scripts/check_layer_deps.py`。
- **驗證方式**：`python scripts/check_layer_deps.py` 通過；在有 DB 的環境 `connect_tick_db().execute("SELECT 1")` 成功。
- **相依**：Phase0-2、Phase0-3。

### Phase1-2. 建表：`stock_tick` hypertable 與 `stock_tick_load_log` ⬜

- **目的**：取代 DolphinDB 的 `create_db()`。
- **做法**：
  - `core/config/schema.py`：新增 `STOCK_TICK_TABLE_NAME: str = "stock_tick"`、`STOCK_TICK_LOAD_LOG_TABLE_NAME: str = "stock_tick_load_log"`。`TICK_DB_NAME`／`TICK_DB_PATH`／`TICK_TABLE_NAME` 留到 Phase5-2 移除。
  - `StockTickLoader.create_db()` 改成依序執行：
    1. `CREATE EXTENSION IF NOT EXISTS timescaledb`
    2. 〈資料表設計〉的兩段 DDL
    3. 建立 index
  - 全部用 `IF NOT EXISTS`／`if_not_exists => TRUE`，重跑不報錯。
  - 移除 `DEFAULT_TICK_DB_START_TIME`／`DEFAULT_TICK_DB_END_TIME`／`TICK_DB_HASH_PARTITIONS`：hypertable 會自動長出 chunk，不需要預先宣告日期範圍。這也順便解決 DolphinDB 分區只開到 2030-12-31 的隱藏上限。
  - 移除 `setup()` 裡的 `setTSDBCacheEngineSize`，那是 DolphinDB 專用設定。
- **產出**：`core/pipeline/tw/loaders/stock_tick_loader.py`、`core/config/schema.py`、`core/config/__init__.py`。
- **驗證方式**：連續呼叫 `create_db()` 兩次不報錯；`SELECT hypertable_name FROM timescaledb_information.hypertables` 回傳 `stock_tick`；`\d stock_tick` 的欄位型別與 DDL 一致。
- **相依**：Phase1-1。

### Phase1-3. 設定壓縮與壓縮 policy ⬜

- **目的**：歷史 tick 寫入後不再修改，壓縮能把磁碟用量與全市場查詢的 I/O 降到約十分之一。
- **做法**：`create_db()` 建完 hypertable 後接著執行：

  ```sql
  ALTER TABLE stock_tick SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = 'stock_id',
      timescaledb.compress_orderby   = 'time, seq'
  );
  SELECT add_compression_policy('stock_tick', INTERVAL '14 days', if_not_exists => TRUE);
  ```

  - 14 天讓每日更新寫入的最近兩個 chunk 保持未壓縮，重跑最近幾天時不用先解壓。
  - 另外在 loader 提供 `compress_chunks_before(older_than: datetime.date) -> int`，包一層 `SELECT compress_chunk(c, if_not_compressed => TRUE) FROM show_chunks('stock_tick', older_than => ...) c`，給 Phase4-3 的匯入腳本逐週呼叫。
- **產出**：`core/pipeline/tw/loaders/stock_tick_loader.py`。
- **驗證方式**：`SELECT * FROM timescaledb_information.jobs WHERE hypertable_name = 'stock_tick'` 有 compression job；寫入樣本後呼叫 `compress_chunks_before()`，`chunk_compression_stats('stock_tick')` 顯示已壓縮。
- **相依**：Phase1-2。

---

## Phase 2：寫入路徑

### Phase2-1. 改寫 `StockTickLoader` 寫入路徑（`COPY`＋冪等） ⬜

- **目的**：取代 `loadTextEx`，並補上 DolphinDB 版缺少的兩件事：DB 層冪等，以及逐檔回報失敗。
- **做法**：
  - `add_to_db(remove_files: bool = False, dir_path: Optional[Path] = None) -> None`：`dir_path` 預設 `TICK_DOWNLOADS_PATH`，讓 Phase4-3 可以指定其他資料夾。逐檔呼叫 `load_csv()`，單檔失敗不中斷整批，最後呼叫 `BaseDataLoader.finish_load()` 彙報結果。現行 tick 線沒有呼叫 `finish_load()`（`tests/test_loader_failure_reporting.py` 特別註明這個例外），改寫後要補上。
  - `load_csv(csv_path: Path) -> int`：
    1. `pd.read_csv(csv_path, dtype={"stock_id": str}, parse_dates=["time"])`，檢查 9 個欄位齊全。
    2. 加上 `trade_date = time.dt.date`，依原始列序算出 `seq = groupby(["stock_id", "trade_date"]).cumcount()`。
    3. **一個交易日一個 transaction**，逐日處理：
       - `DELETE FROM stock_tick WHERE stock_id = %s AND time >= %s AND time < %s`（該日 00:00 到隔天 00:00）
       - 用 `cursor.copy("COPY stock_tick (stock_id, time, seq, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type) FROM STDIN")` 以 `write_row()` 寫入
       - `INSERT INTO stock_tick_load_log ... ON CONFLICT (stock_id, trade_date) DO UPDATE SET row_count = EXCLUDED.row_count, source_file = EXCLUDED.source_file, loaded_at = now()`
       - `COMMIT`
    4. 回傳寫入的總列數。
  - **為什麼選「刪除後重寫」而不是 `ON CONFLICT DO NOTHING`**：完全相同的重複列本來就是合法資料（實測 32 列），沒有自然唯一鍵可以用；而且 CSV 是「整段區間覆寫」產生的，同一天再出現時應該以新檔為準。
  - **目標 chunk 已壓縮時**：TimescaleDB 2.11 以上支援對壓縮 chunk 做 `DELETE`／`INSERT`，但很慢。日常更新只會碰到最近 14 天（未壓縮），可以忽略。歷史重灌時，由 Phase4-3 的腳本先 `decompress_chunk` 再寫入。
  - SQL 參數一律走 `%s` 佔位符（CLAUDE.md §2.10），表名從 `schema.py` 常數以 `psycopg.sql.Identifier` 組合。
  - 移除 `append_csv_to_dolphinDB`／`append_all_csv_to_dolphinDB`／`clear_all_cache`／`delete_dolphinDB`。
- **產出**：`core/pipeline/tw/loaders/stock_tick_loader.py`。
- **驗證方式**：新增整合測試（見 Phase5-1）：
  1. 同一份 CSV 載入兩次，`stock_tick` 列數不變，`load_log.row_count` 與 CSV 一致。
  2. 載入一份內容較少的同日 CSV，舊列被完整取代。
  3. 一個欄位損壞的 CSV 會讓 `finish_load()` 拋出 `DataLoadError`，其他檔案照常入庫。
- **相依**：Phase1-2。

### Phase2-2. updater 續跑依據改為 `stock_tick_load_log` ⬜

- **目的**：`tick_metadata.json` 的日期是從 CSV 掃出來的，入庫失敗時也會前進，改成以資料庫實際寫入的紀錄為準。
- **做法**：
  - `StockTickUtils`：
    - 新增 `get_loaded_last_dates() -> Dict[str, datetime.date]`，查詢 `SELECT stock_id, max(trade_date) FROM stock_tick_load_log GROUP BY stock_id`。
    - `check_date_crawled()` 改用這份結果，語意維持「`date <= last_date` 就跳過」，和現行一致（停牌日不會每次重爬）。
    - `get_table_latest_date()` 改查 `max(trade_date)`。
    - update 開頭查一次，傳給各 thread，**不要每檔股票查一次 DB**。
  - `StockTickUpdater.update()`：
    - 開頭那段「比對 metadata 刪 CSV」改成：CSV 裡每個交易日在 `load_log` 都有紀錄、且 `row_count` 一致，才刪除。
    - 結尾的 `update_tick_metadata_from_csv()` 移除。
  - `generate_tick_metadata_backup()`、`update_tick_metadata_from_csv()`、`load_tick_metadata_stocks()`、`scan_tick_downloads_folder()` 移除。`tick_metadata.json` 檔案本身留到 Phase5-2 再刪，作為對照。
  - `scripts/manual/manual_init_tick_metadata.py` 失去用途，在 Phase5-2 刪除。
  - 模組說明字串（`"""台股 tick 的 DolphinDB 與 metadata 工具"""`）與 class docstring 同步更新。
- **產出**：`core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py`。
- **驗證方式**：以 Phase4-2 的 3 天樣本入庫後，手動刪掉某檔股票最後一天的 `load_log` 與資料，對 2024-05-13～05-15 跑 `update()`（可用替身取代 crawler），只有那檔股票被重爬。
- **相依**：Phase2-1。

---

## Phase 3：讀取路徑

### Phase3-1. 改寫 `StockTickAPI` 讀取路徑 ⬜

- **目的**：讓回測用的查詢在 TimescaleDB 上跑得快，並符合〈讀取介面契約〉。
- **做法**：
  - `setup()`：移除 DolphinDB session 與 `setTSDBCacheEngineSize`，改成取得 `get_connectorx_uri()`，並確認資料表存在（查一次 `to_regclass('stock_tick')`，不存在時記 error 並拋出）。現行「資料庫不存在只 `print`」的行為一併修正，避免回測跑完才發現整段沒有報價。
  - 三個查詢方法共用私有方法 `_query_ticks(where_sql: str, params: Tuple, order_by: str) -> pd.DataFrame`：

    ```python
    tick: pd.DataFrame = cx.read_sql(
        self.uri,
        query,  # SELECT <9 欄> FROM stock_tick WHERE ... ORDER BY ...
        return_type="pandas",
    )
    ```

    - **ConnectorX 不支援參數佔位符**。日期由 `datetime.date` 格式化成 `'YYYY-MM-DD'`；`stock_id` 先以 `str.isalnum()` 驗證、不合法就拋 `ValueError`，再嵌入 SQL。這個例外要在程式註解寫清楚理由（CLAUDE.md §2.10 要求參數化，這裡是 driver 限制）。
    - 只 `SELECT` 契約列出的 9 個欄位，不用 `SELECT *`：壓縮 chunk 是按欄位解壓的，而且回傳結果不應帶出 `seq`。
    - 查完之後強制轉 dtype（`astype`），保證空表與非空表的欄位型別一致。
  - `get_last_tick()` 不改。
  - `close()`：ConnectorX 每次查詢自行開關連線，沒有常駐連線，覆寫成 no-op 並寫註解說明。
  - **不採用 `pd.read_sql`**：它是逐列轉成 Python 物件，百萬列級查詢要慢上數十倍，這是換 TimescaleDB 後最容易踩到的效能陷阱。
- **產出**：`core/api/tw/stock_tick_api.py`。
- **驗證方式**：`pytest tests/test_api_public_interfaces.py -k tick` 通過；Phase5-1 的整合測試驗證三個查詢的排序與 dtype；Phase4-2 記錄查詢耗時。
- **相依**：Phase1-1、Phase1-2。

### Phase3-2. DataFeed／Adapter 文字與連線生命週期收尾 ⬜

- **目的**：清掉回測層對 DolphinDB 的描述。行為不變。
- **做法**：
  - `core/backtest/datafeed/tw/stock_datafeed.py`：class docstring 的「DolphinDB 的 Tick」改成「TimescaleDB 的 Tick」，`setup()` 的 docstring 同步修改。
  - `core/api/base.py` 的 `close()` docstring 中「非 SQLite 的資料源（如 DolphinDB）自行覆寫」改成 TimescaleDB。
  - `core/strategies/stock/foreign_sell_short_day_trade_strategy.py` 的註解「tick 資料在 DolphinDB」改掉。
  - `StockQuoteAdapter.convert_to_tick_quotes()` 不用改，但要確認 `generate_stock_quotes()` 對 `time` 欄的使用可以接受 `datetime64[ns]`。
- **產出**：上列檔案。
- **驗證方式**：寫一支只在測試裡用的 `Scale.TICK` 最小策略，對 Phase4-2 的 3 天樣本跑一次回測，不報錯，而且 `get_quotes()` 每天回傳的 `TickQuote` 數量等於當天的 DB 列數。
- **相依**：Phase3-1。

---

## Phase 4：歷史資料遷移

### Phase4-1. 盤點 Google 雲端的歷史 CSV ⬜

- **目的**：現在只知道雲端上「有全部的 tick CSV」，但不知道檔案怎麼切、欄位是否和現行 cleaner 輸出一致。先盤點，Phase2-1 的解析和 Phase4-3 的匯入才不會做白工。
- **做法**：
  - 下載到**本機非同步資料夾**（建議 `~/tick_history/`），不要放在 Google 雲端的串流資料夾，也不要放在專案的 `data/` 底下，避免雲端占位檔和 git 誤加。
  - 盤點下列項目，寫進本步驟末尾的「盤點紀錄」：
    1. 檔案佈局：一檔股票一個檔，還是一天一個檔？是否分年份資料夾？是否壓縮？
    2. 日期範圍：最早、最晚交易日，以及是否有整段缺漏。
    3. 欄位格式：是否都是 9 欄、相同欄名；`time` 精度是否全部是 26 字元（早期資料可能沒補到 microsecond）；`stock_id` 前導 0 是否保留。
    4. 總檔數、總大小、抽樣 5 天的全市場每日列數，用來回填〈容量推估〉。
    5. 和 DolphinDB 的差異：`tick_metadata.json` 顯示 1,379 檔停在 2024-05-10，確認雲端資料是否涵蓋到這個日期。
  - 格式和現行 cleaner 輸出不一致時，在匯入腳本加上轉換，**不改 loader 的欄位契約**。
- **產出**：本文件的盤點紀錄。
- **驗證方式**：盤點紀錄涵蓋上述 5 項。
- **相依**：無，可最先做。

### Phase4-2. 試點：本機 541 檔入庫與效能量測 ⬜

- **目的**：在全量匯入前驗證 schema 與冪等性，量測壓縮率和查詢速度，決定 chunk 間隔要不要調整。
- **做法**：
  1. 對 `data/downloads/tw_stock/tick/` 的 541 個 CSV 跑 `StockTickLoader().add_to_db()`，記錄耗時。
  2. 記錄壓縮前大小：`hypertable_size('stock_tick')`。
  3. `compress_chunks_before(datetime.date(2024, 5, 16))` 之後，記錄 `hypertable_compression_stats('stock_tick')`。
  4. 分別在**壓縮前**與**壓縮後**，各量 3 次：
     - `StockTickAPI().get_ordered_ticks(d, d)`（三天各一次）
     - `get_stock_ticks("2330", d, d)`
  5. 把量測值依「全市場每日列數 ÷ 樣本每日列數」換算成全市場的推估耗時。
- **門檻**：換算後全市場單日 `get_ordered_ticks()` **≤ 10 秒**，以 1,000 個交易日的回測來說約 3 小時。超過時依序檢查：
  - 是否誤用 `SELECT *`
  - `ORDER BY` 能否由壓縮的 `orderby` 直接提供
  - 改用 ConnectorX 的 `return_type="arrow"` 再轉 pandas
- **產出**：本步驟末尾的「量測紀錄」表。
- **驗證方式**：量測紀錄填寫完整；DB 列數與 CSV 總列數（957,262）一致。
- **相依**：Phase1-3、Phase2-1、Phase3-1。

### Phase4-3. 歷史 CSV 全量匯入與完整性比對 ⬜

- **目的**：把雲端的歷史資料全部搬進 TimescaleDB。
- **做法**：新增 `scripts/manual/manual_tick_history_import.py`：
  - 參數：`--source-dir`、`--start-date`、`--end-date`、`--resume`。
  - **逐週處理**：載入一週 → `compress_chunks_before(該週結束)` → 在 log 記錄進度 → 下一週。峰值磁碟用量只會多出一週的未壓縮資料。
  - `--resume`：以 `stock_tick_load_log` 判斷已完成的「股票 × 交易日」並跳過，可以隨時中斷後重跑。
  - 要重灌已壓縮的週時，先 `decompress_chunk` 再呼叫 `load_csv()`。
  - Phase4-1 若發現格式差異，在這支腳本內轉成 cleaner 的輸出格式後再交給 loader。
  - 用 `from loguru import logger` 輸出進度，檔名 `tick_history_import.log`。
- **產出**：`scripts/manual/manual_tick_history_import.py`、本步驟末尾的「匯入紀錄」。
- **驗證方式**：
  1. 每個「股票 × 交易日」：CSV 列數 ＝ `SELECT count(*) FROM stock_tick WHERE ...` ＝ `load_log.row_count`。腳本最後跑一次全量比對，列出不一致的組合（應為 0 筆）。
  2. 抽樣 20 個「股票 × 交易日」，`get_stock_ticks()` 的結果與 CSV 逐列比對（`time`、價格、量）完全相同。
  3. 抽樣 5 個交易日，每日成交量加總 ≈ `price` 表當日成交股數 ÷ 1000（盤後零股與定價交易會造成小幅落差，差距記進匯入紀錄）。
  4. 全部 chunk 除了最近 14 天之外都已壓縮。
- **相依**：Phase4-1、Phase4-2。

---

## Phase 5：測試與收斂

### Phase5-1. 測試改寫與新增 ⬜

- **目的**：讓 CI 在沒有 TimescaleDB 的機器上照常通過，有 DB 的機器上能驗證真實行為。
- **做法**：
  - `tests/test_api_public_interfaces.py`：`get_last_tick` 兩個測試以 `StockTickAPI.__new__` 繞過 `__init__`，改寫後照常可用，只需更新 docstring 裡的「DolphinDB 連線」字樣。
  - `tests/test_strategy_data_access.py`：禁止策略直接連 tick 資料庫的規則，從 `dolphindb|ddb` 擴充為 `dolphindb|ddb|psycopg|connectorx`。
  - `tests/test_entrypoint_and_logging.py`：`test_require_tick_db_path_raises_when_unset` 改成驗證 `require_tick_database_url()`。
  - 新增 `tests/test_stock_tick_timescale.py`：
    - 模組層級 `pytest.mark.skipif(not os.getenv("TICK_DATABASE_URL"), ...)`。
    - 每個測試建立獨立的 schema（`CREATE SCHEMA test_<uuid>`，`search_path` 指向它），結束時 `DROP SCHEMA ... CASCADE`，**不碰正式的 `stock_tick`**。
    - 涵蓋 Phase2-1 的 3 個冪等／失敗情境、〈讀取介面契約〉的排序與 dtype、同時間戳記多筆成交的 `seq` 排序、`bid_price = 0` 原樣保存。
    - 測試資料用手寫的小 DataFrame，不讀 `data/downloads/`。
- **產出**：上列測試檔。
- **驗證方式**：未設 `TICK_DATABASE_URL` 時 `pytest` 全數通過（新測試被 skip）；設定後 `pytest tests/test_stock_tick_timescale.py` 全數通過。
- **相依**：Phase2-2、Phase3-1。

### Phase5-2. 移除台股 tick 的 DolphinDB 程式與設定 ⬜

- **目的**：兩套儲存不長期並存。
- **做法**：
  - 移除：
    - `core/config/settings.py` 的 `DDB_HOST`／`DDB_PORT`／`DDB_USER`／`DDB_PASSWORD`
    - `core/config/schema.py` 的 `TICK_DB_NAME`／`TICK_DB_PATH`／`TICK_TABLE_NAME`／`require_tick_db_path()`
    - `.env.example` 的 DolphinDB 區塊
    - `data/downloads/tw_stock/meta/tick/tick_metadata.json`（以及 backup）
    - `scripts/manual/manual_init_tick_metadata.py`
    - `pyproject.toml` 裡 `stock_tick_utils.py`／`stock_tick_loader.py` 的 F401 例外
  - `scripts/manual/manual_tick_updater.py`／`manual_tick_crawler.py`：逐支判斷改寫或刪除。
  - `tasks/update_db.py` 說明文字中的 `futures_tick（Shioaji → DolphinDB…）` 依下方裁示結果更新。
  - **需使用者裁示：期貨 tick 的 DolphinDB 程式怎麼處理**（`futures_tick_crawler.py`／`cleaner`／`updater`／`loader`、`DDB_PATH`、`futures-tick` extra）：
    - **選項 A（建議）**：一併刪除。期貨 tick 已裁示不做（2026-09-15），這些程式沒有落地目標；日後要做時，比照本文件另立 backlog 改寫成 TimescaleDB 的 `futures_tick` 表。`tasks/update_db.py` 的 `futures_tick` target 與 `DataType.FUTURES_TICK` 一併移除，`tests/test_futures_tick.py` 與 `test_entrypoint_and_logging.py` 的兩個相關測試跟著調整。
    - **選項 B**：原樣保留，`DDB_PATH` 與 `futures-tick` extra 繼續存在，只刪台股的部分。
- **DolphinDB 殘留的完整盤點**（施作時直接照這份走，不必重新掃）：
  - **程式**：`core/api/tw/stock_tick_api.py`（本規劃改寫成 TimescaleDB，不刪）；`core/pipeline/tw/crawlers/{stock,futures}_tick_crawler.py`、`cleaners/{stock,futures}_tick_cleaner.py`、`loaders/{stock,futures}_tick_loader.py`、`updaters/{stock,futures}_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py`；`core/backtest/datafeed/tw/stock_datafeed.py` 的 `Scale.TICK` 分支；`tasks/update_db.py` 的 `tick`／`futures_tick` target；`scripts/manual/manual_tick_{crawler,updater}.py`、`manual_init_tick_metadata.py`。
  - **設定**：`core/config/settings.py`（`TICK_UPDATE_START_DATE`、`DDB_*`、tick 爬蟲多帳號 `API_KEYS`）、`core/config/schema.py`（`TICK_DB_*`、`require_tick_db_path()`、`TICK_TABLE_NAME`、`FUTURES_TICK_TABLE_NAME`）、`core/config/__init__.py`、`core/pipeline/utils/constant.py`、`pyproject.toml`（`tick` extra 與 per-file-ignores）、`.env.example`。
  - **測試**：`tests/test_futures_tick.py`、`tests/test_entrypoint_and_logging.py`、`tests/test_strategy_data_access.py`、`tests/test_api_public_interfaces.py`、`tests/test_config_consistency.py` 的 tick 相關段落。
  - **文件**：兩份 README、`strategy_lab/README.md`、`scripts/manual/README.md`，以及 `docs/` 下 `pipeline/etl-ingestion.md`、`futures/tw-futures-platform.md`、`setup/dev-setup.md`、`exchanges/data_coverage.md`、`backtest/module-map.md`、`deployment/dev-deployment.md`、`dev/runtime-artifacts.md`、`dev/code-quality.md`、兩份 `commands/command-usage*.md`。
  - **本機資料**（不在版控）：`data/downloads/tw_stock/tick/` 541 個 CSV 與 `tick_metadata.json`、`data/downloads/tw_futures/tick/` 1 個 CSV。
- **產出**：上列檔案。
- **驗證方式**：`grep -rn "dolphindb\|DDB_\|tick_metadata" core tasks scripts .env.example` 只剩裁示保留的期貨部分（選項 A 時應為 0 筆）；`pytest` 全數通過；`python scripts/check_layer_deps.py` 通過。
- **相依**：Phase4-3（確認新儲存資料完整後才刪對照）、Phase5-1。

### Phase5-3. 更新文件 ⬜

- **目的**：現行說明文件不再描述 DolphinDB。
- **做法**：更新下列文件中 tick 儲存、環境建立、`.env` 設定的段落：
  - `README.md`／`README_en.md`
  - `docs/setup/dev-setup.md`
  - `docs/deployment/dev-deployment.md`
  - `docs/exchanges/data_coverage.md`（tick 的日期範圍改成 Phase4-3 的實際結果）
  - `docs/pipeline/etl-ingestion.md`
  - `docs/dev/runtime-artifacts.md`
  - `docs/dev/naming-axes.md`
  - `docs/dev/code-quality.md`
  - `docs/backtest/module-map.md`
  - `docs/commands/command-usage.md`／`command-usage.zh-TW.md`
  - `core/strategies/README.md`
  - `strategy_lab/README.md`
  - `scripts/manual/README.md`

  新的 schema 設計理由（〈資料表設計〉的決策表）寫進 `docs/pipeline/etl-ingestion.md` 的 tick 小節；進度表與量測紀錄不搬過去。
- **產出**：上列文件。
- **驗證方式**：`grep -rni "dolphin" README.md README_en.md docs core/strategies/README.md strategy_lab/README.md scripts/manual/README.md` 只剩裁示保留的期貨部分；依 `docs/setup/dev-setup.md` 從零啟動 TimescaleDB 並跑通 Phase4-2 的查詢。
- **相依**：Phase5-2。

---

## 風險與對策

| 風險 | 說明 | 對策 |
|------|------|------|
| 讀取效能不如 DolphinDB | 用 `pd.read_sql` 或 `SELECT *` 會慢數十倍 | 讀取固定走 ConnectorX 並明列欄位；Phase4-2 設門檻，量過才全量匯入 |
| 回測結果與 DolphinDB 版不同 | ① 價格由 float32 改成 float64；② 同一時間戳記跨股票的順序由不確定改成固定 | 目前沒有 `Scale.TICK` 策略，也沒有 tick 回測的回歸基準，不影響既有回歸；在 `docs/pipeline/etl-ingestion.md` 記下這兩點語意 |
| 磁碟寫滿 | 未壓縮的全量資料推估 80～160 GB | Docker Desktop 磁碟上限調到 ≥ 150 GB；匯入逐週壓縮 |
| 時區錯位 | 用 `TIMESTAMPTZ` 時，ConnectorX 會回傳 UTC | schema 固定用 `TIMESTAMP`；整合測試驗證 `time` 為 naive 且等於 CSV 原值 |
| 雲端 CSV 格式不一致 | 早期資料可能與現行 cleaner 輸出不同 | Phase4-1 先盤點，差異在匯入腳本轉換 |
| 與 PostgreSQL 遷移計畫衝突 | 兩份工作都要動 compose、driver、`core/db/` | service 名稱、目錄、環境變數鍵已在本文件預先對齊（Phase0-1、Phase0-2、Phase1-1），先做的建立、後做的沿用 |

---

## 關聯與狀態

- **優先級**：P2（tick 級回測目前沒有資料源；無其他工作被它阻塞）
- **相關程式**：`core/pipeline/tw/loaders/stock_tick_loader.py`、`core/pipeline/tw/updaters/stock_tick_updater.py`、`core/pipeline/tw/utils/stock_tick_utils.py`、`core/pipeline/tw/cleaners/stock_tick_cleaner.py`（不改，欄位契約的來源）、`core/api/tw/stock_tick_api.py`、`core/adapters/tw/stock_quote_adapter.py`、`core/backtest/datafeed/tw/stock_datafeed.py`、`core/config/`、`tasks/update_db.py`
- **相關 backlog**：
  - 2026-09-14／09-15 使用者裁示（原記於已刪除的爬蟲缺口回補文件）：不再使用 DolphinDB、台股 tick 不回補、期貨 tick 不做；本文件是台股 tick 的新落地方式。DolphinDB 殘留中，台股部分由本文件 Phase5-2 處理，期貨 tick 的殘留依 Phase5-2 的裁示處理；完整殘留清單見 Phase5-2。
  - [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)：共用 PostgreSQL 容器（Phase0-1）、driver（Phase0-3）、`core/db/`（Phase1-1）。兩份工作都不以對方為前置，先做的建立、後做的沿用。
