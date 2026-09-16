# Backlog 索引

本檔案彙整 `backlog/` 內**所有待辦事項文件**的優先級與完成狀態，作為單一入口。

- 新增待辦 `.md` 時：在下表新增一列。
- 實作進度變動時：同步更新該列的「狀態」與「進度」。
- 項目完成並整份移出 `backlog/`（見 [`manage-backlog` skill §5](../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理)）時：刪除該列。

**狀態圖例**：⬜ 未開始（僅規劃）｜🔄 進行中｜✅ 完成｜⛔ 中斷（需在該文件註明中斷點與恢復下一步）｜⏸ 暫緩

> 文件結構與狀態標記規範見 [`manage-backlog` skill](../.claude/skills/manage-backlog/SKILL.md)。

**優先級（Priority）**：`P0` 最高（阻塞其他項目或已在進行）→ `P3` 最低（長期架構規劃）。各文件內部的階段編號一律寫成 `Phase1-1`，不縮寫成 `P1-1`，避免與優先級混淆。

> **2026-08-15：「梯次」欄已移除**（連同 `排程與相依.md`）。該欄回答「什麼時候能動工」，
> 用來標示前置相依尚未解除的工作。護欄、API 介面收斂、baseline 換代、引擎重構四個閘門
> 全數結清後，**剩餘每一份工作都是現在就能開工**，該欄所有列的值變得相同、不再帶資訊。
> 跨文件的相依與「建議同批施作」的提示，一律改寫在各列的「相依」欄。

> **2026-09-15：全份複查**。對照重構後的程式碼逐份檢查，更新了過時的路徑、改動面盤點與相依；
> 各列「相依」欄只留現況與解除條件，逐日施作紀錄一律留在各文件內（維護規則第 6 點）。

---

## 待辦清單

