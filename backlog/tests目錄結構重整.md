# tests 目錄結構重整

## Abstract

- **背景／問題**：2026-10-13 實查，`tests/` 共 185 支測試檔、2,390 條測試、62,439 行，與原始碼
  （`core/`＋`apps/`＋`frontend/` 共 69,107 行）的比例約 0.9 : 1，規模本身健康。問題在組織方式：
  `backtest/`、`live/`、`portfolio/`、`analysis/` 四個子目錄已鏡像 `core/` 同名套件，**其餘 104 支
  直接平鋪在 `tests/` 根目錄**，對應 `core/api`、`core/dao`、`core/pipeline`、`core/models`、
  `frontend/`、`scripts/` 等十幾個來源。改一個模組要找對應測試得靠檔名猜；守門測試（`test_readme_parity.py`、
  `test_naming_axes.py`、`test_check_layer_deps_*.py` 等）與業務測試混在一起，讀的人分不出哪些在測功能、
  哪些在掃 repo 規則。
- **目標**：`tests/` 單層鏡像原始碼目錄（與 pytest 官方建議及 freqtrade、hummingbot 的做法一致），
  快慢仍由既有 marker（`slow`、`shioaji_sim`）區分，不另分 `unit/`／`integration/` 兩層。
  `pipeline/` 因 40 支過多再依角色分 `cleaner/`、`crawler/`、`updater/`、`loader/`、`shared/` 一層。
  量化系統特有的兩類測試獨立出來：回測 vs 實盤的 `parity/`，以及連 Shioaji 模擬環境的 `live/sim/`。
  工具腳本的測試放 `scripts/`，掃整個 repo 的規則檢查放 `meta/`，兩者分開。完整目標結構見〈附一〉，
  104 支檔案的歸類表見〈附二〉（依各檔實際 import 的模組歸類，不是依檔名）。
- **範圍界線**：**不做**
  1. 不改任何測試內容、斷言或 fixture——本份只 `git mv` 與改路徑引用，測試數量前後一致（2,390 條）。
  2. 不動 `backtest/`、`live/`、`portfolio/`、`analysis/` 既有檔案的位置（只有 `live/test_shioaji_sim.py`
     移入 `live/sim/`，以及根目錄少數檔案移入）。
  3. 不抽 NautilusTrader 式的 `test_kit/` 共用 factory 目錄：三個 `conftest.py` 已扮演此角色，
     搬完若發現明顯重複再另立工作。
  4. 不把守門測試改成 lint 或 CI 腳本——它們留在 `pytest` 內，只是集中到 `meta/`。
  5. 不改 `tests/pipeline/` 以外任何目錄名來配合 [core目錄邊界收斂.md](core目錄邊界收斂.md) 的
     搬移；`tests/pipeline/` 改名 `tests/etl/` 隨該文件 Phase5-1 一起做，不在本份做。
- **驗收標準**：
  1. `tests/` 根目錄只剩 `conftest.py`、`__init__.py`、`entry_sandbox.py` 與子目錄。
  2. `pytest --collect-only -q` 的測試數量與搬移前一致；`pytest -m "not slow"` 全綠；CI 全綠。
  3. 全 repo grep `tests/test_` 歸零（CI、pre-commit、Dockerfile、`pyproject.toml`、`docs/`、程式碼註解、
     `.claude/skills/`、tests 內互相引用），`scripts/check_doc_paths.py` 通過。
  4. `docs/dev/` 有一份測試目錄說明，寫明每個子目錄放哪類測試、新測試怎麼決定放哪。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase1-1 | 建立目標目錄骨架 | `tests/{api,dao,pipeline/*,models,market,adapters,position,strategies,strategy_lab,parity,apps,frontend,config,scripts,meta,live/sim}/__init__.py` | `pytest --collect-only -q` 數量不變 | ⬜ | — |
