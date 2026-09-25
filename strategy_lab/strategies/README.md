# strategies — 完整策略研究

每一個策略研究主題一個 `snake_case` 子資料夾（與 `ideas/`、`data_analysis/`、`notebooks/` 命名對齊）。**結構不強制**，但要能「**一鍵重現所有圖表與績效**」。

> 提醒：這裡是 R&D，**不是**正式回測。
> 正式回測請看 [`core/strategies/README.md`](../../core/strategies/README.md)，並用：
>
> ```bash
> .venv/bin/python run.py --strategy <StrategyName>
> ```

---

## 索引

| 策略資料夾 | 主題 | 核心訊號 | 標的 | 狀態 |
|------------|------|----------|------|------|
| [`tsmc_overnight_signal/`](tsmc_overnight_signal/README.md) | 跨市場隔夜訊號領先 | TSM ADR / ^SOX / TWD=X → Ridge 預測 | 2330.TW | 已產出中／英 Word 報告；結論為否定，搬進 `core/` 的成品策略已於 2026-09-17 移除 |

> 新增研究時，**請同步在此表登記**。

---

## 一個研究資料夾裡可以放什麼？

**沒有硬規定**——能讓研究結論清楚、可重現就好。常見成份：

| 元件 | 適合放這裡 |
|------|-----------|
| `README.md` | 研究問題、假設、樣本切分、模型、績效摘要、風險提醒 |
| `*.py` | 主要研究程式（資料 → 訊號 → 分析 → 圖表）|
| `*.ipynb` | 互動式探索／視覺化 |
| `output/` | 圖表 (PNG/HTML) 與績效 CSV |
| `reports/` | Word／PDF 報告與其產生器 |
| `notes.md` | 額外的研究筆記、相關文獻連結 |

**重點是**：請優先使用 `core/api/` 的資料 API 與 `core/utils/` 的工具
（手續費、單位換算、交易日判斷），不要在 lab 內重新發明輪子。
完整的 API 清單與用法請看上一層的 [`strategy_lab/README.md`](../README.md)。

---

## 範例：最精簡的研究檔（單檔即可）

```python
"""
strategy_lab/strategies/my_idea/explore.py

研究問題：
  TSM ADR 的隔夜報酬是否能預測隔日 2330.TW 報酬？
"""
import datetime as dt
import numpy as np
import pandas as pd
import yfinance as yf

from core.api.tw.stock_price_api import StockPriceAPI

price = StockPriceAPI()
tw = price.get_stock_price("2330", dt.date(2024, 1, 1), dt.date(2024, 12, 31))
tw["date"] = pd.to_datetime(tw["date"])
tw = tw.sort_values("date").set_index("date")
tw["r_tw"] = tw["收盤價"].pct_change()

us = yf.download("TSM", start="2024-01-01", end="2024-12-31", auto_adjust=True, progress=False)
us["r_tsm"] = us["Close"].pct_change()

merged = tw.join(us["r_tsm"], how="inner").dropna()
ic = np.corrcoef(merged["r_tsm"], merged["r_tw"])[0, 1]
print(f"IC(TSM_t, 2330_t) = {ic:.4f}")
```

執行：

```bash
.venv/bin/python -m strategy_lab.strategies.my_idea.explore
```

---

## 範例：較完整的多檔結構（適合長期研究）

如果研究做得夠深，可以分檔：

```
strategies/<your_topic>/
├── README.md             # 假設、模型、績效、風險
├── data.py               # 抓資料 + 對齊
├── features.py           # 特徵工程
├── model.py              # 模型訓練與預測
├── backtest.py           # 自寫的 vectorized backtest
├── plots.py              # plotly 圖表
├── output/               # CSV / PNG / HTML
└── reports/              # （可選）Word / PDF
```

完整可參考 [`tsmc_overnight_signal/`](tsmc_overnight_signal/README.md)。

---

## 「我覺得這策略 OK 了」之後怎麼做？

把訊號邏輯複寫進 `core/strategies/stock/<your_name>.py`，
繼承 `BaseStockStrategy`，實作以下 5 個 method：

- `setup_account` / `setup_apis`
- `generate_open_signals` / `generate_close_signals` / `generate_stop_loss_signals`

`check_*_signal` 由基底提供、**不要覆寫**（覆寫會讓基底實作靜默失效）；
開倉張數由 `core/portfolio/` 換算，不必寫 `calculate_position_size`。

詳細寫法 → [`core/strategies/README.md`](../../core/strategies/README.md)
（含完整範本與每個 method 的範例）。

完成後執行：

```bash
.venv/bin/python run.py --strategy <YourStrategyName>
```

結果會落到 `results/<策略>/`——**資料夾名稱取自 `self.strategy_name`，不是類別名稱**，
含 `<策略>_balance_curve.png`、`<策略>_mdd.png`、`<策略>_trading_report.csv`
等標準報表（**檔名一律帶策略名前綴**；完整清單見
[回測引擎說明](../../core/backtest/README.md#回測結果)）。
