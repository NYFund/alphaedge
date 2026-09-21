# 改用 uv 管理套件

## Abstract

- **背景／問題**：相依目前有兩份來源——`pyproject.toml` 只寫版本下限，`requirements.txt` 是 104 行的 pip freeze，兩份靠人工與 `tests/test_config_consistency.py` 維持一致。本機 `.venv` 則是「手動裝過什麼就有什麼」，與兩份清單都可能脫節：tqdm、html5lib 都曾經只補進本機 venv、CI 才炸；shioaji 在 PyPI 已到 1.7.5，本機與鎖定檔仍停在 1.3.3，卻沒有任何地方提醒。重建 venv（例如 2026-09-02 搬離 iCloud）也得手動修。
- **目標**：以 uv 管理環境與鎖定檔，`pyproject.toml` 成為唯一的相依宣告，`uv.lock` 由它自動解析產生；本機、CI、`core/Dockerfile` 三處都以 `uv sync` 從同一份 lock 安裝，`.venv` 變成可隨時 `rm -rf .venv && uv sync` 重建的產物。
- **範圍界線（不做）**：
  - **不升級任何套件的版本**：lock 解析出的版本必須與現行 `requirements.txt` 一致，shioaji 以 `==1.3.3` 釘住。升級 shioaji 另立 [Shioaji升級至1.7.md](Shioaji升級至1.7.md)，兩件事混在一起時，測試紅了分不出是工具換了還是套件換了。
  - **不動 `frontend/Dockerfile` 與 `frontend/requirements.txt`**：前端映像刻意不安裝本專案、不 import `core`，那份 requirements 是它唯一的相依來源，與本工作無關。
  - **不把 `[project.optional-dependencies]` 改成 `[dependency-groups]`**：`dev` 放在 extras 或 dependency group 各有取捨，屬另一個決定；本次以 `uv sync --extra dev` 沿用現行結構。
  - **不改 build backend**：維持 setuptools 與 namespace package 設定。
- **驗收標準**：
  1. `uv sync --locked --extra dev` 建出的環境跑 `pytest`（含 `slow`）全綠、`ruff check .`／`ruff format --check .` 全綠、`./scripts/run_regression.sh` 雙線零變動。
  2. CI 與 `core/Dockerfile` 改用 `uv.lock` 安裝，CI 綠燈、映像建得起來且 `run.py --help` 可執行。
  3. `requirements.txt` 已刪除，repo 內（`frontend/requirements.txt` 除外）不再有指向它的程式、測試、文件或設定。

---

## 進度追蹤表

| 編號 | 步驟名稱 | 產出檔案 | 驗證方式 | 狀態 | 備註／中斷點 |
|------|----------|----------|----------|:----:|--------------|
| S1 | 產生 `uv.lock` 並對齊現行版本 | `uv.lock`、`.python-version`、`pyproject.toml` | `uv export` 與 `requirements.txt` 逐套件比對，版本全數相同（或差異逐項記錄並說明） | ✅ | 2026-09-21：101 個套件版本全數相同；不需 `environments` 限定平台 |
| S2 | 盤點 `requirements.txt` 多出的套件 | `pyproject.toml`、本文件 | 每個「lock 裡沒有、freeze 裡有」的套件都有保留／移除的結論 | ✅ | 2026-09-21：`requirements.txt` 對 lock 差集為空；本機 venv 多出 13 個套件全數零 import、移除。**`ta` 是 finmind 的相依**，`架構重構與冗餘收斂.md` 的結論不成立 |
| S3 | 本機切換到 uv 環境 | `.venv`（不進版控） | 驗收標準第 1 條 | ✅ | 2026-09-21：pytest 2003 passed（含 `slow`、0 skip）、ruff 全綠、回歸雙線通過 |
| S4 | CI 改用 uv | `.github/workflows/ci.yml` | CI 綠燈，且 `uv lock --check` 在 lock 過期時會紅 | 🔄 | 設定已改、本機逐步驗過；2026-09-21 push 後：安裝、lint、閘門、SHORT 回歸全綠；**`not slow` 測試有 8 條既有失敗**（CI 無資料庫卻直接開 DB），與 uv 無關，見 S4 章節 |
| S5 | `core/Dockerfile` 改用 uv | `core/Dockerfile` | 映像建得起來、`run.py --help` 可執行、仍是 editable 安裝 | 🔄 | Dockerfile 已改；**待開 Docker daemon 建置驗證** |
| S6 | 測試與 Dependabot 護欄改指 `uv.lock` | `tests/test_config_consistency.py`、`tests/test_order_state_parity.py`、`.github/dependabot.yml`、`pyproject.toml` | 護欄測試改寫後全綠；刻意讓 lock 過期時會被擋下 | ✅ | 2026-09-21：改寫為靜態比對 lock 的 `requires-dist`，反向驗證會紅 |
| S7 | 文件改寫並刪除 `requirements.txt` | `README.md`、`README_en.md`、`docs/**`、`frontend/README.md`、`strategy_lab/strategies/tsmc_overnight_signal/README.md`、相關 `backlog/*.md` | `grep -rn "requirements.txt"` 只剩 `frontend/requirements.txt` 相關；`python scripts/check_doc_paths.py` 全綠 | ✅ | 2026-09-21：`架構重構與冗餘收斂.md` 三處殘留**刻意保留**，依施作順序於最後一步統一修正 |

