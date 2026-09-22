"""
報價轉換器：把各市場的原始資料轉成回測與實盤共用的 `BaseQuote`

實作依市場分在子目錄（`tw/`），**呼叫端一律用完整路徑**
（`core.adapters.tw.stock_quote_adapter`）——套件層 re-export 兩個名字，
只會讓同一個類別有兩條 import 路徑，看 grep 結果的人得多查一次。

**本層不做 I/O**：不得 import `core.api`、`core.dao` 或資料庫驅動。
查詢由資料源（feed）負責，轉換規則才能被別的來源重用；adapter 自己查的話，
規則會綁死在一個 API 上，而且測試為了驗一條轉換規則得先有連線。
這條約束由 `scripts/check_layer_deps.py` 檢查，不是只靠慣例。
"""
