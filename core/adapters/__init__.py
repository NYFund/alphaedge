"""
報價轉換器：把各市場的原始資料轉成回測與實盤共用的 `BaseQuote`

實作依市場分在子目錄（`tw/`），**呼叫端一律用完整路徑**
（`core.adapters.tw.stock_quote_adapter`）——套件層 re-export 兩個名字，
只會讓同一個類別有兩條 import 路徑，看 grep 結果的人得多查一次。

**本層不做 I/O**：不得 import `core.api`、`core.dao` 或資料庫驅動。
查詢由資料源（feed）負責，轉換規則才能被別的來源重用；adapter 自己查的話，
規則會綁死在一個 API 上，而且測試為了驗一條轉換規則得先有連線。
這條約束由 `scripts/check_layer_deps.py` 檢查，不是只靠慣例。

〈為什麼專案有兩個 raw → Quote 的轉換器〉

| 路徑 | 位置 | 輸入 |
|------|------|------|
| 回測 | `core/adapters/tw/` | `price`／`futures_price` 表的 DataFrame（中文欄位）|
| 實盤 | `core/broker/tw/shioaji_quote_stream.py` | 券商的 `Snapshot`／逐筆推播（pydantic 物件）|

**這個重複是設計，不是意外，不要合併。** ports & adapters 的分工就是如此：
normalization 屬於來源、contract 屬於核心。硬合併會讓 `core/adapters/` 相依
券商 SDK 的資料形狀，而本層的第一條約束正是「不做 I/O、不綁任一個來源」。

**兩邊共用的只有兩樣**：
1. **輸出契約**：`core/models/` 的 `StockQuote`／`FuturesQuote`。
   策略讀到的物件必須一模一樣，否則同一支策略在兩邊會拿到不同結構。
2. **與來源無關的規則**：`core/adapters/quote_validation.py`
   （價格有效性、重複代號）。這一層刻意吃值不吃列，三條路徑才套得上同一份。

**命名一律 `from_<來源形狀>`**：`from_day_rows()`／`from_tick_rows()`／
`from_stock_snapshot()`／`from_tick_message()`。`to_*` 讀起來像是 `Quote`
自己的方法，而它其實是「由某個來源建 Quote」的工廠——名字要說出來源是什麼，
因為**來源的形狀正是兩條路徑唯一的差別**。
"""