| 優先級 | 狀態 | 檔案 | 說明 | 進度 | 相依 |
|:------:|:----:|------|------|------|------|
| P1 | 🔄 | [健檢第五輪收斂.md](健檢第五輪收斂.md) | 第五輪全專案健檢：回測做多路徑記帳口徑（除權息、鎖漲停買進）、已入庫的歷史資料錯誤（`price` 重播日、籌碼自營商欄、`stock_id` 前導 0）、期貨日夜盤整併與時段完整性、半份資料防線的 cleaner 缺口、前端容器、文件與規則漂移 | 0 / 30 項 ✅；Phase4-3、Phase5-1 🔄 | **無前置相依**。Phase1-7、Phase1-8、Phase3-7 需使用者裁示（Phase4-3 的刪除裁示已於 2026-09-15 定案）；Phase2-3、Phase3-2 的重爬與 [爬蟲缺口回補與非交易日批次清理.md](爬蟲缺口回補與非交易日批次清理.md) 共用交易所請求節奏，不要同時跑；Phase2-6 與 Phase3-3 同型、建議同批 |
| P2 | ⬜ | [回測績效指標統一由報表輸出.md](回測績效指標統一由報表輸出.md) | reporter 新增整體績效指標 CSV（公式一律走 `risk_metrics.py`），補齊年化波動度、Profit Factor、勝敗比、Information Ratio；前端改讀 CSV 不再自算，MDD 收斂為單一實作 | 0 / 5 項 | **無前置相依**，不改記帳、回歸基準零變動。S3 期貨 IR 口徑、S4 舊結果 fallback 需使用者裁示。S4 與 [健檢第五輪收斂.md](健檢第五輪收斂.md) Phase4-1 相關：S4 若讓前端不再 import `core`，Phase4-1 的兩個失敗 import 少掉一個（`frontend.config` 那個仍在），建議先裁示 S4 再動 Phase4-1，免得 `frontend/Dockerfile` 改兩次。2026-09-15 由 `StockBacktestAnalyzer` 刪除後的盤點立項 |
| P2 | ⬜ | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) | 台股 tick 落地由 DolphinDB 改為 TimescaleDB：`stock_tick` hypertable（7 天 chunk、依 `stock_id` 壓縮）、`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json`、`StockTickAPI` 改用 ConnectorX 讀取（介面不變）、Google 雲端歷史 CSV 全量匯入、移除台股 tick 的 DolphinDB 程式 | 0 / 16 項（Phase0-1~Phase5-3） | **無前置相依**，Phase4-1（盤點雲端 CSV）可最先做。與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 共用 `postgres` service、driver 與 `core/dao/` 連線入口，不互為前置，先做的建立、後做的沿用；環境變數刻意用 `TICK_DATABASE_URL` 而非 `DATABASE_URL`，避免日頻資料提前被切走。Phase5-2 的期貨 tick DolphinDB 程式去留需使用者裁示。2026-09-16 立項 |
| P3 | ⏸ | [爬蟲缺口回補與非交易日批次清理.md](爬蟲缺口回補與非交易日批次清理.md) | 全 ETL 涵蓋盤點後的收斂：三張日頻表的非交易日與單一市場批次、財報三表缺漏年季、`chip`／`margin`／月營收續跑、期貨三大法人籌碼與連續契約、台股與期貨 tick 的去留、股期行情回補 | 8 / 9 項 ✅（S1~S5、S7、S9、S10；S6 已移除不重新編號）；S8 ⏸ | **無前置相依。僅剩 S8 股期行情暫緩**：目前沒有任何策略交易股票期貨，解除條件為「開始開發股期策略」，屆時建議先跑前 10 檔 × 2020 起（約 2.5 天）。**注意**：表內股期行情不足 5 日、排不出流動性，直接跑 `--target futures_stock_price` 會退回整份 320 檔標的池，須先暖身（見該文件 S8）。**2026-09-15 優先級 P1 → P3**：唯一剩餘步驟要等股期策略立項，不再阻塞任何工作。S7／S9（tick）已裁示不做；DolphinDB 殘留的台股部分已由 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 立項（2026-09-16，改用 TimescaleDB 落地），期貨 tick 殘留的去留在該文件 Phase5-2 裁示。明確不含：任何 FinMind 工作（原 S6 券商分點，2026-09-05 使用者裁示移除）、爬蟲並行化、權益變動表、指數期貨日行情 |
| P3 | ⬜ | [券商分點NO_DATA的metadata語意.md](券商分點NO_DATA的metadata語意.md) | 券商分點 metadata 增加 `last_attempted_date`，已確認無資料的區間不再重複請求 FinMind | 0 / 2 項（S1 必做、S2 延遲重試可選） | **無前置相依，純效率問題**。方案已於 2026-08-01 選定（做法一）。**解除條件**：確認要省 FinMind quota，或 NO_DATA 重打開始影響日更時間。**2026-09-15 對照程式**：確認未實作；兩個最容易漏的保留點都在 `BrokerTradingMetadataStore.refresh_from_database()` 內，已寫進 S1。目前券商分點只回補 5 家、停在 `2026-01-28`，且沒有回補在跑，重複請求的實際成本為零 |
| P3 | ⬜ | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) | 由 SQLite3（`tw_stock.db`、`tw_futures.db`）遷移到 PostgreSQL 的分階段實作計畫 | 0 / 16 項（Phase0-1~Phase5-3） | **DAO 資料存取層已完成**（2026-09-16，設計見 [資料存取層](../docs/dev/data-access-layer.md)）：本計畫的 Phase1-2、Phase2-1~Phase2-3 縮減為改寫 `core/dao/` 內部，`core/db/` 改為 `core/dao/`，欄位 Enum 下沉已提前完成；各步驟產出欄待重新盤點。影響面廣，建議在其他重構收斂後再動。**2026-09-15 重新盤點**：改動面不只 `core/api/`——非測試程式 55 檔 `import sqlite3`（`core/pipeline/` 34、`core/api/` 12、`scripts/` 6、回測 DataFeed 2、`tasks/` 1）、`PRAGMA table_info` 14 處、測試 30 檔；原文件漏了期貨庫與回測 DataFeed。命名軸線交接的三項 schema 收斂（台股表名補 `stock_` 前綴、`stock_id` → `symbol`、欄位 Enum 下沉 `schema.py`）排在本批 |
| P3 | 🔄 | [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) | 美股平行模組擴充：`us/` 資料層、provider 抽象、美股回測 model | 1 / 9 項 ✅（Phase3-3，2026-09-02）；**Phase1-1 可直接開工** | **無前置相依**。**2026-09-15 依現行架構改寫目錄落點**：市場軸 `us/` 只開在 `pipeline`／`api`／`adapters`／`backtest/datafeed`；美股策略放 `core/strategies/stock/`、回測 model 放 `core/backtest/models/`，原規劃的 `strategies/us/`、`backtest/engine/`、`backtest/calendars/` 已取消；Phase2-1 改相依 Phase1-3。欄位語言已定案用英文（見 [ETL 入庫約定 §3.4](../docs/pipeline/etl-ingestion.md)）。建議 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 先收斂 |
| P3 | ⬜ | [台股新聞情緒溫度計篩選工具.md](台股新聞情緒溫度計篩選工具.md) | 每日爬取台股財經新聞，萃取個股利多/利空情緒並生成可篩選的溫度計指標 | 0 / 7 項（S1~S6 ⬜、S7 ⏸） | 與回測引擎無關，**無外部相依**；但 S1 新聞來源授權檢查阻塞其餘所有步驟，須先做。2026-09-15 補齊產出路徑（ETL 入庫約定檢查表、`core/api/tw/`、研究放 `strategy_lab/data_analysis/news_sentiment/`） |

---

## 維護規則

1. 每次**新增**待辦 `.md`，同步在上表新增一列，並填寫優先級、狀態、說明與相依。
2. 每次**實作**推進（子任務完成、狀態欄變更），同步更新該列的「狀態」與「進度」。
3. 項目完成並整份移出 `backlog/` 時，刪除本表對應那一列（移出方式見 [`manage-backlog` skill §5](../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理)）。
4. 優先級為當下研判結果，可隨需求調整，但調整後必須讓本表與各文件內的優先序敘述一致。
5. **跨文件的相依寫在「相依」欄**：包含前置條件、與哪些步驟建議同批施作、以及該判斷的日期與理由。
6. 本檔只放**通則**（狀態圖例、優先級、待辦清單、維護規則）；各文件內部的施作紀錄與中斷點寫在該文件自身，不要回流到本檔。
