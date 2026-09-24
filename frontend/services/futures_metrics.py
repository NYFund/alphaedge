from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

"""
期貨專屬的報表指標

**期貨的風險視角與股票不同**：股票看的是「投入多少錢、值多少錢」，期貨看的是
「**佔用多少保證金、留了幾口**」——契約價值本身不佔用資金，用股票那組指標
（持股市值、資金水位）看期貨只會看到一堆與風險無關的數字。

本模組只做**純資料計算**，不含任何 Streamlit 呼叫，這樣才測得到
（`frontend/app.py` 在 import 時就會執行 Streamlit 的版面設定，無法在測試裡 import）。
"""

# 期貨交易明細才有的欄位（股票報表沒有這幾欄）
FUTURES_COLUMNS: List[str] = ["Contract ID", "Multiplier", "Margin"]

# 曝險曲線的欄位名
DATE_COLUMN: str = "Date"
MARGIN_COLUMN: str = "佔用保證金"
LOTS_COLUMN: str = "未平倉口數"


def is_futures_report(df: pd.DataFrame) -> bool:
    """
    判斷這份交易明細是不是期貨的

    以**欄位**而不是策略名稱判斷：名稱可以任意取，欄位是報表產生器決定的。
    """

    if df is None or df.empty:
        return False

    return all(column in df.columns for column in FUTURES_COLUMNS)


def to_numeric(df: pd.DataFrame, column: str) -> pd.Series:
    """取數值欄；欄位不存在或無法轉換時回空序列（不猜 0）"""

    if column not in df.columns:
        return pd.Series(dtype=float)

    return pd.to_numeric(df[column], errors="coerce").dropna()


def build_exposure_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    - Description:
        由交易明細走出**逐日的佔用保證金與未平倉口數**

        每筆交易在 `Entry Date` 佔用、在 `Exit Date` 釋放；把所有區間疊起來
        即為曝險曲線。**只在有變動的日子產生節點**——沒有交易的日子沿用前值，
        繪圖時用階梯線即可。

        ⚠️ **這是「以進出場日推導」的近似**：逐日盯市會讓保證金隨結算價變動，
        但交易明細只記錄開倉當下繳的那一筆。要精確到每日重算，得由回測引擎
        另外輸出逐日保證金——那屬於引擎的輸出格式變更，成本遠高於本近似的價值。
    - Parameters:
        - df: pd.DataFrame
            期貨交易明細
    - Return:
        - pd.DataFrame
            `Date` / `佔用保證金` / `未平倉口數`；資料不足時為空 DataFrame
    """

    required: List[str] = ["Entry Date", "Exit Date", "Margin", "Buy Volume"]
    if df is None or df.empty or not all(column in df.columns for column in required):
        return pd.DataFrame(columns=[DATE_COLUMN, MARGIN_COLUMN, LOTS_COLUMN])

    events: List[Dict] = []
    for _, row in df.iterrows():
        entry = pd.to_datetime(row["Entry Date"], errors="coerce")
        exit_ = pd.to_datetime(row["Exit Date"], errors="coerce")
        margin = pd.to_numeric(row["Margin"], errors="coerce")
        volume = pd.to_numeric(row["Buy Volume"], errors="coerce")

        if pd.isna(entry) or pd.isna(margin) or pd.isna(volume):
            continue

        events.append({"date": entry, "margin": float(margin), "lots": int(volume)})
        # 平倉日釋放；尚未平倉（Exit Date 為空）者一路留到最後
        if not pd.isna(exit_):
            events.append(
                {"date": exit_, "margin": -float(margin), "lots": -int(volume)}
            )

    if not events:
        return pd.DataFrame(columns=[DATE_COLUMN, MARGIN_COLUMN, LOTS_COLUMN])

    changes: pd.DataFrame = pd.DataFrame(events).groupby("date", as_index=False).sum()
    changes = changes.sort_values("date")

    return pd.DataFrame(
        {
            DATE_COLUMN: changes["date"],
            MARGIN_COLUMN: changes["margin"].cumsum().round(2),
            LOTS_COLUMN: changes["lots"].cumsum().astype(int),
        }
    ).reset_index(drop=True)


def summarise_margin(df: pd.DataFrame, starting_capital: Optional[float]) -> Dict:
    """
    - Description:
        期貨的保證金與口數摘要

        **`資金使用率` 用的是「同時佔用的保證金峰值」而不是總和**：把每筆交易的
        保證金加起來會得到一個沒有意義的巨大數字（同一筆錢用了 47 次），
        真正該看的是「最多同時押了多少」。
    - Parameters:
        - df: pd.DataFrame
            期貨交易明細
        - starting_capital: Optional[float]
            初始資金；None 時不計算使用率
    - Return:
        - Dict
            指標名稱 → 值（無法計算者為 None）
    """

    margins: pd.Series = to_numeric(df, "Margin")
    lots: pd.Series = to_numeric(df, "Buy Volume")
    roi: pd.Series = to_numeric(df, "ROI")
    settled: pd.Series = to_numeric(df, "Settled PnL")

    exposure: pd.DataFrame = build_exposure_series(df)
    peak_margin: Optional[float] = (
        float(exposure[MARGIN_COLUMN].max()) if not exposure.empty else None
    )
    peak_lots: Optional[int] = (
        int(exposure[LOTS_COLUMN].max()) if not exposure.empty else None
    )

    usage: Optional[float] = None
    if peak_margin is not None and starting_capital:
        usage = round(peak_margin / starting_capital * 100, 2)

    return {
        "峰值佔用保證金": peak_margin,
        "峰值未平倉口數": peak_lots,
        "資金使用率": usage,
        "平均每筆保證金": round(float(margins.mean()), 2)
        if not margins.empty
        else None,
        "總交易口數": int(lots.sum()) if not lots.empty else None,
        # 期貨的 ROI 分母是保證金，故這個平均值才是「資金效率」
        "平均保證金報酬率": round(float(roi.mean()), 2) if not roi.empty else None,
        # 逐日盯市已結算的損益佔比：接近 100% 代表獲利幾乎都在持有期間就已入帳
        "逐日結算損益": round(float(settled.sum()), 2) if not settled.empty else None,
    }
