# core 目錄邊界收斂

## Abstract

- **背景／問題**：`core/` 目前同時裝著三種東西：交易框架本體、使用框架的具體策略，以及另一個
  應用程式——資料管線（`core/pipeline/`，21,759 行，佔 `core/` 近三分之一，比 `live/` 加
  `backtest/` 還多）。業界的慣例是框架不含使用者策略（freqtrade 的 `user_data/strategies/`、
  LEAN 的 `Engine` 與 `Algorithm.Python` 分專案），資料擷取也多半獨立於交易框架之外。
  另外盤點到三個較小的邊界問題：
  1. 回測的缺日診斷 import 了 `core.pipeline.shared.date_planner`，讀 ETL 的中間進度檔判斷
     休市——這是 `core/` 其餘部分對 `pipeline` 的**唯一一條**依賴，也是 `pipeline` 搬不出去
     的唯一障礙。
  2. `core/managers/` 只裝三支 `position_manager.py`，名稱籠統，容易變成雜物間。
  3. `core/Dockerfile` 打包的是 `run.py`＋`core`＋`tasks` 整個後端，不只是 `core`。
- **目標**：`core/` 只剩交易框架本體。具體策略搬到頂層 `strategies/`、資料管線搬到頂層
  `pipeline/`，兩者都只能單向依賴 `core`；策略契約（抽象基底）留在 `core/strategies/`。
  完成後頂層的邊界是：`core/` 框架、`strategies/` 正式策略、`pipeline/` 資料擷取、
  `strategy_lab/` 研究、`apps/` 入口（由 [回測與實盤入口拆分及架構收斂.md](回測與實盤入口拆分及架構收斂.md) 建立）。
- **範圍界線**：**不做**
  1. 不改任何交易、成本、ETL 邏輯——本份只搬位置與改 import，回歸雙線一律零變動。
  2. 不改策略類別名稱：類別名即 `--strategy` 參數，排程與 compose 依賴它。
  3. 不把策略拆成獨立 repo 或獨立發布的套件；要做另開文件。
  4. 不動 `core/` 其餘子套件的分層（`api`、`dao`、`models`、`market`、`broker`、`adapters`、
     `execution`、`portfolio`、`backtest`、`live`、`datafeed`、`utils`、`config` 都屬框架本體）。
  5. `datafeed` 分成 `core/datafeed/`（契約）與 `backtest/`、`live/` 各自的實作是正確模式，不動。
- **驗收標準**：
  1. `core/` 內對 `core.pipeline` 與頂層 `strategies` 的 import 歸零，並由 `check_layer_deps.py` 守住。
  2. `core/strategies/` 只剩契約（`base.py`、`stock/base.py`、`futures/base.py` 與套件門面）。
  3. `core/managers/` 改名為 `core/position/`；`Dockerfile` 移到專案根目錄。
  4. Phase5 解除暫緩後，`core/pipeline/` 搬到頂層 `pipeline/`。
  5. 〈附：搬出 `core/` 的目錄範圍護欄清單〉的每一處都涵蓋新的頂層套件——**搬完測試全綠不代表
     護欄還在**，範圍寫死 `core` 的護欄會靜默少掃一塊。
  6. 每一步都通過回歸雙線（`./scripts/run_regression.sh`）、`pytest -m "not slow"`、
     三支閘門腳本與 `check_doc_paths.py`。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase1-1 | 回測缺日診斷改用 `MarketHolidayAPI` | `core/backtest/datafeed/tw/stock_datafeed.py`、對應測試 | `core/backtest` 對 `core.pipeline` 的 import 歸零；2025–2026 區間的休市日可被歸因 | ⬜ | 只影響起跑時的 log，不影響交易日判定 |
