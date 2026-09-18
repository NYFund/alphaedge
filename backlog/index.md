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
| P2 | ⬜ | [回測滑價口徑收斂.md](回測滑價口徑收斂.md) | 全專案滑價路徑盤點後的六項缺口：期貨強制出場（追繳平倉、到期兜底出場）與換月轉倉兩腿未經 `FillModel`；期貨跳動點固定 1 點且一次回測只有一個 spec；做多開倉餘額不足時靜默丟棄（無 log、無事件計數）；`clamp_filled_price()` 就地修改傳入訂單；強制出場的滑價後價格不做區間檢查也不計數；期貨策略誤用基底 `FillConfig` 會靜默退成基點模式。另附五項後續優化候選（逐標的滑價、tick 用 bid/ask、市場衝擊、報表滑價統計、以實盤成交校準） | 1 / 10 項 ✅（S1）；S2~S10 ⬜ | **無前置相依，可直接開工**。D1~D4 已於 2026-09-17 全數依建議定案；複查後新增的 D5（強制出場超區間只警告不夾回）、D6（期貨 `fill_config` 型別不符拋錯）待裁示（到期出場套滑價、轉倉兩腿都套、sizer 公式不動只補可見度、計數命名 `rejected_insufficient_balance`）。期貨兩項由已結案移出的健檢第五輪收斂 Phase1-8 第 2 項分出（見 git 歷史 `148cae9`），該項只做完台股半邊。**回歸雙線零變動**（雙線皆台股，且現行唯一期貨策略未設 `fill_config`；另兩項只加可見度與改複製方式）。2026-09-17 立項 |
| P2 | ⬜ | [回測績效指標統一由報表輸出.md](回測績效指標統一由報表輸出.md) | reporter 新增整體績效指標 CSV（公式一律走 `risk_metrics.py`），補齊年化波動度、Profit Factor、勝敗比、Information Ratio；前端改讀 CSV 不再自算，MDD 收斂為單一實作 | 0 / 5 項 | **無前置相依**，不改記帳、回歸基準零變動。S3 期貨 IR 口徑、S4 舊結果 fallback 需使用者裁示。前端容器的 import 問題已於 2026-09-17 修好（`PYTHONPATH=/app`），本項不再與它相關。2026-09-15 由 `StockBacktestAnalyzer` 刪除後的盤點立項 |
| P2 | ⬜ | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) | 台股 tick 落地由 DolphinDB 改為 TimescaleDB：`stock_tick` hypertable（7 天 chunk、依 `stock_id` 壓縮）、`COPY` 以「股票 × 交易日」冪等寫入、`stock_tick_load_log` 取代 `tick_metadata.json`、`StockTickAPI` 改用 ConnectorX 讀取（介面不變）、Google 雲端歷史 CSV 全量匯入、移除台股 tick 的 DolphinDB 程式 | 0 / 16 項（Phase0-1~Phase5-3） | **無前置相依**，Phase4-1（盤點雲端 CSV）可最先做。與 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 共用 `postgres` service、driver 與 `core/dao/` 連線入口，不互為前置，先做的建立、後做的沿用；環境變數刻意用 `TICK_DATABASE_URL` 而非 `DATABASE_URL`，避免日頻資料提前被切走。Phase5-2 的期貨 tick DolphinDB 程式去留需使用者裁示。2026-09-16 立項 |
| P2 | ⬜ | [docs已載明但未實作的缺口盤點.md](docs已載明但未實作的缺口盤點.md) | `docs/` 17 份文件的「已知限制／已知簡化／未實作」逐條對照程式碼與本索引後，找出既沒被 backlog 追蹤、也沒被裁示不做的十項：股期回測開倉的乘數與保證金阻斷、前端已棄用的 `use_container_width`、`ruff` ignore 清單已歸零的規則、期貨對標序列改讀連續合約、2017-05-15 前不再查夜盤、台股報表識別欄改 `Symbol`、平盤下放空與可當沖清單、漲跌停公告值、財報與月營收「申報期內部分申報」的判準 | 0 / 10 項（S1~S7 ⬜、S8~S9 ⏸、S10 ⬜） | **無前置相依，S1 建議先做**——`FuturesPositionManager.get_multiplier()` 直接查 `FUTURES_MULTIPLIER`，股期第一筆開倉就 `KeyError`，是本批唯一「程式當場中斷」級的缺口（DataFeed 的 `resolve_multiplier()` 已做對，只有部位管理層沒接上）。S2 相依 S1；S3 在 Streamlit 移除該參數後前端會直接壞掉；S7 會讓回歸 baseline 失效，須與其他要重產 baseline 的工作合併成同一批；S8、S9 共用同一批交易所公告資料源，卡來源盤點。附錄 A 列出已由既有 backlog 追蹤的條目、附錄 B 列出裁示不立項者與理由。2026-09-17 立項 |
| P3 | ⬜ | [證券代號前導0的殘餘防線.md](證券代號前導0的殘餘防線.md) | 證券代號被推斷成整數（`0050` → `50`）的最後一塊：入庫前檢查「同一批一個代號只能對到一個名稱」以涵蓋 `margin`（其主鍵不含證券名稱，冒名列會被 `INSERT OR IGNORE` 吞掉、事後查不出來）、`margin` 歷史資料的跨表稽核、以及 `margin` 主鍵是否納入證券名稱的判斷 | 0 / 3 項（S1~S3） | **無前置相依**。寫入端防護與 `price`／`chip` 的事後護欄已於 2026-09-17 完成（修 523 列、新增 `test_no_symbol_carries_two_names_on_the_same_day`），**`margin` 現況抽查為乾淨**，故列 P3；S1 成本低且是唯一涵蓋 `margin` 的防線，建議先做。S2 的跨表比對要先解名稱噪音（實測正規化後仍有 3,508 筆，多為兩來源寫法不同與改名生效日不同步）。S3 只做判斷，schema 變更歸 [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)。2026-09-17 立項 |
| P3 | ⬜ | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) | 由 SQLite3（`tw_stock.db`、`tw_futures.db`）遷移到 PostgreSQL 的分階段實作計畫 | 0 / 16 項（Phase0-1~Phase5-3） | **DAO 資料存取層已完成**（2026-09-16，設計見 [資料存取層](../docs/dev/data-access-layer.md)），**2026-09-16 已依實況重新盤點改動面**：SQLite 專屬語法全在 `core/dao/`（`sqlite_master` 1、`INSERT OR` 4、`SAVEPOINT` 1、`GLOB` 1 處），非測試 `import sqlite3` 只剩 DAO 內 5 檔，另有 21 個 `connect_sqlite()` 連線取得點與 40 個直接連 SQLite 的測試檔；Phase1-2、Phase2-1~Phase2-3 縮減為改寫 `core/dao/` 內部，連線入口沿用 `core/dao/connection.py`。影響面廣，建議在其他重構收斂後再動。命名軸線交接的兩項 schema 收斂（台股表名補 `stock_` 前綴、`stock_id` → `symbol`）排在本批，欄位 Enum 下沉已完成 |
| P3 | ⏸ | [暫緩工作彙整.md](暫緩工作彙整.md) | 集中各文件因外部條件暫緩的工作：FinMind 連網冒煙腳本、券商分點 metadata 的 `last_attempted_date` 與 NO_DATA 延遲重試、股票期貨行情前 N 檔回補 | 0 / 4 項（S1~S4 ⏸） | **全部暫緩，無可開工項**。S1~S3 等 FinMind 帳號升級（目前 `register`，無券商分點權限，2026-09-16 以真實 API 確認）；S4 等開始開發股期策略，**解除時不可直接跑 `--target futures_stock_price`**，須先暖身（見該文件 S4）。2026-09-17 由 `DAO重構後續收斂.md`、`券商分點NO_DATA的metadata語意.md`、`爬蟲缺口回補與非交易日批次清理.md` 的暫緩項集中而成，三份原文件已刪除 |
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
