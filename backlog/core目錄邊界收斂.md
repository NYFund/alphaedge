# core 目錄邊界收斂

## Abstract

- **背景／問題**：`core/` 同時裝著三種東西：交易框架本體、使用框架的具體策略，以及另一個
  應用程式——資料管線（`core/pipeline/`）。2026-09-26 盤點時 21,759 行；**2026-10-10 main 實查
  22,966 行，佔 `core/` 全部 66,812 行的 34%**，比 `live/`（12,232）加 `backtest/`（6,384）還多
  （`feature/tick-timescaledb` 移除 DolphinDB 後為 22,127 行）。業界的慣例是框架不含使用者策略
  （freqtrade 的 `user_data/strategies/`、LEAN 的 `Engine` 與 `Algorithm.Python` 分專案），
  資料擷取也多半獨立於交易框架之外。另外盤點到三個較小的邊界問題（皆已處理）：
  1. 回測的缺日診斷 import 了 `core.pipeline.shared.date_planner`——這是 `core/` 其餘部分對
     `pipeline` 的**唯一一條**依賴，也是 `pipeline` 搬不出去的唯一障礙（Phase1-1 已切斷）。
  2. `core/managers/` 只裝三支 `position_manager.py`，名稱籠統（Phase3-1 已改名 `core/position/`）。
  3. `core/Dockerfile` 打包的是整個後端，不只是 `core`（Phase4-1 已移到根目錄）。
- **目標**：`core/` 只剩交易框架本體。具體策略搬到頂層 `strategies/`、資料管線搬到頂層
  `etl/`（同時由 `pipeline` 改名），兩者都只能單向依賴 `core`；策略契約（抽象基底）留在 `core/strategies/`。
  完成後頂層的邊界是：`core/` 框架、`strategies/` 正式策略、`etl/` 資料擷取、
  `strategy_lab/` 研究、`apps/` 入口（由 [回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) 建立）。
- **範圍界線**：**不做**
  1. 不改任何交易、成本、ETL 邏輯——本份只搬位置與改 import，回歸雙線一律零變動。
  2. 不改策略類別名稱：類別名即 `--strategy` 參數，排程與 compose 依賴它。
     （`MomentumStrategy1` 改名屬 `實盤下單架構規劃.md` Phase7-10，不在本份範圍，只與 Phase2-2 同一批施作。）
  3. 不把策略拆成獨立 repo 或獨立發布的套件；要做另開文件。
  4. 不動 `core/` 其餘子套件的分層（2026-10-10 實查：`api`、`dao`、`models`、`market`、`broker`、`adapters`、
     `portfolio`、`position`、`analysis`、`backtest`、`live`、`datafeed`、`utils`、`config` 都屬框架本體；
     原列的 `core/execution/` 已於 2026-10-07 併入 `core/portfolio/order_rules.py`）。
  5. `datafeed` 分成 `core/datafeed/`（契約）與 `backtest/`、`live/` 各自的實作是正確模式，不動。
- **驗收標準**：
  1. `core/` 內對 `core.pipeline` 與頂層 `strategies` 的 import 歸零，並由 `check_layer_deps.py` 守住。
  2. `core/strategies/` 只剩契約（`base.py`、`stock/base.py`、`futures/base.py` 與套件門面）。
  3. `core/managers/` 改名為 `core/position/`；`Dockerfile` 移到專案根目錄。（✅ 皆已完成）
  4. Phase5 解除暫緩後，`core/pipeline/` 搬到頂層並改名為 `etl/`。
  5. 〈附：搬出 `core/` 的目錄範圍護欄清單〉的每一處都涵蓋新的頂層套件——**搬完測試全綠不代表
     護欄還在**，範圍寫死 `core` 的護欄會靜默少掃一塊。
  6. 每一步都通過回歸雙線（`./scripts/run_regression.sh`）、`pytest -m "not slow"`、
     三支閘門腳本與 `check_doc_paths.py`。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase1-1 | 回測缺日診斷改用 `MarketHolidayAPI` | `core/backtest/datafeed/tw/stock_datafeed.py`、對應測試 | `core/backtest` 對 `core.pipeline` 的 import 歸零；2025–2026 區間的休市日可被歸因 | ✅ | 2026-09-30 完成（`fdb9967`）：import 歸零；2025 全年 18 個缺日全數歸因為官方休市（改前只報數字）；3 條新測試。順帶修掉 `tests/test_dao_futures_margin.py` 讀到本機真實 CSV 的沙箱漏洞 |
