"""
部位建構層（Portfolio Construction）：決定「持有什麼、多少」

Alpha 層（`core/strategies/`）選出標的與方向，本層把它換算成部位大小。
**這是回測與實盤共用的邏輯**，不是回測概念——`sizing.py` 原本放在
`core/backtest/models/` 底下，會讓策略層在實盤執行時反向相依回測層。

本層只可 import `core.models`／`core.utils`／`core.config`／`core.api`，
**不可 import `core.backtest`／`core.live`／`core.strategies`**
（`scripts/check_layer_deps.py` 會擋）。

**刻意不做套件層 eager import**：與 `core/api/`、`core/dao/` 一致，
呼叫端一律指名模組（`from core.portfolio.sizing import EqualWeightSizer`）。
"""
