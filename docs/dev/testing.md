# 測試與回歸

> 本文件說明測試怎麼組織、怎麼跑、回歸線怎麼守，以及新增功能時要跑哪些檢查。
> ruff 規則、CI job 內容、pre-commit hook 清單、marker 定義與覆蓋率設定的權威說明在
> [程式碼品質工具鏈](code-quality.md)，本文件只引用、不重複。

---

## 一、測試目錄配置

| 位置 | 內容 |
|------|------|
| `tests/` 根目錄 | 資料層（DAO、API、crawler、cleaner、pipeline）、具體策略、前端、設定與入口，以及大部分守門測試 |
| `tests/backtest/` | 回測引擎、成本／成交／結算 model、DataFeed、報表，以及 LONG／SHORT 兩條回歸線與其 baseline 產生腳本 |
| `tests/live/` | 實盤各層：OMS、帳本、風控、交易模式、Shioaji 轉接層、parity 檢查；全部跑在 `FakeBroker` 上 |
| `tests/portfolio/` | `core/portfolio/` 的訊號、組合建構、部位大小與下單規則 |
| `tests/analysis/` | 績效指標計算 |
| `tests/fixtures/` | 共用的靜態樣本：前端報表 CSV／PNG、交易所休市日 JSON |
| `tests/live/fixtures/` | 實盤 replay 用的錄製逐筆行情（`.jsonl`） |
| `tests/backtest/snapshots/` | 回歸線的 baseline CSV |

另有兩支非測試的輔助模組：`tests/entry_sandbox.py`（以子行程跑入口時的環境沙箱，見第三節）與
`tests/backtest/compare_cost_formula.py`（成本口徑差異的分析工具，非測試，不會被 pytest 收集）。

### 守門測試與行為測試

測試分成兩類，寫法與失敗時的意義都不同：

- **行為測試**：餵輸入、驗輸出，例如 `tests/backtest/test_cost_model.py` 驗手續費與稅、
  `tests/live/test_order_manager.py` 驗委託狀態機。紅燈代表程式行為錯了。
- **守門測試**：不跑業務邏輯，而是以 AST 或原始碼掃描檢查**全專案的結構性約束**，
  防止新程式碼悄悄違反慣例。紅燈代表有人加了不該加的東西，或文件／註記與實況脫節。

守門測試的例子：

| 測試 | 掃什麼 | 保護什麼 |
|------|--------|----------|
| `tests/test_module_docs.py` | `core/` 每個模組的模組層級字串；`strategies/` 的策略 class docstring | 新增模組時不會漏寫說明（`CLAUDE.md` §2.3 的兩種合法位置都算） |
| `tests/test_quality_ratchet.py` | `pyproject.toml` ignore 清單行尾註記的「現存處數」與實際 ruff 結果 | ratchet 註記不會低報，問題規模不被誤判 |
| `tests/test_subprocess_uv_no_sync.py` | 測試與腳本中以子行程呼叫的 `uv run` | 一律帶 `--no-sync`，跑測試不會順手移除 extras |
| `tests/test_entrypoint_and_logging.py` | 原始碼中的 `exc_info=` | 與 pre-commit 的 `no-stdlib-exc-info` 同一條規則的測試端護欄 |
| `tests/test_model_symmetry.py` | 台股與期貨 models 的欄位宣告 | 兩市場共有欄位只在基底宣告一次，訊號到訂單的欄位搬運只有一份 |
| `tests/test_check_layer_deps_strategy_contract.py` 等 `test_check_layer_deps_*` | 以檔案路徑載入 `scripts/check_layer_deps.py` 餵合成案例 | 分層檢查本身會命中、不誤報 |
| `tests/test_doc_symbol_references.py` | `scripts/check_doc_paths.py` 的符號索引 | 文件符號掃描不會「悄悄不再命中任何東西」 |

最後兩列是「守護欄的護欄」：檢查腳本最常見的死法是改壞後回報 0 處，看起來比修好還漂亮，
所以要有測試證明它對刻意寫錯的輸入真的會紅。