| Phase1-2 | 分層檢查禁止框架 import `core.pipeline` | `scripts/check_layer_deps.py` | 刻意加一條違規 import，檢查要紅 | ✅ | 2026-09-30 完成（`e6d85de`）：新增 E'''' 項（專屬 AST 掃描，偏離原規劃的分層規則寫法）；突變驗證紅→綠；2 條單元測試 |
| Phase2-1 | 建立頂層 `strategies/` 套件，並讓分層與目錄範圍護欄涵蓋它 | `strategies/__init__.py`、`scripts/check_*.py`、`.pre-commit-config.yaml`、`tests/` 四支護欄、`pyproject.toml`、`Dockerfile`、CI | `core` 內 import `strategies` 時檢查要紅；在 `strategies/` 放一個違規，每道護欄各自要紅 | ✅ | 2026-10-01 完成（`122f715`）；八項護欄以暫時檔逐一觸發皆轉紅；`test_strategy_data_access.py` 的假綠燈先行修掉 |
| Phase2-2 | 具體策略與 `StrategyLoader` 搬到 `strategies/` | `strategies/{stock,futures}/*.py`、`strategies/loader.py`、`apps/`、`tests/` | 回歸雙線零變動；`--strategy` 列表與改前相同；策略欄位字面值護欄仍掃得到每一支策略 | 🔄 | **2026-10-09 已在 `feature/post-rehearsal` 實作（`bd7662a`），未進 main**；剩 `實盤下單架構規劃.md` Phase7-1 演練結束後合併、重裝 launchd（與該文件 Phase7-10 改名、`回測與實盤入口拆分及架構收斂.md` Phase1-8 同一次部署） |
| Phase2-3 | `core/strategies/` 收斂成只剩契約 | `core/strategies/**`、`scripts/check_layer_deps.py` | 目錄內只剩 base 與門面；門面檢查通過 | 🔄 | **2026-10-09 已在 `feature/post-rehearsal` 實作（`ed29b46`），未進 main**；分層登記改為契約，另加「`core/strategies/` 只放契約」的專屬檢查。剩演練後合併 |
| Phase2-4 | 策略相關文件與規則入口同步 | `.claude/skills/develop-strategy/`、`strategy_lab/CLAUDE.md`、`CLAUDE.md`、README、`docs/` | `check_doc_paths.py`；全文 grep 舊路徑只剩契約 | 🔄 | **2026-10-09 已在 `feature/post-rehearsal` 實作（`91445c1`），未進 main**。剩演練後合併 |
| Phase3-1 | `core/managers/` 改名為 `core/position/` | `core/position/**`、各 import 端、`pyproject.toml`、`scripts/check_layer_deps.py`、文件 | 回歸雙線零變動；全文 grep `core.managers` 為零 | ✅ | 2026-10-01 完成（`08f1b44`）；回歸雙線零變動；主目錄已於 2026-10-01 盤後更新 |
| Phase4-1 | `core/Dockerfile` 移到專案根目錄 | `Dockerfile`、`docker-compose.yml`、`.github/workflows/ci.yml`、文件 | CI 的映像建置與冒煙通過；`docker compose build` 成功 | ✅ | 2026-10-08 完成（`ea6b106`），**偏離原規格**：提前到 Phase2-2、Phase5-1 之前做（見該步驟）；CI `docker` job 建置與三項冒煙通過 |
| Phase5-1 | `core/pipeline/` 搬到頂層並改名 `etl/` | `etl/**`、資料更新入口、`scripts/`、`tests/`、`pyproject.toml`、目錄範圍護欄、文件 | 回歸雙線零變動；資料更新入口各 target 冒煙；`core` 對 `etl` 的 import 為零；附錄護欄全數涵蓋 `etl/` | ⏸ | 等 TimescaleDB 與 PostgreSQL 兩份計畫的 pipeline 改動落地，避免搬兩次。2026-10-10 現況：`台股tick改用TimescaleDB.md` 16 步已在 `feature/tick-timescaledb` 全數完成、待合併進 main；`PostgreSQL遷移計畫.md` 0 / 16 未動工。2026-10-01 定案：搬移時一併改名 `etl`，連同日誌桶 `logs/pipeline/` 全部改，範圍見步驟章節 |

---

## Phase1：切斷框架對資料管線的依賴

### Phase1-1. 回測缺日診斷改用 `MarketHolidayAPI` ✅

- **目的**：`TwStockDataFeed` 的缺日診斷 import 了 `core.pipeline.shared.date_planner`：`DatePlanner.generate_weekdays()`
  產生平日（純日期運算），`DateProgressStore("price").no_data` 讀 ETL 的中間進度檔當作休市。這是 `core/` 其餘部分
  對 `core.pipeline` 的唯一一條依賴（2026-09-26 以 grep 確認）。而且該診斷其實已失效：`no_data` 只在 ETL 問到
  「查無資料」時才寫入，本機為空，任何區間的缺日診斷都停在「只報數字、不下判斷」。
- **相依**：無。

> **✅ 完成紀錄（2026-09-30，`fdb9967`）**
> - `TwStockDataFeed.report_calendar_gaps()` 改用 `MarketHolidayAPI`：休市日取 `get_closures()`，
>   年度以 `get_covered_years()` 分成「已涵蓋→歸因」與「未涵蓋→只報數字」兩段；平日改為就地的日期運算。
>   `core/` 在 `core/pipeline/` 以外對 `core.pipeline` 的 import 為零。
> - **已知限制**：`market_holiday` 的來源是交易所年初公告的行事曆，**不含**颱風假等臨時休市，
>   這類日期會被列為「不在官方休市清單裡」（刻意的保守行為，已寫進 docstring）；
>   `--target market_holiday` 只抓去年到明年，2024 以前的年度不會變精確。
> - 改前改後（唯讀連本機 `tw_stock.db`）：2025 全年 18 個缺日由「無法分辨」變成全數歸因為官方休市；
>   2024～2025 區間則 2025 的 18 個歸因、2024 未入庫的 20 個只報數字。
> - 測試：`tests/backtest/test_market_calendar_bounds.py` 三條（歸因官方休市、已涵蓋年度的真缺口、跨到未涵蓋年度只報數字）。
>   回歸：只改 log 與未被使用的回傳值，雙線不受影響。
> - **順帶修正一個假綠燈**：`tests/test_dao_futures_margin.py` 的 `test_chain_check_reports_a_gap_and_is_wired_into_update`
>   在主目錄靠本機 `data/downloads/` 的真實 CSV 才會綠；`make_updater()` 改為一併指走 cleaner 的目錄、測試自己寫入所需 CSV（突變驗證）。

### Phase1-2. 分層檢查禁止框架 import `core.pipeline` ✅

- **目的**：Phase1-1 切掉之後，要有機器檢查防止依賴長回來，否則 Phase5-1 搬家時才發現。
- **相依**：Phase1-1。

> **✅ 完成紀錄（2026-09-30，`e6d85de`）**
> - `scripts/check_layer_deps.py` 新增 `check_framework_pipeline_imports()`，報告區段「E''''. 框架 import 資料管線」，計入違規總數。
>   **偏離原規劃（沒有寫成分層規則）**：`core.pipeline` 與 `core.api` 同為等級 3，引擎層（等級 4）往下 import 它在分層檢查裡是合法的，
>   所以改成與 E''／E''' 同型的專屬 AST 掃描。
> - 突變驗證：在 `core/backtest/datafeed/tw/stock_datafeed.py` 加一行 `from core.pipeline.shared.date_planner import DatePlanner`，
>   檢查列出 1 處、結束碼 1；還原後 0。
> - 測試：`tests/test_check_layer_deps_pipeline.py`（兩種 import 寫法都抓；`core/pipeline/` 自身、入口層、docstring 字樣與
>   `core.pipelines` 這類前綴相似的名稱不誤報）。文件：`docs/backtest/module-map.md` 新增一條注意事項。

---

## Phase2：具體策略搬出 `core/`

目標結構（2026-10-10 依 `feature/post-rehearsal` 的實際結果更新）：

```
core/strategies/          # 策略契約：引擎、factory、報表都要認得的介面
    base.py
    stock/base.py
    futures/base.py
strategies/               # 具體策略：使用框架的程式，不屬於框架
    README.md             # 原 core/strategies/README.md
    loader.py             # 原 core/strategies/strategy_loader.py
    stock/volume_breakout_momentum_strategy.py      # 原 momentum_strategy_1.py（實盤下單架構規劃 Phase7-10 改名）
    stock/intraday_momentum_strategy.py
    stock/investment_trust_momentum_swing_strategy.py
    stock/foreign_selling_reversal_short_strategy.py
    futures/momentum_futures_strategy.py
```

main 上目前仍是舊結構：五支具體策略與 `strategy_loader.py` 都在 `core/strategies/`，頂層 `strategies/` 只有 Phase2-1 建立的三個空門面。

**契約為什麼留在 `core/`**：回測引擎、實盤引擎、兩邊的資料源、factory、報表等十幾個模組都要認得
策略介面；契約搬出去，`core` 就得反過來 import 外部套件。

**`StrategyLoader` 為什麼跟著搬**：它的職責是掃描具體策略，正式呼叫端只有入口層
（2026-09-26 確認時是 `run.py`；2026-09-30 入口拆分後為 `apps/_common.py` 與 `apps/live.py`）。留在 `core/` 的話，框架就必須知道頂層 `strategies/` 的存在，違反單向依賴。

### Phase2-1. 建立頂層 `strategies/` 套件，並讓分層與目錄範圍護欄涵蓋它 ✅

- **目的**：先把新套件與守門規則建好，搬檔那一步才有檢查可以驗。**重點是護欄範圍**：十幾道護欄以寫死的
  目錄清單決定掃描範圍，策略搬到頂層之後會靜默少掃一塊，測試照樣全綠。
- **相依**：無。

> **✅ 完成紀錄（2026-10-01，`122f715`）**
> - 新增 `strategies/`、`strategies/stock/`、`strategies/futures/`，三個 `__init__.py` 只有說明字串。
> - **命名確認**：頂層 `strategies` 與 `strategy_lab/strategies/` 同名不衝突——研究區一律以 `strategy_lab.strategies...` 完整路徑引用，
>   全庫沒有裸的 `import strategies`；`strategy_lab/CLAUDE.md` 規定一律從根目錄以 `-m` 執行。不改名。
> - `scripts/check_layer_deps.py`：`_SCAN_DIRS`、`_NON_CORE_TOPS`、`_DB_DRIVER_GUARDED_DIRS`、`_INSTRUMENT_AXIS_PACKAGES` 加入 `strategies`；
>   `_LAYER_RULES` 登記 `("strategies", 7, "策略層", False)`。**超出原規劃兩處**：門面檢查（E'）擴及新套件且新門面**一律不准 import
>   任何專案模組**；E'' 區段標題改為列出現行範圍。
> - 附錄其餘護欄全數加入 `strategies`（含 `pyproject.toml` 的 `include` 與 `[tool.coverage.run] source`、CI 的 `--cov`、`COPY strategies`）。
> - **`tests/test_strategy_data_access.py` 的假綠燈先行修掉**：掃描範圍改為 `core/strategies/` ＋ `strategies/`，
>   自我檢查由「檔案數 ≥ 6」改為「策略載入器找得到的每一支策略，原始檔都在掃描範圍內」（突變驗證）。
> - CI 的映像冒煙新增 `import strategies`：`--help` 不載入策略，漏 COPY 時冒煙照樣綠。
> - 驗證：以暫時檔逐一觸發八項護欄全數轉紅；`pytest` 2480 passed；`check_layer_deps.py` 0；`check_doc_paths.py` 0。
>   **映像內 import 未在本機驗證**（Docker daemon 未啟動），由 CI 把關。

### Phase2-2. 具體策略與 `StrategyLoader` 搬到 `strategies/` 🔄

- **目的**：把具體策略從框架中移出，這是本份文件的主體。
- **做法**：
  1. 以 `git mv` 搬移具體策略（2026-10-09 實作時為五支），保留歷史。
  2. `core/strategies/strategy_loader.py` → `strategies/loader.py`，掃描目標改成頂層
     `strategies` 套件；逐模組隔離、同名拋錯兩條既有行為不變。
  3. 呼叫端改 import：`apps/_common.py`、`apps/live.py`、測試、`scripts/` 中引用者。
  4. ~~`check_layer_deps.py` 的 `_INSTRUMENT_AXIS_PACKAGES` 加上 `strategies`~~——**已於 Phase2-1 完成**。
  5. ~~`tests/test_strategy_data_access.py` 的掃描範圍與自驗~~——**已於 Phase2-1 先行完成**；搬完只需確認它仍綠、且突變會轉紅。
- **產出**：`strategies/**`、`apps/`、`tests/`、`scripts/check_layer_deps.py`。
- **驗證方式**：
  1. 回歸雙線零變動。
  2. `StrategyLoader.load_strategies()` 回傳的類別名稱集合與改前完全相同。
  3. `pytest -m "not slow"` 全綠、分層檢查 0 違規。
  4. 在 `strategies/stock/` 任一策略刻意寫一個欄位字面值（例如 `"收盤價"`），
     `test_strategy_data_access.py` 要紅。
- **相依**：Phase2-1。施作時機限制：
  1. **避開 `實盤下單架構規劃.md` Phase7-1 模擬演練的排程時段**：模組路徑改變後，
     常駐的實盤行程必須重啟才載得到新路徑。
  2. ~~排在 `回測與實盤入口拆分及架構收斂.md` Phase1-4（策略依名稱載入）之後~~——已滿足，loader 只改一次。
  3. 施作前確認 `core/strategies/` 沒有開發中未 commit 的變更，避免搬移與開發互相覆蓋。
  4. **與 `實盤下單架構規劃.md` Phase7-10（`MomentumStrategy1` 改名為 `VolumeBreakoutMomentumStrategy`）同一批施作，
     先搬家、再改名**（2026-10-05 決定）：兩步動的是同一批策略檔、測試與 launchd 排程參數，都要等演練結束；
     分開做要重裝兩次排程、跑兩次回歸。也不在演練期間先搬沒演練的策略——具體策略會分散在兩處、
     `StrategyLoader` 要同時掃兩個位置，多出一段過渡狀態。

> **🔄 實作紀錄（2026-10-09，`feature/post-rehearsal` 的 `bd7662a`）**
> - 五支具體策略以 `git mv` 搬到 `strategies/{stock,futures}/`（含 2026-10-08 新增的 `intraday_momentum_strategy.py`）；`strategy_loader.py` → `strategies/loader.py`，掃描目標改為頂層套件，載入的類別集合與改前相同。
>   緊接著 `8cd350c` 完成 `實盤下單架構規劃.md` Phase7-10 的改名（先搬家再改名，已依序完成）。
> - **做法外的發現**：日誌桶以模組名前綴分流，backtest 桶只認 `core.strategies`——搬家後策略的記錄會靜靜落進 pipeline 桶。已在 `LogManager.BUCKET_PREFIXES` 加上 `strategies`。
> - 呼叫端：`apps/`、`scripts/manual/`、29 個測試；仍在規劃中的 backlog 由 `check_doc_paths.py` 抓到 7 處舊路徑，一併改。
> - 驗證：回歸雙線通過；全套 2,761 passed、分層 0 違規；在搬過去的策略寫入欄位字面值，`test_strategy_data_access.py` 轉紅。
> - 剩：演練結束後合併、重啟常駐行程（與 `實盤下單架構規劃.md` Phase7-10 的紀錄庫改名、launchd 重裝同一次）。
> - **備註（2026-10-10 複查）**：分支上 `tests/test_strategy_data_access.py` 的 `STRATEGY_DIRS` 仍掃 `core/strategies/` ＋ `strategies/`，
>   上方註解還寫著「契約與尚未搬出的具體策略」；搬完後 `core/strategies/` 只剩契約，該註解已過時（掃描範圍保留無妨，契約也不該寫欄位字面值）。

### Phase2-3. `core/strategies/` 收斂成只剩契約 🔄

- **目的**：搬完之後，`core/strategies/` 的分層登記仍把 `core.strategies` 整包當成第 7 層
  策略層，要改成只剩契約的第 4 層。
- **做法**：
  1. 確認 `core/strategies/` 下的 `.py` 只剩 `base.py`、`stock/base.py`、`futures/base.py`
     與三個 `__init__.py`（`README.md` 由 Phase2-4 搬走）。
  2. `check_layer_deps.py`：`("core.strategies", 7, "策略層")` 改為契約層；
     `check_strategy_facades()` 的門面清單沿用。
  3. 更新 `core/strategies/__init__.py` 與兩個 base 內提到「掃描 `core/strategies/`」的 docstring。
- **產出**：`core/strategies/**`、`scripts/check_layer_deps.py`。
- **驗證方式**：分層檢查 0 違規；在 `core/strategies/stock/` 刻意放一支具體策略，
  分層或門面檢查要紅。
- **相依**：Phase2-2。

> **🔄 實作紀錄（2026-10-09，`feature/post-rehearsal` 的 `ed29b46`）**
> - `("core.strategies", 7, "策略層")` 改為 `("core.strategies", 4, "策略契約（套件門面）", True)`。
> - **偏離原規格（加嚴）**：原本的驗證「在 `core/strategies/stock/` 放一支具體策略，分層或門面檢查要紅」光靠改分層等級做不到——具體策略 import 的都是同層或更低層，
>   放回去照樣零違規。新增 `check_strategy_contract_only()`：`core/strategies/` 只准有契約與門面六個檔，實測放一支具體策略時腳本以結束碼 1 失敗。
> - 契約套件、股票門面與期貨基底的說明改為「具體策略在頂層 `strategies/`」。
> - 2026-10-10 實查分支：`core/strategies/` 只剩六個 `.py`，共 904 行（main 上含具體策略與 loader 為 2,809 行）。剩演練後合併。

### Phase2-4. 策略相關文件與規則入口同步 🔄

- **目的**：寫策略的入口文件全部指向新位置，否則下一支策略會照舊文件寫回 `core/`。
- **做法**：更新以下檔案中的策略路徑：
  1. `.claude/skills/develop-strategy/SKILL.md`（權威檔，含 frontmatter 的 `description`）。
     `.cursor/rules/strategy-development-sdd.mdc` 的本文只是指標不用改，但它 frontmatter 的
     `description` 寫著舊路徑，那是 Cursor 判斷何時套用規則的依據，**要一起改**。
  2. `CLAUDE.md` 的 skill 表觸發時機欄。
  3. `strategy_lab/CLAUDE.md`、`strategy_lab/README.md`：「成熟策略搬到 `core/strategies/stock/`」改為 `strategies/stock/`。
  4. `core/strategies/README.md` 移到頂層 `strategies/`，並修正內文。
  5. `README.md`、`README_en.md` 的專案樹與說明；`docs/` 約 7 份。
  6. `backlog/` 內提到舊路徑當作**新策略落點**的敘述（例如 `美股ETL與回測架構規劃.md`），改為新位置。
- **產出**：上述文件。
- **驗證方式**：`check_doc_paths.py` 通過；`grep -rn "core/strategies/\(stock\|futures\)/[a-z_]*strategy" --include=*.md .` 為零。
- **相依**：Phase2-3。

> **🔄 實作紀錄（2026-10-09，`feature/post-rehearsal` 的 `91445c1`）**
> - 做法 1～6 全數完成：skill（含 frontmatter）、Cursor 規則的 `description`、`CLAUDE.md`、`strategy_lab/`、策略 README（由 `core/strategies/` 搬到頂層）、README 中英、`docs/` 與兩份規劃中的 backlog。
>   backlog 的部分（`美股ETL與回測架構規劃.md`、`台股新聞情緒溫度計篩選工具.md`）已同步到 main。
> - **做法外的發現（假綠燈）**：`tests/test_module_docs.py` 對 `core/strategies/{stock,futures}/` 的豁免在搬家後只剩兩個 `base.py` 可豁免，反而放過了契約；
>   已移除豁免，「策略檔要有 class docstring」改掃頂層 `strategies/` 並先確認掃得到檔案。
> - 驗證：`check_doc_paths.py` 0 錯；非 backlog 的舊路徑引用為零（backlog 歷史紀錄保留原路徑）。剩演練後合併。

---

## Phase3：`core/managers/` 改名

### Phase3-1. `core/managers/` 改名為 `core/position/` ✅

- **目的**：`core/managers/` 底下只有三支 `position_manager.py`，職責是部位記帳（FIFO 拆單、成本攤提、損益）。
  `managers` 這個名稱不說明職責，之後很容易有不相干的 `XxxManager` 被丟進來。
- **相依**：排在 `回測與實盤入口拆分及架構收斂.md` Phase3 之後（兩邊都改 `core/managers` 的 import）——已依序完成。

> **✅ 完成紀錄（2026-10-01，`08f1b44`）**
> - `git mv core/managers core/position`，三支 `position_manager.py` 與類別名不變。
> - 呼叫端：生產碼 14 檔、測試 15 檔的 import；`check_layer_deps.py` 的分層登記與 `_INSTRUMENT_AXIS_PACKAGES`；
>   `core/utils/log_manager.py` 的 `BUCKET_PREFIXES`（不跟著改的話，部位記帳的 log 會靜默改落進總括桶）；
>   `pyproject.toml` 的 namespace package 註解；README 兩份、`docs/` 六份；仍在規劃中的 backlog 改新路徑，已完成步驟的紀錄不改。
> - 驗證：`git grep -nE "core[./]managers" -- ':!backlog'` 為零；回歸雙線（資料庫快照）SHORT 6 passed、LONG 1 passed 無 skip；
>   `pytest` 2469 passed；`check_layer_deps.py` 0；`check_doc_paths.py` 0。主目錄於 2026-10-01 盤後段結束後更新（`6ddbffc`）。

---

## Phase4：`Dockerfile` 移到專案根目錄

### Phase4-1. `core/Dockerfile` 移到專案根目錄 ✅

- **目的**：這份 Dockerfile 打包的是整個後端（main 上為 `core`、`tasks`、`apps`、`strategies`；部署 `feature/post-rehearsal` 後
  `tasks` 併入 `apps`；Phase5 後再加 `etl`），放在 `core/` 底下會讓人以為它只打包框架。`frontend/Dockerfile` 只打包前端，留在原位。
- **相依**：原訂排在 Phase2-1 與 Phase5-1 之後。

> **✅ 完成紀錄（2026-10-08，`ea6b106`）**
> - **偏離原規格**：沒有等 Phase2-2 與 Phase5-1（⏸）。Phase2-1 已把 `COPY strategies` 加上，Phase2-2 只搬目錄內容、不改 COPY 清單；
>   Phase5-1 恢復時在根目錄 `Dockerfile` 補一行 `COPY etl`（該步驟做法第 7 點已寫）。
> - `git mv core/Dockerfile Dockerfile`；引用處：`docker-compose.yml` 兩個 service 的 `dockerfile:`、CI、README 兩份、`docs/deployment/prod-deployment.md`、
>   `docs/setup/dev-setup.md`、`pyproject.toml`、`core/config/settings.py`、`frontend/Dockerfile` 的註解。建置指令改為 `docker build -t alphaedge-core .`。
> - 驗證：`grep -rn "core/Dockerfile"` 排除 `backlog/` 為零；`docker compose config` 解析出 `dockerfile: Dockerfile`；
>   本機 Docker daemon 未啟動，`docker compose build` 改以 CI 的 `docker` job 代替。

---

## Phase5：資料管線搬出 `core/`

### Phase5-1. `core/pipeline/` 搬到頂層並改名 `etl/` ⏸

- **目的**：`core/pipeline/`（爬蟲、清洗、入庫、updater）是寫資料庫的另一個應用程式，
  交易框架只讀資料庫。兩者的執行時機（排程與手動 vs 回測與實盤）與失敗模式都不同。
  搬出去之後，`etl` 單向依賴 `core`（`dao`、`config`、`broker` 的 Shioaji session），
  `core` 完全不知道 `etl` 存在。
- **為什麼改名 `etl`**（2026-10-01 定案）：
  1. 目錄結構本身就是 ETL——`crawlers/`（Extract）→ `cleaners/`（Transform）→ `loaders/`（Load），
     `updaters/` 負責串接。
  2. 專案其他地方早已這樣稱呼：`check_layer_deps.py` 的分層標籤是「資料層（ETL）」、
     `docs/pipeline/etl-ingestion.md`、`美股ETL與回測架構規劃.md`。
  3. `pipeline` 在量化專案裡有歧義（回測流程、訊號流程、ML pipeline），看名字分不出是「爬資料寫 DB」。
  4. 搬移本來就要改全部 import，同時改名幾乎零額外成本；單獨改名則要多搬一次。
- **資料更新入口的位置**：main 上是 `tasks/update_db.py`、`tasks/clean_logs.py`；
  `回測與實盤入口拆分及架構收斂.md` Phase1-8 部署後改為 `apps/update_db.py`、`apps/clean_logs.py`。
  以下以「資料更新入口」「日誌清理入口」稱呼，施作時以當時的位置為準。
- **改名範圍**（2026-10-01 使用者裁示：**全部改**，不保留任何 `pipeline` 字樣）：
  1. 套件路徑與 import：`core.pipeline.*` → `etl.*`。
  2. 由套件名衍生的識別字：`check_layer_deps.py` 的 `_PIPELINE_PACKAGE`／`_PIPELINE_DIR`／
     `check_framework_pipeline_imports()`、`tests/test_check_layer_deps_pipeline.py`、
     `core/config/paths.py` 的 `CLEANER_SCHEMA_DIR_PATH` 路徑字串。
  3. 常數：`PIPELINE_DOWNLOADS_PATH` → `ETL_DOWNLOADS_PATH`（磁碟上仍是 `data/downloads/`，不受影響）；
     `PIPELINE_LOGS_DIR_PATH` → `ETL_LOGS_DIR_PATH`（連同 `tests/test_config_paths.py` 的常數清單）。
  4. 日誌桶：`logs/pipeline/` → `logs/etl/`；`core/utils/log_manager.py` 的桶名、docstring 與註解；
     日誌清理入口的 `"pipeline"` 鍵；`tests/test_entrypoint_and_logging.py`、`tests/conftest.py` 中的桶路徑。
  5. 文件：`docs/pipeline/` 目錄 → `docs/etl/`；程式註解與文件散文中指這個套件或日誌桶的「pipeline」改為「ETL」。
- **日誌桶改名的注意事項**：`logs/pipeline/` 是磁碟上的執行期產物（2026-10-01 本機約 26 MB），
  程式改名後舊目錄不會自動搬，日誌清理入口也不再掃它，會變成永遠不被清理的孤兒目錄。處理方式：
  1. 開發的 worktree 與主目錄各自執行一次 `mv logs/pipeline/* logs/etl/ && rmdir logs/pipeline`
     （先 `mkdir -p logs/etl`）。
  2. 主目錄由 launchd 常駐執行，只能在**段落空檔**更新程式並搬目錄，避免排程中途一半寫舊桶、一半寫新桶。
  3. 2026-10-01 已確認 `~/Library/LaunchAgents/com.alphaedge.*.plist` 沒有寫死 `logs/pipeline`
     （launchd 自己的輸出在 `logs/launchd/`），施作時再 grep 一次。
- **做法**：
  1. `git mv core/pipeline etl`，import 由 `core.pipeline.*` 改為 `etl.*`。
  2. 呼叫端：資料更新入口、`scripts/` 約 5 支、約 60 個測試檔（2026-10-10 於 `feature/tick-timescaledb` 實查；2026-09-26 時約 56 個）。
  3. `pyproject.toml`：`include` 加 `etl*`；`per-file-ignores` 中 `core/pipeline/...` 的路徑改名。
  4. `check_layer_deps.py`：Phase1-2 的規則改為把 `etl` 加進 `_NON_CORE_TOPS`；
     `_MARKET_AXIS_PACKAGES` 中的 `core/pipeline` 改為 `etl`；**`_DB_DRIVER_GUARDED_DIRS` 要加上 `etl`**
     （2026-10-10 main 為 `core`、`apps`、`strategies`、`tasks`，部署後為 `core`、`apps`、`strategies`）——
     ETL 是寫資料庫最多的地方，漏掉等於「`sqlite3` 只能出現在 `core/dao/`」這條規則對它失效。
  5. 附錄清單中其餘每一處都加上 `etl`。
  6. 上列「改名範圍」第 2～5 點的識別字、常數、日誌桶與 `docs/` 目錄；依「日誌桶改名的注意事項」搬移既有日誌。
  7. `Dockerfile` 加 `COPY etl`；`CLAUDE.md`、README 兩份、`docs/` 約 8 份、`backlog/` 中的路徑。
- **產出**：`etl/**` 與上述呼叫端、設定、文件。
- **驗證方式**：
  1. 回歸雙線零變動、`pytest -m "not slow"` 全綠。
  2. 資料更新入口每個 target 以測試資料根冒煙一次。
  3. 分層檢查 0 違規；`grep -rn "core.pipeline" .` 為零；`grep -rni "pipeline"`（排除 `.git/`、`.venv/`、`backlog/` 的歷史紀錄）為零。
  4. 在 `etl/` 放一個暫時檔，觸發 `import sqlite3` 與附錄中每道護欄的違規，每一道都要紅。
  5. `check_doc_paths.py` 通過（`docs/etl/` 的連結全數更新）。
  6. 跑一次資料更新入口後，新日誌出現在 `logs/etl/`；`logs/pipeline/` 不存在；日誌清理入口列得到 `etl` 桶。
- **相依**：Phase1-2。
- **暫緩原因與解除條件**：[台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 會改寫
  tick 的 loader 與 updater，[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 會改寫
  pipeline 呼叫的 DAO 與連線。現在搬的話，那兩份計畫的產出路徑全部要改，而且施作期間
  兩邊互相衝突。**解除條件：兩份計畫中涉及 `core/pipeline/` 的步驟都完成**；若其中一份
  長期不動工，改由使用者裁示是否先搬。
- **2026-10-10 解除條件現況**：
  - `台股tick改用TimescaleDB.md`：16 步已在 `feature/tick-timescaledb` 全數完成（含移除 DolphinDB），
    該分支包含 `feature/post-rehearsal` 的全部內容，要等後者於演練後合併、再合併它，才算進 main。
  - `PostgreSQL遷移計畫.md`：0 / 16，未動工（index 列為 P3、建議在其他重構收斂後再動）。
  - 也就是說 TimescaleDB 那一半合併後，本步驟只剩 PostgreSQL 一個阻擋；屆時適用上一段「長期不動工，改由使用者裁示是否先搬」。

---

## 附：與其他 backlog 文件的交互

本份搬移的目錄也出現在其他文件的產出路徑與驗證指令中。先做本份的話，那些步驟要改用新路徑；
先做那些的話，本份照常進行。

| 文件與步驟 | 影響 | 處理 |
|------------|------|------|
| `回測與實盤入口拆分及架構收斂.md` Phase1-4（策略依名稱載入） | 產出寫的是舊位置的 loader | ✅ 已解決：`回測與實盤入口拆分及架構收斂.md` Phase1-4 先完成，本份 Phase2-2 搬移時 loader 只改一次 |
| `回測與實盤入口拆分及架構收斂.md` Phase3-1（`FillConfig` 移到 `core/models/`） | 驗證指令只掃 `core/strategies`；具體策略 `foreign_selling_reversal_short_strategy.py` 也 import `FillConfig` | ✅ 已解決：`回測與實盤入口拆分及架構收斂.md` Phase3-1 先完成，搬家時只是跟著搬，不需再驗 |
| `回測與實盤入口拆分及架構收斂.md` Phase3-3（禁止回測以外的模組 import `core.backtest.models`） | 規則若只寫「`core.*` 中非 `core.backtest`」，會漏掉頂層 `strategies` | ✅ 已滿足：規則的受限範圍是 `core/backtest/` 與 `tests/` 以外的所有檔案，涵蓋 `strategies/` |
| `回測與實盤入口拆分及架構收斂.md` Phase3（整階段） | 會改 `core/managers` 的 import | ✅ 已依序完成：本份 Phase3-1 排在它之後 |
| `回測與實盤入口拆分及架構收斂.md` Phase1-8（`tasks/` 併入 `apps/`） | Phase5-1 的呼叫端與驗證指令原寫 `tasks/` 的入口 | Phase5-1 已改寫成「資料更新入口」，施作時以當時位置為準；附錄的護欄範圍兩邊都列 |
| `實盤下單架構規劃.md` Phase7-10（`MomentumStrategy1` 改名） | 與 Phase2-2 動同一批策略檔、測試與 launchd 參數 | 同一批施作、先搬家再改名（2026-10-05 決定）；兩者皆已在 `feature/post-rehearsal` 依序完成，待演練後一起部署 |
| `美股ETL與回測架構規劃.md` | 美股策略落點、市場軸 `us/` 規劃開在 `core/pipeline/` | 策略落點已由 Phase2-4 改為頂層 `strategies/stock/`；`core/pipeline/` 的路徑留待 Phase5-1 施作時改 |
| `台股tick改用TimescaleDB.md`、`PostgreSQL遷移計畫.md` | 大量產出路徑在 `core/pipeline/` 底下 | 本份 Phase5-1 暫緩到它們完成，見該步驟 |

---

## 附：搬出 `core/` 的目錄範圍護欄清單

以下護欄以**寫死的目錄清單**決定掃描範圍（2026-09-26 以 grep 盤點，2026-10-10 依 main 與 `feature/post-rehearsal` 重查）。
任何新的頂層套件（本份的 `strategies/`、`etl/`，以及 `回測與實盤入口拆分及架構收斂.md` 的 `apps/`）
都要逐一判斷是否加入。**不加入時要在該處註解寫明理由**，不要只是漏掉。
`apps/` 已於 2026-09-30（`回測與實盤入口拆分及架構收斂.md` Phase1-1）逐項判斷完畢：覆蓋率兩處刻意不加，其餘全數加入。

**下表「目前範圍」是 main 的現況**；標「＋`tasks`」者，部署 `feature/post-rehearsal`（`回測與實盤入口拆分及架構收斂.md` Phase1-8）後會移除 `tasks`，
`apps/` 原本就在每一份清單裡，範圍不會縮小。

| 位置 | 目前範圍（main） | 守的是什麼 |
|------|----------|------------|
| `scripts/check_layer_deps.py` 的 `_SCAN_DIRS` | `core`、`apps`、`strategies`、`frontend`、`strategy_lab`、`scripts`、`tests`＋`tasks` | 分層相依 |
| `scripts/check_layer_deps.py` 的 `_NON_CORE_TOPS` | 同上去掉 `core` | `core/` 不得 import 的頂層套件 |
| `scripts/check_layer_deps.py` 的 `_DB_DRIVER_GUARDED_DIRS` | `core`、`apps`、`strategies`＋`tasks` | `sqlite3` 只能出現在 `core/dao/` |
| `scripts/check_doc_paths.py` 的 `_SCAN_DIRS` | `core`、`apps`、`strategies`、`strategy_lab` 等＋`tasks` | 文件路徑與符號引用 |
| `scripts/check_api_orphan_methods.py` 的 `_SCAN_DIRS` | `core`、`apps`、`strategies`、`strategy_lab` 等＋`tasks` | `core/api/` 公開方法是否有呼叫端 |
| `.pre-commit-config.yaml` 的 `no-stdlib-exc-info` | `core`、`apps`、`strategies`、`scripts`、`strategy_lab`＋`tasks` | loguru 不得用 stdlib `exc_info=` |
| `.pre-commit-config.yaml` 的 `no-doc-step-refs` | `core`、`apps`、`strategies`、`tests`、`frontend`、`strategy_lab`＋`tasks` | 註解不得引用 backlog 步驟編號 |
| `tests/test_entrypoint_and_logging.py` 的 `_GUARDED_PACKAGES` | `core`、`apps`、`strategies`、`scripts`、`strategy_lab`＋`tasks` | 同 `no-stdlib-exc-info`，**兩處必須同步** |
| `tests/test_config_consistency.py` 的 `ENV_SCAN_PATHS` | `core`、`apps`、`strategies`、`frontend`、`scripts`＋`tasks` | 程式讀的環境變數與 `.env.example` 雙向一致 |
| `tests/test_temp_file_cleanup.py` 的 `SCAN_DIRS` | `core`、`apps`、`strategies`、`scripts`＋`tasks` | 暫存檔有清理 |
| `tests/test_strategy_data_access.py` 的 `STRATEGY_DIRS` | `core/strategies`、`strategies` | 策略不得寫資料庫欄位字面值；自我檢查為「載入器找得到的每支策略都在掃描範圍內」（2026-10-01），搬家不再有假綠燈 |
| `pyproject.toml` 的 `[tool.coverage.run] source` | `core`、`strategies` | 覆蓋率報告範圍（入口層刻意不列） |
| `.github/workflows/ci.yml` 的 `--cov` | `--cov=core --cov=strategies` | 同上（CI 端） |
| `pyproject.toml` 的 `[tool.setuptools.packages.find] include` | `core*`、`apps*`、`strategies*`、`tests*`＋`tasks*` | editable 安裝後可 import |
| 根目錄 `Dockerfile` 的 `COPY` | `core`、`apps`、`strategies`＋`tasks` | 映像內容 |
