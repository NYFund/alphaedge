# tests 目錄結構重整

## Abstract

- **背景／問題**：2026-10-18 以 `feature/framework-symmetry`（包含整條分支鏈的最新程式碼）實查，`tests/` 共
  230 支測試檔、3,317 條測試、74,659 行，與原始碼（`core/`＋`apps/`＋`frontend/`＋`strategies/` 共 74,522 行）
  約 1 : 1，規模本身健康。問題在組織方式：`backtest/`、`live/`、`portfolio/`、`analysis/` 四個子目錄已鏡像
  `core/` 同名套件，**其餘 127 支直接平鋪在 `tests/` 根目錄**（main 上 104 支；分支鏈逐段增加：
  `feature/post-rehearsal` 106、`feature/tick-timescaledb` 111、`feature/tick-backtest` 112、`feature/framework-symmetry` 127），
  對應 `core/api`、`core/dao`、`core/pipeline`、`core/models`、`core/market`、`apps/`、`frontend/`、`scripts/`
  等十幾個來源。改一個模組要找對應測試得靠檔名猜；守門測試（`test_readme_parity.py`、`test_naming_axes.py`、
  `test_check_layer_deps_*.py` 等）與業務測試混在一起，讀的人分不出哪些在測功能、哪些在掃 repo 規則。
- **目標**：`tests/` 單層鏡像原始碼目錄（與 pytest 官方建議及 freqtrade、hummingbot 的做法一致），
  快慢仍由既有 marker（`slow`、`shioaji_sim`、`shioaji_sim_order`）區分，不另分 `unit/`／`integration/` 兩層。
  `pipeline/` 因 45 支過多再依角色分 `cleaner/`、`crawler/`、`updater/`、`loader/`、`shared/` 一層。
  量化系統特有的兩類測試獨立出來：回測 vs 實盤的 `parity/`，以及連 Shioaji 模擬環境的 `live/sim/`。
  工具腳本的測試放 `scripts/`，掃整個 repo 的規則檢查放 `meta/`，兩者分開。完整目標結構見〈附一〉，
  根目錄檔案的歸類表見〈附二〉（依各檔實際 import 的模組歸類，不是依檔名）。
- **範圍界線**：**不做**
  1. 不改任何測試內容、斷言或 fixture——本份只 `git mv` 與改路徑引用，測試數量前後一致（以開工當下的
     `pytest --collect-only -q` 為準）。
  2. 不動 `backtest/`、`live/`、`portfolio/`、`analysis/` 既有檔案的位置（只有 `live/test_shioaji_sim.py`
     移入 `live/sim/`，以及根目錄少數檔案移入）。
  3. 不抽 NautilusTrader 式的 `test_kit/` 共用 factory 目錄：既有的 `conftest.py`（根目錄、`backtest/`、`live/`）
     已扮演此角色，搬完若發現明顯重複再另立工作。
  4. 不把守門測試改成 lint 或 CI 腳本——它們留在 `pytest` 內，只是集中到 `meta/`。
  5. 不改 `tests/pipeline/` 以外任何目錄名來配合 [core目錄邊界收斂.md](core目錄邊界收斂.md) 的
     搬移；`tests/pipeline/` 改名 `tests/etl/` 隨該文件 Phase5-1 一起做，不在本份做。
