from .order_preprocess import (
    check_max_holdings,
    get_allowed_directions,
    get_execution_order,
    resolve_close_action,
    resolve_open_action,
    sort_orders,
    validate_orders,
)

"""
共用委託前處理：回測與實盤唯一的一份

這一層是兩邊不漂移的關鍵。方向白名單、`max_holdings`、排序這三件事如果各寫一份，
漂移不會報錯——只會讓實盤少送或多送一張單，而回測績效仍然漂亮。

**只 import `core.utils`／`core.models`**，由呼叫端傳入純參數（`position_type`、
`enable_intraday`、目前持倉檔數…），**不收策略物件**：收了就會多一條
`core.execution` → `core.strategies.base` 的同層邊，而這一層要能被更低層重用。
"""

__all__ = [
    "check_max_holdings",
    "get_allowed_directions",
    "get_execution_order",
    "resolve_close_action",
    "resolve_open_action",
    "sort_orders",
    "validate_orders",
]
