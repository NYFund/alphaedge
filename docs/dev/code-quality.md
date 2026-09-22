# 程式碼品質工具鏈

> 本文件描述 `pyproject.toml`／`ruff`／CI／`pre-commit`／覆蓋率的**現行設定與其理由**。
> coding style 的權威來源是 `CLAUDE.md` §2，本文件的 ruff 設定即為其可執行版本。

---

## 概觀

| 項目 | 檔案 | 作用 |
|------|------|------|
| 套件定義 | `pyproject.toml` `[project]` | 唯一的相依宣告；`uv sync` 以 editable 安裝後任意目錄可 `import core` |
| Lint／格式 | `pyproject.toml` `[tool.ruff]` | 執行 `CLAUDE.md` §2.5（import 排序）、§2.10（行寬 88、雙引號） |
| CI | `.github/workflows/ci.yml` | 每次 push 跑 lint、格式、分層相依檢查、文件路徑檢查、API 死介面檢查、SHORT 回歸線、`pytest -m "not slow"` |
| 本機防線 | `.pre-commit-config.yaml` | commit 前先跑一次同一組檢查（需自行 `pre-commit install`） |

`uv.lock` 由 `uv lock` 從 `pyproject.toml` 解析產生，鎖定整棵相依樹；本機、CI（`--locked`）與 Docker build（`--frozen`）都從同一份安裝。

### 常用指令

```bash
uv sync                             # 相依 + 專案本身 + pytest / pytest-cov / ruff（dev group 預設即裝）

ruff check .                        # lint
ruff format .                       # 格式化
python scripts/check_layer_deps.py  # 分層相依、循環 import、跨軸目錄污染
python scripts/check_doc_paths.py   # 文件裡的路徑引用與 Markdown 連結
pytest -m "not slow"                # 略過需要 tw_stock.db 與外部 API 的測試
pytest                              # 全部（需 data/db/tw_stock.db）
./scripts/run_regression.sh         # LONG ＋ SHORT 回歸，必須逐筆相同
```

### optional extras

`dolphindb`（tick）、`streamlit`（frontend）、`python-docx`（lab）**刻意不放進主
`dependencies`**：回測與 ETL 主流程不需要它們。
需要時以 `uv sync --extra tick` 等方式個別安裝；開發工具是預設會裝的 `dev` dependency group，加裝 extra 時不會被移除。

---

## 一、`ruff` 的 ignore 清單為什麼長這樣

**清單裡的每一條都必須有理由**，分三類。改動前先確認屬於哪一類——尤其第一類，
「順手打開」會直接與專案規範打架。

### 1. 與 `CLAUDE.md` 衝突，永久關閉

| 規則 | 衝突點 |
|------|--------|
| `UP006`／`UP035` | §2.4.3 明訂用 `typing.List`／`Dict`，不改 `list[...]` |
| `UP007` | §2.4.3 `Union[X, Y]`，不改 `X \| Y` |
| `UP045` | §2.4.4 `Optional[T]`，不改 `T \| None` |
| `UP042` | §2.7 明訂 `class XxxEnum(str, Enum)`，不改 `StrEnum` |
| `E501` | formatter 已管行寬，但拆不了中文註解與 docstring；留著只會讓 CI 永遠紅燈，並逼人把中文註解硬折行 |

> ⚠️ **`UP` 家族不只 `UP006`／`UP035` 會動型別註解與 Enum 寫法**。只關其中幾條就跑
> `ruff check --fix`，會一次改掉數百個違反專案規範的地方。

### 2. 設計選擇，非缺陷

| 規則 | 理由 |
|------|------|
| `B027` | 基底類別刻意留的 no-op 掛點（例如股票的 `settle_daily`：股票沒有每日結算）。標成 `@abstractmethod` 反而會強迫每個子類別寫空實作 |
| `B905` | `zip(..., strict=)` 屬行為決策：補 `strict=True` 可能讓原本靜默的長度不一致改為拋錯，須逐點確認後再開 |

### 3. 待收斂，暫時關閉