- **驗收標準**：
  1. `tests/` 根目錄只剩 `conftest.py`、`__init__.py`、`entry_sandbox.py` 與子目錄。
  2. `pytest --collect-only -q` 的測試數量與搬移前一致；`pytest -m "not slow"` 全綠；CI 全綠
     （含 TimescaleDB 整合測試那一步）。
  3. 全 repo grep `tests/test_` 與 `tests/live/test_shioaji_sim` 歸零（CI、pre-commit、Dockerfile、`pyproject.toml`、
     `.env.example`、`docs/`、程式碼註解、`.claude/skills/`、tests 內互相引用），`scripts/check_doc_paths.py` 通過。
  4. `docs/dev/` 有測試目錄說明，寫明每個子目錄放哪類測試、新測試怎麼決定放哪。

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| Phase1-1 | 建立目標目錄骨架 | `tests/{api,dao,pipeline/*,models,market,adapters,position,strategies,strategy_lab,parity,apps,frontend,config,scripts,meta,live/sim}/__init__.py` | `pytest --collect-only -q` 數量不變 | ⬜ | — |
| Phase2-1 | 搬資料存取層：`api/`、`dao/` | 26 支測試、對應外部引用（含 CI 的 TimescaleDB 步驟） | `pytest -m "not slow"` 全綠；grep 該批舊路徑歸零；CI 全綠 | ⬜ | `test_stock_tick_timescale.py` 被 CI 以路徑直接執行 |
| Phase2-2 | 搬 `pipeline/` 五個子目錄 | 45 支測試、對應外部引用 | 同上 | ⬜ | 最大的一批；`docs/pipeline/*.md` 引用最多 |
| Phase2-3 | 搬領域模型與策略層：`models/`、`market/`、`adapters/`、`position/`、`strategies/`、`strategy_lab/` | 20 支測試（`feature/stock-rehearsal` 合併後 21 支）、對應外部引用 | 同上 | ⬜ | — |
| Phase2-4 | 搬引擎與對稱性：`backtest/`、`parity/`、`live/sim/` | 6 支測試、對應外部引用 | 同上；`pytest -m shioaji_sim --collect-only` 仍收得到 | ⬜ | — |
| Phase2-5 | 搬入口、介面與守門：`apps/`、`frontend/`、`config/`、`scripts/`、`meta/` | 31 支測試、CI、pre-commit、`frontend/Dockerfile`、`.env.example` | 同上；pre-commit 全套通過；CI 全綠 | ⬜ | CI 與 pre-commit 寫死的路徑大多在這批 |
| Phase3-1 | 全 repo 引用收尾與空目錄清理 | 全 repo | grep `tests/test_` 歸零；`check_doc_paths.py` 通過；`tests/downloads/` 處置 | ⬜ | — |
| Phase3-2 | 測試目錄說明更新 | `docs/dev/testing.md`、`.claude/skills/health-check/SKILL.md` | `check_doc_paths.py` 通過；`test_readme_parity.py` 通過 | ⬜ | 2026-10-18 改為更新既有的 `docs/dev/testing.md`（該文件在 `feature/framework-symmetry` 新增），不另開新文件 |

---

## Phase1：準備

### Phase1-1. 建立目標目錄骨架 ⬜

- **目的**：先把所有目標子目錄連同 `__init__.py` 建好，讓後續每一批搬移只做 `git mv`，不夾帶建目錄的雜訊。
- **做法**：依〈附一〉建立目錄；每個目錄放空的 `__init__.py`。`tests/` 已是套件（有 `__init__.py`），
  子目錄必須跟進，否則不同目錄若出現同名測試檔，pytest 的 rootdir-relative import 會互相覆蓋。
- **產出**：約 22 個 `__init__.py`。
- **驗證方式**：`pytest --collect-only -q | tail -1` 的數量與搬移前相同（空套件不影響收集）。
- **相依**：見本文件〈施作時機〉——要等分支鏈全部合併後才開工。

## Phase2：分批搬移

每一批的做法相同，只有範圍不同：

1. 開工時先重跑一次〈附二〉的歸類：`ls tests/test_*.py` 與附二逐一比對，附二沒有的新檔依「實際 import 的被測模組」
   補進對應批次（2026-10-13 → 2026-10-18 之間分支鏈就多了 24 支、刪了 1 支）。
2. 依〈附二〉對該批檔案逐一 `git mv tests/test_x.py tests/<dir>/test_x.py`（保留 rename 歷史）。
3. 更新該批的外部引用（〈附三〉列出 2026-10-18 的實查清單，開工時要重新 grep 一次）：
   - 程式碼註解與 docstring 中「對應測試見 `tests/test_x.py`」的指路（CLAUDE.md §2.1 允許註解指向測試，
     只是路徑要跟著改）。
   - `docs/**/*.md`、`strategies/README.md`、`scripts/manual/README.md` 的路徑。
   - tests 內互相引用（例如 `tests/backtest/test_futures_backtest.py` 註解指向 `tests/test_futures_cost.py`）。
4. 跑 `pytest -m "not slow"` 與 `uv run --no-sync python scripts/check_doc_paths.py`，全綠才 commit。
5. 每批一個 commit，訊息寫明搬了哪些目錄。

### Phase2-1. 搬資料存取層：`api/`、`dao/` ⬜

