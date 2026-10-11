"""
回測的「一天怎麼跑」：`BaseSimulator` 與其實作

`Backtester` 只留逐日迴圈與每日前後的共用處理（`prepare_day()`／`finish_day()`），
當天的報價怎麼餵給策略、委託怎麼成交，交給這裡的 simulator。

刻意不在此 eager import：與 `core/backtest/__init__.py` 同一個理由，
simulator 相依 `core.strategies`，套件層 import 會讓循環相依提早觸發。
"""
