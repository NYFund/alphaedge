#!/usr/bin/env python3
"""
台股科技業「創高後續上行機率」分析入口。

請於 AlphaEdge 專案根目錄以 `-m` 執行：
    .venv/bin/python -m strategy_lab.data_analysis.tech_new_high_continuation.run

**必須用 `-m`**：`strategy_lab` 不在 `pyproject` 的 `packages.find` 裡（只裝
`core*`／`apps*`／`strategies*`／`tests*`），直接跑檔案路徑時 `sys.path[0]` 是腳本自己的目錄，
`import strategy_lab.…` 會失敗。`-m` 會把工作目錄放進 `sys.path`，
專案根目錄底下就找得到（原本靠 `sys.path.insert` 硬塞）。

輸出目錄：strategy_lab/data_analysis/tech_new_high_continuation/output/
"""

from __future__ import annotations

from pathlib import Path

from strategy_lab.data_analysis.tech_new_high_continuation.analysis import (
    run_analysis,
    write_outputs,
)


def main() -> Path:
    events, by_stock, summary = run_analysis()
    return write_outputs(events, by_stock, summary)


if __name__ == "__main__":
    output_dir = main()
    print(f"輸出已寫入：{output_dir}")
