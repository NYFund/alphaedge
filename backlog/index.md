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
| P1 | 🔄 | [重構後全專案健檢.md](重構後全專案健檢.md) | 2026-09-25 對重構後全專案的掃描，處理四類看不見的問題：有定義、無執行端；假綠燈；重複實作已分岔；照著文件做會出錯。共 51 步 | **45 / 51 項 ✅**（Phase2、Phase4、Phase5、Phase6、Phase8 全數完成）；🔄 5：Phase1-1（改為移除欄位）、Phase1-5、Phase2-8、Phase3-2、Phase3-7 已在 `feature/post-rehearsal` 實作、待演練後部署；⬜ 1：Phase7-1 拆 `core/live/trader.py`（同一步把收成交回報的 `drain_once` 等三個方法改名為 `apply_new_fills` 家族） | 5 個 🔄 等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 演練結束後隨 `feature/post-rehearsal` merge。本文件 Phase7-1 要等 `feature/post-rehearsal` merge，且 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase6-6 換月實跑通過（2026-10-20，屆時無期貨部位則順延 2026-11-17）之後才動 |
| P1 | 🔄 | [實盤委託價格類型設計.md](實盤委託價格類型設計.md) | 2026-10-06 演練第一次送單因 `price_type` 無人填寫而失敗。同日定案：策略只宣告 `live_execution`（`ExecutionStyle.MARKET`／`LIMIT`），由執行層依段落與商品換算成券商委託；臨時補值已移除 | 2 / 3 項（S1 ✅ 定案、S3 ✅ 移除臨時補值）；**S2 🔄 程式已在 main，股票 2026-10-08 實測通過，剩期貨 `MKP`＋IOC 經引擎實測** | 期貨實測與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase6-6 換月演練同一次驗證（演練專用期貨策略 2026-10-10 已部署，10/14 起建倉）；股票連續時段（保護價＋IOC）2026-10-10 起不列入本文件，改由 [盤中動能策略.md](盤中動能策略.md) S4 驗證 |
| P2 | 🔄 | [實盤下單架構規劃.md](實盤下單架構規劃.md) | 以 Shioaji 實作實盤下單：券商閘道 `core/broker/`＋實盤引擎 `core/live/`（OMS、風控與 `TradingMode`、對帳、多策略歸屬與資金分配、盤中迴圈、執行層、告警與存活監控、parity）＋ `tw_trading.db`；同一支策略不改寫即可從回測切到模擬／正式環境。2026-09-25 裁示分成股票線與期貨線，各自獨立宣告通過 | **44 / 62 項 ✅**；🔄 12：Phase7-1 演練（1a 兩線各 5 日 ✅；1b 下單路徑未發生，疑模擬環境不撮合集合競價，驗法待裁示）、Phase4-8／4-9／5-5／6-1／6-6 待實跑、Phase7-2S／7-7／7-9／7-10／7-11 已在 `feature/post-rehearsal` 實作待部署、Phase7-8 剩移除演練排程；⬜ 4：Phase7-2F、Phase7-4、Phase7-16 成交入帳失敗不中止段落、Phase7-17 期貨金額乘契約乘數；⏸ 2：Phase6-3 股期、Phase6-4 零股 | Phase6-6 換月實跑 2026-10-20（2026-10-10 已部署演練專用期貨策略，10/14～10/19 建倉）；**期貨的金額類風控少乘契約乘數**（2026-10-10 發現，另立 Phase7-17：10/21 演練平倉後、口徑定案後才修）；Phase7-16 排在 [重構後全專案健檢.md](重構後全專案健檢.md) Phase7-1 拆 `trader.py` 之後；兩者都須在 Phase7-2F 上線前完成；演練後與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase2-2～2-4、[回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) Phase1-8、[盤中動能策略.md](盤中動能策略.md) S3 同一批部署；Phase6-3 與 [暫緩工作彙整.md](暫緩工作彙整.md) S4 同條件；`tw_trading.db` 維持 SQLite，不隨 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 遷移 |
| P2 | 🔄 | [盤中動能策略.md](盤中動能策略.md) | `IntradayMomentumStrategy`：盤中漲幅 ≥ 9% 且累計量 ≥ 5,000 張即買（`MARKET`），進場當天跌回 8% 以下停損（現股當沖），隔天開盤出場；日 K 近似回測後以方案 B 上模擬環境 | 2 / 4 項（S1 ✅、S2 ✅ 日 K 回測，量級受偏樂觀假設主導、不可直接採信）；**S3 🔄 程式完成，在 `feature/post-rehearsal`，待演練後部署**；S4 ⬜ 模擬實跑 | S3 與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10 同批部署，排在該文件 Phase7-1 演練結束後；S4 的結果決定 [策略與執行分層.md](策略與執行分層.md) S4 是否解除暫緩，並兼驗股票連續時段送單（回填 [實盤委託價格類型設計.md](實盤委託價格類型設計.md) S2） |
| P2 | 🔄 | [回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) | 把兼任回測與實盤的 `run.py` 拆成 `python -m apps.backtest`／`apps.live`（`run.py` 已於 2026-10-01 刪除）；回測期間與資金可由參數覆寫；市場規則與 `FillConfig` 移出 `core/backtest/` 並加分層檢查；`tasks/` 併入 `apps/`；多策略組合回測的去留評估 | **14 / 16 項 ✅**（皆已在 main）；**Phase1-8 🔄 `tasks/` 併入 `apps/`，已在 `feature/post-rehearsal` 實作，待演練後部署，merge 後要立刻重裝 launchd**；Phase4-1 ⏸ | Phase1-8 與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase2-2～2-4、[實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10 同一次部署。Phase4-1 ⏸ 等 `feature/post-rehearsal` 部署後、股票線兩支策略同時跑的多日 parity 統計（2026-10-10 使用者同意改寫解除條件） |
| P2 | 🔄 | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) | 台股 tick 由 DolphinDB 改落地 TimescaleDB：`stock_tick` hypertable、`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json`、`StockTickAPI` 改用 ConnectorX（介面不變）、歷史 CSV 全量匯入、移除台股與期貨 tick 的 DolphinDB 程式 | **16 / 16 步完成，全在 `feature/tick-timescaledb`、尚未進 main**。全量匯入 2026-10-10 完成：1,050,896,400 列、1,781,133 組「股票 × 交易日」比對全數相符、壓縮後 12 GB、全市場單日查詢 0.7～1.5 秒 | 該分支從 `feature/post-rehearsal` 分出，要等後者演練後進 main 才能合併；合併後的部署收尾見文件〈現況與合併部署〉，之後整份移出。本機歷史 CSV（`data/tick_history/`）2026-10-10 已依使用者指示刪除，備份 dump 已由使用者另存雲端 |
| P2 | 🔄 | [台股tick級回測框架.md](台股tick級回測框架.md) | 2026-10-10 檢查發現現行 `Scale.TICK` 回測是「整天的 tick 當一根 K 棒」：有前視、委託無時間軸、同一檔數千筆報價會重複平倉、TICK 成交量上限未實作、`volume` 與實盤語意不同（實盤是當日累計量）。改成**逐筆重放**：策略宣告支援的級別、回測 `--scale` 覆寫；一次一筆、與實盤 `is_tick_triggered` 同契約；下一筆 tick 才成交、市價吃 bid/ask；標的範圍「策略清單／全市場」回測前自選（2026-10-10 使用者定案）；另定：`supported_scales` 宣告支援級別、`live_schedule` 改名 `execution_schedule` 供回測與實盤共用、策略清單沿用 `get_live_symbols()`（沒有就報錯） | 2 / 15 項（**Phase0-1 ✅ 2026-10-10** 介面七點與架構命名全數定案：`Backtester` 只留逐日迴圈，日 K／逐筆分別交給 `BarSimulator`／`TickSimulator`，另有 `PendingOrderManager`、`StockTickDataFeed`；新增 Phase1-4 先把日 K 抽成 `BarSimulator` 的純重構，回歸雙線須零變動）；**Phase2-1 ✅ 2026-10-10** tick 查詢支援多檔 `stock_ids` 與當日累計量 `cum_volume`（pandas 端計算），在 `feature/tick-backtest`；Phase1-3 改名動到實盤程式，部署排在 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 演練結束後 | 相依 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)（實作分支 `feature/tick-backtest` 已從 `feature/tick-timescaledb` 分出，合併順序跟在它與 `feature/post-rehearsal` 後面）；吸收 [滑價模型後續優化.md](滑價模型後續優化.md) S3；Phase5-2 的實跑結果回饋 [盤中動能策略.md](盤中動能策略.md) S2／S4 |
| P3 | 🔄 | [策略與執行分層.md](策略與執行分層.md) | 實盤「怎麼送單」原本沒有專屬的一層。比較三種分層深度後，2026-10-06 裁示採折衷方案 B：策略宣告 `live_execution`，由執行層集中換算（實作由 [實盤委託價格類型設計.md](實盤委託價格類型設計.md) S2 承接）；完整分層與回測依執行方式成交暫緩 | 3 / 5 項 ✅（S1 送單落點盤點、S2 採方案 B、S3 parity 執行差異歸因，皆已在 main）；S4、S5 ⏸ | **S4 解除條件：[盤中動能策略.md](盤中動能策略.md) S4 完成**（「出現盤中策略」的升級條件 2026-10-08 已成立）；S5 相依 S4，會改變回測結果，須重產 baseline |
| P3 | 🔄 | [core目錄邊界收斂.md](core目錄邊界收斂.md) | 讓 `core/` 只剩交易框架：切斷框架對 `core.pipeline` 的依賴並加檢查；具體策略與 `StrategyLoader` 搬到頂層 `strategies/`；`core/managers/` 改名 `core/position/`；`Dockerfile` 移到根目錄；`core/pipeline/`（2026-10-10 為 22,966 行，佔 `core/` 34%）搬到頂層改名 `etl/`。全程只搬位置、回歸雙線零變動 | **5 / 9 項 ✅**；**Phase2-2～2-4 🔄 已在 `feature/post-rehearsal` 實作，待演練後部署**；Phase5-1 ⏸ | Phase2-2 與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10 同批（已依序先搬家再改名）。Phase5-1 ⏸ 等 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)（已在分支完成、未進 main）與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)（未動工）涉及 `core/pipeline/` 的步驟完成；長期不動工時由使用者裁示是否先搬 |
| P3 | 🔄 | [滑價模型後續優化.md](滑價模型後續優化.md) | 滑價口徑收斂時一併盤點、但刻意不做的五項強化：報表滑價統計、台股逐標的設定、tick 吃 bid/ask、市場衝擊、實盤成交回填校準 | 1 / 5 項 ✅（S1，2026-09-18）；S2～S5 ⬜ | S2：D2 裁示維持不開工。S3：相依 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 合併進 main。S4：相依校準資料。S5：[實盤委託價格類型設計.md](實盤委託價格類型設計.md) S2 已改用決策價算滑價，剩樣本門檻、單位換算、成交量加權與決策價／回測基準的時點差。**S2～S5 都會改變回測結果，須重產 baseline** |
| P3 | 🔄 | [暫緩工作彙整.md](暫緩工作彙整.md) | 集中各文件因外部條件暫緩的工作：FinMind 連網冒煙腳本、券商分點 NO_DATA 延遲重試、股期行情回補、漲跌停公告值、`ColumnMapping` 泛型化、期貨報表 refused bequest、`LiveReporter` 績效指標、曝險上限口徑統一、`core/pipeline/` finmind 盲捕、當沖三段式等 | 剩 12 項，全部 ⏸（2026-10-10 依使用者裁示移出已完成的 S5、S7 與改判不做的 S9，編號不重排） | S1～S3、S13 等 FinMind 帳號升級；S4 等股期策略立項（與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase6-3 同條件）；S6 等漲跌停邊界策略出現；S8、S10 等 [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) 恢復；S11 等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 通過且累積約 20 個交易日快照；S12 等口徑裁示；S14、S15 等實盤出現「先確認成交、再開倉」的段落 |
| P3 | ⬜ | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) | 研究庫 `tw_stock.db`、`tw_futures.db` 分階段遷到 PostgreSQL，併入與台股 tick 同一個 `alphaedge` 資料庫；實盤紀錄庫 `tw_trading.db` 維持 SQLite（2026-09-25 定案） | 0 / 16 項（Phase0-1～Phase5-3）。2026-10-10 重測改動面：繼承 `BaseDAO` 的 DAO 19 支（研究庫 18）、`connect_sqlite()` 17 處、57 個測試檔直連 SQLite | **前置**：`feature/post-rehearsal` 部署後再合併 `feature/tick-timescaledb`——本計畫沿用其 `postgres` service、`psycopg`、`core/dao/timescale.py`（Phase0-1、Phase0-3、Phase1-1 已據此縮減）。[重構後全專案健檢.md](重構後全專案健檢.md) Phase3-6 已完成、不再是前置。正式遷移（Phase3-2）與切換 backend（Phase5-1）等使用者下指令 |
| P3 | ⏸ | [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) | 美股平行模組擴充：`us/` 資料層、provider 抽象、美股回測 model | 1 / 9 項 ✅（Phase3-3，2026-09-02）；其餘 8 項 ⏸（2026-10-01 使用者裁示整份暫緩，恢復時從 Phase1-1 開始） | 無前置相依。2026-10-10 依現況校正落點：spec 與成本放 `core/market/us/`、策略放頂層 `strategies/`、`yfinance` 已移到 `lab` extra；恢復時先決定美股表寫 SQLite 還是直接寫 PostgreSQL（見 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)） |
| P3 | ⬜ | [台股新聞情緒溫度計篩選工具.md](台股新聞情緒溫度計篩選工具.md) | 每日爬取台股財經新聞，萃取個股利多／利空情緒並產生可篩選的溫度計指標（MVP 只做規則版） | 0 / 7 項（S1～S6 ⬜、S7 ⏸） | 無外部相依；S1 來源授權檢查擋住其餘所有步驟。2026-10-10 複查引用的介面皆仍成立；入口合併後為 `apps/update_db.py` |

---

## 維護規則

1. 每次**新增**待辦 `.md`，同步在上表新增一列，並填寫優先級、狀態、說明與相依。
2. 每次**實作**推進（子任務完成、狀態欄變更），同步更新該列的「狀態」與「進度」。
3. 項目完成並整份移出 `backlog/` 時，刪除本表對應那一列（移出方式見 [`manage-backlog` skill §5](../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理)）。
4. 優先級為當下研判結果，可隨需求調整，但調整後必須讓本表與各文件內的優先序敘述一致。
5. **跨文件的相依寫在「相依」欄**：包含前置條件、與哪些步驟建議同批施作、以及該判斷的日期與理由。
6. 本檔只放**通則**（狀態圖例、優先級、待辦清單、維護規則）；各文件內部的施作紀錄與中斷點寫在該文件自身，不要回流到本檔。