---

## 步驟詳述

### S1. 產生 `uv.lock` 並對齊現行版本 ✅

- **目的**：從 `pyproject.toml` 解析出 lock，且版本與現在實際在跑的環境一致，後續的測試才量得到「換工具」而不是「換版本」。
- **做法**：
  - 新增 `.python-version`，內容 `3.12`（本機 `.venv` 目前為 3.12.14，CI 與 Docker 也是 3.12）。
  - `pyproject.toml` 的 shioaji 暫時改為 `"shioaji==1.3.3"`，並在旁邊註解「升級另案處理」的理由；升級時由 [Shioaji升級至1.7.md](Shioaji升級至1.7.md) 放寬。
  - 執行 `uv lock`。uv 預設替所有平台解析，**shioaji 是平台相關 wheel**，若某平台無 wheel 導致解析失敗，在 `[tool.uv]` 加 `environments` 限定 macOS 與 Linux，並註明原因。
  - 以 `uv export --no-hashes --no-emit-project` 輸出，與 `requirements.txt` 逐套件比對。版本不同的套件用 `uv lock --upgrade-package <套件>==<requirements.txt 的版本>` 拉回，直到只剩 S2 要處理的差異。
- **產出**：`uv.lock`、`.python-version`、`pyproject.toml`。
- **驗證方式**：比對結果寫進本步驟章節：版本全數相同，或每一項差異都有原因。
- **相依**：無。

> **✅ 完成紀錄（2026-09-21，uv 0.12.17）**
> - 第一次 `uv lock` 沒有舊 lock 可參考，解析到各套件最新版，與 `requirements.txt` 有 60 個套件版本不同（例如 plotly 6.7.0 → 7.1.0、yfinance 1.3.0 → 1.7.0）。改為一次以 `-P <套件>==<版本>` 帶入 `requirements.txt` 全部 101 行重新解析後，**版本全數相同**。
> - 解析時沒有遇到平台限制，shioaji 1.3.3 在 uv 預設的全平台解析下成立，**不需要**在 `[tool.uv]` 加 `environments`。
> - lock 比 `requirements.txt` 多出的套件只有兩類，都是預期中的：
>   - 只在 Windows 安裝的 marker 相依：`colorama`、`tzdata`（另含 emscripten）、`win32-setctime`。
>   - extras 帶進來的：`dev`（`ruff`、`pytest-cov`、`coverage`）、`frontend`（`streamlit` 與其相依）、`tick`（`dolphindb`）、`lab`（`python-docx`）。現行 `requirements.txt` 本來就不含這些，CI 是在第二步 `pip install -e ".[dev]"` 才裝上 dev 工具。

### S2. 盤點 `requirements.txt` 多出的套件 ✅

