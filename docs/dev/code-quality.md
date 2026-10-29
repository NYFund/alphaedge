# 程式碼品質工具鏈

> 本文件描述 `pyproject.toml`／`ruff`／CI／`pre-commit`／測試與覆蓋率的**現行設定與其理由**。
> coding style 的權威來源是 `CLAUDE.md` §2，本文件的 ruff 設定即為其可執行版本。

---

## 概觀

| 項目 | 檔案 | 作用 |
|------|------|------|
| 套件定義 | `pyproject.toml` `[project]` | 唯一的相依宣告；`uv sync` 以 editable 安裝後任意目錄可 `import core` |
| Lint／格式 | `pyproject.toml` `[tool.ruff]` | 執行 `CLAUDE.md` §2.4（型別註解）、§2.5（import 排序）、§2.10（行寬 88、雙引號） |
| 測試 | `pyproject.toml` `[tool.pytest.ini_options]`、`conftest.py`、`tests/conftest.py` | marker 定義、產物隔離、實連測試的閘門 |
| CI | `.github/workflows/ci.yml` | 每次 push 與 pull request 執行，兩個平行 job：`check`（靜態檢查與測試）、`docker`（映像建置與冒煙） |
| 本機防線 | `.pre-commit-config.yaml` | commit 前先跑同一組靜態檢查（需自行 `uv tool install pre-commit` ＋ `pre-commit install`） |

`uv.lock` 由 `uv lock` 從 `pyproject.toml` 解析產生，鎖定整棵相依樹；本機、CI（`--locked`）與 Docker build（`--frozen`）都從同一份安裝。
CI 的 uv 版本釘在 `astral-sh/setup-uv` 的 `version`，與 `Dockerfile` 複製的 uv 映像同版：lock 格式與解析行為跟著 uv 版本走。

### 常用指令

```bash
uv sync                             # 相依 + 專案本身 + dev group（pytest / pytest-timeout / pytest-cov / ruff）
                                    # 要前端、tick 與 lab 時：uv sync --extra frontend --extra tick --extra lab
                                    # 之後一律 uv run --no-sync，不帶的話 uv run 會先同步並移除 extras

ruff check .                        # lint
ruff format .                       # 格式化
python scripts/check_layer_deps.py  # 分層相依、循環 import、跨軸目錄污染等（見下方護欄表）
python scripts/check_doc_paths.py   # 文件裡的路徑與符號引用漂移
python scripts/check_api_orphan_methods.py  # core/api 零呼叫且零測試的公開方法
pytest -m "not slow"                # 略過需要資料庫與外部 API 的測試
pytest                              # 全部（需 data/db/tw_stock.db 等）
./scripts/run_regression.sh         # LONG ＋ SHORT 回歸，必須逐筆相同
```

### optional extras

`streamlit`（frontend）、`psycopg`／`connectorx`／`pyarrow`（tick）、`python-docx`／`yfinance`（lab）**刻意不放進主
`dependencies`**：日線回測與 ETL 主流程不需要它們，放進去只會讓每個環境（含 Docker 映像）變重。
tick 的兩個驅動只在 `core/dao/` 內惰性 import，沒裝時 `import core.dao` 照樣成功。
開發工具是預設會裝的 `dev` dependency group，加裝 extra 時不會被移除。

⚠️ 反過來，**extra 會被不帶它的 `uv sync`／`uv run` 移除**（`uv run` 預設先做一次同步）。
測試與腳本以子行程呼叫 `uv run` 時一律帶 `--no-sync`，否則跑一次測試就會拔掉開發環境的
`streamlit` 等套件；`tests/test_subprocess_uv_no_sync.py` 會掃 `tests/`、`scripts/` 擋這種寫法。

---

## 一、`ruff` 的規則與 ignore 清單

`select = ["E", "F", "I", "UP", "B", "BLE", "TRY", "ANN201", "ANN204"]`。
**ignore 清單裡的每一條都必須有理由**，分三類。改動前先確認屬於哪一類——尤其第一類，
「順手打開」會直接與專案規範打架。

### 1. 與 `CLAUDE.md` 衝突，永久關閉

