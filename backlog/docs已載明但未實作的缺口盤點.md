# docs 已載明但未實作的缺口盤點

## Abstract

- **背景／問題**：`docs/` 底下 17 份文件各自帶著「已知限制」「已知簡化」「未實作」「目前未處理」的條目。這些條目寫下來是對的——它們讓讀者知道邊界在哪；但**寫進 `docs/` 不等於有人會做**。2026-09-17 逐條對照 `backlog/index.md` 與程式碼後，確認有一批條目既沒有被任何 backlog 文件追蹤，也沒有被裁示不做，等於「被丟進 `docs/` 就沒有下文」。其中 `FuturesPositionManager` 的股期乘數是**會讓回測當場中斷**的程式缺口，只寫在 `README.md` 的支援範圍表裡。
- **目標**：把這批條目收成可獨立施作的步驟，每一條都有歸屬——本文件的步驟、既有 backlog 文件，或明確裁示不做並寫明理由。做完之後，`docs/` 的每一條已知限制都能追到一個去處。
- **範圍界線**：
  - **不收已被既有 backlog 追蹤的條目**（對照表見附錄 A），避免同一件事在兩份文件各記一半。
  - **不收已裁示不做的 tick 工作**（台股 tick 不回補、期貨 tick 不做，2026-09-14／09-15 使用者裁示）。
  - **不重寫回測引擎**：事件驅動迴圈、per-instrument 粒度的 model 掛載屬引擎典範轉移，解除條件已寫在 [多市場回測引擎架構 §5.1](../docs/backtest/multi-market-engine.md#51-事件驅動迴圈長期方向)，本文件不碰。
  - **不改回測記帳口徑**：S1、S2 補的是「開不了倉」，不是改已能開倉那條路徑的算法；回歸雙線必須零變動。
- **驗收標準**：S1~S10 全部標 ✅ 或改判不做，且附錄 A、B 的歸屬仍成立時，本文件移出 `backlog/`。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 期貨部位管理層的乘數改由 resolver 決定（股期開倉不再 KeyError） | `core/managers/futures/position_manager.py`、`core/backtest/factory.py`、`tests/backtest/test_futures_backtest.py` | 新增一條股期開倉測試；回歸雙線零變動 | ⬜ | 唯一的「程式當場中斷」級缺口。相依：無 |
| S2 | 股期保證金：查表模式接上比例表 | `core/managers/futures/position_manager.py`、`core/api/tw/futures_margin_api.py`、對應測試 | 股期以 `stock_futures_margin_rate_history` 算得出每口保證金；指數期貨路徑不變 | ⬜ | 相依 S1（S1 不做的話走不到這一步） |
| S3 | 前端改用 `width="stretch"` 取代已棄用的 `use_container_width` | `frontend/app.py` | 前端啟動無棄用警告；表格與圖表版面不變 | ⬜ | 8 處。Streamlit 移除該參數後前端會直接壞掉。相依：無 |
| S4 | `ruff` ignore 清單校正：移除已歸零的規則 | `pyproject.toml`、`docs/dev/code-quality.md` | `ruff check .` 全綠；移除的規則以 `--select` 現查確認為 0 | ⬜ | 2026-09-17 實查：`B008`／`F841`／`F811` 已歸零，`B007` 剩 1。相依：無 |
| S5 | 期貨報表對標序列改讀 `futures_continuous` | `core/backtest/report/futures_reporter.py` | 換月接點不再有展期假跳空；`futures_continuous` 缺該商品時退回近月拼接並記 warning | ⬜ | 相依：無（`futures_continuous` 已有 2015-01-05~ 的資料） |
| S6 | 2017-05-15 之前不再查詢夜盤 | `core/pipeline/tw/updaters/futures_price_updater.py` | 回補該區間的請求數減約一成，`No valid futures price rows` warning 消失；表內列數不變 | ⬜ | 純請求量與雜訊優化，資料本來就是對的。相依：無 |
| S7 | 台股報表識別欄 `Stock ID` → `Symbol` | `core/backtest/report/reporter.py`、`frontend/services/`、`tests/backtest/snapshots/` | 前端仍讀得到；LONG／SHORT baseline 重產一次 | ⬜ | **會讓 baseline 失效**，必須與其他要重產 baseline 的工作合併成同一批做。相依：無，但須排程 |
| S8 | 平盤下放空限制與每日可當沖清單：資料源 ＋ 撮合呼叫端 | 新增 ETL（處置／警示股、每日可當沖清單）、`core/backtest/models/fill_model.py`、`tests/backtest/test_unimplemented_constraints.py` | 設了 `allow_below_reference=False` 會真的擋單並計數；建構期警告移除 | ⏸ | 卡資料源盤點。相依：先確認來源端點（見 S8 章節） |
| S9 | 漲跌停改用公告值 | `core/backtest/datafeed/tw/stock_datafeed.py`（`get_price_limit_basis()`）、`core/backtest/models/instrument_spec.py` | 公告值與公式值的差異筆數統計；缺公告值時退回公式版 | ⏸ | 掛點已存在且已被 `Backtester` 呼叫，缺的只有公告值資料源。相依：同 S8 的來源盤點 |
| S10 | 申報期內「部分申報」的完整性判準 | `core/pipeline/tw/updaters/financial_statement_updater.py`、`monthly_revenue_report_updater.py` | 申報期內的部分結果不再與「來源真的只有這麼多」混為一談 | ⬜ | [ETL 入庫約定 §3.5](../docs/pipeline/etl-ingestion.md) 明寫「目前未處理」。相依：無 |

---

## S1. 期貨部位管理層的乘數改由 resolver 決定 ⬜

- **目的**：讓股票期貨（含 ETF 期貨）回測**開得了第一筆倉**。目前 `--strategy` 一旦交易股期，開倉當下就 `KeyError`，整場回測中斷。

- **現況**（2026-09-17 實查）：
  - `core/backtest/datafeed/tw/futures_datafeed.py` 的 `resolve_multiplier(product, date)` **已經做對了**：指數期貨查 `FUTURES_MULTIPLIER` 常數，股期逐日查 `futures_stock_universe` 的契約單位（會隨除權息被交易所調整，寫死必錯）。`build_multiplier_resolver(date)` 把它綁定日期後交給 adapter，`FuturesQuote.multiplier` 因此是對的。
  - 但 `core/managers/futures/position_manager.py` 的 `get_multiplier()` 是 `@staticmethod`，內容只有 `return FUTURES_MULTIPLIER[product]`。`open_position()` 拿到 order 之後**重新查一次表**，股期不在常數表內，當場 `KeyError`。
  - 換句話說：**DataFeed 這半邊已經接上，部位管理這半邊沒有**。

- **做法**：
  1. `FuturesPositionManager.__init__()` 增加 `multiplier_resolver: Optional[Callable[[str, datetime.date], int]] = None`；未注入時維持現行的常數查表（保留給不經 DataFeed 的單元測試）。
  2. `get_multiplier()` 由 `@staticmethod` 改為實例方法，簽章帶日期：`get_multiplier(self, product: str, date: datetime.date) -> int`。有 resolver 就走 resolver，否則走 `FUTURES_MULTIPLIER[product]`。**查不到一律 raise，不退回近似值**——沿用現行註解的理由，乘數猜錯只會讓 PnL 靜默偏掉。
  3. `core/backtest/factory.py` 的 `build_tw_futures_backtester()` 在建好 `TwFuturesDataFeed` 之後，把 `data_feed.resolve_multiplier` 注入 `FuturesPositionManager`。注意**建構順序**：現行 `position_manager` 建立在 `data_feed` 之前，需調整順序或改以 setter 注入。
  4. 平倉路徑已經用 `position.multiplier`（開倉當下存下來的值），不需要改——這也是對的：同一個部位的乘數不該因為後來除權息調整而改變。

- **產出**：`core/managers/futures/position_manager.py`、`core/backtest/factory.py`、`tests/backtest/test_futures_backtest.py`。

- **驗證方式**：
  1. 新增一條測試：以假的標的池快照讓 resolver 回傳 2,000 股，對股期商品開倉、平倉，確認保證金與 PnL 依該乘數計算。
  2. `./scripts/run_regression.sh` **逐筆相同**——現行唯一的期貨策略 `MomentumFuturesStrategy` 交易 TX，走的是常數那條路徑，不應有任何變動。

- **相依**：無。實際跑股期回測還需要行情（[暫緩工作彙整.md](暫緩工作彙整.md) S4），但**本步驟不等它**：程式缺口該先補起來，否則股期行情回補完才發現開不了倉。

---

## S2. 股期保證金：查表模式接上比例表 ⬜

- **目的**：讓股期在**預設的查表模式**下算得出應繳保證金，不必被迫改用 `FuturesMarginConfig.ratio()`。

- **現況**：
  - 保證金分兩張表，**分表依據是「金額 vs 比例」而不是「指數 vs 股票」**：指數期貨與 ETF 期貨給每口固定金額（`futures_margin_history`），個股期貨給適用比例 ＋ 級距（`stock_futures_margin_rate_history`）。
  - `FuturesPositionManager.calculate_margin()` 只呼叫 `FuturesMarginAPI.get_initial_margin()`，那是**金額表**；個股期貨查不到就 raise `ValueError`，開倉中止。
  - `FuturesMarginAPI.calculate_stock_futures_margin()`（比例表那條路徑，公式為 `標的股價 × 契約單位 × 比例`）**已經實作且有測試，但全專案沒有任何呼叫端**——只有 `tests/test_api_public_interfaces.py` 在用。
  - ETF 期貨不受影響：`NYF` 等在金額表內（2020-07-22 起）。

- **做法**：
  1. `calculate_margin()` 先查金額表；回 `None` 時再走 `calculate_stock_futures_margin()`，兩者皆無才 raise。
  2. 比例表的公式需要**標的股價**，而 `tw_stock.db` 與 `tw_futures.db` 是兩個檔。確認 `FuturesMarginAPI` 取標的股價的路徑（跨庫唯讀連線或由 DataFeed 注入 `StockPriceAPI`），**不要在部位管理層自行開連線**——連線一律由 DataFeed 持有。
  3. 維持保證金（`calculate_maintenance_margin()`）同樣要處理：股期的維持比例也在同一張比例表。

- **產出**：`core/managers/futures/position_manager.py`、`core/api/tw/futures_margin_api.py`（若需補跨庫取價）、對應測試。

- **驗證方式**：以假表資料驗「個股期貨走比例、ETF 期貨走金額、兩者皆無則 raise」三條分支；回歸雙線零變動。

- **相依**：S1（乘數過不了就走不到保證金）。

---

## S3. 前端改用 `width="stretch"` ⬜

- **目的**：`frontend/app.py` 有 8 處使用 Streamlit 已棄用的 `use_container_width` 參數（`st.dataframe` 3 處、`st.plotly_chart` 4 處、`st.image` 1 處），執行時會印棄用警告。而 `frontend/requirements.txt` 只釘下限 `streamlit>=1.40`——**上游移除該參數的那天，重建的前端映像會在渲染表格與圖表時直接出錯**，而不是降級。

- **做法**：逐處改為 `width="stretch"`。改完確認本機與容器兩種啟動方式的版面沒變（容器的 `PYTHONPATH=/app` 與本機的 editable 安裝是兩條不同的 import 路徑）。順帶評估要不要在 `frontend/requirements.txt` 釘上限，避免下一個棄用參數用同樣的方式炸開。

- **產出**：`frontend/app.py`、必要時 `frontend/requirements.txt`。

- **驗證方式**：`streamlit run frontend/app.py` 無棄用警告；載入一份既有回測結果，表格、Plotly 圖與 PNG 三種元件的寬度表現與改動前相同。

- **相依**：無。

---

## S4. `ruff` ignore 清單校正 ⬜

- **目的**：`pyproject.toml` 的 ignore 清單第三類自稱「潛在缺陷，已逐點記錄，待後續收斂」，並明寫「**修掉之後要把對應那條從本清單移除**」。實際上有幾條已經歸零卻還留著——留著等於對那類問題永久失明。

- **現況**（2026-09-17 以 `ruff check . --select <規則> --statistics` 實查）：

  | 規則 | 清單註記的數量 | 實際 | 處置 |
  |------|:--------------:|:----:|------|
  | `B008` | 5 | **0** | 從 ignore 移除 |
  | `F841` | 3 | **0** | 從 ignore 移除 |
  | `F811` | 1 | **0** | 從 ignore 移除 |
  | `B007` | 2 | 1 | 修掉最後一處後移除 |
  | `B904` | 3 | 3 | 維持 |
  | `E722` | 3 | 3 | 維持 |
  | `B006` | 4 | 4 | 維持 |
  | `BLE001` | 85 | 70 | 維持，更新註記 |
  | `TRY300` | 19 | 20 | 維持，更新註記 |

- **做法**：移除已歸零的三條、修掉 `B007` 最後一處後一併移除，其餘更新行尾的數量註記。**不要順手打開 `UP` 家族**——那會一次改掉數百處違反 `CLAUDE.md` §2.4／§2.7 的型別註解與 Enum 寫法。

- **產出**：`pyproject.toml`、`docs/dev/code-quality.md`（第三類那段的敘述）。

- **驗證方式**：移除後 `ruff check .` 仍全綠；`pre-commit run --all-files` 通過。

- **相依**：無。

---

## S5. 期貨報表對標序列改讀 `futures_continuous` ⬜

- **目的**：`FuturesBacktestReporter` 的對標曲線是**近月拼接**（每個交易日取最近到期月的收盤價），換月當天有一段展期價差造成的假跳空，只能當粗略參考。而 `futures_continuous` 表已經存著三種調整方式 × 三種換月規則的連續合約（2015-01-05 起，43,095 列），正是為了這件事建的。

- **做法**：`build_near_month_close_series()` 改為優先讀 `futures_continuous`（`method=BACKWARD`、換月規則對齊策略的 `roll_config.rule`，讓對標與策略實際轉倉的時點一致）；查不到該商品或該區間時退回現行的近月拼接並記 warning。圖表註腳標明採用的是哪一種序列——兩種口徑的曲線不可混著看。

- **產出**：`core/backtest/report/futures_reporter.py`。

- **驗證方式**：同一次回測分別以兩種序列出圖，確認換月接點的跳空消失；`futures_continuous` 沒有該商品時仍能出圖。

- **相依**：無。

---

## S6. 2017-05-15 之前不再查詢夜盤 ⬜

- **目的**：台指期的盤後交易時段自 2017-05-15 之後才有，但 `FuturesPriceUpdater` 對每個日期一律迭代 `FuturesSession.data_sessions()`（日盤、夜盤各打一次）。回補 2015~2017 那段等於多打約一成的請求，並產生大量 `No valid futures price rows` warning——**資料是對的，雜訊是多的**，而雜訊會淹掉真正該看的那幾行。

- **做法**：在迭代時段之前，依商品的夜盤起始日跳過夜盤查詢。起始日各商品不同（TX／MTX 2017-05-16、TE 2018-11-20、ZEF 2021-06-29、TMF 2024-07-30、TF／ZFF 2025-06-24），需要一張常數表，落點比照 `FUTURES_PRODUCT_LISTING_DATES`。**這張表是觀測值不是制度公告**，註解要寫清楚，並附一條測試釘住「表內每個商品的夜盤最早日期 ≥ 常數」。

- **產出**：`core/config/settings.py`（或 `core/utils/constant.py`，依既有 `FUTURES_PRODUCT_LISTING_DATES` 的落點）、`core/pipeline/tw/updaters/futures_price_updater.py`、對應測試。

- **驗證方式**：以一段 2016 年的區間乾跑，確認請求數下降且 `futures_price_daily` 的列數與改動前相同。

- **相依**：無。

---

## S7. 台股報表識別欄 `Stock ID` → `Symbol` ⬜

- **目的**：領域模型的識別欄早已統一為 `symbol`，但台股報表輸出的欄名仍是 `Stock ID`（期貨是 `Contract ID`），兩種報表的識別欄名不一致，前端得依欄名分辨報表型別。

- **做法**：`reporter.py` 的欄位清單與 `record.symbol` 的對應改名；`frontend/services/` 讀欄名的地方同步；`futures_metrics.py` 以 `Contract ID` 判斷是不是期貨報表的邏輯要重新確認（`Stock ID` 消失後那個判準還成不成立）。

- **產出**：`core/backtest/report/reporter.py`、`frontend/services/metrics.py`／`futures_metrics.py`、`tests/backtest/snapshots/`。

- **驗證方式**：重產 LONG 與 SHORT baseline 一次；前端載入新舊兩份結果都不報錯（或明確決定不相容並在 `report_loader.py` 擋掉舊格式）。

- **相依**：無前置步驟，但**必須排程**：一旦重產 baseline，先前每一次「逐筆相同」的驗證都失去意義。要與其他同樣會改變回測輸出的工作合併成同一批，只重產一次。

---

## S8. 平盤下放空限制與每日可當沖清單 ⏸

> **⏸ 暫緩紀錄（2026-09-17）**
> - 暫緩原因：卡資料源。處置股／警示股公告與每日可當沖清單都還沒有 ETL。
> - 解除條件：完成下方的來源盤點，確認端點可爬且有足夠歷史。

- **目的**：`ShortConstraint.allow_below_reference` 與 `day_trade_whitelist` 兩個欄位**有定義、沒有撮合呼叫端**——設了不會生效。現行做法是 `StockCostModel.check_unimplemented_constraints()` 在建構時發出警告（至少不靜默），並以 `tests/backtest/test_unimplemented_constraints.py` 釘住這個狀態。後果是回測**高估可放空與可當沖的機會數**。

- **做法**：
  1. **先做來源盤點**（這一步才是阻塞點）：證交所的處置股／警示股公告、每日可當沖標的清單，確認端點格式、歷史涵蓋起點與是否需要逐日爬。歷史不足回測區間的話，本步驟要重新評估值不值得做。
  2. ETL 落地依 [ETL 入庫約定 §四](../docs/pipeline/etl-ingestion.md) 的新增 updater 檢查表，DAO 依 [資料存取層 §七](../docs/dev/data-access-layer.md)。
  3. 撮合端接在 `TwStockFillModel`，拒單計入新的 `event_counts` key（既有 key 不可更名，新增可以）。
  4. 接上之後**移除建構期警告**並改寫 `test_unimplemented_constraints.py`——那條測試的用途是釘住「尚未實作」，實作完就該反過來釘住「會擋單」。

- **產出**：新的 crawler／cleaner／loader／updater／DAO 一組、`core/backtest/models/fill_model.py`、`core/backtest/models/cost_model.py`、對應測試。

- **驗證方式**：SHORT 回歸線新增一組情境（平盤下放空被擋、非當沖清單標的被擋）；既有 12 組情境快照零變動。

- **相依**：來源盤點。與 S9 共用同一批公告資料源，建議同批施作。

---

## S9. 漲跌停改用公告值 ⏸

> **⏸ 暫緩紀錄（2026-09-17）**
> - 暫緩原因：同 S8，卡公告值資料源。
> - 解除條件：來源盤點完成；或出現真的依賴漲停判定的策略。

- **目的**：`TwStockSpec.get_price_limits()` 以「前收 ±幅度後往內對齊檔位」推算漲跌停，**與交易所公告值多數差一檔**。影響 `FillModel.validate()` 的邊界拒單與 `limit_up_cover_failed` 計數——也就是放空策略最致命的那個尾部風險的計數會偏。

- **做法**：掛點**已經存在且已被呼叫**（`BaseDataFeed.get_price_limit_basis()`，`Backtester` 於每根 bar 推入，除權息日已改用 `dividend` 表的開盤競價基準）。缺的只是公告值本身：把公告的漲跌停價接進同一個掛點，公式版退為 fallback。**不要另開一條路徑**——兩處各算一份漲跌停必然漂移。

- **產出**：`core/backtest/datafeed/tw/stock_datafeed.py`、`core/backtest/models/instrument_spec.py`、對應測試。

- **驗證方式**：抽一段區間統計公告值與公式值的差異筆數與方向；缺公告值的日期仍能回測。

- **相依**：同 S8 的來源盤點。

---

## S10. 申報期內「部分申報」的完整性判準 ⬜

- **目的**：[ETL 入庫約定 §3.5](../docs/pipeline/etl-ingestion.md) 的「多來源拼成一份資料」規則明寫一條例外：**申報期內的部分申報（財報、月營收在申報期間只拿得到已送件的公司）「不在這條規則內……目前未處理」**。現況是那段期間拿到的部分結果會被當成完整結果入庫，之後不再重問——與「來源當下就只有這麼多」無法區分。

- **做法**：
  1. 先確認現況到底會不會留下永久缺口：`fs` 與 `mrr` 的 resume 是年季／年月差集，已入庫的年季就不會再被請求。若申報期內跑過一次、之後沒有補救機制，那些季就**永久只有先送件的那批公司**。
  2. 若確認有缺口，做法比照 `equity_change` 的 `no_data` 寫入條件——**只有申報期已關閉（各行業最晚期限 ＋ 30 天寬限）的年季才算完成**，申報期內入庫的年季記為 `incomplete`，下次仍會被請求。判斷申報期是否關閉的邏輯 `FinancialStatementUpdater.is_season_filed()` 已經有了，缺的是把它接到「算不算完成」這個判斷上。
  3. `mrr` 同型（逐年月），一併處理。

- **產出**：`core/pipeline/tw/updaters/financial_statement_updater.py`、`core/pipeline/tw/updaters/monthly_revenue_report_updater.py`、對應測試。

- **驗證方式**：以假時鐘把「現在」設在申報期內與期後各跑一次，確認前者的年季會被重新請求、後者不會。實際資料面則抽一個近期年季比對家數與 `taiwan_stock_info` 的現況家數。

- **相依**：無。

---

## 附錄 A：已由既有 backlog 追蹤的 `docs/` 條目

本盤點確認以下條目**已有歸屬**，不在本文件重複：

| `docs/` 條目 | 歸屬 |
|--------------|------|
| `--mode live` 實盤路徑未實作 | [實盤下單架構規劃.md](實盤下單架構規劃.md) |
| 期貨跳動點只登錄台指期系列、一次回測只有一個 spec | **已於 2026-09-18 完成**（逐商品查 `FUTURES_TICK_SIZE`，七檔查證自 TAIFEX 規格頁）；`README.md`／`README_en.md` 的限制敘述已改寫 |
| 前端自算績效指標、MDD 兩套實作 | [回測績效指標統一由報表輸出.md](回測績效指標統一由報表輸出.md) |
| 台股表名缺 `stock_` 前綴、`stock_id` → `symbol` 資料層改名、`create_symbol_date_index()` 寫死 `stock_id` | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) |
| 財報三表主鍵含 `公司名稱`（同一檔同年季多列） | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 評估 |
| DAO 內部仍是 `sqlite3` | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) |
| `data/downloads/` 的 `tw_stock/`／`tw_futures/` 目錄命名 | [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)，或下次動 `downloads/` 時順手 |
| 台股 tick 仍在 DolphinDB | [台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) |
| 股期行情只有三檔試跑資料 | [暫緩工作彙整.md](暫緩工作彙整.md) S4 |
| 券商分點 metadata 語意、FinMind 連網冒煙腳本 | [暫緩工作彙整.md](暫緩工作彙整.md) S1~S3 |
| 事件驅動迴圈（T+1 延遲成交、限價單未成交、Tick 委託排序） | [美股ETL與回測架構規劃.md](美股ETL與回測架構規劃.md) 的解除條件，設計說明在 [多市場回測引擎架構 §5.1](../docs/backtest/multi-market-engine.md#51-事件驅動迴圈長期方向) |

## 附錄 B：`docs/` 已載明、本次裁示不立項的條目

以下條目留在 `docs/` 的「已知限制／已知簡化」即可，**不進 backlog**——理由一併記在這裡，避免下次盤點又重新討論一遍：

| 條目 | 為何不立項 |
|------|------------|
| T+2 交割未模擬 | 對日頻策略影響小，實作成本高；正確的解法是事件驅動迴圈，屬引擎典範轉移 |
| 融資做多槓桿未啟用（`MarginCost` 只定義） | 會動到 LONG 的資金計算、破壞回歸保護線，且目前無策略需求 |
| 同一標的雙向持倉（net position 語意） | 跨標的多空並存不受限；放寬需要部位、成本攤提與報表全部連動，且目前無策略需求 |
| TICK 級別的成交量上限 | 目前沒有 tick 策略；日 K 已有 `FillConfig.max_volume_share` |
| 期貨 Tick 級別回測未實作 | 期貨 tick 已裁示不做（2026-09-15） |
| DolphinDB 期貨 tick 寫入路徑未實測 | 同上 |
| per-instrument 粒度的 model 掛載（跨市場組合／避險） | 業界確實是這個粒度，但升級路徑乾淨、目前無需求；`docs/` 已寫明升級方式 |
| `core/utils/instrument.py` 的 `StockUtils` 未移出 | 有 pipeline／adapters／`strategy_lab` 三方使用者，搬進 `core/backtest/` 會讓資料管線反向相依引擎，是更嚴重的層級問題 |
| `settlement_model.py` 相依 `futures_roll` 與兩個 PositionManager | 已登錄在 `scripts/check_layer_deps.py` 的 `_KNOWN_REVERSE`（ratchet 擋新增）；抽 `RollModel` 掛點要等有第二種轉倉需求 |
| 期貨保證金 2020-03 之前無資料 | 需人工登錄 16 則掃描影像公告（**不採 OCR**：`477000` 讀成 `47700` 不會報錯）；真的要回測 2015~2019 時再做 |
| 價差部位保證金未模擬 | 現況是兩腿各繳全額，**偏保守**（高估保證金、低估可開口數）；出現價差策略需求時再做 |
| 三大法人期貨籌碼只有近三年 | 來源只保留約三年，無從回補 |
| 股東會停券無資料源 | 缺股東會行事曆；除權息停券已接上，`max_holding_days` 保險絲是現行近似 |
| `SBL` 逐檔議定費率 | `accrue_holding_cost()` 已能逐日計提，卡的是借券成交資料源 |
| `corporate_action` 不含合併換股、私募、股權轉換、代號變更、下市 | 無結構化端點；假跳空護欄（`tests/test_corporate_action_guard.py`）會在漏掉時變紅 |
| `equity_change` 少數公司 2013~2014 年季走「採 IFRSs 前」端點 | 需新端點、新版面、新 cleaner；只有 6 檔 × Q2／Q4 撞到，實務影響很小 |
| `equity_change` 的倖存者偏誤（不含已下市公司與興櫃） | 爬取清單取自 `taiwan_stock_info` 現況，來源如此；研究時自行留意 |
| 券商分點既有 CSV 不能重建 DB | 刻意的設計（不寫 CSV 省下每組合一次檔案 I/O），DB 是唯一來源 |
| 新的補行交易日補不到（尚未出現在任何表時） | 台股自 2019 年起已無補行交易日，實務影響低 |
| 覆蓋率不設 `fail_under` 門檻 | 刻意：覆蓋率偏低時設門檻只會鼓勵寫無效測試 |
