from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd

"""
前端的交易明細輔助

**前端不計算任何績效指標**：Sharpe、Sortino、MDD、波動度、獲利因子、勝敗比與
Information Ratio 一律讀 reporter 落地的 `<策略>_metrics_summary.csv`，
公式只存在於 `core/backtest/analysis/performance_metrics.py` 一處。
同一個指標算在兩個地方，最後一定會出現「報表說 1.2、前端說 0.8」而沒有人知道哪個對。

⚠️ **前端因此完全不 import `core`**：映像的相依面只剩 `frontend/` 自己，
由 `tests/test_frontend_metrics.py` 盯住。

本模組不含任何 Streamlit 呼叫，這樣才測得到——`frontend/app.py` 在 import 時
就會執行 Streamlit 的版面設定，無法在測試裡 import。
"""

# 回測區間取自進出場日；`Sell Date` 對 SHORT 是開倉日，單獨用它定義不出區間
DATE_COLUMNS: List[str] = [
    "Entry Date",
    "Exit Date",
    "Buy Date",
    "Sell Date",
    "Date",
]


def extract_backtest_date_range(
    df: pd.DataFrame,
) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """
    - Description:
        由交易明細取回測區間

        取**所有**進出場日欄位的極值：只看 `Sell Date` 對 SHORT 是開倉日，
        區間的右端會提早。
    - Parameters:
        - df: pd.DataFrame
            交易明細
    - Return:
        - Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]
            起訖日；沒有任何日期欄位時為 `(None, None)`
    """

    parsed_dates: List[pd.Series] = []
    for column in DATE_COLUMNS:
        if column in df.columns:
            dates: pd.Series = pd.to_datetime(df[column], errors="coerce").dropna()
            if not dates.empty:
                parsed_dates.append(dates)

    if not parsed_dates:
        return None, None

    merged: pd.Series = pd.concat(parsed_dates, ignore_index=True)
    if merged.empty:
        return None, None
    return pd.Timestamp(merged.min()), pd.Timestamp(merged.max())


__all__ = [
    "DATE_COLUMNS",
    "extract_backtest_date_range",
]
