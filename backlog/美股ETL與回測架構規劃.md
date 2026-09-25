# 美股 ETL 與回測架構規劃

## Abstract

- **背景／問題**：資料層只有台灣市場（`core/pipeline/tw/`、`core/api/tw/`），沒有美股資料來源、也沒有 provider 抽象層。
  回測引擎已是「單一引擎 ＋ 可插拔 model」、以 `(market, instrument_type)` 分派（見 [多市場回測引擎架構](../docs/backtest/multi-market-engine.md)），
  `Market.US` 也已定義，但沒有任何對應的 DataFeed、交易日曆、成交與成本 model，無法支撐美股（多交易所、時區、交易日曆、拆股／配息）。
- **目標**：以「平行模組」方式建置美股 ETL 與回測——市場軸目錄新增 `us/`（`core/pipeline/us/`、`core/api/us/`、`core/adapters/us/`、
  `core/dao/us/`、`core/market/us/`、`core/backtest/datafeed/us/`），
  共用 `core/pipeline/shared/` 的 base 類別與既有回測引擎；市場欄位、交易日曆與成本模型分開。跑通最小閉環後再逐步補可信度。
- **範圍界線**：**保留現有台股流程不動**，不做一次性大重構；本規劃**不含**日內／高頻資料、不含實盤下單路徑、
  不含選擇權與 ETF 衍生商品、不含事件驅動引擎改寫（長期方向見 [多市場回測引擎架構 §5.1](../docs/backtest/multi-market-engine.md#51-事件驅動迴圈長期方向)）。
- **驗收標準**：`us_universe` 與 `us_price_daily` 可日更、重跑冪等、中斷可續跑；一支美股日線動能策略可經 `run.py --strategy` 跑完回測，
  並產出資產曲線與交易明細；raw／adjusted 兩種回測模式可切換。

> **2026-09-15 依現行架構改寫**：原規劃的目錄樹與產出路徑含 `core/strategies/us/`、`core/models/us/`、
> `core/backtest/engine/`、`core/backtest/calendars/`、`core/pipeline/shared/checkpoint_store.py`，
> 與 [命名軸線](../docs/dev/naming-axes.md) 的定案（每層目錄只承載一條軸）及已完成的多市場回測引擎衝突，已全部改到現行位置（對照見 §二）。
> 步驟拆分與優先順序不變；唯一的相依變動是 Phase2-1 改為相依 Phase1-3（美股 DataFeed 在 Phase1-3 才建立）。
>
> **2026-09-25 依架構重構後的現況再校正**：市場軸目錄已由 4 個增為 8 個（`scripts/check_layer_deps.py` 的 `_MARKET_AXIS_PACKAGES`），
> 美股需要開 `us/` 的是其中六處（`pipeline`／`api`／`adapters`／`dao`／`market`／`backtest/datafeed`），另兩處 `broker`、`live/datafeed` 屬實盤、不在本規劃範圍；
> SQL 只能寫在 `core/dao/`（見 [資料存取層](../docs/dev/data-access-layer.md)），loader 不再負責建表與寫入；
> 美股交易日曆改放 `core/market/us/`（ETL 差集續跑也要用，放回測層會讓 pipeline 反向相依回測）；
> `BaseDataFeed` 已在中立的 `core/datafeed/base.py`；結算模型已是 `settlement_model/` package；
> 交易單位除了 `UsStockSpec`，還要動 `core/portfolio/sizing.py`（`EqualWeightSizer` 寫死以張計）。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase1-1 | 建立 `us/` 目錄骨架與 provider 介面 | `core/pipeline/us/`（含 `providers/base.py`）、`core/api/us/` | 骨架可 import；`scripts/check_layer_deps.py` 通過；假 provider 可通過介面測試 | ⬜ | 策略**不**開 `us/` 目錄，見 §二 |
| Phase1-2 | `us_universe` ＋ `us_price_daily` ETL（差集續跑、冪等寫入） | `core/pipeline/us/*`、`core/dao/us/`（`us_universe`／`us_price_daily` 的 DAO）、`core/api/us/{price,universe}_api.py`、`core/config/schema.py`、`core/pipeline/utils/constant.py` 的 `DataType` | 中斷後可續跑；重跑不產生重複資料；統計行格式與台股一致 | ⬜ | 相依 Phase1-1；須符合 [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)〈新增或修改 updater 的檢查表〉與 [資料存取層](../docs/dev/data-access-layer.md)（SQL 只寫在 DAO） |
| Phase1-3 | 美股日線動能策略跑通回測 | `core/strategies/stock/momentum_us_strategy.py`、`core/backtest/datafeed/us/`、`core/market/us/market_calendar.py`、`core/adapters/us/`、`core/backtest/models/`（美股 spec／fill／`settlement_model/us_stock.py`）、`core/portfolio/sizing.py`、`core/backtest/factory.py` | 產出資產曲線與交易明細；交易日數與 NYSE 日曆一致 | ⬜ | 相依 Phase1-2；成本先用最小版本，Phase2-2 補完整；報表沿用 `core/backtest/report/reporter.py`；`EqualWeightSizer` 寫死以張計，須參數化或另建美股 sizer |
| Phase2-1 | `us_corporate_actions` ＋ raw/adjusted 回測切換 | `core/pipeline/us/*`、`core/backtest/datafeed/us/stock_datafeed.py` | 同一策略在兩種模式下結果可解釋 | ⬜ | 相依 Phase1-3；`BaseDataFeed` 在中立的 `core/datafeed/base.py`（回測與實盤共用同一份契約） |
| Phase2-2 | 美股成本模型（手續費 ＋ SEC fee ＋ 滑價） | `core/backtest/models/cost_model.py` | 費用計算有單元測試 | ⬜ | 相依 Phase1-3；繼承既有 `BaseCostModel` |
| Phase2-3 | 資料品質檢核與異常告警 | `core/pipeline/us/*` | 缺洞天數、成交量異常可被偵測 | ⬜ | 相依 Phase1-2 |
| Phase3-1 | `us_fundamentals` ETL 支援因子策略 | `core/pipeline/us/*`、`core/api/us/fundamentals_api.py` | 財報欄位可查詢且無未來資料污染 | ⬜ | 相依 Phase2-1 |
| Phase3-2 | 參數掃描框架（walk-forward / grid search） | `core/backtest/` | 可批次產出參數組合的績效比較 | ⬜ | 相依 Phase2-2 |
| Phase3-3 | 多市場共用介面，台股逐步歸位 `tw/` | 全專案 | 台股回歸測試逐筆相同 | ✅ | **2026-09-02 結案**：所有市場軸目錄都只有 `tw/`（清單以 `scripts/check_layer_deps.py` 的 `_MARKET_AXIS_PACKAGES` 為準）；`models`／`strategies`／`managers` 依定案承載的是**軸 B**，本來就不該有 `us/`。詳見該步驟 |

---

## 一、現況評估：目前架構需不需要改？

結論：**不需要再調整既有架構，美股只需平行新增。** 原規劃寫的「增量式調整」（市場分層、回測核心拆分）已由其他工作完成（見 Phase3-3）。

### 目前可沿用的基礎（2026-09-15）

- **ETL 四層 base 類別已在 `core/pipeline/shared/`**（`BaseDataCrawler`／`BaseDataCleaner`／`BaseDataLoader`／`BaseDataUpdater`），
  `us/` 直接繼承，不會反向相依 `tw/`。
- **續跑與失敗語意已有共用機制**：`DatePlanner`／`DateProgressStore`（差集續跑、`no_data`／`incomplete`）、
  `CrawlResult`／`CrawlStatus`（分流「查無資料」與「失敗」）、`UpdateStats` 統計行、`GracefulStop`、`RequestUtils`；
  逐檔（年季 × 個股）來源另有 `SeasonPlanner`／`SeasonProgressStore`（`core/pipeline/shared/season_planner.py`），季頻財報可沿用。
- **SQL 集中在 `core/dao/`**：`BaseDAO`／`DBConnection`（`core/dao/base.py`、`connection.py`）市場無關，美股 DAO 放 `core/dao/us/` 直接繼承。
- **報價驗證已是來源無關的純函式**：`core/adapters/quote_validation.py`，美股 adapter 可直接沿用。
- `tasks/update_db.py` 以 `DataType`（`core/pipeline/utils/constant.py`）列舉 target，新增美股 target 只需擴充列舉與分派。
- **回測已是單一引擎 ＋ 可插拔 model**：新增（美股, 股票）組合依 [多市場回測引擎架構〈四、新增一個（市場, 商品）組合要做什麼〉](../docs/backtest/multi-market-engine.md)
  的六個步驟，既有檔案只改 `factory.py` 一個分支。`Market.US` 已定義於 `core/utils/constant/`。
- `yfinance` 已是主相依（`strategy_lab` 的 `tsmc_overnight_signal` 用它抓美股日線），
  Phase1-1 的第一個 provider 可沿用，不增加相依。

### 目前主要缺口

- 沒有美股資料層：無 provider 抽象、無 `us_*` 資料表、無美股查詢 API。
- 美股欄位（`ticker`、`exchange`、`adj_close`、拆股／配息）需要新 schema；欄位語言已定案用英文（§3.4）。
- 回測端沒有美股的交易日曆、DataFeed、`InstrumentSpec`／`FillModel`／`CostModel`／`SettlementModel`。
- 部位大小模型只支援台股：`core/portfolio/sizing.py` 的 `EqualWeightSizer` 以張（`Units.LOT`）計、至少 1 張，
  `BaseStockStrategy` 固定用它，`StockPortfolioConstructor`（`core/portfolio/construction.py`）的說明也寫明「台股開倉」。
- `BaseStockStrategy`（`core/strategies/stock/base.py`）預設 `Market.TW` 並持有台股 API（`self.price`、`self.chip`…），
  美股策略需覆寫 `self.market`，且不應拿到台股 API（見 Phase1-3）。

### 建議原則

- **保留現有台股流程不動**，美股採「平行模組」建置，降低回歸風險。
- **共享核心，不共享市場細節**：共用 ETL base、續跑機制與回測引擎，但市場欄位、交易日曆、成本模型分開。

---

## 二、目錄落點（依命名軸線定案）

市場軸目錄新增 `us/`，商品類別由檔名承載；承載軸 B（商品類別）的目錄不動。

市場軸目錄以 `scripts/check_layer_deps.py` 的 `_MARKET_AXIS_PACKAGES` 為準，目前共 8 個：
`api`、`broker`、`live/datafeed`、`adapters`、`dao`、`backtest/datafeed`、`market`、`pipeline`。
本規劃只在其中六處開 `us/`（`pipeline`／`api`／`adapters`／`dao`／`market`／`backtest/datafeed`）；
`broker` 與 `live/datafeed` 是實盤路徑，**不在本規劃範圍**（見 Abstract 範圍界線）。

```text
core/
├── api/
│   ├── base.py                      # 既有：BaseDataAPI
│   ├── tw/                          # 既有
│   └── us/                          # 新增：美股查詢 API（查詢委派給 core/dao/us/）
│       ├── price_api.py
│       ├── universe_api.py
│       ├── corporate_actions_api.py
│       └── fundamentals_api.py
├── adapters/
│   ├── quote_validation.py          # 既有：來源無關的報價驗證，美股直接沿用
│   ├── tw/                          # 既有
│   └── us/                          # 新增：美股 DataFrame → Quote
├── dao/
│   ├── base.py、connection.py       # 既有：BaseDAO／DBConnection（市場無關）
│   ├── tw/                          # 既有
│   └── us/                          # 新增：us_universe／us_price_daily／us_corporate_actions／
│                                    #       us_fundamentals_quarterly 的 DAO（建表、UNIQUE、寫入、查詢）
├── market/
│   ├── tw/                          # 既有：market_calendar.py、futures_calendar.py…
│   └── us/                          # 新增
│       └── market_calendar.py       # NYSE/NASDAQ 交易日曆（ETL／回測／策略共用）
├── pipeline/
│   ├── shared/                      # 既有：四層 base、DatePlanner、SeasonPlanner、GracefulStop、RequestUtils、source_priority
│   ├── tw/                          # 既有
│   └── us/                          # 新增：美股 ETL
│       ├── providers/
│       │   ├── base.py              # Provider 介面（fetch_xxx）
│       │   └── yahoo_provider.py
│       ├── crawlers/                # us_price_crawler.py、us_universe_crawler.py …
│       ├── cleaners/
│       ├── loaders/
│       └── updaters/
├── backtest/
│   ├── factory.py                   # 既有：加一個 (Market.US, InstrumentType.STOCK) 分支
│   ├── models/
│   │   ├── instrument_spec.py       # 既有：新增 UsStockSpec
│   │   ├── fill_model.py            # 既有：新增 UsStockFillModel
│   │   ├── cost_model.py            # 既有：新增 UsStockCostModel
│   │   └── settlement_model/        # 既有 package（base／tw_stock／tw_futures）
│   │       └── us_stock.py          # 新增：UsStockSettlementModel，並在 __init__.py 登記
│   └── datafeed/
│       ├── tw/                      # 既有
│       └── us/                      # 新增
│           └── stock_datafeed.py    # UsStockDataFeed
├── datafeed/
│   └── base.py                      # 既有：BaseDataFeed（回測與實盤共用契約）
├── portfolio/
│   └── sizing.py                    # 既有：EqualWeightSizer 以張計，需參數化交易單位或新增美股 sizer
└── strategies/
    └── stock/                       # 既有：美股策略也放這裡，靠 self.market = Market.US 區分
```

資料庫檔依命名軸線「檔名帶軸 A」：`data/db/us_stock.db`，常數 `US_STOCK_DB_PATH` 放 `core/config/schema.py`。

**原規劃有、但現在不開的目錄**：

| 原規劃 | 不開的理由 | 改放 |
|--------|------------|------|
| `core/strategies/us/`、`core/models/us/`、`core/managers/us/` | 這三個目錄承載軸 B（`base/`＋`stock/`＋`futures/`），加 `us/` 會破壞定案 | `stock/` 底下，市場由 `self.market` 宣告 |
| `core/backtest/engine/`（`event_loop`／`order_matcher`／`portfolio`／`fee_models`） | 單一引擎已在 `backtester.py`，撮合、成本、部位分別是 `FillModel`／`CostModel`／`core/managers/` | `core/backtest/models/`；事件驅動迴圈見多市場回測引擎架構 §5.1 |
| `core/backtest/calendars/` | 交易日曆屬市場結構，ETL（差集續跑）、回測、實盤與策略共用，台股已在 `core/market/tw/market_calendar.py`；放回測層會讓 pipeline 反向相依回測 | `core/market/us/market_calendar.py` |
| `core/pipeline/shared/checkpoint_store.py` | `DateProgressStore` 已存在 | 直接沿用 `core/pipeline/shared/date_planner.py` |

---

## 三、美股 ETL 設計（業界常見模式）

### 3.1 資料域切分（Data Domains）

建議先做四個最核心 domain：

1. **Universe（股票池）**：ticker、交易所、是否可交易、產業分類、上市／下市狀態。
2. **Prices（行情）**：OHLCV（日線先行）、adjusted close、資料來源與版本。
3. **Corporate Actions（公司行為）**：split、dividend，用來還原／調整回測價格序列。
4. **Fundamentals（基本面）**：財報關鍵欄位（營收、EPS、毛利率），先做低頻資料。

### 3.2 ETL 分層責任

- `crawler`：單純對外 API 拉資料（含 retry、rate limit、timeout、raw schema）；回傳 `CrawlResult`，分流「查無資料」與「失敗」。
- `cleaner`：欄位標準化（`ticker`、`trade_date`、`open/high/low/close/adj_close/volume`）、型別校正、重複去除。
- `loader`：讀 CSV、檔內去重、逐檔彙報（`finish_load`）；**不寫 SQL、不建表**，寫入委派給 DAO。
- `DAO`（`core/dao/us/`）：建表與索引、唯一鍵約束（`UNIQUE`）、批次寫入、查詢、savepoint／commit。
- `updater`：流程編排（日期範圍切片、續跑、錯誤重試策略、統計行）；建立並持有 DAO，交給 loader 共用。

職責切分依 [資料存取層](../docs/dev/data-access-layer.md)〈職責表〉，由 `scripts/check_layer_deps.py` 強制 DAO 以外不得 `import sqlite3`。

### 3.3 關鍵工程機制

- **續跑用差集，不用 `MAX(date) + 1`**：沿用 `DatePlanner`（候選 ＝ 交易日曆 − 表內已有 − `no_data` ＋ `incomplete`），
  理由見 [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)〈Resume 為什麼是「差集」〉。
  **交易日曆來源要先定**：台股以 `price` 表自身當日曆，美股若照做，第一次回補時沒有日曆可用——
  需要外部日曆（例如新增 `pandas_market_calendars` 相依）或以指數行情當基準，Phase1-2 動工時決定。
  日曆落點固定在 `core/market/us/market_calendar.py`（`core.market` 與 `core.pipeline` 同層，pipeline 可直接 import）；
  若 Phase1-2 就需要外部日曆，該檔在 Phase1-2 先建，Phase1-3 沿用。
- **冪等寫入**：DB 用 `UNIQUE` ＋ `INSERT OR IGNORE`（與台股一致，見 ETL 入庫約定 §3.1），**實作在 `core/dao/us/` 的 DAO**，不寫在 loader；
  只有 provider 會修正歷史值的欄位（如 `adj_close`）才需要 UPSERT，動工時逐表判斷。
- **Data Quality Gate**：在 loader 前檢查空值率、價格邏輯（`low <= open/close <= high`）。
- **Source Priority**：主來源失敗時 fallback（例如 Yahoo → 付費 provider）。
  注意現有的 `dedup_by_source_priority()`（`core/pipeline/shared/source_priority.py`）解決的是另一件事——
  **同一筆資料有多個來源時保留優先序最高的那筆**（去重），不是「主來源失敗改打備援」；
  失敗 fallback 需另外在 provider／crawler 層設計，多來源並存時的去重則可直接沿用該函式。
- **Metadata 審計**：保留 `source`、`ingested_at`。台股目前以 `UpdateStats` 統計行寫進 log、不落表；
  美股是否另建 `etl_job_runs` 表在 Phase2-3 決定。

### 3.4 建議資料表（`us_stock.db`，SQLite 先行）

- `us_universe`
- `us_price_daily`
- `us_corporate_actions`
- `us_fundamentals_quarterly`
- `etl_job_runs`（可選，見 Phase2-3）

原規劃的 `etl_checkpoints` 表不建：續跑狀態沿用 `DateProgressStore`。
表名前綴 `us_` 保留——[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 的目標是單一資料庫，屆時前綴是必要的。

建議唯一鍵：

- `us_price_daily`：`(ticker, trade_date, source)`
- `us_corporate_actions`：`(ticker, action_date, action_type, source)`
- `us_fundamentals_quarterly`：`(ticker, fiscal_period_end, source)`

> **✅ 欄位語言已定案（2026-09-01）：美股一律用英文欄名**，即上表的
> `ticker`／`trade_date`／`open`…，直接沿用 provider 回傳的欄名。
>
> 規則寫死為**「欄位語言跟著資料來源走」**：交易所網頁／檔案爬來的保留中文欄
> （`price` 的 `開盤價`、`futures_price_daily` 的 `結算價`），API 來源用其原始英文欄。
> 專案裡已經有**五張全英文的表**（四張 FinMind ＋ `futures_stock_universe`），
> 美股用英文是延用既有那一套，不是新增第三套規則。完整理由與對照表見
> [ETL 入庫約定 §3.4](../docs/pipeline/etl-ingestion.md)。

---

## 四、美股回測架構設計（業界常見模式）

### 4.1 回測核心分層（對照現行實作）

| 分層 | 職責 | 現行落點 |
|------|------|----------|
| DataFeed | 供應策略所需資料（價格、公司行為、基本面） | `BaseDataFeed` → 新增 `UsStockDataFeed` |
| Signal / Strategy | 產生交易訊號（不直接操作資金帳本） | `BaseStockStrategy`（`self.market = Market.US`） |
| Execution Simulator | 模擬成交（滑價、手續費、最小交易單位） | `InstrumentSpec` ＋ `FillModel` ＋ `CostModel`；委託前處理（方向白名單、執行順序、持倉檔數上限）在 `core/execution/order_preprocess.py`，回測實盤共用 |
| Portfolio / Risk | 倉位、現金、風險控制 | `core/portfolio/`（`sizing.py` 部位大小、`construction.py` 組合建構）＋ `core/managers/stock/position_manager.py` ＋ `SettlementModel` |
| Performance / Report | 績效指標與圖表輸出 | `core/backtest/report/reporter.py`、`core/backtest/analysis/` |

### 4.2 美股特有設計點

- **交易日曆**：使用 NYSE/NASDAQ 交易日，不可直接沿用台股日曆。
- **時區統一**：建議內部統一 UTC 儲存，顯示轉 `America/New_York`。
- **價格調整模式**：支援 `raw` 與 `adjusted` 兩種回測模式（由策略參數決定）。
- **成本模型**：手續費、SEC fee、最小費用、滑價模型需可插拔（即 `BaseCostModel` 的子類）。
- **交易單位**：美股 1 股即可交易，`UsStockSpec` 的計價單位與台股（1 張 ＝ 1000 股）不同。
  **只改 spec 不夠**：下單數量由 sizer 決定，`core/portfolio/sizing.py` 的 `EqualWeightSizer` 寫死
  `int(每檔資金 / (參考價 × Units.LOT))`、不足 1 張不下單，美股需參數化交易單位或另建 sizer（見 Phase1-3）。
- **流動性過濾**：回測前過濾平均成交量太低標的，避免不實際成交假象。

### 4.3 回測輸入契約（建議）

策略最少依賴以下欄位：`ticker`、`datetime`（或 `trade_date`）、`open`、`high`、`low`、`close`、`adj_close`、`volume`、`is_trading_day`。

若使用基本面，另需：`report_date`、`publish_date`、財務欄位（如 `revenue`、`eps_ttm`）。

---

## 五、任務入口與執行流程建議

### 5.1 CLI target 建議（對齊現有 `tasks/update_db.py` 風格）

在 `DataType` 新增 `us_universe`、`us_price`、`us_actions`、`us_fundamentals`。
既有的 `all`／`no_tick` 是否納入美股 target 要一併決定（建議不納入，另設 `us_all`，避免台股日更被美股 provider 的失敗拖住）。

### 5.2 典型日更流程

1. 更新 `us_universe`（新增／下市股票狀態）。
2. 更新 `us_price`（昨日到今日）。
3. 更新 `us_actions`（拆股／配息）。
4. 依需要更新 `us_fundamentals`（低頻，週更或月更）。
5. 印出統計行與資料品質統計（是否落表見 Phase2-3）。

---

## Phase 1：最小可跑版本

### Phase1-1. 建立 `us/` 目錄骨架與 provider 介面 ⬜

- **目的**：先把平行模組的骨架與對外 API 抽象定下來，後續兩步才有落點。
- **做法**：依 §二建立 `core/pipeline/us/`（含 `providers/`、四層子目錄）與 `core/api/us/`；
  `providers/base.py` 定義 provider 介面（`fetch_xxx`），先實作一個 provider（建議沿用已是相依的 `yfinance`，免金鑰）。
  **不建** `core/strategies/us/`（見 §二）。
- **產出**：`core/pipeline/us/`、`core/api/us/` 的骨架檔。
- **驗證方式**：骨架可 import；`python scripts/check_layer_deps.py` 通過（含跨軸目錄污染檢查）；provider 介面可用一支假 provider 通過測試。
- **相依**：無。

### Phase1-2. `us_universe` ＋ `us_price_daily` ETL ⬜

- **目的**：完成最核心的兩個資料域，讓策略有資料可跑。
- **做法**：四層 ETL 全套，繼承 `core/pipeline/shared/` 的 base 類別；落實 §3.3 的差集續跑與冪等寫入，
  逐條對照 [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)〈新增或修改 updater 的檢查表〉（分批入庫、`DataLoadError`、統計行、`FAILED` 不可記成查無資料）；
  `core/config/schema.py` 新增 `US_STOCK_DB_PATH` 與表名常數；`DataType` 新增 `us_universe`、`us_price` 兩個 target。
  建表、`UNIQUE`、寫入與查詢寫在 `core/dao/us/` 的 DAO（繼承 `BaseDAO`）；loader 只讀 CSV、檔內去重、逐檔彙報，
  `core/api/us/` 的查詢委派給 DAO（依 [資料存取層](../docs/dev/data-access-layer.md)）。
- **產出**：`core/pipeline/us/*`、`core/dao/us/`（`us_universe`、`us_price_daily` 的 DAO）、`core/api/us/price_api.py`、`core/api/us/universe_api.py`、`core/config/schema.py`、`core/pipeline/utils/constant.py`、`tasks/update_db.py`；若差集續跑採外部日曆，另含 `core/market/us/market_calendar.py`（見 §3.3）。
- **驗證方式**：中斷後重跑可續跑且不產生重複資料；抽樣比對來源網站數據；新增的 API 公開方法有測試（`scripts/check_api_orphan_methods.py` 通過）。
- **相依**：Phase1-1。

### Phase1-3. 美股日線動能策略跑通回測 ⬜

- **目的**：驗證最小閉環（資料 → 策略 → 報表）可跑通。
- **做法**：依 [多市場回測引擎架構〈四〉](../docs/backtest/multi-market-engine.md) 新增（美股, 股票）組合：
  1. `core/backtest/datafeed/us/stock_datafeed.py`：`UsStockDataFeed`（繼承 `core/datafeed/base.py` 的 `BaseDataFeed`）；
     NYSE/NASDAQ 交易日曆放 `core/market/us/market_calendar.py`（§4.2；Phase1-2 已建就直接沿用）。
  2. `core/adapters/us/`：美股 DataFrame → `StockQuote`（沿用 `core/models/stock/`；報價驗證沿用 `core/adapters/quote_validation.py`）。
  3. `core/backtest/models/`：`UsStockSpec`（`instrument_spec.py`）／`UsStockFillModel`（`fill_model.py`）／
     `UsStockSettlementModel`（`settlement_model/us_stock.py`，並在 `settlement_model/__init__.py` re-export），
     成本先給最小版本（例如只算固定手續費），Phase2-2 補完整。
  4. `core/portfolio/sizing.py`：`EqualWeightSizer` 寫死以張（`Units.LOT`）計、至少 1 張，美股 1 股即可交易——
     把交易單位參數化（預設維持 `Units.LOT`，台股回歸不變），或新增美股 sizer；美股策略在 `__init__` 覆寫 `self.sizer`
     （`BaseStockStrategy` 固定建 `EqualWeightSizer()`）。`StockPortfolioConstructor` 說明寫「台股開倉」，一併改成市場中立。
  5. `core/backtest/factory.py`：加 `(Market.US, InstrumentType.STOCK)` 分支。
  6. `core/strategies/stock/momentum_us_strategy.py`：日線動能策略，`self.market = Market.US`。
     `BaseStockStrategy` 會帶入台股 API，是否需要另一支不帶台股 API 的美股基底，動工時決定。
  回測報表沿用 `core/backtest/report/reporter.py`，先完成可比較的資產曲線與交易明細。
- **產出**：上述各檔（含 `core/portfolio/sizing.py`、`core/market/us/market_calendar.py`）。
- **驗證方式**：`python run.py --strategy <美股策略類別名>` 可產出資產曲線與交易明細；交易日數與 NYSE 日曆一致；
  台股回歸雙線（`./scripts/run_regression.sh`）逐筆相同（證明 factory 分支沒有影響台股）。
- **相依**：Phase1-2。

---

## Phase 2：回測可信度提升

### Phase2-1. `us_corporate_actions` ＋ raw/adjusted 切換 ⬜

- **目的**：沒有公司行為資料，回測價格序列在拆股／配息日會出現假跳空。
- **做法**：補 `us_corporate_actions` ETL；`UsStockDataFeed` 支援 `raw` 與 `adjusted` 兩種模式，
  開關沿用台股既有的 `build_backtester(strategy, adjusted_price=...)`，不另立策略參數。
  台股作法可直接對照（見 [資料覆蓋範圍〈股價還原〉](../docs/exchanges/data_coverage.md)）：後復權、**查詢時即時還原**不落地還原欄位、
  訊號用還原價而成交與成本用原始價。美股若 provider 已給 `adj_close`，要先決定是採用 provider 的值還是自行由公司行為累乘，兩者不可混用。
- **產出**：`core/pipeline/us/*`、`core/backtest/datafeed/us/stock_datafeed.py`。
- **驗證方式**：挑一檔有拆股紀錄的標的，`adjusted` 模式下拆股日無假跳空；兩種模式的績效差異可解釋。
- **相依**：Phase1-3（原為 Phase1-2；美股 DataFeed 在 Phase1-3 建立）。

### Phase2-2. 美股成本模型 ⬜

- **目的**：手續費結構與台股不同（含 SEC fee、最小費用、無證交稅），不可沿用台股模型。
- **做法**：於 `core/backtest/models/cost_model.py` 新增 `UsStockCostModel`（繼承 `BaseCostModel`）：手續費、SEC fee、最小費用；
  滑價放 `CostModel` 或 `FillModel`，動工時對照台股實作決定。放空相關方法（`borrow_fee` 等）若不支援須明確拋錯，不可靜默回 0。
- **產出**：`core/backtest/models/cost_model.py`、`core/backtest/factory.py`（改用完整版成本模型）。
- **驗證方式**：各項費用有單元測試，含最小費用的邊界案例。
- **相依**：Phase1-3。

### Phase2-3. 資料品質檢核與異常告警 ⬜

- **目的**：資料缺洞會靜默地讓回測結果失真。
- **做法**：落實 §3.3 的 Data Quality Gate——空值率、價格邏輯（`low <= open/close <= high`）、缺洞天數、成交量異常；
  決定是否建 `etl_job_runs` 表（台股目前只寫統計行到 log）。
- **產出**：`core/pipeline/us/*`、（可選）`etl_job_runs` 表。
- **驗證方式**：人為注入缺洞與異常價格，檢核機制可偵測並告警。
- **相依**：Phase1-2。

---

## Phase 3：策略研究效率提升

### Phase3-1. `us_fundamentals` ETL ⬜

- **目的**：支援因子與基本面策略。
- **做法**：補 `us_fundamentals_quarterly` 四層 ETL；**須同時記錄 `report_date` 與 `publish_date`**，回測一律以 `publish_date` 為可見時點，避免未來資料污染。
- **產出**：`core/pipeline/us/*`、`core/api/us/fundamentals_api.py`。
- **驗證方式**：查詢指定日期只回傳該日之前已公布的財報。
- **相依**：Phase2-1。

### Phase3-2. 參數掃描框架 ⬜

- **目的**：讓策略參數的敏感度可被系統性檢驗。
- **做法**：建立 walk-forward / grid search 框架，批次產出參數組合的績效比較。框架與市場無關，台股策略同樣可用。
- **產出**：`core/backtest/` 下新增掃描模組（須符合 `scripts/check_layer_deps.py` 的分層規則）。
- **驗證方式**：可對一支策略批次跑出參數矩陣與績效表。
- **相依**：Phase2-2。

### Phase3-3. 多市場共用介面，台股逐步歸位 `tw/` ✅

- **目的**：把驗證過的共用元件抽出，讓台股與美股共享核心。
- **做法**：規劃多市場共用介面，逐步把台股流程整理為 `tw/` 子模組。
- **產出**：全專案目錄調整。
- **驗證方式**：台股回歸測試逐筆相同。
- **相依**：原訂 Phase1-1~Phase3-2；實際上由另外兩條線分別完成，不需等美股閉環，見下方完成紀錄。

> **✅ 完成（2026-09-02 結案）**
> 本步驟**不是由美股這條線做掉的**，而是被兩件其他工作分別完成：引擎層共用介面由
> [多市場回測引擎架構](../docs/backtest/multi-market-engine.md) 完成，目錄歸位由
> [命名軸線](../docs/dev/naming-axes.md) 收斂與台期貨規劃 Phase5-3 完成。
>
> | 目錄 | 現況 | 說明 |
> |------|------|------|
> | `core/pipeline/` | `shared/` ＋ `tw/` ＋ `utils/` | 2026-08-31 命名軸線收斂；`utils/` 是層不是軸（2026-09-13） |
> | `core/api/` | `base.py` ＋ `tw/` | **2026-09-02 台期貨 Phase5-3 收斂** |
> | `core/adapters/` | `tw/` | 同上 |
> | `core/backtest/datafeed/` | `tw/`（`BaseDataFeed` 已移到中立的 `core/datafeed/`） | 同上 |
> | `core/models/`／`core/strategies/`／`core/managers/` | `base/`＋`stock/`＋`futures/` | **承載軸 B（商品類別），本來就不該有 `us/`** |
>
> 後三者是[命名軸線](../docs/dev/naming-axes.md)〈落地位置〉表已定案的取捨——市場軸由
> `BaseStrategy.market` 宣告、由 `core/backtest/factory.py` 的 `(market, instrument_type)`
> 分派鍵表達，不由目錄承載。把 `us/` 加進去反而會破壞定案。
>
> **驗證**：台股回歸雙線（LONG ＋ 放空）逐筆相同、全套測試 687 綠。

> **後記（2026-09-25）**：上表是 2026-09-02 結案當時的四個市場軸目錄。2026-09-24 架構重構後，
> 市場軸目錄新增 `core/market/`、`core/dao/`、`core/broker/`、`core/live/datafeed/` 四個，
> 同樣只有 `tw/`，本步驟「台股歸位 `tw/`」的結論不變；目前完整清單以 `scripts/check_layer_deps.py` 的
> `_MARKET_AXIS_PACKAGES` 為準。其中 `broker`、`live/datafeed` 屬實盤，美股不在本規劃範圍內開 `us/`。

---

## 結論

- 專案現在的核心設計是健康的，**不用重寫**，而且原規劃要求的「市場分層 ＋ 回測核心拆分」已經完成。
- 美股剩下的工作是**純新增**：`us/` 資料層 ＋ provider 抽象 ＋ 一組美股回測 model 與 factory 分支。
- 最佳做法仍是先跑通最小閉環（Phase 1），再補可信度（Phase 2）與研究效率（Phase 3）。

---

## 關聯與狀態

- **優先級**：P3（長期架構規劃）
- **進度**：1 / 9 項 ✅（Phase3-3，2026-09-02）；其餘 8 項 ⬜，**Phase1-1 可直接開工**
- **相關程式**：`core/pipeline/shared/`、`core/api/base.py`、`core/dao/`、`core/market/`、`core/datafeed/base.py`、`core/backtest/factory.py`、`core/backtest/models/`、`core/backtest/datafeed/`、`core/portfolio/sizing.py`、`core/strategies/stock/`、`core/utils/constant/`、`scripts/check_layer_deps.py`、`tasks/update_db.py`
- **相關文件**：
  - [多市場回測引擎架構](../docs/backtest/multi-market-engine.md)（新增（市場, 商品）組合的步驟；§5.1 事件驅動迴圈的長期方向）
  - [命名軸線](../docs/dev/naming-axes.md)（`us/` 可以放哪些目錄、不能放哪些目錄）
  - [ETL 入庫約定](../docs/pipeline/etl-ingestion.md)（續跑、冪等、失敗語意、欄位語言）
  - [資料存取層](../docs/dev/data-access-layer.md)（SQL 只寫在 `core/dao/`；loader／DAO／updater 職責切分）
  - [台期貨平台](../docs/futures/tw-futures-platform.md)（同樣是「平行市場模組、共享核心、不共享市場細節」的前例）
- **相關 backlog**：
  - [PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md)（美股資料量較大，建議 DB 遷移先收斂；表名前綴的考量同源）
