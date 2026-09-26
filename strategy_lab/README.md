# strategy_lab — 策略研究與開發工作區 (R&D Lab)

這個資料夾是 **AlphaEdge** 專案底下的「**Research & Development 實驗室**」：
**分析資料、思考交易策略、做小型實驗、寫筆記** 的地方。

> **這裡不負責「跑正式回測」**。AlphaEdge 已經在 `core/` 提供一套完整的策略框架與回測器（`Backtester`、`StockBacktestReporter`、`StrategyLoader`），
> 想跑正式回測請把策略放到 `core/strategies/stock/`（股票）或 `core/strategies/futures/`（期貨），然後執行：
>
> ```bash
> .venv/bin/python run.py --strategy <StrategyName>
> ```
>
> `strategy_lab/` 是它的 **上游**：把想法、資料分析、研究筆記都在這裡完成，
> 成熟後再「**搬到 `core/strategies/stock/` 或 `core/strategies/futures/`**」就能直接接上正式框架。

---

## 目錄

- [strategy\_lab — 策略研究與開發工作區 (R\&D Lab)](#strategy_lab--策略研究與開發工作區-rd-lab)
  - [目錄](#目錄)
  - [角色與分工](#角色與分工)
  - [資料夾結構](#資料夾結構)
  - [研究工作流（從想法到上線）](#研究工作流從想法到上線)
  - [可直接複用的資料 API](#可直接複用的資料-api)
    - [StockPriceAPI — 日線價格資料 (SQLite)](#stockpriceapi--日線價格資料-sqlite)
    - [StockTickAPI — 逐筆成交資料 (DolphinDB)](#stocktickapi--逐筆成交資料-dolphindb)
    - [StockChipAPI — 三大法人籌碼 (SQLite)](#stockchipapi--三大法人籌碼-sqlite)
    - [MonthlyRevenueReportAPI — 月營收 (SQLite)](#monthlyrevenuereportapi--月營收-sqlite)
    - [FinancialStatementAPI — 季報財報 (SQLite)](#financialstatementapi--季報財報-sqlite)
    - [MarketCalendar — 交易日工具](#marketcalendar--交易日工具)
  - [可直接複用的工具類 (Utils)](#可直接複用的工具類-utils)
    - [StockUtils — 手續費／證交稅／單位換算](#stockutils--手續費證交稅單位換算)
    - [Units / Commission — 單位與成本常數](#units--commission--單位與成本常數)
    - [Action / Scale / PositionType / Market / InstrumentType — 列舉常數](#action--scale--positiontype--market--instrumenttype--列舉常數)
  - [資料來源摘要](#資料來源摘要)
  - [研究腳本怎麼寫？](#研究腳本怎麼寫)
    - [最小可運行的研究腳本](#最小可運行的研究腳本)
    - [常見模式：IC（Information Coefficient）分析](#常見模式icinformation-coefficient分析)
    - [常見模式：算扣除手續費／證交稅後的真實報酬](#常見模式算扣除手續費證交稅後的真實報酬)
  - [Jupyter Notebook 使用慣例](#jupyter-notebook-使用慣例)
  - [從 R\&D 銜接到正式策略](#從-rd-銜接到正式策略)
  - [命名與檔案慣例](#命名與檔案慣例)
  - [Cursor 使用](#cursor-使用)
  - [聲明](#聲明)

---

## 角色與分工

| 區別          | `core/strategies/`                    | `strategy_lab/`                           |
| ------------- | ------------------------------------- | ----------------------------------------- |
| **角色**      | 正式上線、可被 `run.py` 載入的策略    | R&D、實驗、靈感、半成品、筆記             |
| **結構**     | 強型別，繼承 `BaseStockStrategy`      | 自由發揮，可用 `.py` 或 `.ipynb`          |
| **回測引擎** | `core/backtest/backtester.py`         | 自寫 vectorized backtest 或借用 core API |
| **產出**     | 標準回測報表（`<策略>_balance_curve.png` …） | 圖表、CSV、Word 報告、markdown 筆記       |
| **觸發**     | `run.py --strategy <Name>`            | 直接 `python <script>.py` 或 notebook    |

**重要原則：** 研究階段請優先 **複用 `core/api/` 與 `core/utils/`**，
不要在 lab 內重複實作資料讀取、手續費計算、交易日判斷等功能。

---

## 資料夾結構

四大分類為**大分類**；每個研究主題用**一個 `snake_case` 子資料夾**區分。

```
strategy_lab/
├── README.md                   ← 你正在看的這份（R&D 參考文件）
├── __init__.py
│
├── ideas/                      ← 策略構想、文獻筆記、待驗證假設
│   ├── README.md
│   └── <topic_name>/           ← 例：momentum_breakout/README.md
│
├── data_analysis/              ← 純資料分析、特徵探索、IC 研究
│   ├── README.md
│   └── tech_new_high_continuation/  ← 例：run.py、analysis.py、output/
│
├── notebooks/                  ← 探索性 Jupyter notebook
│   ├── README.md
│   └── <topic_name>/           ← 例：2026_05_eda.ipynb、output/
│
└── strategies/                 ← 完整的策略研究
    ├── README.md
    └── tsmc_overnight_signal/  ← 例：TSMC 跨市場隔夜訊號研究
```

同一主題可跨分類存在（例如 `ideas/momentum_breakout/` 與 `data_analysis/momentum_breakout/`），
**資料夾名稱保持一致**以便對照。

各子資料夾用途請參考各自的 `README.md`。

---

## 研究工作流（從想法到上線）

```
ideas/           data_analysis/      strategies/<name>/      core/strategies/{stock,futures}/<name>.py
  │                  │                     │                            │
  ▼                  ▼                     ▼                            ▼
寫一段假設     EDA、相關性、IC    寫完整 pipeline，產圖表        繼承 BaseStockStrategy
（markdown）   產出 CSV/PNG       與績效報告                     可被 run.py 載入
                                                                       │
                                                                       ▼
                                                            .venv/bin/python run.py
                                                              --strategy <Name>
```

每一階段都是 **可選的**，依想法成熟度決定要在哪一層停下來。
失敗的實驗也記得回頭到 `ideas/` 加註「結論：不 work，原因：⋯⋯」。

---

## 可直接複用的資料 API

所有 API 都在 `core/api/tw/`，直接 import 就能用。
**回傳值統一為 `pandas.DataFrame`**，方便接 `numpy / sklearn / plotly`。

> 這裡是**研究用法**：直接建立 API 物件、拿 `DataFrame` 自己處理欄位。搬進 `core/strategies/`
> 寫正式策略時規則相反——API 由引擎的 `DataFeed` 建立，策略只能用具名查詢方法（`get_close_map()` 等），
> 見 [策略撰寫指南](../core/strategies/README.md)〈資料 API 使用方式〉。

### StockPriceAPI — 日線價格資料 (SQLite)

來源：`data/db/tw_stock.db` 之 price 表。

```python
import datetime
from core.api.tw.stock_price_api import StockPriceAPI

price = StockPriceAPI()

# 1) 取得指定日期「所有」股票的日線
df_one_day = price.get(date=datetime.date(2024, 1, 2))

# 2) 取得指定日期區間「所有」股票的日線
df_range = price.get_range(
    start_date=datetime.date(2024, 1, 1),
    end_date=datetime.date(2024, 1, 31),
)

# 3) 取得「指定個股」的日線
df_2330 = price.get_stock_price(
    stock_id="2330",
    start_date=datetime.date(2024, 1, 1),
    end_date=datetime.date(2024, 12, 31),
)
```

回傳欄位（典型）：`date, stock_id, 開盤價, 最高價, 最低價, 收盤價, 成交股數, ...`（**是 `成交股數` 不是 `成交量`**——後者只存在於期貨表，單位是口。要張數請用 `StockPriceAPI.get_volume_lots_map()`）

### StockTickAPI — 逐筆成交資料 (DolphinDB)

來源：DolphinDB tick 表（需先連線；連線資訊由 `core/config` 帶入）。
**注意：需安裝 `dolphindb` 套件且有可用的 DDB 伺服器。**

```python
import datetime
from core.api.tw.stock_tick_api import StockTickAPI

tick = StockTickAPI()

# 1) 每個個股各自排序的 tick
df = tick.get(
    start_date=datetime.date(2024, 5, 10),
    end_date=datetime.date(2024, 5, 10),
)

# 2) 所有個股混排（模擬盤中時序）
df_ordered = tick.get_ordered_ticks(
    start_date=datetime.date(2024, 5, 10),
    end_date=datetime.date(2024, 5, 10),
)

# 3) 指定個股 tick
df_2330 = tick.get_stock_ticks(
    stock_id="2330",
    start_date=datetime.date(2024, 5, 10),
    end_date=datetime.date(2024, 5, 10),
)

# 4) 當日最後一筆 tick（常用於計算當日收盤）
last = tick.get_last_tick(stock_id="2330", date=datetime.date(2024, 5, 10))
```

### StockChipAPI — 三大法人籌碼 (SQLite)

```python
import datetime
from core.api.tw.stock_chip_api import StockChipAPI

chip = StockChipAPI()

# 指定日所有股票
df = chip.get(date=datetime.date(2024, 1, 2))

# 指定日期區間
df_range = chip.get_range(
    start_date=datetime.date(2024, 1, 1),
    end_date=datetime.date(2024, 1, 31),
)

# 指定個股
df_2330 = chip.get_stock_chip(
    stock_id="2330",
    start_date=datetime.date(2024, 1, 1),
    end_date=datetime.date(2024, 12, 31),
)
```

主要欄位：`date, stock_id, 證券名稱, 外資買賣超股數, 投信買賣超股數, 自營商買賣超股數, ...`

### MonthlyRevenueReportAPI — 月營收 (SQLite)

```python
from core.api.tw.monthly_revenue_report_api import MonthlyRevenueReportAPI

mrr = MonthlyRevenueReportAPI()

# 單月
df = mrr.get(year=2024, month=1)

# 範圍
df_range = mrr.get_range(
    start_year=2023, end_year=2024,
    start_month=1,   end_month=12,
)
```

### FinancialStatementAPI — 季報財報 (SQLite)

```python
from core.api.tw.financial_statement_api import FinancialStatementAPI

fs = FinancialStatementAPI()

# 取得 2024 Q1 的「特定」財報表（須傳入 table_name）
df = fs.get(table_name="<your_fs_table>", year=2024, season=1)
```

> 不同財報（資產負債表、損益表、現金流量表）會落在不同的 SQLite 表，
> 實際表名請以 `tasks/` 內的更新腳本或 `core/config/schema.py` 為準。

### MarketCalendar — 交易日工具

```python
import datetime
from core.api.tw.stock_price_api import StockPriceAPI
from core.market.tw.market_calendar import MarketCalendar

price = StockPriceAPI()

# 1) 某日是否為台股開盤日
is_open = MarketCalendar.check_stock_market_open(api=price, date=datetime.date(2024, 1, 2))

# 2) 取得「嚴格早於指定日」的前一個交易日（會自動跳過休市日）
prev_td = MarketCalendar.get_last_trading_date(api=price, date=datetime.date(2024, 1, 2))
```

---

## 可直接複用的工具類 (Utils)

### StockUtils — 手續費／證交稅／單位換算

`core/utils/instrument.py`。所有 method 都是 `@staticmethod`：

```python
from core.utils.instrument import StockUtils
from core.utils import Units

# 1) 股數 / 張數 轉換（1 張 = 1000 股）
lots = StockUtils.convert_share_to_lot(shares=5000)     # 5
shares = StockUtils.convert_lot_to_share(lots=5)        # 5000

# 2) 計算「買進手續費」（內含最低手續費門檻、折扣）
buy_fee = StockUtils.calculate_transaction_commission(price=600.0, volume=5)

# 3) 計算「賣出證交稅」
sell_tax = StockUtils.calculate_transaction_tax(price=620.0, volume=5)

# 4) 一次拿到「買、賣」雙邊摩擦成本
buy_cost, sell_cost = StockUtils.calculate_transaction_cost(
    buy_price=600.0, sell_price=620.0, volume=5,
)

# 5) 扣完手續費／稅之後的淨損益與 ROI（做多單）
profit = StockUtils.calculate_net_profit(buy_price=600.0, sell_price=620.0, volume=5)
roi    = StockUtils.calculate_roi(buy_price=600.0, sell_price=620.0, volume=5)  # %

# 6) 過濾出「普通股」（4 位數字且不小於 1001，排除 ETF / 權證）
common_ids = StockUtils.filter_common_stocks(stock_ids=["2330", "0050", "9962", "00878"])
# → ['2330', '9962']
```

### Units / Commission — 單位與成本常數

來自 `core/utils/constant/`：

```python
from core.utils import Units, Commission

Units.LOT          # 1000（1 張 = 1000 股）
Units.SHARE        # 1
Commission.CommRate    # 0.001425（券商手續費率）
Commission.Discount    # 0.3（手續費折扣，可依券商調整）
Commission.MinFee      # 20.0（最低手續費）
Commission.TaxRate     # 0.003（證交稅率）
```

實務換算範例：

```python
# 1 張 600 元的股票：
notional = 600 * Units.LOT                          # 600,000
buy_fee  = notional * Commission.CommRate * Commission.Discount  # ≈ 256.5 → 取 max(20, int(.))
sell_tax = notional * Commission.TaxRate            # 1,800
```

### Action / Scale / PositionType / Market / InstrumentType — 列舉常數

```python
from core.utils import Action, InstrumentType, Market, PositionType, Scale

Action.BUY, Action.SELL                  # 'Buy', 'Sell'
Scale.DAY, Scale.TICK                    # 回測級別
PositionType.LONG, PositionType.SHORT    # 部位方向
Market.TW, Market.US                     # 市場（地區）
InstrumentType.STOCK, InstrumentType.FUTURE, InstrumentType.OPTION  # 商品類別
```

研究階段通常**不需要直接用**這些；但若要把研究結果包成正式策略，這些就是必要的。

---

## 資料來源摘要

| 資料種類       | 後端           | 提供 API                      | 來源 / 更新                                |
| -------------- | -------------- | ----------------------------- | ------------------------------------------ |
| 日線價量       | SQLite         | `StockPriceAPI`               | `tasks/` 內的爬蟲；表名見 `core/config/schema.py` |
| 逐筆 Tick      | DolphinDB      | `StockTickAPI`                | Shioaji / 第三方；需另起 DDB 服務          |
| 三大法人籌碼   | SQLite         | `StockChipAPI`                | `tasks/` 內的爬蟲                          |
| 月營收         | SQLite         | `MonthlyRevenueReportAPI`     | `tasks/` 內的爬蟲                          |
| 財報           | SQLite         | `FinancialStatementAPI`       | `tasks/` 內的爬蟲                          |
| 海外 ADR / FX  | yfinance       | 直接 `import yfinance as yf`  | 即時抓取（無本地表）                       |
| 交易日／前一日 | 視 API 而定   | `MarketCalendar`              | 透過 `StockPriceAPI` 推算                 |

> **資料庫位置**：`data/db/tw_stock.db`（SQLite）。
> 想知道目前 DB 內有哪些表，可以快速跑：
>
> ```python
> from core.config import TW_STOCK_DB_PATH
> from core.dao.connection import connect_sqlite
>
> # 唯讀開啟：不會與背景 ETL 搶寫入鎖，檔案不存在時也不會建出空 DB
> conn = connect_sqlite(TW_STOCK_DB_PATH, read_only=True)
> for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
>     print(name)
> conn.close()
> ```
>
> 研究腳本讀資料一律走 `core/api/`（或 API 持有的 `dao`），不要自己 `import sqlite3` 下查詢。

---

## 研究腳本怎麼寫？

### 最小可運行的研究腳本

先建立主題資料夾 `strategy_lab/data_analysis/<your_topic>/`，把腳本存成 `run.py` 或 `analysis.py`，就能在專案根目錄跑。

> **執行一律用 `-m`**（例如 `.venv/bin/python -m strategy_lab.data_analysis.<your_topic>.run`）。
> `strategy_lab` 不在 `pyproject` 的 `packages.find` 裡（只裝 `core*`／`tasks*`／`tests*`），
> 直接跑檔案路徑時 `sys.path[0]` 是腳本自己的目錄，`import strategy_lab.…` 會失敗。
> 舊版靠每支腳本開頭的 `sys.path.insert` 硬塞，那會遮蔽「沒安裝就跑」的 import
> 錯誤（已清除）。


```python
import datetime as dt
import pandas as pd

from core.api.tw.stock_price_api import StockPriceAPI

price = StockPriceAPI()
df = price.get_stock_price(
    stock_id="2330",
    start_date=dt.date(2024, 1, 1),
    end_date=dt.date(2024, 12, 31),
)

# 日報酬 → 月波動度
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").set_index("date")
df["ret"] = df["收盤價"].pct_change()
monthly_vol = df["ret"].resample("ME").std()
print(monthly_vol.tail())
```

執行：

```bash
.venv/bin/python -m strategy_lab.data_analysis.<your_topic>.run
```

> **要產 Word 報告、或要抓美股與匯率資料，得先裝 `lab` extra**：
> `uv sync --extra lab`。`python-docx`（報告輸出）與 `yfinance`（美股／匯率日線）
> 都只在 `[project.optional-dependencies].lab` 裡，預設的 `uv sync` 不會裝，
> 直接跑會 `ModuleNotFoundError`。
> 注意 `uv sync` 會把環境同步成「剛好」指定的內容，要同時保留其他 extra 就一起列出。

### 常見模式：IC（Information Coefficient）分析

判斷「**訊號** 對 **隔日報酬** 是否有預測力」：

```python
import numpy as np

def information_coefficient(pred: np.ndarray, real: np.ndarray) -> float:
    """Pearson 相關係數；訊號值與實際報酬的線性相關度。"""
    if len(pred) < 3:
        return float("nan")
    return float(np.corrcoef(pred, real)[0, 1])

# 用法：
# ic = information_coefficient(pred=signal_t, real=return_t_plus_1)
# |IC| ≥ 0.05 在日頻已屬不錯；可以再切年度看穩定性。
```

### 常見模式：算扣除手續費／證交稅後的真實報酬

```python
from core.utils.instrument import StockUtils

def realistic_pnl(buy_price: float, sell_price: float, volume_lots: int) -> dict:
    profit = StockUtils.calculate_net_profit(
        buy_price=buy_price, sell_price=sell_price, volume=volume_lots,
    )
    roi = StockUtils.calculate_roi(
        buy_price=buy_price, sell_price=sell_price, volume=volume_lots,
    )
    return {"profit_twd": profit, "roi_pct": roi}

print(realistic_pnl(600.0, 620.0, 5))
# → {'profit_twd': ..., 'roi_pct': ...}
```

---

## Jupyter Notebook 使用慣例

- 放在 `strategy_lab/notebooks/<topic_name>/`，檔名建議 `YYYY_MM_<描述>.ipynb`。
- **第一個 cell** 用 markdown 寫研究問題、結論、TODO。
- Notebook 內 **不要定義會被 import 的函式**；要重用請搬到 `data_analysis/` 或 `strategies/`。
- Commit 前 **Restart & Clear All Outputs**，避免 diff 爆炸。
- 想直接拿 core API：

  ```python
  # 在 notebook 開頭一次性把專案根加入 sys.path（若 .venv 已用 -e . 安裝就不需要）
  import sys
  from pathlib import Path
  ROOT = Path.cwd()
  while ROOT.name and not (ROOT / "core").is_dir():
      ROOT = ROOT.parent
  sys.path.insert(0, str(ROOT))

  from core.api.tw.stock_price_api import StockPriceAPI
  ```

---

## 從 R&D 銜接到正式策略

當 `strategy_lab/strategies/<name>/` 的研究結論穩定後，按以下步驟「**搬家**」：

1. 在 `core/strategies/stock/` 建立 `<your_strategy>.py` 繼承 `BaseStockStrategy`；期貨策略則放 `core/strategies/futures/` 並繼承 `BaseFuturesStrategy`。
2. 把研究階段的訊號邏輯抽成 `_build_signals()`，
   並實作框架要求的 5 個 method：
   `setup_account / setup_apis / generate_open_signals / generate_close_signals / generate_stop_loss_signals`。
   `check_*_signal` 由基底提供、**不要覆寫**（覆寫會讓基底實作靜默失效），
   開倉張數由 `core/portfolio/` 換算，也不必寫 `calculate_position_size`。
3. 跑回測：

   ```bash
   .venv/bin/python run.py --strategy <YourStrategyName>
   ```
4. 結果會落到 `results/<strategy_name>/`（資料夾名稱取自策略的 `self.strategy_name`，不是類別名稱），
   會自動產出 `<策略>_balance_curve.png / <策略>_mdd.png / <策略>_trading_report.csv /
   <策略>_metrics_summary.csv` 等標準報表（**檔名一律帶策略名前綴**，共 5 份 CSV ＋ 5 張圖；
   完整清單見 [回測引擎說明](../core/backtest/README.md#回測結果)）。

> 詳細的「怎麼寫 `BaseStockStrategy` 子類別」請看 [`core/strategies/README.md`](../core/strategies/README.md)。
> 現成的範例是 `core/strategies/stock/` 與 `core/strategies/futures/` 底下的策略檔。
>
> **搬進 `core/` 的成品可能會被刪掉**：`tsmc_overnight_signal` 的成品策略就在 2026-09-17
> 因結論為否定而移除。研究資料夾要能獨立成立，不要把結論只寫在成品策略的 docstring 裡。

---

## 命名與檔案慣例

| 物件                  | 命名規則                            | 範例                                              |
| --------------------- | ----------------------------------- | ------------------------------------------------- |
| 研究主題資料夾        | `snake_case`，名稱要看得出主題     | `tech_new_high_continuation/`、`tsmc_overnight_signal/` |
| 想法筆記              | `ideas/<topic>/README.md` 或 `notes.md` | `ideas/momentum_breakout/README.md`           |
| 資料分析              | `data_analysis/<topic>/run.py` 等  | `data_analysis/tech_new_high_continuation/run.py` |
| Jupyter notebook      | `notebooks/<topic>/YYYY_MM_<描述>.ipynb` | `notebooks/macro_carry/2026_05_eda.ipynb`   |
| 策略研究              | `strategies/<topic>/`              | `strategies/tsmc_overnight_signal/`               |
| 研究產出（圖表 / CSV）| 放對應主題的 `output/` 子資料夾    | `data_analysis/<topic>/output/`                   |
| Word／PDF 報告        | 放對應主題的 `reports/` 子資料夾   | `strategies/<topic>/reports/*.docx`               |

**不建議：**

- 在 `strategy_lab/` 頂層直接放 script / notebook / md（請先建 `<category>/<topic>/` 子資料夾）。
  頂層目前只有 `CLAUDE.md`、`README.md` 與 `__init__.py`，那是目錄本身需要的，不是研究產出。
- 重複實作 `core/` 已有的 API、手續費、交易日邏輯。
- 在 R&D 階段就用 `BaseStockStrategy`（除非確定要上線）——R&D 階段請保持自由。

---

## Cursor 使用

本 repo 已設定 [`.cursor/rules/strategy-lab-layout.mdc`](../.cursor/rules/strategy-lab-layout.mdc)（`globs: strategy_lab/**`）。
在 `strategy_lab/` 內工作時，Cursor agent 會自動遵守上述分類與主題子資料夾慣例，無需每次重複說明。

---

## 聲明

本資料夾內所有研究、回測結果、Word 報告皆為 **研究與教學用途**，
**不構成投資建議**。實際下單前，請務必驗證資料、加入合理的滑價／流動性／法規假設，
並通過 `core/` 的正式回測框架驗證後再考慮上線。
