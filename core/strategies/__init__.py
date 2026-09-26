from .base import BaseStrategy

"""Main entry point for strategy modules, including stocks, futures, etc"""

# 刻意不在此 eager import StrategyLoader：它會拉進整個 stock 套件，
# 而策略的市場基底需要 import core.backtest.models 的成交假設，形成循環
# （基底本身吃的是中立契約 core/datafeed/base.py，不 import core.backtest.datafeed）
# （與 core/backtest/__init__.py 同一類問題）。