| Phase2-1 | 搬資料存取層：`api/`、`dao/` | 19 支測試、對應外部引用 | `pytest -m "not slow"` 全綠；grep 該批舊路徑歸零 | ⬜ | — |
| Phase2-2 | 搬 `pipeline/` 五個子目錄 | 40 支測試、對應外部引用 | 同上 | ⬜ | 最大的一批；`docs/pipeline/*.md` 引用最多 |
| Phase2-3 | 搬領域模型與策略層：`models/`、`market/`、`adapters/`、`position/`、`strategies/`、`strategy_lab/` | 15 支測試、對應外部引用 | 同上 | ⬜ | — |
| Phase2-4 | 搬引擎與對稱性：`backtest/`、`parity/`、`live/sim/` | 5 支測試、對應外部引用 | 同上；`pytest -m shioaji_sim --collect-only` 仍收得到 | ⬜ | — |
| Phase2-5 | 搬入口、介面與守門：`apps/`、`frontend/`、`config/`、`scripts/`、`meta/` | 26 支測試、CI、pre-commit、`frontend/Dockerfile` | 同上；pre-commit 全套通過；CI 全綠 | ⬜ | CI 與 pre-commit 寫死的路徑都在這批 |
| Phase3-1 | 全 repo 引用收尾與空目錄清理 | 全 repo | grep `tests/test_` 歸零；`check_doc_paths.py` 通過；`tests/downloads/` 處置 | ⬜ | — |
| Phase3-2 | 測試目錄說明文件 | `docs/dev/test-layout.md`、README 文件索引、`.claude/skills/health-check/SKILL.md` | `check_doc_paths.py` 通過；`test_readme_parity.py` 通過 | ⬜ | — |

---

## Phase1：準備

### Phase1-1. 建立目標目錄骨架 ⬜

- **目的**：先把所有目標子目錄連同 `__init__.py` 建好，讓後續每一批搬移只做 `git mv`，不夾帶建目錄的雜訊。
- **做法**：依〈附一〉建立目錄；每個目錄放空的 `__init__.py`。`tests/` 已是套件（有 `__init__.py`），
  子目錄必須跟進，否則不同目錄若出現同名測試檔，pytest 的 rootdir-relative import 會互相覆蓋。
- **產出**：約 22 個 `__init__.py`。
- **驗證方式**：`pytest --collect-only -q | tail -1` 的數量與搬移前相同（空套件不影響收集）。
- **相依**：見本文件〈施作時機〉——要等長壽分支合併後才開工。

## Phase2：分批搬移

每一批的做法相同，只有範圍不同：

1. 依〈附二〉對該批檔案逐一 `git mv tests/test_x.py tests/<dir>/test_x.py`（保留 rename 歷史）。
2. 更新該批的外部引用（〈附三〉列出 2026-10-13 的實查清單，開工時要重新 grep 一次）：
   - 程式碼註解與 docstring 中「對應測試見 `tests/test_x.py`」的指路（CLAUDE.md §2.1 允許註解指向測試，
     只是路徑要跟著改）。
   - `docs/**/*.md`、`core/strategies/README.md`、`scripts/manual/README.md` 的路徑。
   - tests 內互相引用（例如 `tests/backtest/test_futures_backtest.py` 註解指向 `tests/test_futures_cost.py`）。
3. 跑 `pytest -m "not slow"` 與 `uv run python scripts/check_doc_paths.py`，全綠才 commit。
4. 每批一個 commit，訊息寫明搬了哪些目錄。

### Phase2-1. 搬資料存取層：`api/`、`dao/` ⬜

- **目的**：把 `core/api`、`core/dao` 的測試歸位。
- **做法**：〈附二〉的 `api/`（6 支）與 `dao/`（13 支）。`test_sqlite_error_semantics.py` 同時碰 API 與 DAO，
  測的是 SQLite 錯誤如何被 DAO 層翻譯，歸 `dao/`；`test_futures_chip.py` 測的是 `FuturesChipAPI`，歸 `api/`。
- **產出**：19 支測試的新位置；`tests/conftest.py` 第 196 行附近指向 `test_dao_live_trade.py` 的註解。
- **驗證方式**：`pytest tests/api tests/dao -q` 全綠；`pytest -m "not slow"` 全綠。
- **相依**：Phase1-1。

### Phase2-2. 搬 `pipeline/` 五個子目錄 ⬜

- **目的**：根目錄 104 支裡有 40 支是資料管線測試，是扁平化的主因。
- **做法**：依角色而非鏡像 `core/pipeline/tw/`——`tw/` 底下本身也是幾十支平鋪，鏡像它沒有幫助：
  - `cleaner/`（13 支）：各 `*Cleaner`、股票代號補零、除權息偵測與守門、財報規格。
  - `crawler/`（8 支）：各 `*Crawler`、TPEX 分頁與日期區間、request 工具、批次不可達。
  - `updater/`（12 支）：各 `*Updater`、節流、缺口回補、交易日守門、休市日、期貨連續合約。
  - `loader/`（6 支）：各 `*Loader`、批次載入、失敗回報。
  - `shared/`（1 支）：pipeline 錯誤收斂。
  歸類有疑義時以「被測類別的名稱後綴」為準（`Cleaner`→`cleaner/`），沒有明確後綴的看它主要 patch 哪一層。