- **目的**：把 `core/api`、`core/dao` 的測試歸位。
- **做法**：〈附二〉的 `api/`（8 支）與 `dao/`（18 支）。
  - `test_sqlite_error_semantics.py` 同時碰 API 與 DAO，測的是 SQLite 錯誤如何被 DAO 層翻譯，歸 `dao/`；
    `test_futures_chip.py` 測的是 `FuturesChipAPI`，歸 `api/`。
  - `test_close_series_contract.py`、`test_published_after_close.py` 釘的是 `BaseDataAPI` 的契約，歸 `api/`。
  - `test_stock_tick_api.py` 名為 API，實際只 import `StockTickDAO`（讀取端的參數與 schema 驗證），歸 `dao/`。
  - `test_stock_tick_timescale.py` 是需要真 TimescaleDB 的整合測試（沒設 `TICK_DATABASE_URL` 就整個模組 skip），
    測 `StockTickDAO` 與 `StockTickAPI` 的寫讀，歸 `dao/`。**CI 以路徑直接執行它並把任何 skip 當失敗**
    （`.github/workflows/ci.yml` 第 122、124 行），這一批必須同步改 CI，否則 CI 那一步會找不到檔案。
- **產出**：26 支測試的新位置；`.github/workflows/ci.yml` 第 18、122、124 行；`tests/conftest.py` 第 189 行附近
  指向 `test_dao_live_trade.py` 的註解；`docs/dev/testing.md`、`docs/dev/code-quality.md`、`docs/dev/data-access-layer.md`
  指向 `test_stock_tick_timescale.py` 的路徑；`tests/backtest/test_stock_tick_datafeed.py`、`tests/test_stock_tick_loader.py`、
  `tests/test_tick_history_import.py`、`tests/test_stock_tick_timescale.py` 自身 docstring 的互相引用；
  `tests/test_close_series_contract.py` 指向 `test_stock_data_api.py`、`test_futures_price_api.py`；
  `scripts/manual/README.md` 指向 `test_dao_finmind.py`、`test_finmind_api.py`。
- **驗證方式**：`pytest tests/api tests/dao -q` 全綠；`pytest -m "not slow"` 全綠；CI 的 TimescaleDB 步驟通過且無 skip。
- **相依**：Phase1-1。

### Phase2-2. 搬 `pipeline/` 五個子目錄 ⬜

- **目的**：根目錄 127 支裡有 45 支是資料管線測試，是扁平化的主因。
- **做法**：依角色而非鏡像 `core/pipeline/tw/`——`tw/` 底下本身也是幾十支平鋪，鏡像它沒有幫助：
  - `cleaner/`（12 支）：各 `*Cleaner`、股票代號補零、除權息偵測與守門、財報規格。
  - `crawler/`（9 支）：各 `*Crawler`、TPEX 分頁與日期區間、request 工具、批次不可達、`CrawlResult` 三態契約。
  - `updater/`（14 支）：各 `*Updater`、節流、缺口回補、交易日守門、休市日、期貨連續合約、台股 tick 更新鏈、
    逐年區間 updater 基底。
  - `loader/`（7 支）：各 `*Loader`、批次載入、失敗回報。
  - `shared/`（3 支）：pipeline 錯誤收斂、限流中斷、`step_runner` 流程骨架。
  歸類有疑義時以「被測類別的名稱後綴」為準（`Cleaner`→`cleaner/`），沒有明確後綴的看它主要 patch 哪一層。
- **產出**：45 支測試的新位置；`docs/pipeline/corporate-action.md`、`docs/pipeline/etl-ingestion.md`、
  `core/config/settings.py`、`core/pipeline/tw/cleaners/corporate_action_detector.py`、
  `core/pipeline/tw/updaters/futures_continuous_updater.py`、`pyproject.toml` 第 153 行附近、
  `scripts/manual/README.md`（三支 FinMind loader 測試）、`scripts/manual/manual_corporate_action_detect.py`、
  `tests/test_corporate_action_guard.py`（指向 `test_trading_calendar_guard.py`）、`tests/test_loader_failure_reporting.py`
  （指向 `test_stock_tick_loader.py`）、`tests/test_finmind_broker_trading_batch.py`（指向已不存在的
  `test_broker_trading_updater.py`，順手修正）的引用。
