# 開發環境設定（Dev Setup）

本文件針對目前 `AlphaEdge` 專案實際結構整理（以 `run.py`、`tasks/update_db.py`、`core/` 為主）。

## 前置需求

- Python **3.12+**（`pyproject.toml` 的 `requires-python = ">=3.12"`；CI 亦使用 3.12）
- [uv](https://docs.astral.sh/uv/)（`brew install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh | sh`）。
  uv 會依 `.python-version` 自動取得對應的 Python，本機沒有 3.12 也能建環境
- Git
- （選用）DolphinDB：若要使用 tick 相關 API/更新

## 1) 建立環境並安裝套件

```bash
uv sync                      # 建立 .venv，裝相依、專案本身與開發工具
source .venv/bin/activate    # 或不啟用，改在指令前加 `uv run`
```

`uv sync` 依 `uv.lock` 安裝每個套件的精確版本，並把專案本身裝成 editable，
因此**一行指令就完成環境建置**。安裝後 `core` / `tasks` / `tests` 於**任意工作目錄**皆可 import，
不需再設 `PYTHONPATH`。`.venv` 是可以隨時重建的產物：壞了就 `rm -rf .venv && uv sync`。

開發工具（pytest、pytest-timeout、pytest-cov、ruff）是 `pyproject.toml` 的 `[dependency-groups].dev`，
`uv sync` 預設就會裝，正式映像以 `--no-dev` 排除。其他選用相依（預設不裝，主流程不需要）：
`frontend` Streamlit 介面、`tick` DolphinDB tick 儲存、`lab` `strategy_lab` 報告輸出。

**`uv sync` 會把環境同步成「剛好」指定的內容**，沒列在指令上的 extra 會被移除（`dev` group 不受影響）。
要同時使用多組 extra 時一起列出（`uv sync --extra frontend --extra lab`），或用 `uv sync --all-extras`。
以 pip 安裝時，開發工具要用 `pip install --group dev`（pip 25.1 起支援），不再是 `.[dev]`。

## 2) 相依的宣告與鎖定

- `pyproject.toml`：唯一的相依宣告（套件名稱、版本下限、optional extras、Python 版本下限）。
- `uv.lock`：由 `uv lock` 從 `pyproject.toml` 解析產生，鎖住整棵相依樹（含傳遞相依）。
  本機、CI（`uv sync --locked`）與 `core/Dockerfile`（`uv sync --frozen`）都從這一份安裝，**要進版控、不要手改**。

lock 裡的 Flask、ipython、ta、pytest 看似無關，其實是 FinMind 自己宣告的相依，移不掉。

常用操作：

```bash
uv add <套件>                          # 新增主相依（同時更新 pyproject.toml 與 uv.lock）
uv add --optional dev <套件>           # 新增到某個 extra
uv lock                                # 手改 pyproject.toml 後重新解析；已鎖定的版本不會變動
uv lock --upgrade-package <套件>       # 只升級單一套件（與它強制要求的傳遞相依）
uv lock --check                        # 確認 lock 與 pyproject.toml 一致
```

改了 `pyproject.toml` 卻忘了 `uv lock` 時，`tests/test_config_consistency.py` 會在本機就紅，
CI 的 `uv sync --locked` 也會在安裝步驟直接失敗。

## 3) 設定環境變數

```bash
cp .env.example .env
```

請依需求填寫 `.env`：

- DolphinDB（tick 需要）：`DDB_PATH`、`DDB_HOST`、`DDB_PORT`、`DDB_USER`、`DDB_PASSWORD`
- Shioaji：`API_KEY`、`API_SECRET_KEY`
- FinMind：`FINMIND_API_TOKEN`
- （選填）多組 Shioaji 帳號輪替：`API_KEY_1`~`API_KEY_4`、`API_SECRET_KEY_1`~`API_SECRET_KEY_4`（`core/config/settings.py` 的 `NUM_API`）
- （選填）執行期產物根目錄覆寫：`ALPHAEDGE_DATA_DIR`／`ALPHAEDGE_RESULTS_DIR`／`ALPHAEDGE_LOGS_DIR`（見 [執行期產物](../dev/runtime-artifacts.md)）。**前端讀的是同一個 `ALPHAEDGE_RESULTS_DIR`**，不設也能跑（預設 `PROJECT_ROOT/results`）；舊名 `ALPHAEDGE_BACKTEST_RESULTS` 仍相容一版並會發出警告
- （選填）回測畫完圖在瀏覽器開啟：`ALPHAEDGE_SHOW_FIGURES=1`（等同 `run.py --show`；預設不開）

`.env.example` 與程式實際讀取的環境變數由 `tests/test_config_consistency.py` 雙向核對：
程式新增一個 `os.getenv("X")` 卻沒補進範本，或範本留著程式已不再讀的鍵，測試都會失敗。

## 4) 初始化資料目錄（選用）

多數目錄會在執行時自動建立；若要先手動準備可建立：

```bash
mkdir -p data/db data/downloads logs results
```

## 5) 基本驗證

```bash
# 檢查主要模組可載入
# 注意：一律用完整模組路徑。core/backtest/__init__.py 與 core/strategies/__init__.py
# 刻意不做套件層 eager import（會造成循環 import），故 `from core.backtest import
# Backtester` 會失敗
python -c "from core.backtest.backtester import Backtester; from core.strategies.strategy_loader import StrategyLoader; print('OK')"

# 顯示主程式參數
python run.py --help

# 顯示資料更新參數
python -m tasks.update_db --help
```

## 6) 程式碼品質檢查（選用但建議）

```bash
ruff check .            # lint
ruff format .           # 格式化
pytest -m "not slow"    # 略過需要 tw_stock.db 與外部 API 憑證的測試
```

可安裝 pre-commit 讓每次 commit 前自動跑同一組檢查：

```bash
uv tool install pre-commit && pre-commit install
```

設定理由與暫時關閉的規則見 [程式碼品質工具鏈](../dev/code-quality.md)。

---

完成後可參考 [開發部署](../deployment/dev-deployment.md)。