- **目的**：`requirements.txt` 是整個 venv 的 freeze，可能含有 `pyproject.toml` 沒宣告、也不是任何主相依傳遞帶進來的套件。換成 lock 後它們會從環境消失，要先確認沒有程式在用。注意 `Flask`、`ipython` 看起來像多裝的，實際是 `finmind` 的傳遞相依（`pip show` 的 `Required-by` 確認過），不在差集裡。
- **做法**：
  - 取「freeze 有、lock 沒有」的差集，逐一查 repo 內是否 import（`core/`、`tasks/`、`scripts/`、`tests/`、`strategy_lab/`、`frontend/`、`run.py`）。
  - 有 import → 補進 `pyproject.toml` 的主相依或對應 extra。
  - 只是開發時的便利工具（例如 `ipykernel`）→ 放進 `dev` extra，或改用 `uv tool` 安裝，不進 lock。
  - 沒人用 → 移除。
  - `架構重構與冗餘收斂.md` 已盤點過零 import 的 `ta==0.5.25`，結論一併沿用。
- **產出**：`pyproject.toml`、`uv.lock`，以及本章節末的盤點表（套件、結論、理由）。
- **驗證方式**：差集中每個套件都有結論；S3 的全套測試通過。
- **相依**：S1。

> **✅ 完成紀錄（2026-09-21）**
> - **`requirements.txt` 對 lock 的差集為空**。`decorator`（經由 ipython）與 `frozendict`（經由 yfinance 1.3.0）一度看似多出來，版本拉回後都成為傳遞相依。
> - **`ta` 不能刪**：`pip show ta` 顯示 `Required-by: finmind`，它是 finmind 1.9.8 宣告的相依，lock 也標示 `via finmind`。本文件原本寫「沿用 `架構重構與冗餘收斂.md` 的零 import 結論」，但那份文件說它「零反向相依」並不成立；改用 lock 之後它由 finmind 帶進來，不需要也不應該另外處理。那份文件的對應列留待本工作與 Shioaji 升級完成後統一修正。
> - 本機 `.venv` 本身也與 `requirements.txt` 不一致，另外比對了一次：
>   - 8 個套件版本不同（例如 aiohttp 3.13.5 對 3.14.3、urllib3 2.6.3 對 2.7.0）。以 lock（＝`requirements.txt`，也就是 CI 實際在測的版本）為準，S3 重建後自然對齊。
>   - 多出 13 個套件：`ipykernel` 及其相依（`comm`、`debugpy`、`jupyter-client`、`jupyter-core`、`nbformat`、`fastjsonschema`、`pyzmq`、`tornado`、`appnope`），以及 `based58`、`ciso8601`、`pyyaml`。在 `core/`、`tasks/`、`scripts/`、`tests/`、`strategy_lab/`、`frontend/`、`run.py` 搜尋 import 皆為零，repo 也沒有 `.ipynb`，**全數不進 lock**。需要 Jupyter 時以 `uv run --with ipykernel` 臨時帶入。
> - `pyproject.toml` 不需要新增任何相依。

### S3. 本機切換到 uv 環境 ✅

- **目的**：確認 uv 建出的環境與現行環境行為相同。
- **做法**：
  - 先把現有 `.venv` 改名為 `.venv.pip-backup`（出問題時可以切回去對照），再執行 `uv sync --extra dev`。
  - 以 `uv run` 跑 `pytest`（含 `slow`）、`ruff check .`、`ruff format --check .`、`./scripts/run_regression.sh`。
  - 確認 editable 安裝仍有效：在 repo 以外的目錄執行 `uv run --project <repo> python -c "import core"`。`core/config/paths.py` 以 `__file__` 推算專案根目錄，非 editable 安裝會讓 `results/`、`logs/` 落錯位置。
  - 通過後刪除 `.venv.pip-backup`。
- **產出**：本機 `.venv`（不進版控）。
- **驗證方式**：驗收標準第 1 條。
- **相依**：S2。

> **✅ 完成紀錄（2026-09-21）**
> - `uv sync --locked --extra dev` 建出環境後：`uv run pytest -rs` **2003 passed、0 skipped**（72 秒）；`ruff check .`、`ruff format --check .` 全綠；`./scripts/run_regression.sh` 回歸雙線都實際執行且通過。
> - 在 `/tmp` 執行 `uv run --project <repo> python -c "import core"`，`core` 指向 repo 內的 `core/`，editable 安裝有效。
> - `scripts/run_regression.sh` 優先用 `.venv/bin/python`，uv 建的 `.venv` 在同一位置，腳本不需修改。
> - `.venv.pip-backup` 已刪除。