- **驗證方式**：`pytest tests/pipeline -q` 全綠；`pytest -m "not slow"` 全綠；`check_doc_paths.py` 通過。
- **相依**：Phase1-1。**與 [core目錄邊界收斂.md](core目錄邊界收斂.md) Phase5-1 的關係**：
  該步驟會把 `core/pipeline/` 搬到頂層 `etl/`，屆時 `tests/pipeline/` 一併改名 `tests/etl/`，
  由該步驟負責，本份不預先改名。

### Phase2-3. 搬領域模型與策略層 ⬜

- **目的**：`core/models`、`core/market`、`core/adapters`、`core/position`、策略（`core/strategies` 的契約與頂層
  `strategies/` 的具體策略）、`strategy_lab/` 的測試歸位。
- **做法**：〈附二〉的 `models/`（5）、`market/`（4）、`adapters/`（3）、`position/`（1）、`strategies/`（6，
  `feature/stock-rehearsal` 合併後 7）、`strategy_lab/`（1）。
  - `test_futures_session_combine.py` 同時 import adapters 與 `backtest.datafeed`，測的是日夜盤合併的 adapter 行為，歸 `adapters/`。
  - `test_futures_session_rule.py` 驗四個呼叫端（券商報價串流、實盤資料源、`session_guard`、`FuturesCalendar`）的
    日／夜盤判定一致，規則本身是市場結構，歸 `market/`。
  - `test_futures_margin_model.py` 測 `core/market/tw/futures_margin_model.py`，歸 `market/`（`models/` 對應的是 `core/models`）。
  - `test_model_symmetry.py`、`test_price_limits_contract.py` 釘的是 `core/models` 的欄位與回傳型別契約，歸 `models/`。
  - `test_ridge.py` import 的是 `strategy_lab.strategies.tsmc_overnight_signal.ridge`，不在 `core/`，
    歸 `strategy_lab/`（`tests/analysis/` 對應的是 `core/analysis`，不混放）。
  - `test_strategy_data_access.py` 雖掃策略目錄，但它是「策略層不得出現 DB 欄位字面值」的 repo 規則掃描，
    歸 Phase2-5 的 `meta/`，不放 `strategies/`。
- **產出**：20 支測試的新位置；`core/market/tw/futures_calendar.py`、`docs/backtest/multi-market-engine.md`
  （`test_quote_validation.py`）、`docs/dev/testing.md`（`test_model_symmetry.py`）、`tests/test_futures_position_manager.py`
  與 `tests/backtest/test_futures_backtest.py` 內指向 `test_futures_cost.py` 的註解。
- **驗證方式**：各目錄 `pytest` 全綠；`pytest -m "not slow"` 全綠。
- **相依**：Phase1-1。具體策略已在 `feature/post-rehearsal` 搬到頂層 `strategies/`（[core目錄邊界收斂.md](core目錄邊界收斂.md)
  Phase2-2），`tests/strategies/` 的名稱仍然正確，不需跟著改。

### Phase2-4. 搬引擎與對稱性：`backtest/`、`parity/`、`live/sim/` ⬜

- **目的**：把量化系統特有的兩類測試獨立出來，CI 可以單獨跑 `pytest tests/parity`，實連測試一眼看得到在哪。
- **做法**：
  - `test_futures_roll_backtest.py`、`test_intraday_momentum_tick_backtest.py` 測的是回測引擎（轉倉、逐筆模擬器）的行為，
    移入既有 `backtest/`。
  - `parity/`（3 支）：`test_quote_parity.py`（回測 vs 實盤報價）、`test_order_state_parity.py`（訂單狀態 Enum
    與券商對應）、`test_symbol_exposure_parity.py`（曝險計算）。`live/test_parity_checker.py` 與
    `live/test_quote_date_parity.py` 測的是 `core/live/` 內的 parity 元件本身，留在 `live/`。
  - `live/sim/`：`live/test_shioaji_sim.py` 移入，目錄內放 `__init__.py`；marker 不變。`live/test_broker_contract.py`、
    `live/test_shioaji_broker.py` 只以裸檔名提到它，不必改。
- **產出**：6 支測試的新位置；`core/broker/tw/shioaji_execution_handler.py`、`shioaji_order_mapper.py`、
  `core/utils/constant/order.py`、`docs/backtest/multi-market-engine.md` 指向 `test_order_state_parity.py`
  與 `test_quote_parity.py` 的註解；`docs/dev/testing.md` 指向 `tests/live/test_shioaji_sim.py` 的路徑。
