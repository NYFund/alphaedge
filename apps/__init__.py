"""
應用入口層：每支模組是一個可獨立執行的行程（`python -m apps.<name>`）

**只放入口該做的事**：
- 解析命令列參數（argparse），把字串轉成 `core` 的型別
- 呼叫 `core` 的 factory 組裝並執行
- 把執行結果與例外翻譯成退出碼、處理 SIGTERM 等程序層級的訊號

**不放業務邏輯**：組裝留在 `core/backtest/factory.py`、`core/live/factory.py`，
因為測試、`frontend/` 與 parity 單日回測也要組裝同一套物件，放在這裡它們就得
import 入口層。相對地，factory 只負責組裝，不讀命令列與環境變數做決策——
那些由入口讀好後當參數傳進去。

相依方向只能是 `apps` → `core`：`core` 若需要這裡的東西（例如段落對照表），
就把它往下搬進 `core`，不可反向 import。`scripts/check_layer_deps.py` 把本套件
登記為入口層，反向 import 會讓檢查失敗。
"""
