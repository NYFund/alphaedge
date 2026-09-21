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
| S1 | 產生 `uv.lock` 並對齊現行版本 | `uv.lock`、`.python-version`、`pyproject.toml` | `uv export` 與 `requirements.txt` 逐套件比對，版本全數相同（或差異逐項記錄並說明） | ⬜ | shioaji 以 `==1.3.3` 釘住 |
| S2 | 盤點 `requirements.txt` 多出的套件 | `pyproject.toml`、本文件 | 每個「lock 裡沒有、freeze 裡有」的套件都有保留／移除的結論 | ⬜ | 相依 S1 |
| S3 | 本機切換到 uv 環境 | `.venv`（不進版控） | 驗收標準第 1 條 | ⬜ | 相依 S2 |
| S4 | CI 改用 uv | `.github/workflows/ci.yml` | CI 綠燈，且 `uv lock --check` 在 lock 過期時會紅 | ⬜ | 相依 S3 |
| S5 | `core/Dockerfile` 改用 uv | `core/Dockerfile` | 映像建得起來、`run.py --help` 可執行、仍是 editable 安裝 | ⬜ | 相依 S3 |
| S6 | 測試與 Dependabot 護欄改指 `uv.lock` | `tests/test_config_consistency.py`、`tests/test_order_state_parity.py`、`.github/dependabot.yml`、`pyproject.toml` | 護欄測試改寫後全綠；刻意讓 lock 過期時會被擋下 | ⬜ | 相依 S4 |
| S7 | 文件改寫並刪除 `requirements.txt` | `README.md`、`README_en.md`、`docs/**`、`frontend/README.md`、`strategy_lab/strategies/tsmc_overnight_signal/README.md`、相關 `backlog/*.md` | `grep -rn "requirements.txt"` 只剩 `frontend/requirements.txt` 相關；`python scripts/check_doc_paths.py` 全綠 | ⬜ | 相依 S4~S6 |

---

## 步驟詳述

### S1. 產生 `uv.lock` 並對齊現行版本 ⬜

- **目的**：從 `pyproject.toml` 解析出 lock，且版本與現在實際在跑的環境一致，後續的測試才量得到「換工具」而不是「換版本」。
- **做法**：
  - 新增 `.python-version`，內容 `3.12`（本機 `.venv` 目前為 3.12.14，CI 與 Docker 也是 3.12）。
  - `pyproject.toml` 的 shioaji 暫時改為 `"shioaji==1.3.3"`，並在旁邊註解「升級另案處理」的理由；升級時由 [Shioaji升級至1.7.md](Shioaji升級至1.7.md) 放寬。
  - 執行 `uv lock`。uv 預設替所有平台解析，**shioaji 是平台相關 wheel**，若某平台無 wheel 導致解析失敗，在 `[tool.uv]` 加 `environments` 限定 macOS 與 Linux，並註明原因。
  - 以 `uv export --no-hashes --no-emit-project` 輸出，與 `requirements.txt` 逐套件比對。版本不同的套件用 `uv lock --upgrade-package <套件>==<requirements.txt 的版本>` 拉回，直到只剩 S2 要處理的差異。
- **產出**：`uv.lock`、`.python-version`、`pyproject.toml`。
- **驗證方式**：比對結果寫進本步驟章節：版本全數相同，或每一項差異都有原因。
- **相依**：無。

### S2. 盤點 `requirements.txt` 多出的套件 ⬜

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

### S3. 本機切換到 uv 環境 ⬜

- **目的**：確認 uv 建出的環境與現行環境行為相同。
- **做法**：
  - 先把現有 `.venv` 改名為 `.venv.pip-backup`（出問題時可以切回去對照），再執行 `uv sync --extra dev`。
  - 以 `uv run` 跑 `pytest`（含 `slow`）、`ruff check .`、`ruff format --check .`、`./scripts/run_regression.sh`。
  - 確認 editable 安裝仍有效：在 repo 以外的目錄執行 `uv run --project <repo> python -c "import core"`。`core/config/paths.py` 以 `__file__` 推算專案根目錄，非 editable 安裝會讓 `results/`、`logs/` 落錯位置。
  - 通過後刪除 `.venv.pip-backup`。
- **產出**：本機 `.venv`（不進版控）。
- **驗證方式**：驗收標準第 1 條。
- **相依**：S2。

### S4. CI 改用 uv ⬜