- **驗證方式**：`pytest tests/parity tests/backtest -m "not slow"` 全綠；
  `pytest -m shioaji_sim --collect-only -q` 仍收得到 `live/sim/` 內的測試（不執行）。
- **相依**：Phase1-1。

### Phase2-5. 搬入口、介面與守門：`apps/`、`frontend/`、`config/`、`scripts/`、`meta/` ⬜

- **目的**：最後一批，也是 CI、pre-commit、Dockerfile 寫死路徑最集中的一批，單獨成步以便一次驗證部署面。
- **做法**：
  - `apps/`（5）：`apps/backtest.py`、`apps/live.py`、`apps/update_db.py` 的入口、target 登記表、重試與日誌測試。
  - `frontend/`（5）：Streamlit smoke、config、metrics、report loader。
  - `config/`（2）：路徑與設定一致性。
  - `scripts/`（10）：測的是 `scripts/*.py` 工具本身的邏輯（`check_layer_deps.py` 的四組檢查、`check_doc_paths.py`、
    `manual/` 的兩支腳本、`launchd/rehearsal_schedule.py`、`check_overnight_positions.py`）以及 `docker-compose.yml` 的
    live service 區塊。`test_overnight_position_check.py` 雖只 import DAO，但 docstring 明寫它測的是
    `scripts/check_overnight_positions.py` 的判讀邏輯，歸 `scripts/`。
  - `meta/`（9）：沒有單一被測對象、掃整個 repo 的規則檢查：模組 docstring、README 結構同步、命名軸、
    品質棘輪、暫存檔清理、`uv run --no-sync`、報表檔名有文件、API 公開介面、策略不碰 DB 欄位名。
  - 同步改：`.github/workflows/ci.yml` 第 60 行（frontend smoke）、`.pre-commit-config.yaml` 第 38 與 44 行
    （`test_entrypoint_and_logging.py`）、`frontend/Dockerfile` 第 25 行、`.env.example`（`test_config_consistency.py`）、
    `frontend/config.py`、`frontend/services/*.py`、`scripts/launchd/rehearsal_schedule.py`、`docs/dev/code-quality.md`、
    `docs/dev/testing.md`、`docs/deployment/prod-deployment.md`、`docs/setup/dev-setup.md`、`docs/dev/naming-axes.md`、
    `docs/dev/runtime-artifacts.md`、`docs/backtest/module-map.md`、`strategies/README.md`、
    `.claude/skills/develop-strategy/SKILL.md`、`tests/live/test_live_factory_and_entry.py`（指向 `test_live_entry.py`）。
- **產出**：31 支測試的新位置與上述引用。
- **驗證方式**：`pre-commit run --all-files` 通過（注意要先 `git add` 再跑，否則新路徑掃不到）；
  CI 全綠（frontend job 與 docker job 都會跑到被改的路徑）；`docker compose build frontend` 成功。
- **相依**：Phase2-1～2-4（確保這是最後一批，之後根目錄只剩 `conftest.py`、`__init__.py`、`entry_sandbox.py`）。

## Phase3：收尾

### Phase3-1. 全 repo 引用收尾與空目錄清理 ⬜

- **目的**：分批搬移時每批只改自己的引用，最後要整體掃一次，確保沒有漏網。
- **做法**：
  1. `grep -rn 'tests/test_' --exclude-dir=.git --exclude-dir=.venv .` 必須為零（`backlog/` 本文件的〈附三〉除外，
     完成後整份移出 `backlog/` 時一併消失）；另 grep `tests/live/test_shioaji_sim` 也要為零。
  2. `uv run --no-sync python scripts/check_doc_paths.py` 通過。
  3. `tests/downloads/`：2026-10-18 實查主目錄仍有這個 0 B 的空目錄、未被 git 追蹤（`.gitignore` 第 27 行已涵蓋），
     各 worktree 沒有；是早期測試執行時的殘留，現行測試都寫進 `tmp_path`。確認沒有測試依賴它存在後刪除。
  4. `tests/__pycache__` 與各子目錄的 `__pycache__` 清掉再跑一次全套，避免跑到搬移前的 bytecode。
- **產出**：零散引用修正。
- **驗證方式**：上述 grep 歸零；`pytest -m "not slow"` 全綠；CI 全綠。
- **相依**：Phase2-5。

### Phase3-2. 測試目錄說明更新 ⬜