### S4. CI 改用 uv 🔄

- **目的**：CI 與本機從同一份 lock 安裝。現行 CI 要先 `pip install -r requirements.txt` 再 `pip install -e ".[dev]"`，第二步還可能把鎖定版本往上升。
- **做法**：
  - `actions/setup-python` 換成 `astral-sh/setup-uv`（開 `enable-cache`，快取鍵用 `uv.lock`）。
  - 安裝改為 `uv sync --locked --extra dev`：lock 與 `pyproject.toml` 不一致時直接失敗，取代原本由測試維持的一致性檢查。
  - 其餘步驟（ruff、`scripts/check_*.py`、SHORT 回歸線、`pytest -m "not slow"`、覆蓋率）改以 `uv run` 執行，指令與參數不變。
  - 原本「先裝 requirements.txt 的理由」那段註解（shioaji 1.7 的 `OrderState` 已不是 Enum）改寫成「為什麼要 `--locked`」。
- **產出**：`.github/workflows/ci.yml`。
- **驗證方式**：CI 綠燈；在分支上刻意改 `pyproject.toml` 而不更新 lock，確認 CI 在安裝步驟就紅。
- **相依**：S3。

> **🔄 進度（2026-09-21）**
> - 已完成：`actions/setup-python` 換成 `astral-sh/setup-uv@v10.1.0`（第一次寫成 `@v10`，CI 在 Set up job 就失敗：setup-uv 不發佈浮動主版本 tag），並把 uv 版本釘在 `0.12.17`（與本機相同）；Python 版本改由 `.python-version` 決定。安裝改為 `uv sync --locked --extra dev`，其餘步驟都加上 `uv run`。本機依 CI 順序逐步執行：除下述既有問題外全數通過（`not slow` 1982 passed、SHORT 回歸 6 passed）；`uv lock --check` 在 lock 過期時會提示重新 lock。
> - 2026-09-21 push 到 `feature/uv-migration` 後的 CI 結果（commit `f9cd550`）：`uv sync --locked`、ruff、三支閘門腳本、SHORT 回歸全綠；`pytest -m "not slow"` **1974 passed、8 failed**。
>   - 8 條失敗都是 `sqlite3.OperationalError: unable to open database file`：測試直接建立真的 `FuturesPriceAPI`、`StockLiveDataFeed` 等物件，去開 `data/db/` 底下的資料庫，而 CI 上沒有資料庫。分別是 `test_reporter_falls_back_to_the_near_month_splice`、`test_intraday_strategy_may_still_run_day_backtest`、`test_non_intraday_tick_backtest_is_untouched`、`test_single_strategy_is_not_a_special_path`、`test_singletons_are_shared_across_strategies`、`test_run_record_carries_audit_fields`、`test_notional_uses_the_instrument_unit`、`test_mixing_markets_with_disjoint_windows_is_refused`。
>   - **與 uv 無關**：本機以 `ALPHAEDGE_DATA_DIR` 指向空目錄模擬 CI，本分支與 `main` 都是同樣 8 條失敗。之所以一直沒被發現，是因為 `main` 的 CI 至少從 2026-09-19 起的 12 次都停在前面的「API 死介面檢查」，測試步驟根本沒有執行到；本機有資料庫，所以都是綠的。
> - 未完成：上述 8 條修好後 CI 全綠；「故意讓 lock 過期」的 CI 端驗證。
> - **既有問題，與本工作無關**：`main` 最近三次 CI（2026-09-20～21）都紅在「API 死介面檢查」，原因是 `FuturesMarginAPI.calculate_stock_futures_maintenance_margin` 既沒有呼叫點也沒有測試。把本工作的改動 stash 掉之後照樣紅。2026-09-21 決定補測試：`tests/test_api_public_interfaces.py` 新增 `test_calculate_stock_futures_maintenance_margin`，另外以一筆獨立的 commit 修正。

### S5. `core/Dockerfile` 改用 uv 🔄