- **產出**：40 支測試的新位置；`docs/pipeline/corporate-action.md`、`docs/pipeline/etl-ingestion.md`、
  `core/config/settings.py`、`core/pipeline/tw/cleaners/corporate_action_detector.py`、
  `core/pipeline/tw/updaters/futures_continuous_updater.py`、`pyproject.toml` 第 149 行附近、
  `scripts/manual/README.md` 的引用。
- **驗證方式**：`pytest tests/pipeline -q` 全綠；`pytest -m "not slow"` 全綠；`check_doc_paths.py` 通過。
- **相依**：Phase1-1。**與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1 的關係**：
  該步驟會把 `core/pipeline/` 搬到頂層 `etl/`，屆時 `tests/pipeline/` 一併改名 `tests/etl/`，
  由該步驟負責，本份不預先改名。

### Phase2-3. 搬領域模型與策略層 ⬜

- **目的**：`core/models`、`core/market`、`core/adapters`、`core/position`、`core/strategies`、`strategy_lab/` 的測試歸位。
- **做法**：〈附二〉的 `models/`（3）、`market/`（1）、`adapters/`（3）、`position/`（1）、`strategies/`（6）、
  `strategy_lab/`（1）。
  - `test_futures_session_combine.py` 同時 import adapters 與 `backtest.datafeed`，測的是日夜盤合併的 adapter 行為，歸 `adapters/`。
  - `test_ridge.py` import 的是 `strategy_lab.strategies.tsmc_overnight_signal.ridge`，不在 `core/`，
    歸 `strategy_lab/`（`tests/analysis/` 對應的是 `core/analysis`，不混放）。
  - `test_strategy_data_access.py` 雖掃策略目錄，但它是「策略層不得出現 DB 欄位字面值」的 repo 規則掃描，
    歸 Phase2-5 的 `meta/`，不放 `strategies/`。
- **產出**：15 支測試的新位置；`core/market/tw/futures_calendar.py`、`core/utils/constant/order.py`、
  `core/broker/tw/shioaji_*.py`、`docs/backtest/multi-market-engine.md`、`tests/test_futures_position_manager.py`
  與 `tests/backtest/test_futures_backtest.py` 內指向 `test_futures_cost.py` 的註解。
- **驗證方式**：各目錄 `pytest` 全綠；`pytest -m "not slow"` 全綠。
- **相依**：Phase1-1。[core目錄邊界收斂.md](core目錄邊界收斂.md) Phase2-2 把具體策略搬到頂層 `strategies/`
  後，`tests/strategies/` 的名稱仍然正確，不需跟著改。

### Phase2-4. 搬引擎與對稱性：`backtest/`、`parity/`、`live/sim/` ⬜

- **目的**：把量化系統特有的兩類測試獨立出來，CI 可以單獨跑 `pytest tests/parity`，實連測試一眼看得到在哪。
- **做法**：
  - `test_futures_roll_backtest.py` import `core.backtest.models`，測的是轉倉在回測引擎的行為，移入既有 `backtest/`。
  - `parity/`（3 支）：`test_quote_parity.py`（回測 vs 實盤報價）、`test_order_state_parity.py`（訂單狀態 Enum
    與券商對應）、`test_symbol_exposure_parity.py`（曝險計算）。`live/test_parity_checker.py` 與
    `live/test_quote_date_parity.py` 測的是 `core/live/` 內的 parity 元件本身，留在 `live/`。
  - `live/sim/`：`live/test_shioaji_sim.py` 移入，目錄內放 `__init__.py`；marker 不變。
- **產出**：5 支測試的新位置；`core/broker/tw/shioaji_execution_handler.py`、`shioaji_order_mapper.py`、
  `core/utils/constant/order.py`、`docs/backtest/multi-market-engine.md` 指向 `test_order_state_parity.py`
  與 `test_quote_parity.py` 的註解。
- **驗證方式**：`pytest tests/parity tests/backtest -m "not slow"` 全綠；
  `pytest -m shioaji_sim --collect-only -q` 仍收得到 `live/sim/` 內的測試（不執行）。
- **相依**：Phase1-1。

### Phase2-5. 搬入口、介面與守門：`apps/`、`frontend/`、`config/`、`scripts/`、`meta/` ⬜