- **目的**：讓新測試有明確的歸位規則，否則根目錄會再長回來。
- **做法**：`docs/dev/testing.md`（測試與回歸）已在 `feature/framework-symmetry` 新增並列入兩份 README 的文件索引，
  其〈一、測試目錄配置〉目前寫「`tests/` 根目錄：資料層、具體策略、前端、設定與入口，以及大部分守門測試」。
  **改寫該節，不另開新文件**（原規劃的 `docs/dev/test-layout.md` 會與它重複）：
  1. 目錄樹（〈附一〉去掉檔案數）。
  2. 歸位決策順序：先看被測對象在哪個套件→鏡像；`pipeline/` 看類別後綴；測 `scripts/` 工具→`scripts/`；
     沒有單一被測對象、掃 repo 規則→`meta/`；回測與實盤對稱性→`parity/`；需真實金鑰的實連→`live/sim/`。
  3. 子目錄必須有 `__init__.py` 的理由。
  4. marker 與目錄的分工：目錄回答「測什麼」，marker 回答「怎麼跑」。
  同文件〈守門測試與行為測試〉表格、〈需要外部資源的測試〉表格內的路徑已在 Phase2 各批改過，這裡只複核。
  `.claude/skills/health-check/SKILL.md` 第 134 行提到 `tests/test_*` 護欄的字樣改成新規則（例如 `tests/meta/`）。
- **產出**：`docs/dev/testing.md`、`.claude/skills/health-check/SKILL.md`。
- **驗證方式**：`check_doc_paths.py` 通過；`test_readme_parity.py`（Phase2-5 後位於 `tests/meta/`）通過。
- **相依**：Phase3-1；`docs/dev/testing.md` 要先隨 `feature/framework-symmetry` 進 main（〈施作時機〉的開工條件已涵蓋）。

---

## 施作時機

本份是純搬移，但 `git mv` 一百多支檔案會讓**每一條尚未合併、且改過這些測試的分支**在合併時碰到
rename 衝突。2026-10-18 實查有五個 worktree 分支在飛：分支鏈 `feature/post-rehearsal` → `feature/tick-timescaledb` →
`feature/tick-backtest` → `feature/framework-symmetry`，以及 `feature/stock-rehearsal`。分支鏈在根目錄新增 24 支測試、
刪 1 支（淨增 23），並改過多支既有根目錄測試；`feature/stock-rehearsal` 新增 `tests/test_stock_round_trip_rehearsal_strategy.py`、
改 `tests/test_rehearsal_schedule.py`。因此：

- **開工條件**：[實盤下單架構規劃.md](實盤下單架構規劃.md) Phase7-1 演練結束、分支鏈四條依序合併進 main、
  `feature/stock-rehearsal`（同文件 Phase7-27，排在 2026-10-21 期貨盤後段後合併）也已進 main；
  開工當下若仍有其他分支在飛，先同步到 main 再開工。
- **施作分支**：獨立 worktree，分支名 `refactor/tests-layout`；每個 Phase2 步驟一個 commit，
  全部做完一次 merge，中間不部署（不碰 money path，但演練期間的主目錄仍以 main 為準）。
- **建議 model**：純搬移與改路徑，用預設 model 即可。

---

## 附一：目標目錄結構（2026-10-13 定案；2026-10-18 依 `feature/framework-symmetry` 更新支數）

```
tests/
├── conftest.py                 共用 fixture（現有）
├── entry_sandbox.py            入口測試的沙箱工具（現有）
├── fixtures/                   靜態資料檔（現有）
│
│   ── 資料層（對應 core/api、core/dao、core/pipeline）──
├── api/                        8 支
├── dao/                        18 支
├── pipeline/                   45 支，依角色再分一層
│   ├── cleaner/                12 支
│   ├── crawler/                 9 支
│   ├── updater/                14 支
│   ├── loader/                  7 支
│   └── shared/                  3 支
│
│   ── 領域模型層（對應 core/models、core/market、core/adapters、core/position）──
├── models/                     5 支
├── market/                     4 支
├── adapters/                   3 支
├── position/                   1 支
│
│   ── 策略與組合層（對應 core/strategies 與頂層 strategies/、core/portfolio、core/analysis、strategy_lab/）──
├── strategies/                 6 支（feature/stock-rehearsal 合併後 7）
├── portfolio/                  4 支（現有，不動）
├── analysis/                   1 支（現有，不動）
├── strategy_lab/               1 支
│
│   ── 引擎層（對應 core/backtest、core/live）──
├── backtest/                   42 支（現有 40 ＋ 期貨轉倉回測、盤中動能逐筆回測）
│   └── snapshots/              黃金檔
├── live/                       57 支（現有 58 － 移出的 1 支；feature/stock-rehearsal 另加 2 支）
│   ├── fixtures/
│   └── sim/                    1 支，shioaji_sim 實連測試
├── parity/                     3 支，回測 vs 實盤對稱性
│
│   ── 入口與介面層（對應 apps/、frontend/、core/config）──
├── apps/                       5 支
├── frontend/                   5 支
├── config/                     2 支
│
│   ── 工具與守門（對應 scripts/、全 repo 規則）──
├── scripts/                   10 支，測 scripts/*.py 工具本身
└── meta/                       9 支，掃整個 repo 的規則檢查
```

