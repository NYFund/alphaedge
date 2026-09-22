from core.backtest.models.settlement_model.base import BaseSettlementModel
from core.backtest.models.settlement_model.tw_futures import TwFuturesSettlementModel
from core.backtest.models.settlement_model.tw_stock import TwStockSettlementModel

"""
結算模型：一根 bar 收盤後由市場規則強制執行的動作

**套件層 re-export 三個類別**：原本三者同住一個 1,648 行的模組，
拆開後呼叫端（引擎、factory、兩個 model、一支策略與 4 個測試檔）一行都不必改。
這裡與 `core/strategies/` 那種「門面不可 eager import 具體實作」的規則不同——
那條規則防的是掃描策略時把每支策略都拉進來，此處只有三個類別且互為同一組契約。
"""

__all__ = [
    "BaseSettlementModel",
    "TwFuturesSettlementModel",
    "TwStockSettlementModel",
]
