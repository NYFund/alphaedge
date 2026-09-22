"""
台灣市場的回測資料源

目錄只承載「市場」一條軸，商品類別由檔名承載（`scripts/check_layer_deps.py` 會擋跨軸混放）。

市場結構（交易日曆、結算日、換月規則）不在這裡，在 `core/market/tw/`：
ETL、回測、實盤與策略都要用同一份，放在回測套件底下會讓 ETL 為了拿日曆而反向相依。

**刻意不做套件層 eager import**：這些模組相依 `core.api` 與 `core.adapters`，
在此 re-export 會觸發循環 import（與 `core/backtest/datafeed/__init__.py`
同一個理由）。呼叫端一律使用完整模組路徑。
"""