- **目的**：映像與本機、CI 用同一份 lock。
- **做法**：
  - 從官方映像複製 uv 執行檔（`COPY --from=ghcr.io/astral-sh/uv:<版本> /uv /bin/uv`），版本釘死。
  - 維持「兩層安裝」的快取設計：先 COPY `pyproject.toml`、`uv.lock`，執行 `uv sync --frozen --no-install-project --no-dev`；COPY 原始碼後再 `uv sync --frozen`，專案本身以 editable 安裝。
  - 設 `UV_PROJECT_ENVIRONMENT` 或把 `.venv/bin` 加進 `PATH`，讓 `ENTRYPOINT ["python", "run.py"]` 用到的是 uv 建的環境。
  - 原本的 `pip check` 改為 `uv pip check`。
- **產出**：`core/Dockerfile`。
- **驗證方式**：`docker build -f core/Dockerfile -t alphaedge-core .` 成功；`docker run --rm alphaedge-core --help` 正常；容器內 `python -c "import core, pathlib; print(pathlib.Path(core.__file__))"` 指向 `/app/core`。
- **相依**：S3。

> **🔄 進度（2026-09-21）**
> - 已完成：`core/Dockerfile` 改寫：uv 從 `ghcr.io/astral-sh/uv:0.12.17` 複製；環境放在 `/opt/venv`（`UV_PROJECT_ENVIRONMENT`）並加進 `PATH`；安裝分兩層（先 `uv sync --frozen --no-install-project`，COPY 原始碼後再 `uv sync --frozen`）；`pip check` 改為 `uv pip check --python /opt/venv/bin/python`。
>   - **偏離原規格**：原規格寫「把 `.venv/bin` 加進 PATH」。改放 `/opt/venv` 的理由：`/app` 底下有目錄會被 compose 掛載，環境和原始碼分開比較安全。另外原規格的 `--no-dev` 拿掉了，因為 `dev` 是 extra 而不是 dependency group，`uv sync` 本來就不會裝它。
>   - `core.__file__` 是 namespace package，可能為 `None`；驗證時改看 `core.__path__[0]`。
> - 未完成：本機 Docker daemon 沒有開，還沒實際建置過映像。

### S6. 測試與 Dependabot 護欄改指 `uv.lock` ✅

- **目的**：現有護欄都以 `requirements.txt` 為前提，刪檔前要先改寫，否則護欄會直接失效或誤報。
- **做法**：
  - `tests/test_config_consistency.py` 的 `test_every_pyproject_dependency_is_pinned_in_requirements`：它守的「主相依都有鎖定版本」已由 `uv sync --locked`／`uv lock --check` 在 CI 保證，改寫為檢查 `uv.lock` 存在且 `uv lock --check` 通過；若 CI 已足夠擋下，則刪除並在模組 docstring 說明改由誰負責。
  - `tests/test_order_state_parity.py` 的 docstring：「量的是 `requirements.txt` 鎖定的 shioaji 版本」改為 `uv.lock`。
  - `.github/dependabot.yml`：`package-ecosystem` 由 `pip` 改為 `uv`，更新檔頭關於「requirements.txt 是完整 freeze」的說明；維持目前暫停中的 `open-pull-requests-limit: 0`。
  - `pyproject.toml` 內「鎖定的精確版本見 requirements.txt」「CI 以 `pip install -e ".[dev]"` 安裝」等註解改寫。
- **產出**：上列四個檔案。
- **驗證方式**：`uv run pytest tests/test_config_consistency.py tests/test_order_state_parity.py` 全綠。
- **相依**：S4（CI 的 `--locked` 先到位，測試才能放心移交責任）。

### S7. 文件改寫並刪除 `requirements.txt` ✅

- **目的**：所有安裝說明改成 uv，舊檔刪除後沒有指不到的引用。
- **做法**：
  - 改寫安裝段落：`README.md`、`README_en.md`（兩份同步）、`docs/setup/dev-setup.md`（含「重新產生鎖定檔」那段改為 `uv lock`／`uv lock --upgrade-package`）、`docs/dev/code-quality.md`、`docs/deployment/prod-deployment.md`、`docs/deployment/dev-deployment.md`、`frontend/README.md`、`strategy_lab/strategies/tsmc_overnight_signal/README.md`。
  - `pip install pre-commit` 改為 `uv tool install pre-commit`（`.pre-commit-config.yaml` 檔頭註解同步）。
  - 其他 backlog 中「產出 `requirements.txt`」的步驟改為 `uv.lock`：`台股tick改用TimescaleDB.md` Phase0-3、`PostgreSQL遷移計畫.md` Phase0-3、`實盤下單架構規劃.md` Phase0-1。
  - 刪除 `requirements.txt`。