- **目的**：CI 與本機從同一份 lock 安裝。現行 CI 要先 `pip install -r requirements.txt` 再 `pip install -e ".[dev]"`，第二步還可能把鎖定版本往上升。
- **做法**：
  - `actions/setup-python` 換成 `astral-sh/setup-uv`（開 `enable-cache`，快取鍵用 `uv.lock`）。
  - 安裝改為 `uv sync --locked --extra dev`：lock 與 `pyproject.toml` 不一致時直接失敗，取代原本由測試維持的一致性檢查。
  - 其餘步驟（ruff、`scripts/check_*.py`、SHORT 回歸線、`pytest -m "not slow"`、覆蓋率）改以 `uv run` 執行，指令與參數不變。
  - 原本「先裝 requirements.txt 的理由」那段註解（shioaji 1.7 的 `OrderState` 已不是 Enum）改寫成「為什麼要 `--locked`」。
- **產出**：`.github/workflows/ci.yml`。
- **驗證方式**：CI 綠燈；在分支上刻意改 `pyproject.toml` 而不更新 lock，確認 CI 在安裝步驟就紅。
- **相依**：S3。

### S5. `core/Dockerfile` 改用 uv ⬜

- **目的**：映像與本機、CI 用同一份 lock。
- **做法**：
  - 從官方映像複製 uv 執行檔（`COPY --from=ghcr.io/astral-sh/uv:<版本> /uv /bin/uv`），版本釘死。
  - 維持「兩層安裝」的快取設計：先 COPY `pyproject.toml`、`uv.lock`，執行 `uv sync --frozen --no-install-project --no-dev`；COPY 原始碼後再 `uv sync --frozen`，專案本身以 editable 安裝。
  - 設 `UV_PROJECT_ENVIRONMENT` 或把 `.venv/bin` 加進 `PATH`，讓 `ENTRYPOINT ["python", "run.py"]` 用到的是 uv 建的環境。
  - 原本的 `pip check` 改為 `uv pip check`。
- **產出**：`core/Dockerfile`。
- **驗證方式**：`docker build -f core/Dockerfile -t alphaedge-core .` 成功；`docker run --rm alphaedge-core --help` 正常；容器內 `python -c "import core, pathlib; print(pathlib.Path(core.__file__))"` 指向 `/app/core`。
- **相依**：S3。

### S6. 測試與 Dependabot 護欄改指 `uv.lock` ⬜

- **目的**：現有護欄都以 `requirements.txt` 為前提，刪檔前要先改寫，否則護欄會直接失效或誤報。
- **做法**：
  - `tests/test_config_consistency.py` 的 `test_every_pyproject_dependency_is_pinned_in_requirements`：它守的「主相依都有鎖定版本」已由 `uv sync --locked`／`uv lock --check` 在 CI 保證，改寫為檢查 `uv.lock` 存在且 `uv lock --check` 通過；若 CI 已足夠擋下，則刪除並在模組 docstring 說明改由誰負責。
  - `tests/test_order_state_parity.py` 的 docstring：「量的是 `requirements.txt` 鎖定的 shioaji 版本」改為 `uv.lock`。
  - `.github/dependabot.yml`：`package-ecosystem` 由 `pip` 改為 `uv`，更新檔頭關於「requirements.txt 是完整 freeze」的說明；維持目前暫停中的 `open-pull-requests-limit: 0`。
  - `pyproject.toml` 內「鎖定的精確版本見 requirements.txt」「CI 以 `pip install -e ".[dev]"` 安裝」等註解改寫。
- **產出**：上列四個檔案。
- **驗證方式**：`uv run pytest tests/test_config_consistency.py tests/test_order_state_parity.py` 全綠。
- **相依**：S4（CI 的 `--locked` 先到位，測試才能放心移交責任）。

### S7. 文件改寫並刪除 `requirements.txt` ⬜

- **目的**：所有安裝說明改成 uv，舊檔刪除後沒有指不到的引用。
- **做法**：
  - 改寫安裝段落：`README.md`、`README_en.md`（兩份同步）、`docs/setup/dev-setup.md`（含「重新產生鎖定檔」那段改為 `uv lock`／`uv lock --upgrade-package`）、`docs/dev/code-quality.md`、`docs/deployment/prod-deployment.md`、`docs/deployment/dev-deployment.md`、`frontend/README.md`、`strategy_lab/strategies/tsmc_overnight_signal/README.md`。
  - `pip install pre-commit` 改為 `uv tool install pre-commit`（`.pre-commit-config.yaml` 檔頭註解同步）。
  - 其他 backlog 中「產出 `requirements.txt`」的步驟改為 `uv.lock`：`台股tick改用TimescaleDB.md` Phase0-3、`PostgreSQL遷移計畫.md` Phase0-3、`實盤下單架構規劃.md` Phase0-1。
  - 刪除 `requirements.txt`。
- **產出**：上列文件；刪除 `requirements.txt`。
- **驗證方式**：`grep -rn "requirements.txt" --exclude-dir=.venv .` 只剩 `frontend/requirements.txt` 相關結果；`uv run python scripts/check_doc_paths.py` 全綠。
- **相依**：S4、S5、S6。
