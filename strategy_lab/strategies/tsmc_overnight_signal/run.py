#!/usr/bin/env python3
"""
台積電跨市場隔夜訊號回測入口。

請於 AlphaEdge 專案根目錄以 `-m` 執行：
    .venv/bin/python -m strategy_lab.strategies.tsmc_overnight_signal.run

**必須用 `-m`**：`strategy_lab` 不在 `pyproject` 的 `packages.find` 裡（只裝
`core*`／`apps*`／`strategies*`／`tests*`），直接跑檔案路徑時 `sys.path[0]` 是腳本自己的目錄，
`import strategy_lab.…` 會失敗。

輸出目錄：`strategy_lab/strategies/tsmc_overnight_signal/output/`
（資產曲線、MDD、滾動 Sharpe、月報酬熱圖、IC 等）。
"""

from __future__ import annotations

import datetime as dt

from strategy_lab.strategies.tsmc_overnight_signal.pipeline import main

if __name__ == "__main__":
    output_path = main(
        data_start=dt.date(2020, 1, 1),
        data_end=dt.date(2026, 4, 25),
    )
    print(f"輸出已寫入：{output_path}")