---

## 二、執行方式

```bash
.venv/bin/python -m pytest -m "not slow" -rs   # 日常與 CI 等價的範圍
.venv/bin/python -m pytest -rs                  # 全部（含 slow；需本機資料庫）
```

要用 `uv` 時一律 `uv run --no-sync pytest ...`：不帶 `--no-sync` 的 `uv run` 會先同步 venv、移除
`--extra frontend` 等 extras，前端冒煙測試之後就會改為略過。

**`-rs` 不要省**：它把每條 skip 的原因印在 short summary，否則整組因缺少依賴而全數 skip 也看起來是綠的。

### marker 與預設行為

marker 定義在 `pyproject.toml` 的 `[tool.pytest.ini_options]`，語意見
[程式碼品質工具鏈](code-quality.md) 的「marker」一節。要點：

- `slow`：照常收集，只有 `-m "not slow"` 才排除。需要 `data/db/*.db`、外部 API 或耗時較久者都要標。
- `shioaji_sim`／`shioaji_sim_order`：**預設 skip**。`tests/conftest.py` 的 `pytest_collection_modifyitems`
  檢查 `-m` 字串，沒有明確寫出該 marker 名稱就加上 skip。會送委託的 `shioaji_sim_order` 必須單獨點名：

  ```bash
  .venv/bin/python -m pytest tests/live/test_shioaji_sim.py -m "shioaji_sim and not shioaji_sim_order"
  .venv/bin/python -m pytest tests/live/test_shioaji_sim.py -m shioaji_sim_order
  ```

### 需要外部資源的測試

| 需求 | 測試 | 缺少時 |
|------|------|--------|
| `data/db/tw_stock.db` | LONG 回歸線（`tests/backtest/test_long_regression.py`）與其他標 `slow` 的資料庫測試 | `skipif` 略過 |
| Shioaji 金鑰（`.env`）、網路，部分要盤中 | `tests/live/test_shioaji_sim.py` | marker 閘門預設 skip |
| TimescaleDB 與 `--extra tick` | `tests/test_stock_tick_timescale.py` | 未設 `TICK_DATABASE_URL` 或沒裝 `psycopg` 時整個模組 skip |
| `--extra frontend` | `tests/test_frontend_app_smoke.py` | 略過並在 `-rs` 列出 |

TimescaleDB 整合測試在本機這樣跑（每條測試在自己的暫存 schema 建表、結束時整個 drop，不碰正式表）：

```bash
docker compose up -d postgres
TICK_DATABASE_URL=postgresql://alphaedge:alphaedge@localhost:5432/alphaedge \
    .venv/bin/python -m pytest tests/test_stock_tick_timescale.py -rs
```

### 逾時

`pyproject.toml` 沒有設全域逾時；CI 以 `--timeout=300`（`pytest-timeout`，屬 dev 相依）跑主測試，
本機要模擬時自行加上。以子行程跑入口的測試另由 `tests/entry_sandbox.py` 的 `run_isolated()`
帶 `subprocess` 逾時（預設 180 秒）。實連模擬環境的測試以模組常數限制等待回報與行情的秒數。

---

## 三、共用 fixture 與替身

| 位置 | 提供 | 用途 |
|------|------|------|
| 專案根目錄 `conftest.py` | 產物隔離與絆線 | 把 `results/`、`logs/`、kill switch 與實盤紀錄庫導到暫存目錄，正式產物有變動即判失敗（詳見 [程式碼品質工具鏈](code-quality.md)「測試不得寫進正式產物」） |
| `tests/conftest.py` | `LogManager` 換成 no-op | 測試不寫 `logs/`；原函式保留在 `real_setup_logger` 給驗 sink 的測試 |
| `tests/conftest.py` | `memory_conn`、`dao_factory`、`complete_rows()` | 以 DAO 自己的建表方法建正式 schema，測試只寫關心的欄位，其餘依 schema 補預設值；給了 schema 沒有的欄位直接報錯 |
| `tests/conftest.py` | `dao` | 已建表的記憶體 `LiveTradeDAO`，給實盤測試由 OMS／帳本自己寫入 |
| `tests/conftest.py` | `build_stock_quote()`、`build_futures_quote()`、`make_quote` | 報價 builder；普通函式版給模組層級 factory 與 `parametrize`，fixture 版給直接請求的測試 |
| `tests/backtest/conftest.py` | `ScriptedStrategy`、`ScriptedDataFeed`、`run_bar()`、`make_order`、`make_strategy` | 以「日期 → 訂單」腳本驅動回測，不連資料庫；SHORT 回歸線也建在這上面 |
| `tests/live/conftest.py` | `FakeBroker`、`fake_broker`、`make_ticket` | 不連網的假券商與委託 factory |