## 附二：根目錄檔案歸類表（2026-10-18，`feature/framework-symmetry` 的 127 支，依實際 import 歸類）

檔名省略 `test_` 前綴與 `.py`。標 `*` 的是 2026-10-13 之後分支鏈新增的檔案；`futures_tick` 已隨 DolphinDB tick 程式
一併刪除（`feature/tick-timescaledb` 的 `a288df4f`），不再列入。

| 目標目錄 | 支數 | 檔案 |
|---|:--:|---|
| `api/` | 8 | adjusted_price_api、finmind_api、futures_price_api、futures_stock_universe_api、stock_data_api、futures_chip、close_series_contract*、published_after_close* |
| `dao/` | 18 | dao_base、dao_financial_statement、dao_finmind、dao_futures_chip_continuous、dao_futures_margin、dao_futures_price、dao_futures_stock_universe、dao_live_trade、dao_monthly_revenue、dao_stock_chip_margin、dao_stock_dividend_corporate_action、dao_stock_price、sqlite_error_semantics、dao_key_columns*、margin_dao_split*、optional_tables*、stock_tick_api*、stock_tick_timescale* |
| `pipeline/cleaner/` | 12 | stock_chip_cleaner、stock_dividend_cleaner、stock_margin_cleaner、stock_price_cleaner、futures_price_cleaner、finmind_cleaner_stock_info、financial_statement_cleaner_equity_change、financial_statement_specs、cleaner_boundaries、stock_id_leading_zero、corporate_action、corporate_action_guard |
| `pipeline/crawler/` | 9 | futures_price_crawler、financial_statement_crawler_pre_ifrs、crawler_holiday_pages、tpex_date_range、tpex_page_date、crawl_result_semantics、request_utils、unreachable_batch_fails、crawl_result_contract* |
| `pipeline/updater/` | 14 | futures_price_updater、monthly_revenue_report_updater、updater_throttle、date_gap_backfill、equity_change_interruption、partial_market_guard、trading_calendar_guard、market_holiday、futures_stock_universe、futures_margin、futures_continuous、stock_trading_list_etl、stock_tick_updater*、yearly_range_updater* |
| `pipeline/loader/` | 7 | futures_price_loader、finmind_loader_broker_trading、finmind_reference_table_loader、finmind_broker_trading_batch、batched_loading、loader_failure_reporting、stock_tick_loader* |
| `pipeline/shared/` | 3 | pipeline_error_narrowing、rate_limit_interruption*、step_runner* |
| `models/` | 5 | futures_cost、futures_margin_control、futures_products、model_symmetry*、price_limits_contract* |
| `market/` | 4 | futures_calendar、futures_margin_model*、futures_session_rule*、trading_calendar_base* |
| `adapters/` | 3 | futures_quote_adapter、quote_validation、futures_session_combine |
| `position/` | 1 | futures_position_manager |
| `strategies/` | 6 | futures_strategy、foreign_selling_reversal_short_strategy、intraday_momentum_strategy、futures_rollover_rehearsal_strategy、strategy_layering、strategy_loader_by_name（`feature/stock-rehearsal` 合併後另加 stock_round_trip_rehearsal_strategy） |
| `strategy_lab/` | 1 | ridge |
| `backtest/` | 2 | futures_roll_backtest、intraday_momentum_tick_backtest* |
| `parity/` | 3 | quote_parity、order_state_parity、symbol_exposure_parity |
| `apps/` | 5 | backtest_entry、live_entry、entrypoint_and_logging、update_db_registry*、update_db_retry* |
| `frontend/` | 5 | frontend_app_smoke、frontend_config、frontend_futures_metrics、frontend_metrics、frontend_report_loader |
| `config/` | 2 | config_paths、config_consistency |
| `scripts/` | 10 | check_layer_deps_backtest_internal、check_layer_deps_pipeline、doc_symbol_references、manual_fix_tpex_dealer_totals、rehearsal_schedule、compose_live_service、overnight_position_check、check_layer_deps_db_driver*、check_layer_deps_strategy_contract*、tick_history_import* |
| `meta/` | 9 | module_docs、readme_parity、naming_axes、quality_ratchet、temp_file_cleanup、subprocess_uv_no_sync、report_filenames_documented、api_public_interfaces、strategy_data_access |

