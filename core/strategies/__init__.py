from .base import BaseStrategy

"""
策略契約：`BaseStrategy` 與各商品類別的策略基底

**本套件只放契約**，具體策略在頂層 `strategies/`、由 `strategies.loader.StrategyLoader`
依名稱掃描載入。引擎、factory 與報表只需要認得契約；具體策略放回這裡會被
`scripts/check_layer_deps.py` 擋下。
"""

# 刻意不在此 eager import 策略載入器：它掃描時會載入具體策略，在契約的套件層先載入，
# 等於讓每個 import 契約的人都順帶執行所有策略模組（與 core/backtest/__init__.py 同一類問題）。
