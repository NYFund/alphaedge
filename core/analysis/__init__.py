"""
analysis package: 績效指標與風險調整後報酬

回測報表與（日後的）實盤日報要算同一組指標，故不放在 `core/backtest/` 底下——
放在那裡的話，實盤為了拿公式就得伸手進回測套件。
"""

# 刻意不在套件層 import 任何模組：`performance_metrics.py` 是**只相依 math 與 typing 的
# 純函式**，分層上比 `core.utils` 還低，任何一層都能 import 它而不拉進 pandas／shioaji。
# 套件層一旦 eager import 相依重套件的模組，這個性質就沒了。呼叫端一律用完整路徑
# `from core.analysis.performance_metrics import ...`。
