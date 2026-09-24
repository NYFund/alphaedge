from core.pipeline.tw.updaters.financial_statement.equity_change import (
    EquityChangeMixin,
    EquityChangeSeasonStats,
)

"""
財報 updater 的實作子套件

對外門面是 `FinancialStatementUpdater`，呼叫端只認得它；本套件的元件一律經由
門面繼承或組合使用，不直接對外（與 `updaters/finmind/` 同一個結構）。
"""

__all__ = ["EquityChangeMixin", "EquityChangeSeasonStats"]
