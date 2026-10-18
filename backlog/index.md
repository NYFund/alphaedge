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


> **分支鏈現況（2026-10-18）**：`feature/post-rehearsal` → `feature/tick-timescaledb` → `feature/tick-backtest` → `feature/framework-symmetry` 四條依序疊在一起，都還沒進 main，2026-10-18 裁示：[實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 演練**兩線 1b 判定後**（期貨線預估 2026-10-22、股票線 2026-10-26）依序合併，不等全部驗收；`feature/stock-rehearsal`（同文件 Phase7-27 演練策略）2026-10-21 期貨盤後段結束後先進 main。**合併分支鏈時要處理 `feature/stock-rehearsal` 帶進來的三處衝突**：演練策略要搬到頂層 `strategies/` 並把 `live_schedule` 改名 `execution_schedule`、`rehearsal_schedule.py` 的 `_STOCK_STRATEGIES` 兩邊不同、合併後在主目錄 `--install` 重裝 launchd。下表「在分支」指程式已寫好、等這次合併部署。

| 優先級 | 狀態 | 檔案 | 說明 | 進度 | 相依 |
|:------:|:----:|------|------|------|------|
| P1 | 🔄 | [重構後全專案健檢.md](重構後全專案健檢.md) | 2026-09-25 對重構後全專案的掃描，處理四類看不見的問題：有定義、無執行端；假綠燈；重複實作已分岔；照著文件做會出錯。共 52 步 | **45 / 52 ✅**。🔄 6：Phase1-1、1-5、1-7、2-8、3-2、3-7 在分支（`feature/post-rehearsal` 起），等合併部署。⬜ 1：Phase7-1 拆 `core/live/trader.py`（分支上 2,746 行），2026-10-18 已依分支程式碼重新盤點 11 段落點；開工前要先裁示搬法（mixin 或協作類別）與〈盤後〉區塊落點 | 6 個 🔄 隨分支鏈合併部署。Phase7-1 等分支鏈全部 merge，且 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase6-6 換月實跑（2026-10-20）通過後才動；它是 [框架對稱化重構.md](框架對稱化重構.md) Phase1-6～1-8 與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-16／7-21／7-23 的前置 |
| P2 | 🔄 | [實盤下單架構規劃.md](實盤下單架構規劃.md) | 以 Shioaji 實作實盤下單：券商閘道 `core/broker/`＋實盤引擎 `core/live/`（OMS、風控與 `TradingMode`、對帳、多策略歸屬與資金分配、盤中迴圈、執行層、告警與存活監控、parity）＋ `tw_trading.db`；同一支策略不改寫即可從回測切到模擬／正式環境。股票線與期貨線各自獨立宣告通過 | **49 / 73 ✅**（🔄 18、⬜ 4、⏸ 2；文件開頭有〈現況一覽〉分組表）。**演練**：期貨線 Phase6-6 換月 10/20 尾盤段、10/21 平新月，10/22 判定 1b；股票線 Phase7-27 演練策略 10/21 盤後部署、10/26 判定 1b；驗收 2（盤中）待分支鏈部署、驗收 5（推播）待設定 Discord。**在分支**：Phase7-2S 草稿、7-7、7-9、7-10、7-11、7-23 前兩類、7-25、7-26 第 3、4 項（`feature/post-rehearsal`），7-24、7-4 文件（`feature/framework-symmetry`）。**未開工**：Phase7-17＋7-26 第 1、2 項（設計與動工細節 2026-10-18 皆已定案，10/21 後從 `feature/framework-symmetry` 分出動手）；Phase7-16、7-21、7-23 第 3 類等拆 `trader.py`；Phase7-2F 上線。⏸ Phase6-3 股期、Phase6-4 零股 | 分支鏈於**兩線 1b 判定後部署**（2026-10-18 裁示，不等 Phase7-1 全部驗收）。Phase7-16、7-21 相依 [重構後全專案健檢.md](重構後全專案健檢.md) Phase7-1；[框架對稱化重構.md](框架對稱化重構.md) Phase2-2 相依本文件 Phase7-17；與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase2-2～2-4、[回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) Phase1-8、[盤中動能策略.md](盤中動能策略.md) S3 同批部署；Phase6-3 與 [暫緩工作彙整.md](暫緩工作彙整.md) S4 同條件；`tw_trading.db` 維持 SQLite，不隨 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 遷移 |
| P2 | 🔄 | [盤中動能策略.md](盤中動能策略.md) | `IntradayMomentumStrategy`：盤中漲幅 ≥ 9% 且累計量 ≥ 5,000 張即買（`MARKET`），進場當天跌回 8% 以下停損（現股當沖），隔天開盤出場；日 K 近似回測後以方案 B 上模擬環境 | **2 / 4**。S1 ✅；S2 ✅ 日 K 回測，一年對照日 K +218%、逐筆約持平，日 K 量級不可採信；**S3 🔄 在分支**（`feature/post-rehearsal` 起），等合併部署並重裝 launchd；S4 ⬜ 模擬實跑，以逐筆回測為主要對照，另驗開盤集合競價出場與保護價＋IOC 作廢比例 | S3 與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10、Phase7-25（盤中迴圈缺口，已在同分支修正）同批部署；S4 的結果決定 [策略與執行分層.md](策略與執行分層.md) S4 是否解除暫緩，並回填 [docs/live/execution-layer.md](../docs/live/execution-layer.md) |
| P2 | 🔄 | [回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) | 把兼任回測與實盤的 `run.py` 拆成 `python -m apps.backtest`／`apps.live`（`run.py` 已於 2026-10-01 刪除）；回測期間與資金可由參數覆寫；市場規則與 `FillConfig` 移出 `core/backtest/` 並加分層檢查；`tasks/` 併入 `apps/`；多策略組合回測的去留評估 | **14 / 16 ✅**（皆在 main）。**Phase1-8 🔄 `tasks/` 併入 `apps/` 在分支**，文件已列部署清單：合併後立刻在主目錄 `--install` 重裝 launchd（已安裝的 plist 仍是 `-m tasks.update_db`），隔天 update-db 跑通即 ✅。Phase4-1 ⏸ | Phase1-8 與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase2-2～2-4、[實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10 同一次部署。Phase4-1 等部署後股票線兩支策略的多日 parity 統計；2026-10-18 裁示放寬為「累積多日、任一段落出現 `CROSS_STRATEGY_BLOCKED` 或 `CAPITAL_EXHAUSTED`」 |
| P2 | 🔄 | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) | 台股 tick 由 DolphinDB 改落地 TimescaleDB：`stock_tick` hypertable、`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json`、`StockTickAPI` 改用 ConnectorX（介面不變）、歷史 CSV 全量匯入、移除台股與期貨 tick 的 DolphinDB 程式 | **18 / 18 步完成，在 `feature/tick-timescaledb`**；歷史 1,050,896,400 列已入庫（12 GB，全市場單日查詢 0.7～1.5 秒）。剩文件〈合併部署收尾〉6 項（主目錄 `uv sync` 三個 extra、確認 TimescaleDB、刪除 `tick_metadata.json` 與期貨 tick CSV、完整檢查等），做完整份移出 | 排在 `feature/post-rehearsal` 之後合併。`--target tick` 對正式表第一次執行仍待使用者下指令（不擋移出）。歷史 CSV 已刪，備份 dump 由使用者另存雲端 |
| P2 | 🔄 | [台股tick級回測框架.md](台股tick級回測框架.md) | 把「整天的 tick 當一根 K 棒」的 `Scale.TICK` 回測改成**逐筆重放**：策略以 `supported_scales` 宣告支援級別、`--scale` 覆寫；一次一筆、與實盤 `is_tick_triggered` 同契約；下一筆 tick 才成交、市價吃 bid/ask；`--tick-universe {strategy,all}`；`live_schedule` 改名 `execution_schedule` 供回測與實盤共用 | **16 / 16 完成，在 `feature/tick-backtest`**。`Backtester` 只留逐日迴圈，日 K／逐筆交給 `BarSimulator`／`TickSimulator`；一年實跑日 K 近似 +218%、逐筆約持平。剩文件〈合併部署收尾〉3 項（含 `feature/stock-rehearsal` 演練策略的 `live_schedule` 改名），做完整份移出 | 排在 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 之後合併；已吸收 [滑價模型後續優化.md](滑價模型後續優化.md) S3；實跑結果已回饋 [盤中動能策略.md](盤中動能策略.md) S2／S4 |
| P2 | 🔄 | [框架對稱化重構.md](框架對稱化重構.md) | 2026-10-10 全專案盤點 20 組「功能相同、結構不同」的成對框架（台股↔期貨、回測↔實盤、ETL 四件組、API／DAO），目標是外框與介面一致、只有本質差異各自實作；修掉 3 處已造成實盤與回測行為不一致的漂移 | **30 / 36 完成，在 `feature/framework-symmetry`**（會改變回測結果的步驟清單見文件〈剩餘步驟的施作順序〉，進 main 時對照）。剩 6 項：Phase1-6～1-8（共用委託前處理、實盤引擎改名、平倉鉤子報價範圍；Phase1-6 平倉階段要不要補值待確認）、Phase2-2（計價單位 `get_unit_size()`）、Phase2-6 改名（基底已完成）、Phase5-7（loader 兩種契約） | Phase1-6～1-8 相依 [重構後全專案健檢.md](重構後全專案健檢.md) Phase7-1；Phase2-2 相依 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-17 落地；Phase2-6 改名等前三條分支進 main；Phase5-7 與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) Phase2-2 同段；實盤滑價口徑併入 [滑價模型後續優化.md](滑價模型後續優化.md) S5 |
| P3 | 🔄 | [策略與執行分層.md](策略與執行分層.md) | 實盤「怎麼送單」原本沒有專屬的一層。2026-10-06 裁示採折衷方案 B：策略宣告 `live_execution`，由執行層集中換算（現行設計見 [docs/live/execution-layer.md](../docs/live/execution-layer.md)）；完整分層與回測依執行方式成交暫緩 | **3 / 5 ✅**（S1～S3 在 main，2026-10-18 複查送單落點仍 38 處）。S4、S5 ⏸；S5 的 TICK 級部分已由 [台股tick級回測框架.md](台股tick級回測框架.md) Phase3-2 做到（在分支），2026-10-18 裁示 S5 縮成只做日 K | S4 解除條件：[盤中動能策略.md](盤中動能策略.md) S4 完成；S5 相依 S4，會改變回測結果，須重產 baseline |
| P3 | 🔄 | [core目錄邊界收斂.md](core目錄邊界收斂.md) | 讓 `core/` 只剩交易框架：切斷對 `core.pipeline` 的依賴並加檢查；具體策略與 `StrategyLoader` 搬到頂層 `strategies/`；`core/managers/` 改名 `core/position/`；`Dockerfile` 移到根目錄；`core/pipeline/` 搬到頂層改名 `etl/`。全程只搬位置、回歸零變動 | **5 / 9 ✅**。**Phase2-2～2-4 🔄 在分支**，合併時要多搬 `feature/stock-rehearsal` 的演練策略。Phase5-1 ⏸（`core/pipeline/` 2026-10-18 在 main 為 22,966 行，佔 `core/` 34%） | Phase2-2 與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-10 同批。Phase5-1 等 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md)、[框架對稱化重構.md](框架對稱化重構.md) Phase5（皆在分支）進 main，以及 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 動到 `core/pipeline/` 的步驟完成；長期不動工時由使用者裁示是否先搬 |
| P3 | 🔄 | [滑價模型後續優化.md](滑價模型後續優化.md) | 滑價口徑收斂時一併盤點、但刻意不做的五項強化：報表滑價統計、台股逐標的設定、tick 吃 bid/ask、市場衝擊、實盤成交回填校準 | **2 / 5 ✅**（S1；S3 由 [台股tick級回測框架.md](台股tick級回測框架.md) Phase3-2 在分支完成）。S2 ⬜ 只做日 K 路徑，價差研究等 tick 分支進 main 後做、再重新裁示 D2；S4 ⬜ D3 已裁示只做日 K，剩校準資料；S5 ⬜ 決策價基準已上線，樣本只有模擬環境 5 筆成交 | S2 研究需 [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 合併後在主目錄跑；S5 要正式環境成交，等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 之後。**S2～S5 都會改變回測結果，須重產 baseline** |
| P3 | 🔄 | [暫緩工作彙整.md](暫緩工作彙整.md) | 集中各文件因外部條件暫緩的工作：FinMind 連網冒煙腳本、券商分點 NO_DATA 延遲重試、股期行情回補、漲跌停公告值、`ColumnMapping` 泛型化、`LiveReporter` 績效指標、曝險上限口徑統一、`core/pipeline/` 盲捕、當沖三段式等 | 12 項：11 ⏸、S10 ✅（期貨報表反向繼承已由 [框架對稱化重構.md](框架對稱化重構.md) Phase3-2 在分支做掉、零 baseline 變動，分支進 main 後移出）。2026-10-18 複查其餘外部條件皆未成立 | S1～S3、S13 等 FinMind 帳號升級；S4 等股期策略立項（與 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase6-3 同條件）；S6 等漲跌停邊界策略出現；S8 等 [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) 恢復；S11 等 [實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 通過且累積約 20 個交易日正式帳戶快照；S12 等口徑裁示；S14、S15 等實盤出現「先確認成交、再開倉」的段落 |
| P3 | ⬜ | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) | 研究庫 `tw_stock.db`、`tw_futures.db` 分階段遷到 PostgreSQL，併入與台股 tick 同一個 `alphaedge` 資料庫；實盤紀錄庫 `tw_trading.db` 維持 SQLite（2026-09-25 定案） | **0 / 16**。2026-10-18 以 `feature/framework-symmetry` 重測改動面：研究庫 DAO 19 支、`connect_sqlite()` 14 處（DataFeed 已收成 `connect_db()` 一處，另新增 watchdog 讀研究庫一處）、67 個測試檔直連 SQLite；`ON CONFLICT` 衝突欄位做法待動工時擇一 | 分支鏈四條都進 main 後才動工（沿用 `feature/tick-timescaledb` 的 `postgres` service、`psycopg`、`core/dao/timescale.py`）；Phase2-2 與 [框架對稱化重構.md](框架對稱化重構.md) Phase5-7 同段。正式遷移（Phase3-2）與切換 backend（Phase5-1）等使用者下指令 |
| P3 | ⬜ | [tests目錄結構重整.md](tests目錄結構重整.md) | 把平鋪在 `tests/` 根目錄的測試改成單層鏡像原始碼目錄（`api/`、`dao/`、`pipeline/{cleaner,crawler,updater,loader,shared}/`、`models/`、`strategies/`…），另立 `parity/`、`live/sim/`、`scripts/`、`meta/`；純 `git mv` 與改路徑引用，不改測試內容。共 8 步 | **0 / 8**。2026-10-18 以 `feature/framework-symmetry` 實查：230 支測試檔、根目錄平鋪 127 支（main 104），歸類表與外部引用已依現況重做（CI 有一處以路徑直接執行測試，要同批改）；說明改為更新既有的 `docs/dev/testing.md` | 一百多支 `git mv` 會讓未合併分支碰到 rename 衝突，**等分支鏈四條與 `feature/stock-rehearsal` 都進 main 後**才開工。`tests/pipeline/` 改名 `tests/etl/` 隨 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1 做，本份不做 |
| P3 | ⏸ | [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) | 美股平行模組擴充：`us/` 資料層、provider 抽象、美股回測 model | **1 / 9 ✅**（Phase3-3）；其餘 8 項 ⏸（2026-10-01 使用者裁示整份暫緩）。2026-10-18 已依分支上的基底鉤子（日曆、DataFeed、Fill／Settlement、`assembly.py`、`TARGETS`）改寫做法 | 使用者指示恢復；建議等分支鏈合併後再開工，並先決定美股表寫 SQLite 還是直接寫 PostgreSQL（見 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)） |
| P3 | ⬜ | [台股新聞情緒溫度計篩選工具.md](台股新聞情緒溫度計篩選工具.md) | 每日爬取台股財經新聞，萃取個股利多／利空情緒並產生可篩選的溫度計指標（MVP 只做規則版） | **0 / 7**（S1～S6 ⬜、S7 ⏸）。2026-10-18 已依分支上的 `TARGETS` 註冊表、updater 生命週期、DAO 宣告改寫 S2、S5 | 無外部相依；S1 來源授權檢查擋住其餘所有步驟 |

---

## 維護規則

1. 每次**新增**待辦 `.md`，同步在上表新增一列，並填寫優先級、狀態、說明與相依。
2. 每次**實作**推進（子任務完成、狀態欄變更），同步更新該列的「狀態」與「進度」。
3. 項目完成並整份移出 `backlog/` 時，刪除本表對應那一列（移出方式見 [`manage-backlog` skill §5](../.claude/skills/manage-backlog/SKILL.md#5-完成後的處理)）。
4. 優先級為當下研判結果，可隨需求調整，但調整後必須讓本表與各文件內的優先序敘述一致。
5. **跨文件的相依寫在「相依」欄**：包含前置條件、與哪些步驟建議同批施作、以及該判斷的日期與理由。
6. 本檔只放**通則**（狀態圖例、優先級、待辦清單、維護規則）；各文件內部的施作紀錄與中斷點寫在該文件自身，不要回流到本檔。
