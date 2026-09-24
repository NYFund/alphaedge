from core.backtest.models.settlement_model.base import BaseSettlementModel
from core.backtest.models.settlement_model.tw_futures import TwFuturesSettlementModel
from core.backtest.models.settlement_model.tw_stock import TwStockSettlementModel

"""
結算模型：一根 bar 收盤後由市場規則強制執行的動作

**套件層直接 re-export 具體實作**，與 `core/strategies/` 那種「門面不可 eager
import 具體實作」的規則不同——那條規則防的是掃描策略時把每支策略都拉進來，
此處只有三個類別且互為同一組契約，eager import 沒有成本。
"""

__all__ = [
    "BaseSettlementModel",
    "TwFuturesSettlementModel",
    "TwStockSettlementModel",
]
