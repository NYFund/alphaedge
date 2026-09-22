"""datafeed package: 回測的資料載入與報價轉換

介面本身（`BaseDataFeed`）在中立的 `core/datafeed/`，回測與實盤共用同一份。

**刻意不做套件層 eager import**：`tw_stock_datafeed` 會相依 `core.adapters`
與 `core.api`，一旦在此 re-export，任何被 `core.api` 相依的模組都會觸發循環 import。
與 `core/backtest/__init__.py`、`core/strategies/__init__.py` 同一慣例，
呼叫端一律使用完整模組路徑。
"""
