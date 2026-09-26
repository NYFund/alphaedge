from .base import BaseStockStrategy

"""
台股策略的市場基底

**門面只轉出基底，不轉出具體策略**：策略由 `StrategyLoader` 掃描目錄收錄，
在此列舉等於維護第二份清單，新增策略時必然漏改。
"""