| Phase1-2 | 分層檢查禁止框架 import `core.pipeline` | `scripts/check_layer_deps.py` | 刻意加一條違規 import，檢查要紅 | ⬜ | 相依 Phase1-1 |
| Phase2-1 | 建立頂層 `strategies/` 套件，並讓分層與目錄範圍護欄涵蓋它 | `strategies/__init__.py`、`scripts/check_*.py`、`.pre-commit-config.yaml`、`tests/` 四支護欄、`pyproject.toml`、`core/Dockerfile`、CI | `core` 內 import `strategies` 時檢查要紅；在 `strategies/` 放一個違規，每道護欄各自要紅 | ⬜ | 護欄清單見文末附錄 |
| Phase2-2 | 具體策略與 `StrategyLoader` 搬到 `strategies/` | `strategies/{stock,futures}/*.py`、`strategies/loader.py`、`run.py`、`tests/` | 回歸雙線零變動；`--strategy` 列表與改前相同；策略欄位字面值護欄仍掃得到每一支策略 | ⬜ | 避開 `實盤下單架構規劃.md` Phase7-1 演練時段；等另一支開發中的策略先落地；`test_strategy_data_access.py` 不改會變假綠燈 |
| Phase2-3 | `core/strategies/` 收斂成只剩契約 | `core/strategies/**`、`scripts/check_layer_deps.py` | 目錄內只剩 base 與門面；門面檢查通過 | ⬜ | 相依 Phase2-2 |
| Phase2-4 | 策略相關文件與規則入口同步 | `.claude/skills/develop-strategy/`、`strategy_lab/CLAUDE.md`、`CLAUDE.md`、README、`docs/` | `check_doc_paths.py`；全文 grep 舊路徑只剩契約 | ⬜ | 相依 Phase2-3 |
| Phase3-1 | `core/managers/` 改名為 `core/position/` | `core/position/**`、各 import 端、`pyproject.toml`、`scripts/check_layer_deps.py`、文件 | 回歸雙線零變動；全文 grep `core.managers` 為零 | ⬜ | 排在 `回測與實盤入口拆分及架構收斂.md` Phase3 之後 |
| Phase4-1 | `core/Dockerfile` 移到專案根目錄 | `Dockerfile`、`docker-compose.yml`、`.github/workflows/ci.yml`、文件 | CI 的映像建置與冒煙通過；`docker compose build` 成功 | ⬜ | 排在 Phase2、Phase5 之後，避免 COPY 清單改兩次 |
| Phase5-1 | `core/pipeline/` 搬到頂層 `pipeline/` | `pipeline/**`、`tasks/update_db.py`、`scripts/`、`tests/`、`pyproject.toml`、目錄範圍護欄、文件 | 回歸雙線零變動；`tasks/update_db.py` 各 target 冒煙；`core` 對 `pipeline` 的 import 為零；附錄護欄全數涵蓋 `pipeline/` | ⏸ | 等 TimescaleDB 與 PostgreSQL 兩份計畫的 pipeline 改動落地，避免搬兩次 |

---

## Phase1：切斷框架對資料管線的依賴

### Phase1-1. 回測缺日診斷改用 `MarketHolidayAPI` ⬜

- **目的**：`TwStockDataFeed` 的缺日診斷（「區間內有 N 個平日沒有行情」那段 log）
  import 了 `core.pipeline.shared.date_planner` 的兩樣東西：
  1. `DatePlanner.generate_weekdays()`：產生區間內的平日——純日期運算，與 ETL 無關。
  2. `DateProgressStore("price").no_data`：讀 ETL 的進度檔，取「已向交易所確認沒有資料」
     的日期當作休市。**回測的診斷因此依賴 ETL 的中間狀態檔**。

  這是 `core/` 其餘部分對 `core.pipeline` 的唯一一條依賴（2026-09-26 以 grep 確認），
  切掉它，Phase5-1 的搬家就不會拖到框架。

  **現況的診斷其實已經失效**（2026-09-26 唯讀實查本機資料）：`DateProgressStore("price").no_data`
  是空的——這份紀錄只在 ETL 向站方問到「查無資料」時才寫入，而歷史資料多半不是這樣補進來的。
  所以現在任何區間的缺日診斷都停在「只報數字、不下判斷」。`market_holiday` 表則已涵蓋
  2025、2026 兩個年度，共 36 個平日休市日。改用它不只是切依賴，還會讓這兩年的診斷真正可用。