| 規則 | 衝突點 |
|------|--------|
| `UP006`／`UP035` | §2.4.3 明訂用 `typing.List`／`Dict`，不改 `list[...]` |
| `UP007` | §2.4.3 `Union[X, Y]`，不改 `X \| Y` |
| `UP045` | §2.4.4 `Optional[T]`，不改 `T \| None` |
| `UP042` | §2.7 明訂 `class XxxEnum(str, Enum)`，不改 `StrEnum` |
| `E501` | formatter 已管行寬，但拆不了中文註解與 docstring；留著只會讓 CI 永遠紅燈，並逼人把中文註解硬折行 |
| `TRY003` | 例外訊息長度；與 §2.1 的中文訊息衝突 |

> ⚠️ **`UP` 家族不只上面幾條會動型別註解與 Enum 寫法**。只關其中幾條就跑
> `ruff check --fix`，會一次改掉數百個違反專案規範的地方。

### 2. 設計選擇，非缺陷

| 規則 | 理由 |
|------|------|
| `B027` | 基底類別刻意留的 no-op 掛點（例如股票的 `settle_daily`：股票沒有每日結算）。標成 `@abstractmethod` 反而會強迫每個子類別寫空實作 |
| `B905` | `zip(..., strict=)` 屬行為決策：補 `strict=True` 可能讓原本靜默的長度不一致改為拋錯，須逐點確認後再開 |

### 3. 待收斂，暫時關閉

`BLE001`（盲捕 `except Exception`）、`TRY300`／`TRY301`／`TRY201`／`TRY004`，
以及標為「潛在缺陷」的 `B006`（可變預設參數）／`B904`（`raise` 未帶 `from`）。
**修掉之後要把對應規則從 `ignore` 移除**，不要讓它長期留著——留著等於對那類問題
永久失明，而且「清單上有」會被誤讀成「還有那麼多處沒修」。

**數量一律以 `pyproject.toml` 的行尾註記為準，本文件不複製**：兩邊各記一份，
改了一邊就會分岔。`tests/test_quality_ratchet.py` 會比對註記數量與實際處數。

逐處位置不寫在文件裡（行號會漂移），一律現查：

```bash
ruff check . --select BLE001 --statistics   # 看數量
ruff check . --select B006,B904             # 看逐處位置
```

盲捕在 ETL 場景特別危險：它把「網路逾時」（該重試）與「資料 schema 變了」（該中止）混為一談。
收斂方向是讓例外有型別（`core/pipeline/utils/exceptions.py`），再逐檔把盲捕換成具名例外。

> **為什麼是 ignore 而不是在原地加 `# noqa`**：「規則保持啟用 ＋ 逐點 noqa」的 ratchet 作法
> 行不通——加上中文理由後該行超過 88 字元，`ruff format` 就把運算式拆成多行，
> `noqa` 註解跟著跑到別行、失去效力。若日後要改回 ratchet，`noqa` 必須不帶理由文字。

### per-file-ignores

| 對象 | 規則 | 理由 |
|------|------|------|
| `__init__.py` | `F401` | 套件的 re-export 門面，import 就是對外介面 |
| `tests/*`、`scripts/manual/*` | `ANN201` | 測試 helper／fixture／替身方法與手動腳本 helper 的回傳型別多半是「被 monkeypatch 過的 loader」或 `(物件, 路徑)`，標註要嘛得把類別從函式內部的 import 搬到檔頭（會讓 monkeypatch 失效），要嘛只能寫 `Any`。**`ANN204` 仍然生效**，`__init__` 一律要 `-> None` |

per-file-ignores 同樣不能長期失明：`tests/test_config_consistency.py` 會檢查每一條
指向的路徑還存在、且規則仍確實被觸發，過期的那條會讓測試紅。

### `ANN201`／`ANN204`

這兩條是 `CLAUDE.md` §2.4「所有函式回傳值都要標註，含 `-> None`」的閘門，
`core/`／`apps/`／`strategies/`／`frontend/` 都生效。沒有機器護欄時，缺漏會以百處為單位累積。

只開這兩條、不開整組 `ANN`：`ANN001`（參數）另有數百處未標，
而 `ANN002`／`ANN003`（`*args`／`**kwargs`）跟本專案既有寫法衝突（`ANN101`（`self`）已從 ruff 移除，不必再考慮）。

> ⚠️ **批次補回傳型別時，抽象方法必須單獨挑出來人工判**。它們的 body 是 `pass`，
> 用「函式自己有沒有 return 值」判斷必然得到 `None`——而子類回傳什麼完全看不出來
> （例如 `BaseDataCrawler.crawl()` 的子類有多種回傳型別）。只能逐一對照子類簽章；
> 子類回傳型別不一致時標 `Any` 並在該處註明理由。