另：`live/test_shioaji_sim.py` → `live/sim/`。

## 附三：寫死 `tests/` 路徑的外部引用（2026-10-18 以 `feature/framework-symmetry` 實查，開工時重新 grep）

| 類別 | 位置 |
|---|---|
| CI | `.github/workflows/ci.yml`：第 18、122、124 行 `tests/test_stock_tick_timescale.py`（第 122 行是**實際執行**的指令）；第 60 行 `tests/test_frontend_app_smoke.py` |
| pre-commit | `.pre-commit-config.yaml`：`tests/test_entrypoint_and_logging.py`（第 38、44 行） |
| Docker | `frontend/Dockerfile` 第 25 行：`tests/test_frontend_metrics.py` |
| pyproject | `pyproject.toml` 第 153 行：`tests/test_futures_continuous.py` |
| 環境範本 | `.env.example`：`tests/test_config_consistency.py` |
| 程式碼註解 | `core/broker/tw/shioaji_execution_handler.py`、`core/broker/tw/shioaji_order_mapper.py`、`core/utils/constant/order.py`（`test_order_state_parity`）；`core/config/settings.py`（`test_futures_price_updater`）；`core/market/tw/futures_calendar.py`（`test_futures_calendar`）；`core/pipeline/tw/cleaners/corporate_action_detector.py`、`scripts/manual/manual_corporate_action_detect.py`（`test_corporate_action_guard`）；`core/pipeline/tw/updaters/futures_continuous_updater.py`（`test_futures_continuous`）；`frontend/config.py`、`frontend/services/metrics.py`、`frontend/services/report_loader.py`；`scripts/launchd/rehearsal_schedule.py`（`test_rehearsal_schedule`） |
| docs | `docs/backtest/module-map.md`、`docs/backtest/multi-market-engine.md`（`test_quote_parity`、`test_quote_validation`）、`docs/deployment/prod-deployment.md`、`docs/dev/code-quality.md`、`docs/dev/data-access-layer.md`、`docs/dev/naming-axes.md`、`docs/dev/runtime-artifacts.md`、`docs/dev/testing.md`（另含 `tests/live/test_shioaji_sim.py`）、`docs/pipeline/corporate-action.md`、`docs/pipeline/etl-ingestion.md`（含 `test_crawl_result_contract`）、`docs/setup/dev-setup.md`、`strategies/README.md`、`scripts/manual/README.md`（五支 FinMind 相關測試） |
| skills | `.claude/skills/develop-strategy/SKILL.md`（`test_strategy_data_access`）、`.claude/skills/health-check/SKILL.md`（`tests/test_*` 字樣） |
| tests 互相引用 | `tests/conftest.py`（`test_dao_live_trade`）、`tests/backtest/test_futures_backtest.py` 與 `tests/test_futures_position_manager.py`（`test_futures_cost`）、`tests/backtest/test_stock_tick_datafeed.py`、`tests/test_stock_tick_loader.py`、`tests/test_tick_history_import.py`、`tests/test_stock_tick_timescale.py`（`test_stock_tick_timescale`）、`tests/test_close_series_contract.py`（`test_stock_data_api`、`test_futures_price_api`）、`tests/test_loader_failure_reporting.py`（`test_stock_tick_loader`）、`tests/live/test_live_factory_and_entry.py`（`test_live_entry`）、`tests/test_corporate_action_guard.py`（`test_trading_calendar_guard`）、`tests/test_finmind_broker_trading_batch.py`（指向已不存在的 `test_broker_trading_updater.py`，順手修正） |
