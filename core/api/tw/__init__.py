"""
台灣市場的資料 API（股票 ＋ 期貨）

**目錄只承載「市場」一條軸，商品類別由檔名承載**（`stock_price_api.py` vs
`futures_price_api.py`），與 `core/pipeline/tw/` 一致——每層目錄只放一條軸，
`tw_stock/`／`tw_futures/` 會把兩條軸壓成單一目錄名。

**刻意不做套件層 eager import**：呼叫端一律使用完整模組路徑。`stock_tick_api`
的選用相依（`psycopg`／`connectorx`）經 `core/dao/tw/stock_tick_dao.py` 在函式內才 import，
沒裝的環境照樣 import 得進來，建立 `StockTickAPI` 時才會報缺套件。
"""