- **做法**：
  1. 休市日改由 `MarketHolidayAPI.get_closures()` 取得——`market_holiday` 表是交易所公告的
     正式休市日，語意比「ETL 問過、站方回查無資料」更直接。
  2. 年度未入庫時（`get_covered_years()` 不含該年）維持現行的保守行為：只報數字、不下判斷。
  3. 平日產生改成就地的日期運算，或移到 `core/market/tw/market_calendar.py`，不再 import
     `DatePlanner`。
  4. 颱風假等臨時休市：確認 `market_holiday` 的來源是否涵蓋（交易所年初公告的行事曆
     不會有颱風假）。不涵蓋的話，這類日期在診斷中會被列為「無法歸因」——這是正確的保守行為，
     但要在 docstring 寫明，不要讓讀者以為它漏了。

- **產出**：`core/backtest/datafeed/tw/stock_datafeed.py`、對應測試。

- **驗證方式**：
  1. `grep -rn "core.pipeline" core --include=*.py`，除了 `core/pipeline/` 自身之外為零。
  2. 以現有資料跑一次 2025 年區間的回測：改前 log 為「只報數字」，改後國定假日被歸因為休市，
     只剩真正的缺口列為無法歸因。**診斷輸出會改變，這是預期的**，在本步驟記錄改前改後的 log。
  3. 新測試：區間落在已涵蓋年度時歸因休市；跨到未涵蓋年度時，那一段只報數字不下判斷。
  4. 回歸雙線零變動（這段只寫 log，不影響交易日判定）。

- **相依**：無。

### Phase1-2. 分層檢查禁止框架 import `core.pipeline` ⬜

- **目的**：Phase1-1 切掉之後，要有機器檢查防止依賴長回來，否則 Phase5-1 搬家時才發現。
- **做法**：在 `scripts/check_layer_deps.py` 加一條規則：`core.pipeline` 以外的 `core.*` 模組
  不得 import `core.pipeline.*`。`tasks/`、`scripts/`、`tests/` 不受限。
- **產出**：`scripts/check_layer_deps.py`。
- **驗證方式**：在 `core/backtest/` 任一檔刻意加一行 `from core.pipeline.shared import ...`，
  檢查要紅；移除後轉綠。
- **相依**：Phase1-1。

---

## Phase2：具體策略搬出 `core/`

目標結構：

```
core/strategies/          # 策略契約：引擎、factory、報表都要認得的介面
    base.py
    stock/base.py
    futures/base.py
strategies/               # 具體策略：使用框架的程式，不屬於框架
    loader.py             # 原 core/strategies/strategy_loader.py
    stock/momentum_strategy_1.py
    stock/foreign_sell_short_day_trade_strategy.py
    futures/momentum_futures_strategy.py
```

**契約為什麼留在 `core/`**：回測引擎、實盤引擎、`core/execution/`、報表等十幾個模組都要認得
策略介面；契約搬出去，`core` 就得反過來 import 外部套件。

**`StrategyLoader` 為什麼跟著搬**：它的職責是掃描具體策略，而目前唯一的呼叫端是 `run.py`
（2026-09-26 確認）。留在 `core/` 的話，框架就必須知道頂層 `strategies/` 的存在，違反單向依賴。

### Phase2-1. 建立頂層 `strategies/` 套件，並讓分層與目錄範圍護欄涵蓋它 ⬜

- **目的**：先把新套件與守門規則建好，搬檔那一步才有檢查可以驗。**這一步的重點是護欄範圍**：
  專案有十幾道護欄以寫死的目錄清單決定掃描範圍，多數只列 `core`、`tasks`、`scripts`、
  `strategy_lab`。策略搬到頂層之後，這些護欄會靜默少掃一塊，測試照樣全綠。
