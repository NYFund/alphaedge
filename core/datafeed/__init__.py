"""datafeed package: 引擎與資料源之間的共用契約

`BaseDataFeed` 由回測與實盤共用，不是回測概念——放在 `core/backtest/` 底下的話，
實盤與策略層為了拿一個介面就得伸手進回測套件。

**刻意不做套件層 eager import**：各市場的實作相依 `core.api` 與 `core.adapters`，
一旦在此 re-export，被 `core.api` 相依的模組就會觸發循環 import。
與 `core/backtest/__init__.py`、`core/strategies/__init__.py` 同一慣例，
呼叫端一律使用完整模組路徑。
"""
