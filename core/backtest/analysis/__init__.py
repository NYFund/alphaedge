"""analysis package: 績效指標與風險調整後報酬"""

# 刻意不在套件層 import 任何模組：前端映像只 COPY 本檔與 `performance_metrics.py`，
# 而 `performance_metrics.py` 是**只相依 math 與 typing 的純函式**，前端能不裝後端就算出
# Sharpe／Sortino 靠的就是它。套件層一旦 eager import 相依 pandas／shioaji 的模組，
# 前端就得把整個後端連同 shioaji 一起裝進映像。呼叫端一律用完整路徑
# `from core.backtest.analysis.performance_metrics import ...`。
