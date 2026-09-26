"""
實盤引擎層：從委託送出到收盤對帳的完整一條路

- Features:
    1. `trader.py`：`LiveTrader` 本體，逐段落執行
    2. `segment.py`：段落定義與當下該跑哪一段的判定
    3. `oms/`：委託狀態機與回報正規化
    4. `risk/`：風控設定與擋單判定
    5. `execution` 前處理與 `datafeed/`：實盤報價與研究庫唯讀讀取
    6. `attribution/`、`capital_allocator.py`：多策略歸屬帳與資金額度分配
    7. `reconciler.py`、`account_sync.py`：券商部位對帳與歸屬帳重建
    8. `intraday/`：盤中事件迴圈與時段守門
    9. `after_close.py`：收盤後對帳、報表與 parity 比對
    10. `strategy_guard.py`：啟動前檢查策略宣告與回測語意是否一致
    11. `termination.py`：SIGTERM 收尾與退出碼
    12. `report/`、`notify/`：實盤報表、parity 與告警推播

**套件層刻意不 eager import 任何子模組**（作法與 `core/backtest/__init__.py` 相同）：
`core.live.factory` 會 import 到策略層，而策略層的市場基底又要 import 回測的
**成交假設**（`FillConfig`／`FuturesFillConfig`），在套件層先 import 會讓三者
形成循環。要用哪個元件就直接 import 那個模組。
"""
