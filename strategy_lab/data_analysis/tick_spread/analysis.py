import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from core.api.tw.stock_price_api import StockPriceAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.connection import connect_sqlite
from core.utils.instrument import StockUtils

"""
價差研究的分析：把每檔每日的半價差與日 K 的成交金額、股價對起來

要回答的問題是「日 K 回測的滑價要不要逐標的設定、該怎麼設」：
1. 依日成交金額分級，各級的半價差落在哪裡（中位數、P75、P90）。
2. 半價差有多少是被跳動單位卡住的：價差大多是 1 檔的話，成本主要由股價與檔位表決定，
   用「半個跳動單位 ÷ 股價」就能估，不必維護逐標的的表。
3. 跨年度是否穩定。

只看普通股（`StockUtils.filter_common_stocks()`）：tick 資料本身就沒有 ETF。
"""

# 日成交金額分級（新台幣）：切點取整數級距，方便直接寫進設定
TURNOVER_BINS: List[float] = [0, 1e7, 5e7, 2e8, 1e9, float("inf")]
TURNOVER_LABELS: List[str] = [
    "<1千萬",
    "1千萬~5千萬",
    "5千萬~2億",
    "2億~10億",
    ">=10億",
]

# 股價分段：與普通股檔位表的六段相同，每段內跳動單位固定
PRICE_BINS: List[float] = [0, 10, 50, 100, 500, 1000, float("inf")]
PRICE_LABELS: List[str] = ["<10", "10~50", "50~100", "100~500", "500~1000", ">=1000"]

# 目前唯一設了滑價的策略用的基點，拿來對照
CURRENT_STRATEGY_BPS: float = 10.0


def load_spreads(cache_dir: Path) -> pd.DataFrame:
    """讀回所有月份的每檔每日價差彙總"""

    paths: List[Path] = sorted(cache_dir.glob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"{cache_dir} 沒有彙總檔，請先跑彙總")
    df: pd.DataFrame = pd.concat([pd.read_parquet(path) for path in paths])
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def load_daily_bars(start: datetime.date, end: datetime.date) -> pd.DataFrame:
    """唯讀讀出區間內的日 K（成交金額、收盤價），只留普通股"""

    api: StockPriceAPI = StockPriceAPI(
        conn=connect_sqlite(TW_STOCK_DB_PATH, read_only=True)
    )
    try:
        bars: pd.DataFrame = api.get_range(start, end)
    finally:
        api.conn.close()

    bars = bars[["date", "stock_id", "成交金額", "收盤價"]].rename(
        columns={"成交金額": "turnover", "收盤價": "close"}
    )
    common: List[str] = StockUtils.filter_common_stocks(
        bars["stock_id"].unique().tolist()
    )
    bars = bars[bars["stock_id"].isin(common)]
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    return bars


def build_panel(spreads: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """合併價差與日 K，標上成交金額分級與股價分段；當日沒有成交金額的列丟掉"""

    panel: pd.DataFrame = spreads.merge(bars, on=["date", "stock_id"], how="inner")
    panel = panel[(panel["turnover"] > 0) & (panel["close"] > 0)].copy()
    panel["turnover_bucket"] = pd.cut(
        panel["turnover"], TURNOVER_BINS, labels=TURNOVER_LABELS, right=False
    )
    panel["price_bucket"] = pd.cut(
        panel["close"], PRICE_BINS, labels=PRICE_LABELS, right=False
    )
    panel["year"] = panel["date"].str[:4]
    # 超出跳動單位下限的部分：0 代表價差就是 1 檔，正值是流動性不足多出來的
    panel["excess_half_bps"] = panel["med_half_bps"] - panel["avg_floor_half_bps"]
    return panel


def summarize_by(panel: pd.DataFrame, key: str) -> pd.DataFrame:
    """依分組彙總半價差的分布與跳動單位下限的佔比"""

    grouped = panel.groupby(key, observed=True)
    summary: pd.DataFrame = pd.DataFrame(
        {
            "stock_days": grouped.size(),
            "stocks": grouped["stock_id"].nunique(),
            "med_half_bps_p50": grouped["med_half_bps"].median(),
            "med_half_bps_p75": grouped["med_half_bps"].quantile(0.75),
            "med_half_bps_p90": grouped["med_half_bps"].quantile(0.90),
            "floor_half_bps_p50": grouped["avg_floor_half_bps"].median(),
            "excess_half_bps_p50": grouped["excess_half_bps"].median(),
            "one_tick_ratio_p50": grouped["one_tick_ratio"].median(),
            "share_over_current_bps": grouped["med_half_bps"].apply(
                lambda values: float((values > CURRENT_STRATEGY_BPS).mean())
            ),
        }
    )
    return summary.round(2)


def fit_log_model(panel: pd.DataFrame) -> Dict[str, float]:
    """
    - Description:
        以 log 半價差對 log 跳動單位下限（與 log 成交金額）做最小平方法，看下限解釋了多少

        只看下限的 R² 已經很高的話，「半個跳動單位 ÷ 股價」就是夠用的估計；
        加上成交金額後 R² 明顯上升，才值得做流動性分級。
    - Parameters:
        - panel: pd.DataFrame
            合併後的面板
    - Return:
        - Dict[str, float]
            兩個模型的 R² 與係數
    """

    y: np.ndarray = np.log(panel["med_half_bps"].to_numpy())
    floor: np.ndarray = np.log(panel["avg_floor_half_bps"].to_numpy())
    turnover: np.ndarray = np.log(panel["turnover"].to_numpy())

    def r_squared(features: List[np.ndarray]) -> Tuple[float, np.ndarray]:
        design: np.ndarray = np.column_stack([np.ones_like(y), *features])
        coef: np.ndarray = np.linalg.lstsq(design, y, rcond=None)[0]
        residual: np.ndarray = y - design @ coef
        return float(1 - residual.var() / y.var()), coef

    floor_r2, floor_coef = r_squared([floor])
    both_r2, both_coef = r_squared([floor, turnover])
    return {
        "r2_floor_only": round(floor_r2, 3),
        "r2_floor_and_turnover": round(both_r2, 3),
        "coef_floor": round(float(both_coef[1]), 3),
        "coef_log_turnover": round(float(both_coef[2]), 3),
    }


def run_analysis(cache_dir: Path, output_dir: Path) -> Dict[str, pd.DataFrame]:
    """跑完整分析並把各表寫成 CSV，回傳各表"""

    spreads: pd.DataFrame = load_spreads(cache_dir)
    start: datetime.date = datetime.date.fromisoformat(spreads["date"].min())
    end: datetime.date = datetime.date.fromisoformat(spreads["date"].max())
    panel: pd.DataFrame = build_panel(spreads, load_daily_bars(start, end))

    tables: Dict[str, pd.DataFrame] = {
        "by_turnover": summarize_by(panel, "turnover_bucket"),
        "by_price": summarize_by(panel, "price_bucket"),
        "by_year": summarize_by(panel, "year"),
        "price_x_turnover_p50": panel.pivot_table(
            index="price_bucket",
            columns="turnover_bucket",
            values="med_half_bps",
            aggfunc="median",
            observed=True,
        ).round(1),
        "model_fit": pd.DataFrame([fit_log_model(panel)]),
        "coverage": pd.DataFrame(
            [
                {
                    "start": start,
                    "end": end,
                    "trading_days": panel["date"].nunique(),
                    "stocks": panel["stock_id"].nunique(),
                    "stock_days": len(panel),
                }
            ]
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_csv(output_dir / f"{name}.csv", encoding="utf-8-sig")
    return tables