**建表一律走 `dao_factory`**，不要在測試裡手抄 `CREATE TABLE`：正式 schema 改了，散落各檔的副本不會跟著改。
只有刻意要「壞掉的 schema」（缺欄位、被鎖住）的測試才手寫。

### 實盤測試如何不碰真券商

1. `FakeBroker` 繼承 `core/broker/base.py` 的 `BaseBroker`，以屬性腳本化行為而不是 monkeypatch：
   `reject_symbols`（拒單）、`fill_ratio`（部分成交）、`defer_reports` ＋ `release_reports()`（回報亂序）、
   `fail_connect`／`drop_connection_after`（登入失敗、送單途中斷線）、`ack_on_submit`（確認晚到）。
   這些是模擬環境跑不出來、卻最需要測的路徑。
2. `LiveTrader` 等元件以建構參數注入 broker，測試直接傳 `FakeBroker` 實例。
3. Shioaji 轉接層（`tests/live/test_shioaji_broker.py` 等）以假 session／假 API 驗委派與組裝。
4. 介面契約兩邊驗：`tests/live/test_broker_contract.py` 在 `FakeBroker` 上驗，`tests/live/test_shioaji_sim.py`
   以同一組承諾在模擬環境驗 `ShioajiBroker`，兩邊都通過介面才算成立。
5. 以子行程跑入口的測試一律走 `tests/entry_sandbox.py` 的 `run_isolated()`：產物根導到暫存目錄，
   `API_KEY`／`API_SECRET_KEY` 設成**空字串**（刪掉的話 `load_dotenv()` 會從 `.env` 補回真實金鑰）。

---

## 四、回歸線

`scripts/run_regression.sh` 依序跑兩條線，任一條失敗即以 pytest 的結束碼中止、不續跑：

| 順序 | 回歸線 | 內容 | baseline | 需求 |
|------|--------|------|----------|------|
| 1 | SHORT（`tests/backtest/test_short_regression.py`） | 14 組腳本化放空情境，每組只動一個變因；比對逐筆交易、期末未平倉部位、帳戶終值與事件計數三份表 | `short_regression_baseline.csv`、`short_regression_positions.csv`、`short_regression_summary.csv` | 純記憶體、秒級；CI 也跑 |
| 2 | LONG（`tests/backtest/test_long_regression.py`） | `VolumeBreakoutMomentumStrategy` 2024 全年，經 `build_backtester()` 走正式的 `Backtester.run()` | `volume_breakout_momentum_strategy_baseline.csv` | 需 `data/db/tw_stock.db`，僅本機 |

baseline 都在 `tests/backtest/snapshots/`。

**「必須相同」的意思**：列數相同、欄位組成相同，且以 `pd.testing.assert_frame_equal` 逐欄比對
（`check_dtype=False`，只容許 dtype 不同，數值與日期必須一致）。不是「差不多」，損益差一元也算紅。

**skip 不算通過**：腳本以 `-rs` 執行並偵測輸出中的 `SKIPPED`，有即以結束碼 3 結束並列出原因。
沒有資料庫的機器上 LONG 線會 skip，這時結果是「沒驗」，不是「通過」。

### 何時要跑

動到下列任何一處都要在有資料庫的機器跑一次完整回歸：

