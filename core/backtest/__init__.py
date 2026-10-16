"""backtest package: Backtester and analysis components"""

# 刻意不在此 eager import Backtester：`backtester` 會連帶載入策略契約、部位管理、
# 報表與 simulator，只 import `core.backtest.models` 等子模組的人不該被迫載入整個引擎，
# 子模組之間也容易因此繞出循環 import。
# 策略宣告的成交與成本假設（`FillConfig`／`FuturesFillConfig`、`CostConfig`）放在
# core.models，策略層不 import 回測套件。