### 其他設定

- **`*.md` 在 `extend-exclude` 內**：ruff 會連 Markdown 內的 Python 程式碼區塊一起格式化，
  而文件裡的範例常刻意對齊註解以利閱讀。`CLAUDE.md` §2.5／§2.10 規範的對象是程式碼，不是文件。
- **ruff 版本釘死**：dev group 的 `ruff` 版本須與 `.pre-commit-config.yaml` 的 `ruff-pre-commit` rev 一致。
  不釘的話格式規則一變就出現「本機綠、CI 紅」，而那種紅燈與程式碼品質無關，只會訓練大家忽略 CI。
- **工具快取目錄不必寫進 `.gitignore`**：`.ruff_cache` 與 `.pytest_cache` 會在自己的目錄裡寫一份
  內容為 `*` 的 `.gitignore` 自我排除。要驗證時測底下的檔案或用 `git add -An <dir>`；
  `git check-ignore .ruff_cache` 測目錄本身會回報未命中，看起來像漏了一條。

---

## 二、測試

### marker

| marker | 意義 | 預設行為 |
|--------|------|----------|
| `slow` | 需要 `data/db/*.db`、外部 API 或耗時較久（例如 LONG 回歸） | 照常收集；CI 以 `-m "not slow"` 排除 |
| `shioaji_sim` | 連 Shioaji 模擬環境的實連測試，要真實金鑰 | **預設 skip**，`-m` 字串明確寫出 `shioaji_sim` 才執行 |
| `shioaji_sim_order` | 會在模擬環境**送出委託**的實連測試 | **預設 skip**，必須單獨點名 `shioaji_sim_order`；只寫 `-m shioaji_sim` 不會順便送單 |

後兩者的閘門是 `tests/conftest.py` 的 `pytest_collection_modifyitems`。

**沒有會失敗的斷言就不是測試。** 要驗證行為就寫真的斷言；要人工探勘就寫成
`scripts/manual/` 底下的手動腳本。一支「整段包在 `try/except`、失敗時 `return False`」的
`test_*.py` 會被 pytest 判定 passed，永遠不會紅，還會被誤當成通過的證據。

### 測試不得寫進正式產物

專案根目錄的 `conftest.py` 在任何 `core` 模組 import 之前，把 `results/`、`logs/` 與 kill switch
導到本次 session 的暫存目錄，並把實盤紀錄庫（`LiveTradeDAO.DEFAULT_DB_PATH`）換成暫存路徑；
資料根不導走，因為 `slow` 測試要讀真的 `tw_stock.db`。session 開始與結束各記一次正式
`tw_trading.db`（含 `-wal`）與 `results/` 的檔案狀態，有差異就讓 pytest 以失敗結束並列出檔案。
`tests/conftest.py` 另把 `LogManager` 的檔案 sink 換成 no-op（原函式保留在 `real_setup_logger`，給驗 sink 行為的測試用）。

- **為什麼要這麼嚴**：實盤啟動時會讀回上一次的交易模式，測試留在正式紀錄庫的 `REDUCE_ONLY`
  會讓下一次真正的實盤直接降級或拒絕啟動。
- **為什麼放根目錄、不做成 `-p tests.xxx` plugin**：`core/config/paths.py` 在 import 時就把環境變數
  算成路徑常數，fixture 執行時已經來不及；而 kaleido 把自己的 `tests/` 裝成 site-packages
  的頂層套件，以 `tests.` 開頭的模組名載入 plugin 會載到它的。
- 要驗「未設定環境變數時的預設路徑」的測試，用 `pristine_config_paths` fixture 重新載入一份
  獨立的 `core/config/paths.py`，不要直接讀已被導走的常數。
- 測試建表一律走 `tests/conftest.py` 的 `dao_factory`，以 DAO 自己的建表方法建出正式 schema
  （見 [資料存取層](data-access-layer.md) §七）。

### 覆蓋率

```bash
pytest -m "not slow" --cov=core --cov=strategies --cov-report=term-missing
```