- 回測引擎與其零件：`core/backtest/`、`core/position/`、`core/models/`
- 成本與市場語意：`core/market/`（例如 `core/market/tw/cost_model.py`）
- 回歸線用到的策略：`strategies/stock/volume_breakout_momentum_strategy.py`，以及策略基底 `core/strategies/`

注意回歸線**不經過 reporter**：baseline 腳本直接從 `account.trade_records` 組表，改壞報表欄位兩條線照樣綠。
動 `core/backtest/report/` 要靠 `tests/backtest/test_reporting.py` 與 `tests/backtest/test_reporter_timeline.py`。

### 重產 baseline

```bash
.venv/bin/python -m tests.backtest.make_short_baseline   # 改寫 SHORT 三份快照
.venv/bin/python -m tests.backtest.make_baseline         # 改寫 LONG baseline（需資料庫）
```

只有在**刻意、經過審查的行為改變**時才可重產，例如修正成本公式或結算順序的 bug，並且：

1. 先確認差異完全來自這次的改動，能逐筆解釋每一列為什麼變了。
2. baseline 的變更與造成它的程式改動放在同一個 commit，commit message 寫明預期的差異。
3. 會改變回測結果的工作盡量合併、只重產一次——一旦重產，先前每一次「逐筆相同」的驗證都失去比較基準。

為了讓紅燈變綠而重產 baseline，等於拆掉這道護欄。

---

## 五、突變測試慣例

新寫的測試要證明它**真的抓得到**那個 bug，不只是在修好的程式上通過：

1. 先把要改的原始檔複製一份備份（例如 `cp core/xxx.py /tmp/xxx.py.bak`）。
2. 暫時把程式改壞（還原 bug、拿掉檢查、對調順序），跑新測試，確認它**紅**，且失敗訊息指向該 bug。
3. 從備份複製回來還原，**不要用 `git checkout`**：它會連同這個檔案裡尚未 commit 的修正一起丟掉。
4. 還原後清掉 `__pycache__`（`scripts/clean_pycache.sh` 或只刪該目錄），再跑一次確認轉綠。
   `.pyc` 以原始檔的 mtime 與大小判斷是否過期，改動與還原落在同一秒且長度相同時，
   Python 會繼續載入突變版的 bytecode，測試結果就不可信。

守門測試也適用：在合成輸入裡故意放一個違規，確認掃描會命中。

---

## 六、新增功能時的檢查清單

依序跑完、全綠才算完成（工具細節見 [程式碼品質工具鏈](code-quality.md)）：

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python scripts/check_layer_deps.py
.venv/bin/python scripts/check_doc_paths.py
.venv/bin/python scripts/check_api_orphan_methods.py   # 只在 CI 跑，動 core/api 時本機也跑
git add <新增與修改的檔案>
pre-commit run --all-files
.venv/bin/python -m pytest -m "not slow" -rs
./scripts/run_regression.sh                              # 動到第四節列出的範圍時
```

- **pre-commit 要在 `git add` 之後跑**：`--all-files` 掃的是 git 追蹤中的檔案，尚未 `git add` 的新檔不在範圍內，
  順序反了新檔的違規會漏到 CI 才被抓到。
- 新功能要有行為測試；如果它引入了新的結構性約束（例如「某層不可 import 某套件」），同時補守門測試或
  `scripts/check_layer_deps.py` 的檢查。
- 需要資料庫、外部 API 或金鑰的新測試要標 `slow`（或對應的 Shioaji marker），否則 CI 會紅。
- 沒有會失敗的斷言就不是測試；人工探勘寫成 `scripts/manual/` 底下的手動腳本。

---

## 相關文件

- [程式碼品質工具鏈](code-quality.md)——ruff、CI、pre-commit、marker、產物隔離與覆蓋率
- [多市場回測引擎架構](../backtest/multi-market-engine.md)——回歸護欄的設計與 SHORT 各情境的用意
- [資料存取層](data-access-layer.md)——DAO 的建表方法與測試建表方式
