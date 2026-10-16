# 開發環境設定（Dev Setup）

從 clone 到能跑回測、資料更新與測試的步驟。專案入口在 `apps/`（`apps.backtest`、`apps.live`、`apps.update_db`），
框架在 `core/`，具體策略在頂層 `strategies/`。

## 前置需求

- Python **3.12+**（`pyproject.toml` 的 `requires-python = ">=3.12"`；`.python-version` 為 `3.12`，CI 與 `Dockerfile` 亦同）
- [uv](https://docs.astral.sh/uv/)（`brew install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh | sh`）。
  uv 會依 `.python-version` 自動取得對應的 Python，本機沒有 3.12 也能建環境
- Git
- （選用）Docker Desktop：台股 tick 存在 TimescaleDB，以 `docker compose up -d postgres` 啟動。
  只有 tick 級回測、tick 更新與 tick 整合測試需要

## 1) 建立環境並安裝套件

```bash
uv sync                      # 建立 .venv，裝相依、專案本身與開發工具
source .venv/bin/activate    # 或不啟用，改在指令前加 `uv run --no-sync`
```

`uv sync` 依 `uv.lock` 安裝每個套件的精確版本，並把專案本身（`core`／`apps`／`strategies`／`tests`）裝成 editable，
因此在**任意工作目錄**都能 import，不需設 `PYTHONPATH`。`.venv` 可隨時重建：壞了就 `rm -rf .venv && uv sync`。

開發工具（pytest、pytest-timeout、pytest-cov、ruff）放在 `pyproject.toml` 的 `[dependency-groups].dev`，
`uv sync` 預設就會裝，正式映像以 `--no-dev` 排除。選用相依（預設不裝，日線回測與 ETL 不需要）：

| extra | 內容 | 用途 |
|-------|------|------|
| `frontend` | `streamlit` | `frontend/` 的 Streamlit 介面與前端冒煙測試 |
| `tick` | `psycopg[binary]`、`connectorx`、`pyarrow` | 台股 tick 的 TimescaleDB 寫入／讀取 |
| `lab` | `python-docx`、`yfinance` | `strategy_lab/` 的報告輸出與美股／匯率資料 |

⚠️ **`uv sync` 會把環境同步成「剛好」指令列出的內容**，沒列出的 extra 會被移除（`dev` group 不受影響）。
要同時使用多組時一起列出（`uv sync --extra frontend --extra lab --extra tick`），或用 `uv sync --all-extras`。
**`uv run` 預設也會先做同步**：環境建好後一律用 `uv run --no-sync ...`，否則跑一次指令就可能拔掉已裝的 extra
（排程同理，見 [實盤部署與排程](../deployment/live-deployment.md)）。

以 pip 安裝時，開發工具要用 `pip install --group dev`（pip 25.1 起支援），不是 `.[dev]`。

## 2) 相依的宣告與鎖定

- `pyproject.toml`：唯一的相依宣告（套件名稱、版本下限、extras、Python 版本下限）。
  主 `dependencies` 只列程式碼實際 import 的套件，另加少數執行期才用到、import 掃描抓不到的相依（例如 kaleido、html5lib），
  每一項的理由寫在該檔的註解裡。
- `uv.lock`：由 `uv lock` 從 `pyproject.toml` 解析產生，鎖住整棵相依樹（含傳遞相依）。
  本機、CI（`uv sync --locked`）與根目錄的 `Dockerfile`（`uv sync --frozen`）都從這一份安裝，**要進版控、不要手改**。

lock 裡的 Flask、ipython、ta、pytest 看似無關，其實是 FinMind 自己宣告的相依，移不掉。

常用操作：

```bash
uv add <套件>                          # 新增主相依（同時更新 pyproject.toml 與 uv.lock）
uv add --optional <extra> <套件>       # 新增到某個 extra（frontend／tick／lab）
uv add --dev <套件>                    # 新增到 dev group（開發工具）
uv lock                                # 手改 pyproject.toml 後重新解析；已鎖定的版本不會變動
uv lock --upgrade-package <套件>       # 只升級單一套件（與它強制要求的傳遞相依）
uv lock --check                        # 確認 lock 與 pyproject.toml 一致
```

改了 `pyproject.toml` 卻忘了 `uv lock` 時，`tests/test_config_consistency.py` 在本機就會紅，
CI 的 `uv sync --locked` 也會在安裝步驟直接失敗。

## 3) 設定環境變數

```bash
cp .env.example .env
```

依需求填寫 `.env`（各鍵的細節以 `.env.example` 的註解為準）：

| 用途 | 環境變數 | 備註 |
|------|----------|------|
| Shioaji | `API_KEY`、`API_SECRET_KEY` | 行情、tick 爬取與實盤 |
| FinMind | `FINMIND_API_TOKEN` | |
| TimescaleDB | `TICK_DATABASE_URL` | 只有 tick 需要。本機為 `postgresql://alphaedge:alphaedge@localhost:5432/alphaedge`，帳密須與 `POSTGRES_*` 一致；`docker compose` 的 `core` 容器會自動改用 service 名稱 `postgres` |
| （選填）多帳號輪替 | `API_KEY_1`～`API_KEY_4`、`API_SECRET_KEY_1`～`API_SECRET_KEY_4` | 組數為 `core/config/settings.py` 的 `NUM_API` |
| （選填）產物根目錄 | `ALPHAEDGE_DATA_DIR`／`ALPHAEDGE_RESULTS_DIR`／`ALPHAEDGE_LOGS_DIR` | 見 [執行期產物](../dev/runtime-artifacts.md)。前端讀同一個 `ALPHAEDGE_RESULTS_DIR`；舊名 `ALPHAEDGE_BACKTEST_RESULTS` 仍相容並會發出警告 |
| （選填）回測開圖 | `ALPHAEDGE_SHOW_FIGURES=1` | 等同 `python -m apps.backtest --show`；預設不開 |
| （選填）實盤 | `SHIOAJI_CA_PATH`／`SHIOAJI_CA_PASSWORD`／`SHIOAJI_PERSON_ID`、`ALPHAEDGE_LIVE_KILL_SWITCH_PATH`、`ALPHAEDGE_LIVE_NOTIFY_CHANNEL`／`_TOKEN`／`_TARGET` | 見 [實盤部署與排程](../deployment/live-deployment.md)。刻意沒有「連正式環境」的環境變數，只能由命令列開啟 |
| （選填）compose 內插 | `STRATEGY`、`ALPHAEDGE_CA_DIR`、`SHIOAJI_CA_FILE`、`POSTGRES_USER`／`POSTGRES_PASSWORD`／`POSTGRES_DB` | 程式不讀，只給 `docker compose` 解析設定檔用 |

`.env.example` 與程式實際讀取的環境變數由 `tests/test_config_consistency.py` 雙向核對：
程式新增一個 `os.getenv("X")` 卻沒補進範本，或範本留著程式已不再讀的鍵，測試都會失敗。

## 4) 資料目錄與 TimescaleDB

`data/`、`results/`、`logs/` 會在實際寫檔時自動建立，不需預先準備。
日線資料庫（`data/db/tw_stock.db` 等）不進版控，要以 `python -m apps.update_db` 建立或從他處複製。

要用台股 tick 時：

```bash
uv sync --extra tick          # 連同其他要保留的 extra 一起列
docker compose up -d postgres # TimescaleDB；restart: unless-stopped，Docker Desktop 重啟後會自己起來
```

## 5) 基本驗證

```bash
# 檢查主要模組可載入。取具體實作一律用完整模組路徑：core/backtest/__init__.py 與
# core/strategies/__init__.py 刻意不做套件層 eager import（會造成循環 import），
# 故 `from core.backtest import Backtester` 會失敗
python -c "from core.backtest.backtester import Backtester; from strategies.loader import StrategyLoader; print('OK')"

# 回測／實盤／資料更新入口的參數
python -m apps.backtest --help
python -m apps.live --help
python -m apps.update_db --help
```

## 6) 程式碼品質檢查

```bash
ruff check .            # lint
ruff format .           # 格式化
pytest -m "not slow"    # 略過需要 data/db/*.db 與外部 API 憑證的測試
```

可安裝 pre-commit，讓每次 commit 前自動跑同一組檢查：

```bash
uv tool install pre-commit && pre-commit install
```

完整的檢查清單、CI 步驟與 ruff 規則的理由見 [程式碼品質工具鏈](../dev/code-quality.md)。

---

完成後可參考 [開發部署](../deployment/dev-deployment.md)。
