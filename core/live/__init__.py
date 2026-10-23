"""
實盤引擎層：從委託送出到收盤對帳的完整一條路

- Features:
    1. `trader.py`：`LiveTrader` 本體，逐段落執行
    2. `factory.py`：組裝實盤引擎，市場語意的分派只寫在這裡
    3. `oms/`：委託狀態機與回報正規化
    4. `risk/`：風控設定與擋單判定
    5. `execution/`：執行層，把策略的執行方式換成券商接受的委託
    6. `datafeed/`：實盤報價與研究庫唯讀讀取
    7. `attribution/`、`capital_allocator.py`：多策略歸屬帳與資金額度分配
    8. `reconciler.py`、`account_sync.py`：券商部位對帳與歸屬帳重建
    9. `intraday/`：盤中事件迴圈與時段守門
    10. `after_close.py`：收盤後對帳、報表與 parity 比對
    11. `strategy_guard.py`：啟動前檢查策略宣告與回測語意是否一致
    12. `termination.py`：SIGTERM 收尾與退出碼
    13. `report/`、`notify/`：實盤報表、parity 與告警推播

**套件層刻意不 eager import 任何子模組**（作法與 `core/backtest/__init__.py` 相同）：
`core.live.factory` 會 import 到策略層與回測組裝層，在套件層先 import 的話，
任何只想用一個元件的呼叫端都要付整串相依的成本，也更容易繞出循環。
要用哪個元件就直接 import 那個模組。
"""