- **目的**：最後一批，也是 CI、pre-commit、Dockerfile 寫死路徑最集中的一批，單獨成步以便一次驗證部署面。
- **做法**：
  - `apps/`（3）：`apps/backtest.py`、`apps/live.py` 的入口與日誌測試。
  - `frontend/`（5）：Streamlit smoke、config、metrics、report loader。
  - `config/`（2）：路徑與設定一致性。
  - `scripts/`（7）：測的是 `scripts/*.py` 工具本身的邏輯（`check_layer_deps.py`、`check_doc_paths.py`、
    `manual/`、`launchd/rehearsal_schedule.py`、`check_overnight_positions.py`）以及 `docker-compose.yml` 的
    live service 區塊。`test_overnight_position_check.py` 雖只 import DAO，但 docstring 明寫它測的是
    `scripts/check_overnight_positions.py` 的判讀邏輯，歸 `scripts/`。
  - `meta/`（9）：沒有單一被測對象、掃整個 repo 的規則檢查：模組 docstring、README 結構同步、命名軸、
    品質棘輪、暫存檔清理、`uv run --no-sync`、報表檔名有文件、API 公開介面、策略不碰 DB 欄位名。
  - 同步改：`.github/workflows/ci.yml` 第 37 行（frontend smoke）、`.pre-commit-config.yaml` 第 38 與 44 行
    （`test_entrypoint_and_logging.py`）、`frontend/Dockerfile` 第 25 行、`frontend/config.py`、
    `frontend/services/*.py`、`scripts/launchd/rehearsal_schedule.py`、`docs/dev/code-quality.md`、
    `docs/deployment/prod-deployment.md`、`docs/setup/dev-setup.md`、`docs/dev/naming-axes.md`、
    `docs/dev/runtime-artifacts.md`、`.claude/skills/develop-strategy/SKILL.md`。
- **產出**：26 支測試的新位置與上述引用。
- **驗證方式**：`pre-commit run --all-files` 通過（注意要先 `git add` 再跑，否則新路徑掃不到）；
  CI 全綠（frontend job 與 docker job 都會跑到被改的路徑）；`docker compose build frontend` 成功。
- **相依**：Phase2-1～2-4（確保這是最後一批，之後根目錄只剩 `conftest.py`、`__init__.py`、`entry_sandbox.py`）。

## Phase3：收尾

### Phase3-1. 全 repo 引用收尾與空目錄清理 ⬜

- **目的**：分批搬移時每批只改自己的引用，最後要整體掃一次，確保沒有漏網。
- **做法**：
  1. `grep -rn 'tests/test_' --exclude-dir=.git --exclude-dir=.venv .` 必須為零（`backlog/` 本文件的〈附三〉除外，
     完成後整份移出 `backlog/` 時一併消失）。
  2. `uv run python scripts/check_doc_paths.py` 通過。
  3. `tests/downloads/`：2026-10-13 實查為 0 B 的空目錄且未被 git 追蹤，是測試執行時的殘留；確認沒有測試依賴它
     存在後刪除，並確認 `.gitignore` 已涵蓋。
  4. `tests/__pycache__` 與各子目錄的 `__pycache__` 清掉再跑一次全套，避免跑到搬移前的 bytecode。
- **產出**：零散引用修正。
- **驗證方式**：上述 grep 歸零；`pytest -m "not slow"` 全綠；CI 全綠。
- **相依**：Phase2-5。

### Phase3-2. 測試目錄說明文件 ⬜

- **目的**：讓新測試有明確的歸位規則，否則根目錄會再長回來。
- **做法**：新增 `docs/dev/test-layout.md`，內容：
  1. 目錄樹（〈附一〉去掉檔案數）。
  2. 歸位決策順序：先看被測對象在哪個套件→鏡像；`pipeline/` 看類別後綴；測 `scripts/` 工具→`scripts/`；
     沒有單一被測對象、掃 repo 規則→`meta/`；回測與實盤對稱性→`parity/`；需真實金鑰的實連→`live/sim/`。
  3. 子目錄必須有 `__init__.py` 的理由。
  4. marker 與目錄的分工：目錄回答「測什麼」，marker 回答「怎麼跑」。
  README 兩份語言版的文件索引加上這份；`.claude/skills/health-check/SKILL.md` 第 134 行附近提到
  `tests/test_` 的字樣改成新規則。
