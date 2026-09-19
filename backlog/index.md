# Backlog 索引

本檔案彙整 `backlog/` 內**所有待辦事項文件**的優先級與完成狀態，作為單一入口。

- 新增待辦 `.md` 時：在下表新增一列。
- 實作進度變動時：同步更新該列的「狀態」與「進度」。
- 項目完成並整份移出 `backlog/`（見 [`manage-backlog` skill §5](../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理)）時：刪除該列。

**狀態圖例**：⬜ 未開始（僅規劃）｜🔄 進行中｜✅ 完成｜⛔ 中斷（需在該文件註明中斷點與恢復下一步）｜⏸ 暫緩

> 文件結構與狀態標記規範見 [`manage-backlog` skill](../.claude/skills/manage-backlog/SKILL.md)。

**優先級（Priority）**：`P0` 最高（阻塞其他項目或已在進行）→ `P3` 最低（長期架構規劃）。各文件內部的階段編號一律寫成 `Phase1-1`，不縮寫成 `P1-1`，避免與優先級混淆。

---

## 待辦清單

| 優先級 | 狀態 | 檔案 | 說明 | 進度 | 相依 |
|:------:|:----:|------|------|------|------|
| P2 | ⬜ | [實盤下單架構規劃.md](實盤下單架構規劃.md) | 以 Shioaji 實作實盤下單：`core/broker/`（券商閘道：登入／CA／限流、合約解析、委託轉換、回報正規化、帳務、即時行情）＋ `core/live/`（OMS 狀態機、風控、對帳、`LiveTrader`、`LiveDataFeed`、盤中事件迴圈、`TradingMode` 狀態機、告警與存活監控、實盤與回測 parity 比對）＋ `tw_trading.db` 紀錄庫；同一支策略不改寫即可從回測切到模擬／正式環境，涵蓋現股、融券／借券、當沖、零股、指數期貨、股票期貨（融資依 D3） | 0 / 42 項（Phase0-1~Phase7-4） | **無前置相依**，Phase0-1（Shioaji API 測試與 CA，使用者手動）可最先做。**D1~D7 需使用者裁示**（D1 日頻執行時點阻塞 Phase4-1、D2 盤中報價契約阻塞 Phase5-1、D7 未成交殘量政策阻塞 Phase4-6）。Phase3-4 會動到 `Backtester`，須跑回歸雙線且不可重產 baseline；[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 動工時要把 `tw_trading.db` 列入；Phase6-3 股票期貨與 [暫緩工作彙整.md](暫緩工作彙整.md) S4 同屬股期。2026-09-17 立項，同日對照量化交易常見架構健檢後補 Phase4-7（告警與存活監控）、Phase4-8（訊號 parity 比對）與 D7，並修訂 D4 的 `custom_field` 用途 |
| P3 | 🔄 | [滑價模型後續優化.md](滑價模型後續優化.md) | 滑價口徑收斂（2026-09-18 完成並移出）時一併盤點、但刻意不做的五項強化：報表輸出滑價成本統計、台股滑價逐標的設定、tick 級改用 bid/ask 成交、依下單量放大的市場衝擊模型、用實盤成交回填校正滑價參數 | 1 / 5 項 ✅（S1，2026-09-18）；S2~S5 ⬜ | **S1 已完成**：報表新增 `Slippage Cost` 與佔損益比例；實測 `ForeignSellShortDayTradeStrategy` 2024Q1 的滑價吃掉已實現損益的 **42.31%**，且實際價差是名目 10 bps 的 2.4 倍（檔位對齊放大），README 的「檔位會吸收小額 bps」已依實測改寫為「會放大」。S2 卡「用什麼決定各標的滑價」的依據（D2），無依據不開工；S3 相依 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)；S4 相依校準資料；S5 相依 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase4-6 並需足夠樣本。**S2~S5 都會改變既有回測結果，須重產 baseline**。2026-09-18 由已結案的 `回測滑價口徑收斂.md` 拆出 |
| P2 | ⬜ | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) | 台股 tick 落地由 DolphinDB 改為 TimescaleDB：`stock_tick` hypertable（7 天 chunk、依 `stock_id` 壓縮）、`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json`、`StockTickAPI` 改用 ConnectorX 讀取（介面不變）、Google 雲端歷史 CSV 全量匯入、移除台股 tick 的 DolphinDB 程式 | 0 / 16 項（Phase0-1~Phase5-3） | **無前置相依**，Phase4-1（盤點雲端 CSV）可最先做。與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 共用 `postgres` service、driver 與 `core/dao/` 連線入口，不互為前置，先做的建立、後做的沿用；環境變數刻意用 `TICK_DATABASE_URL` 而非 `DATABASE_URL`，避免日頻資料提前被切走。Phase5-2 的期貨 tick DolphinDB 程式去留需使用者裁示。**2026-09-18 裁示**：實作完成後不自動把資料寫進正式資料庫——`data/downloads/tw_stock/tick/` 的 541 檔只是測試素材（開發只取抽樣），全量入庫、雲端歷史匯入與 `--target tick` 的真實更新都等使用者下指令才執行。2026-09-16 立項 |
| P3 | ⬜ | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) | 由 SQLite3（`tw_stock.db`、`tw_futures.db`）遷移到 PostgreSQL 的分階段實作計畫 | 0 / 16 項（Phase0-1~Phase5-3） | **DAO 資料存取層已完成**（2026-09-16，設計見 [資料存取層](../docs/dev/data-access-layer.md)），**2026-09-16 已依實況重新盤點改動面**：SQLite 專屬語法全在 `core/dao/`（`sqlite_master` 1、`INSERT OR` 4、`SAVEPOINT` 1、`GLOB` 1 處），非測試 `import sqlite3` 只剩 DAO 內 5 檔，另有 21 個 `connect_sqlite()` 連線取得點與 40 個直接連 SQLite 的測試檔；Phase1-2、Phase2-1~Phase2-3 縮減為改寫 `core/dao/` 內部，連線入口沿用 `core/dao/connection.py`。影響面廣，建議在其他重構收斂後再動。命名軸線交接的兩項 schema 收斂（台股表名補 `stock_` 前綴、`stock_id` → `symbol`）排在本批，欄位 Enum 下沉已完成。**2026-09-18 裁示**：實作完成後不自動遷移資料、也不自動切換 backend，Phase3-2 的正式遷移與 Phase5-1 的灰度切換都等使用者下指令 |
| P3 | ⏸ | [暫緩工作彙整.md](暫緩工作彙整.md) | 集中各文件因外部條件暫緩的工作：FinMind 連網冒煙腳本、券商分點 metadata 的 `last_attempted_date` 與 NO_DATA 延遲重試、股票期貨行情前 N 檔回補、平盤下放空限制與每日可當沖清單、漲跌停改用公告值 | 0 / 6 項（S1~S6 ⏸） | **全部暫緩，無可開工項**。S1~S3 等 FinMind 帳號升級（目前 `register`，無券商分點權限，2026-09-16 以真實 API 確認）；S4 等開始開發股期策略，**解除時不可直接跑 `--target futures_stock_price`**，須先暖身（見該文件 S4，做法已於 2026-09-18 依 `resolve_stock_futures_products()` 的 fallback 修正改寫——暖身樣本現在受 `top_n` 約束，不再是整份 320 檔）；S5~S6 等交易所公告資料源盤點（處置／警示股、每日可當沖清單、漲跌停公告值），兩者共用同一批來源，建議同批施作。2026-09-17 由 `DAO重構後續收斂.md`、`券商分點NO_DATA的metadata語意.md`、`爬蟲缺口回補與非交易日批次清理.md` 的暫緩項集中而成；2026-09-18 併入 `docs已載明但未實作的缺口盤點.md` 的 S8、S9（該文件其餘八項已完成並移出）。四份原文件皆已刪除，完成紀錄留在 git 歷史 |
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