- **做法**：
  1. 新增 `strategies/__init__.py`、`strategies/stock/__init__.py`、`strategies/futures/__init__.py`。
  2. `scripts/check_layer_deps.py`：
     - `_SCAN_DIRS` 加上 `strategies`。
     - `_LAYER_RULES` 登記 `("strategies", 7, "策略層", False)`（沿用現行 `core.strategies` 的層級）。
     - `_NON_CORE_TOPS` 加上 `strategies`——這就是「`core/` 內 import 到它即為反向相依」的機制。
  3. 附錄清單中其餘每一處都加上 `strategies`。
  4. `pyproject.toml` 的 `[tool.setuptools.packages.find] include` 加上 `strategies*`；
     `[tool.coverage.run] source` 加上 `strategies`。
  5. `core/Dockerfile` 加上 `COPY strategies /app/strategies`。
  6. **命名確認**：頂層 `strategies` 與 `strategy_lab/strategies/` 同名。從專案根目錄執行時
     不會衝突，但在 `strategy_lab/` 目錄內直接跑 `python` 時，`import strategies` 會解析到
     研究那一份。動手前確認 `strategy_lab/` 的執行慣例（`strategy_lab/CLAUDE.md` 規定用 `-m`
     從根目錄執行）足以排除這個情況；不足的話改名（例如 `trading_strategies/`）並回頭更新本文件。
- **產出**：上述各處。
- **驗證方式**：
  1. 在 `core/` 任一檔刻意 import `strategies`，分層檢查要紅。
  2. 在 `strategies/` 放一個暫時檔，逐一觸發附錄中每道護欄的違規（例如 stdlib `exc_info=`、
     註解引用 backlog 步驟編號），每一道都要紅。
  3. `uv sync` 後從專案根目錄 `python -c "import strategies"` 成功。
  4. 映像建置後 `docker run --rm alphaedge-core python -c "import strategies"` 成功。
- **相依**：無。若 `回測與實盤入口拆分及架構收斂.md` Phase1-1 已建立 `apps/`，
  沿用它登記入口層的寫法（`apps/` 同樣要加進附錄的護欄清單）。

### Phase2-2. 具體策略與 `StrategyLoader` 搬到 `strategies/` ⬜

- **目的**：把具體策略從框架中移出，這是本份文件的主體。
- **做法**：
  1. 以 `git mv` 搬移三支策略（及屆時新增的策略），保留歷史。
  2. `core/strategies/strategy_loader.py` → `strategies/loader.py`，掃描目標改成頂層
     `strategies` 套件；逐模組隔離、同名拋錯兩條既有行為不變。
  3. 呼叫端改 import：`run.py`（或屆時的 `apps/`）、約 25 個測試檔、`scripts/` 中引用者。
  4. `check_layer_deps.py` 的 `_INSTRUMENT_AXIS_PACKAGES` 加上 `strategies`，
     讓商品軸命名檢查繼續涵蓋具體策略。
  5. **`tests/test_strategy_data_access.py` 必須同步改，否則變成假綠燈**：它是「策略不得直接
     寫資料庫欄位字面值」的護欄，掃描目錄寫死為 `core/strategies`，自驗門檻是「至少 6 個檔」。
     搬完之後 `core/strategies/` 剛好剩 3 個 `__init__.py` 加 3 個 `base.py`，**正好 6 個**——
     門檻照樣通過，實際上一支具體策略都沒掃到。做法：
     - `STRATEGY_DIR` 改成同時掃 `strategies/` 與 `core/strategies/`（契約也不該寫欄位字面值）。
     - 自驗改成「每一支 `StrategyLoader` 載入的策略，其原始檔都在掃描清單內」，不再用檔數門檻。
- **產出**：`strategies/**`、`run.py`、`tests/`、`scripts/check_layer_deps.py`。
- **驗證方式**：
  1. 回歸雙線零變動。
  2. `StrategyLoader.load_strategies()` 回傳的類別名稱集合與改前完全相同。
  3. `pytest -m "not slow"` 全綠、分層檢查 0 違規。
  4. 在 `strategies/stock/` 任一策略刻意寫一個欄位字面值（例如 `"收盤價"`），
     `test_strategy_data_access.py` 要紅。