`BLE001`（盲捕 `except Exception`）、`TRY003`／`TRY300`／`TRY301`／`TRY201`／`TRY004`，
以及 `pyproject.toml` 標為「潛在缺陷」的 `B006`／`B904`／`E722`。
**修掉之後要把對應規則從 `ignore` 移除**，不要讓它長期留著——留著等於對那類問題
永久失明，而且「清單上有」會被誤讀成「還有那麼多處沒修」。

**清單裡的數量註記本身也會過期**，故每次動到 ignore 清單都要現查一次重新校正。
2026-09-18 校正後移除 `B008`／`F841`／`F811`（皆已歸零）與 `B007`（修掉最後一處），
並更新 `BLE001`（85 → 70）與 `TRY300`（19 → 20）的註記。

逐處位置不寫在文件裡（行號會漂移），一律現查：

```bash
ruff check . --select BLE001 --statistics   # 看數量
ruff check . --select B006,B904,E722        # 看逐處位置
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
| `scripts/manual/*` | `E402` | 獨立執行的腳本，`.env` 必須在部分 import 之前載入 |
| `stock_tick_utils.py`、`stock_tick_loader.py` 等 tick 模組 | `F401` | `dolphindb` 是選用相依，該處 import 是「可用性探測」 |
| `tests/*`、`scripts/manual/*` | `ANN201` | 測試 helper／fixture／替身方法（24 處）的回傳型別多半是「被 monkeypatch 過的 loader」或 `(物件, 路徑)`，標註要嘛得把類別從函式內部的 import 搬到檔頭（會讓 monkeypatch 失效），要嘛只能寫 `Any`。**`ANN204` 仍然生效**，`__init__` 一律要 `-> None` |

**`ANN201`／`ANN204` 在 `select` 內**，`core/`／`tasks/`／`frontend/`／`run.py` 兩條都生效：
`CLAUDE.md` §2.4 的「所有函式回傳值都要標註，含 `-> None`」沒有機器護欄時，
缺漏會以百處為單位累積。

> ⚠️ **批次補回傳型別時，抽象方法必須單獨挑出來人工判**。它們的 body 是 `pass`，
> 用「函式自己有沒有 return 值」判斷必然得到 `None`——而子類回傳什麼完全看不出來。
> 補齊時實際有 4 個抽象方法被自動化標錯（例如 `BaseDataCrawler.crawl()` 的子類
> 有五種回傳型別、`BaseDataLoader.add_to_db()` 有 `None` 與 `int` 兩種），
> 只能逐一對照子類簽章才抓得到。這類方法一律標 `Any` 並在該處註明理由。

只開這兩條、不開整組 `ANN`：`ANN001`（參數）另有 400 多處未標，
而 `ANN002`／`ANN003`（`*args`／`**kwargs`）與 `ANN101`（`self`）跟本專案既有寫法衝突。

`*.md` 已加入 `extend-exclude`：ruff 會連 Markdown 內的 Python 程式碼區塊一起格式化，
而文件裡的範例常刻意對齊註解以利閱讀。`CLAUDE.md` §2.5／§2.10 規範的對象是程式碼，不是文件。

### 工具產生的快取目錄不必寫進 `.gitignore`

`.ruff_cache` 與 `.pytest_cache` 都會在自己的目錄裡寫一份 `.gitignore`（內容是 `*`）
**自我排除整個目錄**，所以不在專案的 `.gitignore` 裡也進不了版控。

⚠️ **要驗證「一個目錄進不了版控」，不能測目錄本身**：`git check-ignore .ruff_cache`
會回報未命中（因為規則寫在該目錄內部，對目錄本身不生效），看起來像是漏了一條。
該測的是底下的檔案，或直接 `git add -An <dir>` 看有沒有輸出。

---

## 二、測試與覆蓋率

```bash
pytest -m "not slow" --cov=core --cov-report=term-missing
```

**刻意不設 `fail_under` 門檻**：在覆蓋率明顯偏低時設門檻只會鼓勵寫無效測試。
CI 會印出覆蓋率報告但不阻擋。補測試的優先順序建議為 `core/pipeline/` 的 loader → `reporter`。

**沒有會失敗的斷言就不是測試。** 要驗證行為就寫真的斷言；要人工探勘就寫成
`scripts/manual/` 底下的手動腳本。一支「整段包在 `try/except`、失敗時 `return False`」的
`test_*.py` 會被 pytest 判定 passed，永遠不會紅，還會被誤當成通過的證據。

### CI 跑得到與跑不到的護欄

`data/db/*.db` 未進版控，CI 也沒有 Shioaji／FinMind 金鑰。需要這些的測試一律標
`@pytest.mark.slow` 或 `pytestmark = pytest.mark.slow`。

| 護欄 | 何處執行 | 說明 |
|------|----------|------|
| `ruff check` / `ruff format --check` | CI ＋ pre-commit | 版本釘死，與 `.pre-commit-config.yaml` 的 rev 一致——不釘的話 CI 裝最新版，格式規則一變就出現「本機綠、CI 紅」，而那種紅燈與程式碼品質無關，只會訓練大家忽略 CI |
| `scripts/check_layer_deps.py` | CI ＋ pre-commit | 反向 import、循環 import、市場語意洩漏、跨軸目錄污染、`core/dao/` 以外 import `sqlite3` |
| `scripts/check_doc_paths.py` | CI ＋ pre-commit | 文件裡指向程式碼的路徑是否還存在（文件搬檔、程式改名後最容易漂的一項）|
| `scripts/check_api_orphan_methods.py` | **只在 CI** | `core/api` 有沒有零呼叫端的公開方法。**不放 pre-commit**：它要 import `core/api`，比 pygrep 慢一個量級 |
| `no-doc-step-refs`（pygrep） | pre-commit | 註解不得引用 backlog 步驟編號、健檢編號或 `backlog/` 路徑（`CLAUDE.md` §2.1 第 4 點）|
| SHORT 回歸線 | CI ＋ 本機 | 純記憶體、不需要資料庫 |
| **LONG 回歸線與 `slow` 測試** | **只在本機** | 需要 `data/db/tw_stock.db`、`tw_futures.db` 或外部 API |

**skip 不算通過**：`scripts/run_regression.sh` 以 `-rs` 執行並偵測 `SKIPPED`，有即以**結束碼 3**
結束並印出是哪一條、為什麼。否則「沒有資料庫」與「回歸真的通過」在輸出上會長得一模一樣。
動引擎前請在有資料庫的機器跑一次。

驗證 CI 是否真的會綠，可用「移除資料庫與金鑰的副本」在本機模擬：

```bash
rsync -a --exclude='.venv' --exclude='.git' --exclude='data/db' --exclude='.env' ./ /tmp/cisim/
cd /tmp/cisim && env -u API_KEY -u API_SECRET_KEY python -m pytest tests -q -m "not slow"
```

---

## 三、打包時會踩到的坑

### 3.1 `core.managers` 等目錄沒有 `__init__.py`

它們靠 PEP 420 namespace package 運作。`[tool.setuptools.packages.find]` 必須設
`namespaces = true`，否則 editable 安裝後 `core.managers.*` 會 import 不到——
而且**不會在安裝時報錯，是執行期才炸**。

### 3.2 `strategy_lab/` 不在 `packages.find` 裡

`strategy_lab/` 的腳本一律以 `python -m strategy_lab.…` 執行。直接跑檔案路徑會
`ModuleNotFoundError`——這是刻意的，比靠 `sys.path.insert` 硬塞而安靜成功要好。
`tests/test_strategy_data_access.py` 會擋 `sys.path` 注入再出現。

### 3.3 macOS：隱藏旗標會讓 `.pth` 完全失效

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

從 repo 根目錄執行 `python -m pytest` 或 `python run.py` 不受影響（cwd 會進 `sys.path`）。

---

## 相關文件

- [開發環境設定](../setup/dev-setup.md)——建立 venv 與環境變數
- [多市場回測引擎架構](../backtest/multi-market-engine.md)——§六回歸護欄說明 `scripts/run_regression.sh` 的職責
- `CLAUDE.md` §2——coding style 的權威來源