- **產出**：上列文件；刪除 `requirements.txt`。
- **驗證方式**：`grep -rn "requirements.txt" --exclude-dir=.venv .` 只剩 `frontend/requirements.txt` 相關結果；`uv run python scripts/check_doc_paths.py` 全綠。
- **相依**：S4、S5、S6。

> **✅ 完成紀錄（2026-09-21）**
> - 已改寫：`README.md`／`README_en.md`（安裝、前端、開發工具、目錄樹補上 `pyproject.toml`／`uv.lock`）、`docs/setup/dev-setup.md`（原本的「重產 requirements」改成「相依的宣告與鎖定」一節）、`docs/dev/code-quality.md`、`docs/deployment/{dev,prod}-deployment.md`、`docs/futures/tw-futures-platform.md`、`frontend/README.md`、`strategy_lab/strategies/tsmc_overnight_signal/README.md`、`.pre-commit-config.yaml`；`台股tick改用TimescaleDB.md` 與 `PostgreSQL遷移計畫.md` 的 Phase0-3、`實盤下單架構規劃.md` 的 Phase0-1。
> - 順手修正「`pip install -e .`」這類泛指 editable 安裝的註解：`core/config/paths.py`、`docs/dev/runtime-artifacts.md`、`.gitignore`、`tests/test_strategy_data_access.py`、`scripts/check_layer_deps.py`；兩支 docx 報告腳本缺 python-docx 時的提示改為 `uv sync --extra lab`。
> - 文件新增一段提醒：`uv sync` 會把環境同步成剛好指定的內容，沒列在指令上的 extra 會被移除。
> - `requirements.txt` 已刪除；`check_doc_paths.py` 全綠。
> - **偏離原規格**：`grep` 還會搜到 `架構重構與冗餘收斂.md` 的三處（`ta` 那一列，以及 Phase 裡提到「照 README 只裝 requirements.txt」的兩處）。依 2026-09-21 定的施作順序，那份文件等 Shioaji 升級完成後統一更新，這裡刻意不動。

---

## 實作中記下的後續改善

不修不會壞、但值得另外處理的項目記在這裡（例如刻意不做的 `[dependency-groups]` 取捨）。本文件與 [Shioaji升級至1.7.md](Shioaji升級至1.7.md) 都完成後，兩份文件的本章節合併整理成一份新的 backlog 文件，再回頭更新 [架構重構與冗餘收斂.md](架構重構與冗餘收斂.md)。

1. **`dev` 要不要改成 `[dependency-groups]`**：這次依範圍界線沒有改。改成 dependency group 之後，`uv sync` 預設就會裝 dev 工具，也不會出現在發佈的套件 metadata 裡；代價是 CI、Dockerfile 與文件的指令都要跟著改，而且 pip 使用者看不到這一組。
2. **`架構重構與冗餘收斂.md` 對 `ta` 的結論要修正**：它是 finmind 的相依，不是「零反向相依」（見 S2）。另外，那份文件中「照 README 只裝 requirements.txt 的人跑 `generate_docx.py` 會 `ModuleNotFoundError`」那一項，在 uv 之後仍然成立，只是指令變成 `uv sync` 不含 `lab`；那一步的敘述要改寫。
3. **本機 `.venv` 曾與 `requirements.txt` 不一致**（8 個版本不同、13 個多餘套件，見 S2）：這正是本工作要解決的問題，已隨 S3 消除，不需另外處理，只記錄在這裡作為佐證。
4. **`main` 的 CI 既有紅燈**（API 死介面檢查，見 S4）：與 uv 無關，2026-09-21 已補測試修掉，不需另外處理。
5. **8 條 `not slow` 測試在沒有資料庫的環境會失敗**（見 S4）：與 uv 無關，被死介面檢查擋在前面才沒被發現。修法有兩種：標成 `slow`，或改成注入 in-memory DAO。會卡住本文件 S4 的 CI 驗收。