- **相依**：Phase2-1。另有三個施作時機限制：
  1. **避開 `實盤下單架構規劃.md` Phase7-1 模擬演練的排程時段**：模組路徑改變後，
     常駐的實盤行程必須重啟才載得到新路徑。
  2. **建議排在 `回測與實盤入口拆分及架構收斂.md` Phase1-4（策略依名稱載入）之後**，
     loader 只需改寫一次；若先做本步，該步驟改在 `strategies/loader.py` 上進行。
  3. 施作前確認 `core/strategies/` 沒有開發中未 commit 的變更（2026-09-26 就有一支
     `trust_momentum_swing_strategy.py` 與 `core/strategies/README.md` 正在修改），
     避免搬移與開發互相覆蓋。

### Phase2-3. `core/strategies/` 收斂成只剩契約 ⬜

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

### Phase2-4. 策略相關文件與規則入口同步 ⬜

- **目的**：寫策略的入口文件全部指向新位置，否則下一支策略會照舊文件寫回 `core/`。
- **做法**：更新以下檔案中的策略路徑：
  1. `.claude/skills/develop-strategy/SKILL.md`（權威檔，含 frontmatter 的 `description`）。
     `.cursor/rules/strategy-development-sdd.mdc` 的本文只是指標不用改，但它 frontmatter 的
     `description` 寫著 `core/strategies/{stock,futures}/`，那是 Cursor 判斷何時套用規則的依據，
     **要一起改**，否則在新目錄寫策略時規則不會觸發。
  2. `CLAUDE.md` 的 skill 表觸發時機欄。
  3. `strategy_lab/CLAUDE.md`、`strategy_lab/README.md`：「成熟策略搬到 `core/strategies/stock/`」改為 `strategies/stock/`。
  4. `core/strategies/README.md` 移到 `strategies/README.md`，並修正內文。
  5. `README.md`、`README_en.md` 的專案樹與說明；`docs/` 約 7 份。
  6. `backlog/` 內提到 `core/strategies/stock/` 當作**新策略落點**的敘述（例如
     `美股ETL與回測架構規劃.md`），改為新位置。
- **產出**：上述文件。
- **驗證方式**：`check_doc_paths.py` 通過；`grep -rn "core/strategies/\(stock\|futures\)/[a-z_]*strategy" --include=*.md .` 為零。
- **相依**：Phase2-3。

---

## Phase3：`core/managers/` 改名

### Phase3-1. `core/managers/` 改名為 `core/position/` ⬜

- **目的**：`core/managers/` 底下只有 `base/`、`stock/`、`futures/` 三支 `position_manager.py`，
  職責是部位記帳（FIFO 拆單、成本攤提、損益）。`managers` 這個名稱不說明職責，
  之後很容易有不相干的 `XxxManager` 被丟進來。
- **做法**：
  1. `git mv core/managers core/position`，模組名維持 `position_manager.py`。
  2. 更新約 14 個 `core/` 檔與 14 個測試檔的 import。
  3. `pyproject.toml` 中說明 namespace package 的註解提到 `core.managers`，一併改名。
  4. `check_layer_deps.py` 的分層登記與 `_INSTRUMENT_AXIS_PACKAGES`。
  5. README 兩份、`docs/` 約 6 份、`backlog/` 中的路徑。
- **產出**：`core/position/**` 與上述呼叫端、文件。
- **驗證方式**：回歸雙線零變動；`grep -rn "core[./]managers" .` 為零；分層檢查 0 違規。
- **相依**：建議排在 `回測與實盤入口拆分及架構收斂.md` Phase3 之後——該階段要改
  `core/managers` 對 `core.backtest.models` 的 import，兩邊同時動會互相衝突。

---

## Phase4：`Dockerfile` 移到專案根目錄

### Phase4-1. `core/Dockerfile` 移到專案根目錄 ⬜

- **目的**：這份 Dockerfile 打包的是 `run.py`、`core`、`tasks`（Phase2 後再加 `strategies`、
  Phase5 後再加 `pipeline`），也就是整個後端。放在 `core/` 底下會讓人以為它只打包框架。
  `frontend/Dockerfile` 只打包前端，留在原位。
- **做法**：`git mv core/Dockerfile Dockerfile`，更新約 11 個引用處：`docker-compose.yml`
  （兩個 service 的 `dockerfile:`）、`.github/workflows/ci.yml`、`docs/deployment/`、README。