**刻意不設 `fail_under` 門檻**：在覆蓋率明顯偏低時設門檻只會鼓勵寫無效測試。
CI 會印出覆蓋率報告但不阻擋（與測試併在同一次 pytest）。量測範圍是 `core` 與 `strategies`：
入口層（`apps/`）只做參數解析與退出碼翻譯，實盤入口的測試又多以子行程執行而不被計入，刻意不量。

---

## 三、CI 跑得到與跑不到的護欄

`data/db/*.db` 未進版控，CI 也沒有 Shioaji／FinMind 金鑰。需要這些的測試一律標
`@pytest.mark.slow` 或 `pytestmark = pytest.mark.slow`。

`check` job 依序：`uv sync --locked --extra frontend --extra tick` → ruff lint／format →
分層相依 → 文件路徑 → pre-commit 其餘 hook → API 死介面 → SHORT 回歸線 → tick 整合測試 →
`pytest -m "not slow" --timeout=300 -rs` 併覆蓋率。job 帶一個 TimescaleDB service（映像版本與 `docker-compose.yml` 相同），
`TICK_DATABASE_URL` 指向它。

| 護欄 | 何處執行 | 說明 |
|------|----------|------|
| `ruff check` / `ruff format --check` | CI ＋ pre-commit | 版本與 `.pre-commit-config.yaml` 的 rev 一致 |
| `scripts/check_layer_deps.py` | CI ＋ pre-commit | 以 AST 掃全專案 import：`core/` 反向 import 到非 core 套件、低層 import 高層、循環 import、引擎本體（回測引擎、simulator、實盤 `trader.py`）的市場語意洩漏、跨軸目錄污染、策略套件門面 eager import 與 `core/strategies/` 放了具體策略、`core/dao/` 以外 import 資料庫驅動（`sqlite3`／`psycopg`／`connectorx`）、`core/adapters/` 做 I/O、框架 import `core.pipeline`、回測以外 import 回測內部零件（`core.backtest.models`／`datafeed`／`report`／`simulator`）。同層互相 import 與 `sys.path` 注入只列出、不計入結束碼 |
| `scripts/check_doc_paths.py` | CI ＋ pre-commit | `.md` 裡的路徑引用：指不到但同名檔在別處（搬檔）、指不到且 git 歷史存在過（刪檔），以及反引號內 `Class.method()` 的符號漂移 |
| `scripts/check_api_orphan_methods.py` | **只在 CI** | `core/api` 零呼叫且零測試的公開方法。**不放 pre-commit**：它要 import `core/api`，比其他 hook 慢一個量級 |
| `no-doc-step-refs`（pygrep） | CI ＋ pre-commit | 註解不得引用 backlog 步驟編號、健檢編號或 `backlog/` 路徑（`CLAUDE.md` §2.1 第 4 點）|
| `no-stdlib-exc-info`（pygrep） | CI ＋ pre-commit | 禁用 `exc_info=`（loguru 會默默丟掉，要用 `logger.opt(exception=True)`）；測試端的等價護欄是 `tests/test_entrypoint_and_logging.py`，兩邊的掃描範圍須一致 |
| YAML／TOML 語法、檔尾換行、行尾空白、大檔（256 KB） | CI ＋ pre-commit | `pre-commit-hooks` 的通用檢查。CI 以 `uvx pre-commit run --all-files` 跑 pre-commit 的其餘 hook（ruff 與兩支腳本前面已各跑一次，以 `SKIP` 略過）|
| SHORT 回歸線（`tests/backtest/test_short_regression.py`） | CI ＋ 本機 | 純記憶體、不需要資料庫。CI 單獨跑一次，壞掉時一眼看得到 |
| tick 整合測試（`tests/test_stock_tick_timescale.py`） | CI ＋ 本機（需 Docker 與 `--extra tick`） | CI 單獨跑一次，**任何一條 skip 即判紅**：少了 service 或 psycopg 時整個模組會 skip，只看結束碼會是假綠燈 |
| 前端冒煙測試（`tests/test_frontend_app_smoke.py`） | CI ＋ 本機 | 以 Streamlit `AppTest` 把 `frontend/app.py` 真的跑一遍；沒裝 `--extra frontend` 時略過並在 `-rs` 列出，CI 一定會裝 |
| 子行程 `uv run` 必帶 `--no-sync`（`tests/test_subprocess_uv_no_sync.py`） | CI ＋ 本機 | 見上方 optional extras |
| 測試產物隔離與絆線（專案根目錄 `conftest.py`） | 每一次 pytest | 見「測試不得寫進正式產物」 |
| **LONG 回歸線與 `slow` 測試** | **只在本機** | 需要 `data/db/tw_stock.db`、`tw_futures.db` 或外部 API |
| 映像建置與冒煙（`docker` job） | **只在 CI** | core 映像跑一次預設的 `python -m apps.backtest --help`，並另外 `import strategies, strategies.stock, strategies.futures`（`--help` 不載入策略，漏 COPY 頂層 `strategies/` 時要到指定策略那一刻才炸）；frontend 映像在工作目錄 `/` 下 import `frontend.config` 與 `app.py` 的相依（在 `/app` 底下跑的話，拿掉 `PYTHONPATH=/app` 也照樣 import 得到，就驗不到 Streamlit 實際執行時的條件）|

