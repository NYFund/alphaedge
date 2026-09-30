from .base import BaseStrategy

"""Main entry point for strategy modules, including stocks, futures, etc"""

# 刻意不在此 eager import StrategyLoader：它掃描時會載入具體策略，而引擎、factory
# 與報表只需要 `BaseStrategy` 這個契約；在套件層先載入，等於讓每個 import 契約的人
# 都順帶執行所有策略模組（與 core/backtest/__init__.py 同一類問題）。