- **產出**：`Dockerfile` 與上述引用處。
- **驗證方式**：CI 的映像建置與冒煙通過；本機 `docker compose build` 成功；
  `grep -rn "core/Dockerfile" .` 為零。
- **相依**：排在 Phase2-1 與 Phase5-1 之後，避免 COPY 清單與引用處各改兩次。
  Phase5 長期暫緩的話，可在 Phase2 完成後先做，Phase5-1 再補一行 COPY。

---

## Phase5：資料管線搬出 `core/`

### Phase5-1. `core/pipeline/` 搬到頂層 `pipeline/` ⏸

- **目的**：`core/pipeline/`（爬蟲、清洗、入庫、updater）是寫資料庫的另一個應用程式，
  交易框架只讀資料庫。兩者的執行時機（排程與手動 vs 回測與實盤）與失敗模式都不同。
  搬出去之後，`pipeline` 單向依賴 `core`（`dao`、`config`、`broker` 的 Shioaji session），
  `core` 完全不知道 `pipeline` 存在。
- **做法**：
  1. `git mv core/pipeline pipeline`，import 由 `core.pipeline.*` 改為 `pipeline.*`。
  2. 呼叫端：`tasks/update_db.py`、`scripts/` 約 5 支、約 56 個測試檔。
  3. `pyproject.toml`：`include` 加 `pipeline*`；`per-file-ignores` 中 `core/pipeline/...` 的路徑改名。
  4. `check_layer_deps.py`：Phase1-2 的規則改為把 `pipeline` 加進 `_NON_CORE_TOPS`；
     `_MARKET_AXIS_PACKAGES` 中的 `core/pipeline` 改名；**`_DB_DRIVER_GUARDED_DIRS` 目前是
     `("core", "tasks")`，要加上 `pipeline`**——pipeline 是寫資料庫最多的地方，漏掉等於
     「`sqlite3` 只能出現在 `core/dao/`」這條規則對它失效。
  5. 附錄清單中其餘每一處都加上 `pipeline`。
  6. `Dockerfile` 加 `COPY pipeline`；`CLAUDE.md`、README 兩份、`docs/` 約 8 份、`backlog/` 中的路徑。
- **產出**：`pipeline/**` 與上述呼叫端、設定、文件。
- **驗證方式**：
  1. 回歸雙線零變動、`pytest -m "not slow"` 全綠。
  2. `tasks/update_db.py` 每個 target 以測試資料根冒煙一次。
  3. 分層檢查 0 違規；`grep -rn "core.pipeline" .` 為零。
  4. 在 `pipeline/` 放一個暫時檔，觸發 `import sqlite3` 與附錄中每道護欄的違規，每一道都要紅。
- **相依**：Phase1-2。
- **暫緩原因與解除條件**：[台股tick改用TimescaleDB.md](台股tick改用TimescaleDB.md) 會改寫
  tick 的 loader 與 updater，[PostgreSQL遷移計畫.md](PostgreSQL遷移計畫.md) 會改寫
  pipeline 呼叫的 DAO 與連線。現在搬的話，那兩份計畫的產出路徑全部要改，而且施作期間
  兩邊互相衝突。**解除條件：兩份計畫中涉及 `core/pipeline/` 的步驟都完成**；若其中一份
  長期不動工，改由使用者裁示是否先搬。

---

## 附：與其他 backlog 文件的交互

本份搬移的目錄也出現在其他文件的產出路徑與驗證指令中。先做本份的話，那些步驟要改用新路徑；
先做那些的話，本份照常進行。