**`-rs` 不可拿掉**：沒有它，被 skip 的測試在輸出裡只是一個 `s`，整組因為 fixture 不存在而永遠跳過也看起來是綠的。

**skip 不算通過**：`scripts/run_regression.sh` 以 `-rs` 執行並偵測 `SKIPPED`，有即以**結束碼 3**
結束並印出是哪一條、為什麼。否則「沒有資料庫」與「回歸真的通過」在輸出上會長得一模一樣。
動引擎前請在有資料庫的機器跑一次。

驗證 CI 是否真的會綠，可用「移除資料庫與金鑰的副本」在本機模擬：

```bash
rsync -a --exclude='.venv' --exclude='.git' --exclude='data/db' --exclude='.env' ./ /tmp/cisim/
cd /tmp/cisim && env -u API_KEY -u API_SECRET_KEY python -m pytest tests -q -m "not slow"
```

---

## 四、打包時會踩到的坑

### 4.1 新增套件目錄要補 `__init__.py`

`[tool.setuptools.packages.find]` 收 `core*`、`apps*`、`strategies*`、`tests*`，所有含 `.py` 的套件目錄都有 `__init__.py`。
`namespaces = true` 仍保留當保險：缺 `__init__.py` 的目錄會被當成 PEP 420 namespace package，
關掉這個選項的話，editable 安裝後該套件會 import 不到——
而且**不會在安裝時報錯，是執行期才炸**。

### 4.2 `strategy_lab/` 不在 `packages.find` 裡

`strategy_lab/` 的腳本一律以 `python -m strategy_lab.…` 從 repo 根目錄執行。直接跑檔案路徑會
`ModuleNotFoundError`——這是刻意的，比靠 `sys.path.insert` 硬塞而安靜成功要好。
`tests/test_strategy_data_access.py` 會擋 `sys.path` 注入再出現。

### 4.3 macOS：隱藏旗標會讓 `.pth` 完全失效

若 `.venv` 內的檔案帶著 macOS 的 `UF_HIDDEN` 旗標，**CPython ≥3.11 的 `site.addpackage()`
會靜默略過隱藏的 `.pth`**——editable 安裝因此完全不生效，且沒有任何錯誤訊息。

症狀是 `uv sync` 顯示成功，但在 repo 以外的目錄 `import core` 仍然 `ModuleNotFoundError`。
檢查與修復：

```bash
ls -lO .venv/lib/python3.12/site-packages/*.pth   # 出現 hidden 即中招
chflags nohidden .venv .venv/lib/python3.12/site-packages \
                 .venv/lib/python3.12/site-packages/*.pth
```

**這個旗標可能會反覆回來**（新安裝套件產生的 `.pth` 可能一出生就是隱藏的，
例如從隱藏的父目錄繼承，或有備份／同步工具在背景重新標記）。
因此不要把 editable 安裝當成本機的可靠前提；執行需要 `import core` 的獨立腳本時，
直接指定 `PYTHONPATH`：

```bash
PYTHONPATH=. python scripts/some_script.py
```

從 repo 根目錄執行 `python -m pytest` 或 `python -m apps.backtest` 不受影響（cwd 會進 `sys.path`）。

---

## 相關文件

- [開發環境設定](../setup/dev-setup.md)——建立 venv 與環境變數
- [多市場回測引擎架構](../backtest/multi-market-engine.md)——§六回歸護欄說明 `scripts/run_regression.sh` 的職責
- [資料存取層](data-access-layer.md)——`core/dao/` 的分層規則與測試建表方式
- `CLAUDE.md` §2——coding style 的權威來源
