"""market package: 市場結構（交易日曆、結算日、換月規則）

ETL、回測、實盤與策略都要用**同一份**市場結構，所以它不屬於回測套件——
業界也是把它獨立成 library（`exchange_calendars`、`pandas_market_calendars`），
正因為每一條路徑都要問同樣的問題：今天開不開市、這個契約什麼時候到期。

**刻意不做套件層 eager import**：日曆的建構子收 `core.api` 的物件，
在套件層 re-export 會讓 import 順序變得脆弱。呼叫端一律使用完整模組路徑。
"""