| 文件與步驟 | 影響 | 處理 |
|------------|------|------|
| `回測與實盤入口拆分及架構收斂.md` Phase1-4（策略依名稱載入） | 產出寫的是 `core/strategies/strategy_loader.py` | 本份 Phase2-2 先做的話，改在 `strategies/loader.py` 上施作 |
| `回測與實盤入口拆分及架構收斂.md` Phase3-1（`FillConfig` 移到 `core/models/`） | 驗證指令 `grep -rn "fill_model import" core/strategies` 只掃 `core/`；具體策略 `foreign_sell_short_day_trade_strategy.py` 也 import `FillConfig` | 本份 Phase2-2 先做的話，驗證指令要加掃 `strategies/` |
| `回測與實盤入口拆分及架構收斂.md` Phase3-3（禁止回測以外的模組 import `core.backtest.models`） | 規則若只寫「`core.*` 中非 `core.backtest`」，會漏掉頂層 `strategies` | 規則的適用範圍要包含 `strategies` |
| `回測與實盤入口拆分及架構收斂.md` Phase3（整階段） | 會改 `core/managers` 的 import | 本份 Phase3-1 排在它之後 |
| `美股ETL與回測架構規劃.md` | 美股策略的落點寫的是 `core/strategies/stock/`；市場軸 `us/` 規劃開在 `core/pipeline/` | 本份 Phase2-4 與 Phase5-1 施作時一併改路徑 |
| `台股tick改用TimescaleDB.md`、`PostgreSQL遷移計畫.md` | 大量產出路徑在 `core/pipeline/` 底下 | 本份 Phase5-1 暫緩到它們完成，見該步驟 |

---

## 附：搬出 `core/` 的目錄範圍護欄清單

以下護欄以**寫死的目錄清單**決定掃描範圍（2026-09-26 以 grep 盤點）。任何新的頂層套件
（本份的 `strategies/`、`pipeline/`，以及 `回測與實盤入口拆分及架構收斂.md` 的 `apps/`）
都要逐一判斷是否加入。**不加入時要在該處註解寫明理由**，不要只是漏掉。

| 位置 | 目前範圍 | 守的是什麼 |
|------|----------|------------|
| `scripts/check_layer_deps.py` 的 `_SCAN_DIRS` | `core`、`tasks`、`frontend`、`strategy_lab`、`scripts`、`tests` | 分層相依 |
| `scripts/check_layer_deps.py` 的 `_NON_CORE_TOPS` | 同上加 `run` | `core/` 不得 import 的頂層套件 |
| `scripts/check_layer_deps.py` 的 `_DB_DRIVER_GUARDED_DIRS` | `core`、`tasks` | `sqlite3` 只能出現在 `core/dao/` |
| `scripts/check_doc_paths.py` 的 `_SCAN_DIRS` | `core`、`tasks`、`strategy_lab` 等 | 文件路徑與符號引用 |
| `scripts/check_api_orphan_methods.py` 的 `_SCAN_DIRS` | `core`、`tasks`、`strategy_lab` 等 | `core/api/` 公開方法是否有呼叫端 |
| `.pre-commit-config.yaml` 的 `no-stdlib-exc-info` | `core`、`tasks`、`scripts`、`strategy_lab` | loguru 不得用 stdlib `exc_info=` |
| `.pre-commit-config.yaml` 的 `no-doc-step-refs` | `core`、`tasks`、`tests`、`frontend`、`strategy_lab` | 註解不得引用 backlog 步驟編號 |
| `tests/test_entrypoint_and_logging.py` 的 `_GUARDED_PACKAGES` | `core`、`tasks`、`scripts`、`strategy_lab` | 同 `no-stdlib-exc-info`，**兩處必須同步** |
| `tests/test_config_consistency.py` 的 `ENV_SCAN_PATHS` | `core`、`tasks`、`frontend`、`scripts` | 程式讀的環境變數與 `.env.example` 雙向一致 |
| `tests/test_temp_file_cleanup.py` 的 `SCAN_DIRS` | `core`、`tasks`、`scripts` | 暫存檔有清理 |
| `tests/test_strategy_data_access.py` 的 `STRATEGY_DIR` | `core/strategies` | 策略不得寫資料庫欄位字面值（見 Phase2-2） |
| `pyproject.toml` 的 `[tool.coverage.run] source` | `core` | 覆蓋率報告範圍 |
| `.github/workflows/ci.yml` 的 `--cov=core` | `core` | 同上（CI 端） |
| `pyproject.toml` 的 `[tool.setuptools.packages.find] include` | `core*`、`tasks*`、`tests*` | editable 安裝後可 import |
| `core/Dockerfile` 的 `COPY` | `run.py`、`core`、`tasks` | 映像內容 |
