from typing import FrozenSet

"""
實盤風控／守門事件（`live_risk_event.category`）的類別名登錄

**類別名只登錄在這裡。** 寫入端（風控、資金分配、保證金閘、跨策略守門、執行層）與
讀取端（盤後 parity 的歸因對照表）各抄一份字串，改名或新增只改到一邊時不會有任何錯誤：
parity 曾經對照著引擎從未寫出的名字，被擋下的單一律被歸成「快照口徑」，真正的原因被蓋掉。
`tests/live/test_event_category_registry.py` 掃描 `core/live/` 所有寫事件的字面值，
不在本檔的就紅；parity 的對照表也必須與 `ORDER_BLOCK_CATEGORIES` 一致。

兩組：
- `ORDER_BLOCK_CATEGORIES`：**某一張策略委託被擋在送出之前**，事件帶 `symbol`。
  parity 看到「回測有單、實盤沒有」時以這些事件歸因，每一個都要有對應的 parity 類別。
- `OTHER_EVENT_CATEGORIES`：策略層級的狀態變化、盤後處理、換月流程等，不對應單一委託，
  parity 不用它們歸因。新增類別時要決定它屬於哪一組。
"""

# === 逐單擋下（事件帶 symbol；parity 以此歸因）===
# 事前風控（`core/live/risk/risk_manager.py`）
SINGLE_ORDER_AMOUNT: str = "SINGLE_ORDER_AMOUNT"
DAILY_AMOUNT: str = "DAILY_AMOUNT"
VOLUME_CAP: str = "VOLUME_CAP"
PRICE_LIMIT: str = "PRICE_LIMIT"
PRICE_DEVIATION: str = "PRICE_DEVIATION"
ORDER_RATE: str = "ORDER_RATE"
BATCH_EXPOSURE: str = "BATCH_EXPOSURE"
KILL_SWITCH: str = "KILL_SWITCH"
MODE_HALTED: str = "MODE_HALTED"
MODE_REDUCE_ONLY: str = "MODE_REDUCE_ONLY"
# 委託前處理與送單流程（`core/live/trader.py`）
MAX_HOLDINGS: str = "MAX_HOLDINGS"
MARGIN_INSUFFICIENT: str = "MARGIN_INSUFFICIENT"
EXECUTION_UNAVAILABLE: str = "EXECUTION_UNAVAILABLE"
# 資金額度分配（`core/live/capital_allocator.py`）
CAPITAL_EXHAUSTED: str = "CAPITAL_EXHAUSTED"
# 跨策略同標的守門（`core/live/attribution/conflict_guard.py`）
CROSS_STRATEGY_BLOCKED: str = "CROSS_STRATEGY_BLOCKED"

ORDER_BLOCK_CATEGORIES: FrozenSet[str] = frozenset(
    {
        SINGLE_ORDER_AMOUNT,
        DAILY_AMOUNT,
        VOLUME_CAP,
        PRICE_LIMIT,
        PRICE_DEVIATION,
        ORDER_RATE,
        BATCH_EXPOSURE,
        KILL_SWITCH,
        MODE_HALTED,
        MODE_REDUCE_ONLY,
        MAX_HOLDINGS,
        MARGIN_INSUFFICIENT,
        EXECUTION_UNAVAILABLE,
        CAPITAL_EXHAUSTED,
        CROSS_STRATEGY_BLOCKED,
    }
)

# === 策略層級、盤後與換月流程（不對應單一委託）===
# 交易模式降級（`core/live/risk/risk_manager.py`）
DAILY_LOSS: str = "DAILY_LOSS"
ACCOUNT_DAILY_LOSS: str = "ACCOUNT_DAILY_LOSS"
DEGRADE: str = "DEGRADE"
# 盤中與送單流程（`core/live/trader.py`）
DAY_TRADE_COVER_FAILED: str = "DAY_TRADE_COVER_FAILED"
PENDING_ACTION_TRUNCATED: str = "PENDING_ACTION_TRUNCATED"
UNSENT_EXIT: str = "UNSENT_EXIT"
REFERENCE_PRICE_MISSING: str = "REFERENCE_PRICE_MISSING"
ROLL_SKIPPED: str = "ROLL_SKIPPED"
ROLL_FAILED: str = "ROLL_FAILED"
ROLL_ABANDONED: str = "ROLL_ABANDONED"
ROLL_OPEN_FAILED: str = "ROLL_OPEN_FAILED"
# 券商登入後仍組不出標的池：策略拿到 0 筆報價、永遠不會出訊號，而鉤子照常被呼叫、不會報錯
UNIVERSE_EMPTY: str = "UNIVERSE_EMPTY"
# 券商連線中斷與重連成功：**不降級**，是否有漏接由重連後的對帳判定（不一致時由對帳器降級）
BROKER_SESSION_DOWN: str = "BROKER_SESSION_DOWN"
BROKER_RECONNECTED: str = "BROKER_RECONNECTED"
# 盤後處理與對帳（`core/live/after_close.py`、`core/live/reconciler.py`）
UNFILLED_EXIT: str = "UNFILLED_EXIT"
COST_MODEL_DRIFT: str = "COST_MODEL_DRIFT"
RECONCILE_MISMATCH: str = "RECONCILE_MISMATCH"
# 成交回報的跨行程歸屬（`core/live/oms/order_manager.py`、`core/live/after_close.py`）：
# 兩條線是兩個行程、同收一筆成交，先寫入的若不是擁有者，擁有者要認領；盤後再以成交事實回填
FILL_CLAIMED: str = "FILL_CLAIMED"
FILL_BACKFILLED: str = "FILL_BACKFILLED"
# OMS 的回報與接管異常（`core/live/oms/order_manager.py`）
FILL_UNMATCHED: str = "FILL_UNMATCHED"
FILL_REPORT_MISSING: str = "FILL_REPORT_MISSING"
ORDER_STATE_INVALID: str = "ORDER_STATE_INVALID"
RECOVER_AMBIGUOUS: str = "RECOVER_AMBIGUOUS"

OTHER_EVENT_CATEGORIES: FrozenSet[str] = frozenset(
    {
        DAILY_LOSS,
        ACCOUNT_DAILY_LOSS,
        DEGRADE,
        DAY_TRADE_COVER_FAILED,
        PENDING_ACTION_TRUNCATED,
        UNSENT_EXIT,
        REFERENCE_PRICE_MISSING,
        ROLL_SKIPPED,
        ROLL_FAILED,
        ROLL_ABANDONED,
        ROLL_OPEN_FAILED,
        UNIVERSE_EMPTY,
        BROKER_SESSION_DOWN,
        BROKER_RECONNECTED,
        UNFILLED_EXIT,
        COST_MODEL_DRIFT,
        RECONCILE_MISMATCH,
        FILL_CLAIMED,
        FILL_BACKFILLED,
        FILL_UNMATCHED,
        FILL_REPORT_MISSING,
        ORDER_STATE_INVALID,
        RECOVER_AMBIGUOUS,
    }
)

ALL_EVENT_CATEGORIES: FrozenSet[str] = ORDER_BLOCK_CATEGORIES | OTHER_EVENT_CATEGORIES
