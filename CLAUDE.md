# AlphaEdge — Claude Code 專案規則

本檔案是 Claude Code 在本專案的**常駐規則**，所有對話與變更一律套用。

| 章節 | 適用範圍 | 對應 Cursor Rule |
|------|----------|------------------|
| [1. 通用原則](#1-通用原則) | 全專案 | — |
| [2. Coding Style](#2-coding-style) | `**/*.py` | `.cursor/rules/coding-style-zh.mdc` |
| [3. Backlog 管理](#3-backlog-管理) | `backlog/**` | — |
| [4. Commit 與 Push](#4-commit-與-push) | 全專案 | `.cursor/rules/commit-message-zh.mdc`（alwaysApply） |
| [5. 目錄專屬規則](#5-目錄專屬規則) | 各子目錄 | `.cursor/rules/strategy-lab-layout.mdc` |

情節性的流程規範不放本檔案，改以 `.claude/skills/` 按需載入：

| Skill | 觸發時機 | 對應 Cursor Rule |
|-------|----------|------------------|
| [`manage-backlog`](.claude/skills/manage-backlog/SKILL.md) | 新增／更新 `backlog/**` 文件與 `index.md` | `.cursor/skills/manage-backlog/SKILL.md` |
| [`develop-strategy`](.claude/skills/develop-strategy/SKILL.md) | 新增／修改 `core/strategies/{stock,futures}/` 策略 | `.cursor/rules/strategy-development-sdd.mdc` |
| [`commit-push-merge`](.claude/skills/commit-push-merge/SKILL.md) | 使用者要求「commit + push + merge」 | `.cursor/skills/commit-push-merge/SKILL.md`、`.cursor/rules/commit-message-zh.mdc` |

> **上表「對應 Cursor Rule」欄一律是指標，不是副本。** 規則內容只維護在本檔、`.claude/skills/`
> 或 `strategy_lab/CLAUDE.md`；`.cursor/` 底下的檔案只寫「去讀哪一份」，不重複規則本文。
> 要改規則就改權威檔，不要改 `.cursor/` 的指標檔——兩邊各存一份必然漂移。

---

## 1. 通用原則

1. 除非使用者明確要求英文，**回覆、文件與註解一律使用繁體中文**。
2. 專有名詞保留英文原詞，不要硬翻（`tick`、`bid/ask`、`ROI`、`OHLC`、`Long/Short`、`Ridge`、`API`、`SQLite`）。
3. 中文標點使用全形（`，`、`：`、`（）`）；同一檔案內標點與空白風格保持一致。
4. 既有程式碼的風格優先於個人偏好：新增或修改時沿用現有寫法，不要引入新風格。

---

## 2. Coding Style

> 以下規則由現有 `core/`、`strategy_lab/` 程式碼歸納而成。

### 2.1 註解語言

1. **註解一律以繁體中文為主**，專有名詞保留英文（見 §1）。
2. 中英夾雜時保留英文原詞，例如：`# 內外盤別{1: 外盤, 2: 內盤, 0: 無法判定}`、`self.volume: int = volume  # 成交量（Unit: Lot）`。
3. 註解說明「為什麼」與業務語意，不要複述程式碼字面行為。
4. **註解要自足，不引用文件**：不寫「見 `docs/…` §3.2」、backlog 的步驟編號（`S3`、`Phase2-4`）或健檢發現編號（`F-0xx`）。理由直接寫在註解裡；需要指路時指向程式碼（類別、函式、測試），不指向文件——文件會改版、會刪除，註解不會跟著更新。

### 2.2 Docstring

1. 每個 class 與 public method 都要有 docstring；單行說明用一行三引號寫完：

   ```python
   def get(self, date: datetime.date) -> pd.DataFrame:
       """取得所有股票指定日期的 Price"""

       query: str = f"""..."""
   ```

2. **docstring 之後空一行才開始寫程式**（本專案通用習慣）。
3. 抽象基底類別（`core/**/base.py`）與需要詳列參數的函式，使用專案自訂的結構化格式：

   ```python
   """
   - Description:
       開倉策略（Long & Short），需要包含買賣的標的、價位和數量
   - Parameter:
       - stock_quotes: List[StockQuote]
           目標股票的報價資訊
   - Return:
       - List[StockOrder]
           開倉訂單
   """
   ```

   不要改用 Google style 的 `Args:` / `Returns:`（專案中僅零星出現，屬例外）。
4. 策略類別的 class docstring 用條列描述交易邏輯，區塊固定為「買進條件 / 賣出條件 / 停損條件」，並註明未實作的部分：

   ```python
   class MomentumStrategy1(BaseStockStrategy):
       """
       動能策略 1（日線）

       買進條件（全部滿足）：
       - 當日收盤相對「前一交易日」收盤漲幅 ≥ 門檻（預設 9%）

       停損條件：
       - 未實作（一律不回傳停損單）
       """
   ```

### 2.3 模組層級說明字串

1. 模組總覽說明字串放在 **import 區塊之後、第一個 class/function 之前**（`core/api/`、`core/models/`、`core/pipeline/` 皆如此）：

   ```python
   from core.api.base import BaseDataAPI

   """Stock Price API: query SQLite price table"""


   class StockPriceAPI(BaseDataAPI):
   ```

2. 內容較長時用多行三引號，依序寫「模組用途 → Features 條列 → 使用場景」（見 `core/utils/instrument.py`）。

### 2.4 Type Hints

1. **所有函式參數與回傳值都要標註型別**，包含回傳 `None` 的 `-> None`。
2. **區域變數與 instance 屬性也標註型別**，這是本專案明顯特徵，務必沿用：

   ```python
   self.conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
   df: pd.DataFrame = pd.read_sql_query(query, self.conn, params=(date,))
   open_positions: List[StockQuote] = []
   ```

3. 使用 `typing` 的 `List` / `Dict` / `Optional` / `Tuple` / `Union` 大寫寫法，不要改成 `list[...]` / `X | None`。
4. 可選欄位一律 `Optional[T]`，並在 `__init__` 給預設值 `None`。

### 2.5 Import 排序

依 Ruff/isort 預設分組，組間空一行，組內字母排序：

```python
# 1. 標準函式庫
import datetime
from typing import List, Optional

# 2. 第三方套件
import pandas as pd
from loguru import logger

# 3. 專案內部（core.* 以絕對路徑；同目錄可用相對 .module）
from core.api.base import BaseDataAPI
from core.config import PRICE_TABLE_NAME, TW_STOCK_DB_PATH
```

### 2.6 命名

| 對象 | 規則 | 範例 |
| --- | --- | --- |
| 模組 / 檔案 | `snake_case` | `stock_price_api.py` |
| 類別 | `PascalCase`，資料 API 結尾 `API`、清洗器 `Cleaner`、更新器 `Updater`、爬蟲 `Crawler`、管理器 `Manager` | `StockChipAPI`、`StockTickCleaner` |
| 函式 / 變數 | `snake_case`；查詢類前綴 `get_`、設定類 `setup_`、**Alpha 層產生訊號 `generate_*_signals`**、引擎契約 `check_*_signal`（由基底提供，策略不實作）、計算 `calculate_` | `get_stock_price()`、`generate_open_signals()` |
| 模組私有 | 前綴底線 | `_drop_explained()`、`_PROJECT_ROOT` |
| 常數 | `UPPER_SNAKE_CASE`，策略參數放 class 層級常數並附中文單位註解 | `MIN_VOLUME_LOTS: int = 5000  # 最小成交量（張）` |
| 策略類別 | 類別名即策略識別名稱，需與 `run.py --strategy` 對應 | `MomentumStrategy1` |

### 2.7 常數與 Enum

1. 字串常數先定義模組層級 `UPPER_SNAKE_CASE`，再由 `class XxxEnum(str, Enum)` 引用（見 `core/utils/constant.py`），不要直接在 Enum 內寫字面值。
2. 分類常數上方加中文分組註解：`# 定義下單類型常量`。

### 2.8 區塊分隔註解

1. 設定檔等長檔案用等寬分隔線標題（見 `core/config/paths.py`）：

   ```python
   # -----------------------------------------------------------------------
   # === General Directory Path ===
   # -----------------------------------------------------------------------
   ```

2. `__init__` 內部屬性分組，可沿用既有的三引號分組標記（`""" === Strategy Setting === """`）或一般 `#` 註解；新程式碼建議用 `#`，不要再新增三引號分組。

### 2.9 日誌

1. 一律使用 `from loguru import logger`，不要用 `print()`（現存 `print` 屬既有例外，不要擴散）。
2. 需要落地檔案的模組在 `setup()` 內呼叫 `LogManager.setup_logger("xxx.log")`。

### 2.10 格式化工具

1. 程式碼由 formatter 產生（Ruff/Black 風格，行寬 88、雙引號、magic trailing comma），提交前確保格式一致，不要手動調整成其他排版。
2. SQL 查詢用三引號多行字串，參數一律走 `params=(...)` 佔位符，不要用 f-string 拼接使用者輸入。

---

## 3. Backlog 管理

`backlog/` 存放**尚未實作**的待辦與規劃紀錄；說明文件（API 文檔、教學）放 `docs/`。所有待辦文件的清單、優先級與狀態一律只維護在 [`backlog/index.md`](backlog/index.md)。

完整規範（狀態圖例、文件必要結構、狀態更新方式、`index.md` 索引維護、完成後的處理方式）放在 [`.claude/skills/manage-backlog/SKILL.md`](.claude/skills/manage-backlog/SKILL.md)。**動到 `backlog/` 內容前，一律先載入該 skill**，不要憑記憶撰寫。

---

## 4. Commit 與 Push

當使用者要求「commit + push」時：

1. Commit message 使用中文說明（可保留 conventional commit 前綴，例如 `feat(backtest):`）。
2. Commit 內文用編號條列（`1.`、`2.`、`3.`），逐項描述每個變更。
3. 多種變更並存時，先依主題分組，再在各組內維持編號條列。
4. 回覆使用者時，需清楚列出：
   - 分支名稱
   - commit hash
   - push 目標（例如 `origin/main`）
   - 本次變更檔案清單

當使用者要求「commit + push + merge」時，先依上述規則 commit + push，merge 進 `main`
與同步其他分支的流程則**一律先載入** [`commit-push-merge`](.claude/skills/commit-push-merge/SKILL.md) skill 再執行。

---

## 5. 目錄專屬規則

| 目錄 | 規則檔 | 說明 |
|------|--------|------|
| `strategy_lab/` | [`strategy_lab/CLAUDE.md`](strategy_lab/CLAUDE.md) | 對應 `.cursor/rules/strategy-lab-layout.mdc`，只在該目錄下工作時套用 |
