"""backtest package: Backtester and analysis components"""

# 刻意不在此 eager import Backtester：backtester 相依 core.strategies，
# 而 core.strategies 的市場基底需要 import core.backtest.models 的成交假設
# （`FillConfig`／`FuturesFillConfig`；成本設定已下沉到 core.models），
# 套件層的 eager import 會讓兩者形成循環。
