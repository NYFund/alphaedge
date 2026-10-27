"""
資料 API：查詢已入庫的資料，不負責爬取與清洗

`base.py` 是市場與商品皆無關的骨架；各市場的實作在子目錄（`tw/`），
目錄只承載「市場」一條軸，商品類別由檔名承載（`scripts/check_layer_deps.py` 會擋跨軸混放）。

**刻意不做套件層 eager import**：呼叫端一律使用完整模組路徑，套件層沒有門面要維護。
選用相依（台股 tick 的 `psycopg`／`connectorx`）都在 `core/dao/` 的函式內才 import，
所以即使沒裝，import 任何 API 模組也不會失敗；要等真的查詢時才會報缺套件。
"""