- **產出**：`docs/dev/test-layout.md`、README、health-check skill。
- **驗證方式**：`check_doc_paths.py` 通過；`test_readme_parity.py`（Phase2-5 後位於 `tests/meta/`）通過。
- **相依**：Phase3-1。

---

## 施作時機

本份是純搬移，但 `git mv` 一百多支檔案會讓**每一條尚未合併、且改過這些測試的分支**在合併時碰到
rename 衝突。2026-10-13 實查有五個 worktree 分支在飛（`feature/post-rehearsal`、`feature/tick-timescaledb`、
`feature/tick-backtest`、`feature/framework-symmetry`、`review/fable-recheck`），其中 `feature/post-rehearsal`
已改過 `tests/test_strategy_data_access.py` 等多支根目錄測試。因此：

- **開工條件**：[實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 演練結束、`feature/post-rehearsal`
  與 `feature/tick-timescaledb` 合併進 main 之後；其餘仍在飛的分支要先同步到 main 再開工。
- **施作分支**：獨立 worktree，分支名 `refactor/tests-layout`；每個 Phase2 步驟一個 commit，
  全部做完一次 merge，中間不部署（不碰 money path，但演練期間的主目錄仍以 main 為準）。
- **建議 model**：純搬移與改路徑，用預設 model 即可。

---

## 附一：目標目錄結構（2026-10-13 定案）

```
tests/
├── conftest.py                 共用 fixture（現有）
├── entry_sandbox.py            入口測試的沙箱工具（現有）
├── fixtures/                   靜態資料檔（現有）
│
│   ── 資料層（對應 core/api、core/dao、core/pipeline）──
├── api/                        6 支
├── dao/                        13 支
├── pipeline/                   40 支，依角色再分一層
│   ├── cleaner/                13 支
│   ├── crawler/                 8 支
│   ├── updater/                12 支
│   ├── loader/                  6 支
│   └── shared/                  1 支
│
│   ── 領域模型層（對應 core/models、core/market、core/adapters、core/position）──
├── models/                     3 支
├── market/                     1 支
├── adapters/                   3 支
├── position/                   1 支
│
│   ── 策略與組合層（對應 core/strategies、core/portfolio、core/analysis、strategy_lab/）──
├── strategies/                 6 支
├── portfolio/                  4 支（現有，不動）
├── analysis/                   1 支（現有，不動）
├── strategy_lab/               1 支
│
│   ── 引擎層（對應 core/backtest、core/live）──
├── backtest/                   28 支（現有 27 ＋ 期貨轉倉回測）
│   └── snapshots/              黃金檔
├── live/                       50 支（現有）
│   ├── fixtures/
│   └── sim/                    1 支，shioaji_sim 實連測試
├── parity/                     3 支，回測 vs 實盤對稱性
│
│   ── 入口與介面層（對應 apps/、frontend/、core/config）──
├── apps/                       3 支
├── frontend/                   5 支
├── config/                     2 支
│
│   ── 工具與守門（對應 scripts/、全 repo 規則）──
├── scripts/                    7 支，測 scripts/*.py 工具本身
└── meta/                       9 支，掃整個 repo 的規則檢查
```

## 附二：根目錄 104 支檔案歸類表（2026-10-13，依實際 import 歸類）

檔名省略 `test_` 前綴與 `.py`。

| 目標目錄 | 支數 | 檔案 |
|---|:--:|---|
| `api/` | 6 | adjusted_price_api、finmind_api、futures_price_api、futures_stock_universe_api、stock_data_api、futures_chip |
| `dao/` | 13 | dao_base、dao_financial_statement、dao_finmind、dao_futures_chip_continuous、dao_futures_margin、dao_futures_price、dao_futures_stock_universe、dao_live_trade、dao_monthly_revenue、dao_stock_chip_margin、dao_stock_dividend_corporate_action、dao_stock_price、sqlite_error_semantics |
| `pipeline/cleaner/` | 13 | stock_chip_cleaner、stock_dividend_cleaner、stock_margin_cleaner、stock_price_cleaner、futures_price_cleaner、finmind_cleaner_stock_info、financial_statement_cleaner_equity_change、financial_statement_specs、cleaner_boundaries、futures_tick、stock_id_leading_zero、corporate_action、corporate_action_guard |
| `pipeline/crawler/` | 8 | futures_price_crawler、financial_statement_crawler_pre_ifrs、crawler_holiday_pages、tpex_date_range、tpex_page_date、crawl_result_semantics、request_utils、unreachable_batch_fails |
| `pipeline/updater/` | 12 | futures_price_updater、monthly_revenue_report_updater、updater_throttle、date_gap_backfill、equity_change_interruption、partial_market_guard、trading_calendar_guard、market_holiday、futures_stock_universe、futures_margin、futures_continuous、stock_trading_list_etl |
| `pipeline/loader/` | 6 | futures_price_loader、finmind_loader_broker_trading、finmind_reference_table_loader、finmind_broker_trading_batch、batched_loading、loader_failure_reporting |
| `pipeline/shared/` | 1 | pipeline_error_narrowing |
| `models/` | 3 | futures_cost、futures_margin_control、futures_products |
| `market/` | 1 | futures_calendar |
| `adapters/` | 3 | futures_quote_adapter、quote_validation、futures_session_combine |
| `position/` | 1 | futures_position_manager |
| `strategies/` | 6 | futures_strategy、foreign_selling_reversal_short_strategy、intraday_momentum_strategy、futures_rollover_rehearsal_strategy、strategy_layering、strategy_loader_by_name |
| `strategy_lab/` | 1 | ridge |
| `backtest/` | 1 | futures_roll_backtest |
| `parity/` | 3 | quote_parity、order_state_parity、symbol_exposure_parity |
| `apps/` | 3 | backtest_entry、live_entry、entrypoint_and_logging |
| `frontend/` | 5 | frontend_app_smoke、frontend_config、frontend_futures_metrics、frontend_metrics、frontend_report_loader |
| `config/` | 2 | config_paths、config_consistency |
| `scripts/` | 7 | check_layer_deps_backtest_internal、check_layer_deps_pipeline、doc_symbol_references、manual_fix_tpex_dealer_totals、rehearsal_schedule、compose_live_service、overnight_position_check |
| `meta/` | 9 | module_docs、readme_parity、naming_axes、quality_ratchet、temp_file_cleanup、subprocess_uv_no_sync、report_filenames_documented、api_public_interfaces、strategy_data_access |

另：`live/test_shioaji_sim.py` → `live/sim/`。

## 附三：寫死 `tests/` 路徑的外部引用（2026-10-13 實查，開工時重新 grep）

| 類別 | 位置 |
|---|---|
| CI | `.github/workflows/ci.yml`：`tests/test_frontend_app_smoke.py` |
| pre-commit | `.pre-commit-config.yaml`：`tests/test_entrypoint_and_logging.py`（兩處） |
| Docker | `frontend/Dockerfile`：`tests/test_frontend_metrics.py` |
| pyproject | `pyproject.toml`：`tests/test_futures_continuous.py` |
| 程式碼註解 | `core/broker/tw/shioaji_execution_handler.py`、`core/broker/tw/shioaji_order_mapper.py`、`core/utils/constant/order.py`（`test_order_state_parity`）；`core/config/settings.py`（`test_futures_price_updater`）；`core/market/tw/futures_calendar.py`；`core/pipeline/tw/cleaners/corporate_action_detector.py`（`test_corporate_action_guard`）；`core/pipeline/tw/updaters/futures_continuous_updater.py`；`frontend/config.py`、`frontend/services/metrics.py`、`frontend/services/report_loader.py`；`scripts/launchd/rehearsal_schedule.py` |
| docs | `docs/backtest/module-map.md`、`docs/backtest/multi-market-engine.md`、`docs/deployment/prod-deployment.md`、`docs/dev/code-quality.md`、`docs/dev/naming-axes.md`、`docs/dev/runtime-artifacts.md`、`docs/pipeline/corporate-action.md`、`docs/pipeline/etl-ingestion.md`、`docs/setup/dev-setup.md`、`core/strategies/README.md`、`scripts/manual/README.md` |
| skills | `.claude/skills/develop-strategy/SKILL.md`（`test_strategy_data_access`）、`.claude/skills/health-check/SKILL.md`（`tests/test_` 字樣） |
| tests 互相引用 | `tests/conftest.py`（`test_dao_live_trade`）、`tests/backtest/test_futures_backtest.py` 與 `tests/test_futures_position_manager.py`（`test_futures_cost`）、`tests/live/test_live_factory_and_entry.py`（`test_live_entry`）、`tests/test_corporate_action_guard.py`（`test_trading_calendar_guard`）、`tests/test_finmind_broker_trading_batch.py`（指向已不存在的 `test_broker_trading_updater.py`，順手修正） |
