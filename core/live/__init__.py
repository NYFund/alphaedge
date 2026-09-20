"""
實盤引擎層：委託管理、風控、對帳、資料載入、盤中迴圈、報表與通知

**套件層刻意不 eager import 任何子模組**（作法與 `core/backtest/__init__.py` 相同）：
`core.live.factory` 會 import 到策略層，而策略層又要 import 回測的成本設定，
在套件層先 import 會讓三者形成循環。要用哪個元件就直接 import 那個模組。
"""
