"""
研究問題：台股科技業股票在「最高價創歷史新高」後，
         N 日收盤價高於創高日收盤價的機率為何？

定義：
- 創高：當日最高價嚴格大於過去所有交易日最高價（歷史新高）
- 持續上行：創高後第 N 個交易日收盤價 > 創高當日收盤價
- 機率：符合持續上行的事件數 ÷ 全部創高事件數

資料來源：SQLite price + taiwan_stock_info（2013-01-01 起）

產出：
- output/tech_new_high_events.csv            全部創高事件明細
- output/tech_new_high_prob_by_stock.csv     逐檔機率 + 事件數
- output/tech_new_high_prob_summary.csv      整體 + 分產業機率
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable, Sequence, Tuple

import pandas as pd
from loguru import logger

from core.api.tw.finmind_api import FinMindAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.utils.instrument import StockUtils

_ANALYSIS_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = _ANALYSIS_DIR / "output"

TECH_INDUSTRIES: frozenset[str] = frozenset(
    {
        "半導體業",
        "電子零組件業",
        "電腦及週邊設備業",
        "光電業",
        "通信網路業",
        "其他電子業",
        "電子通路業",
        "資訊服務業",
        "電子商務業",
        "電子工業",
        "其他電子類",
    }
)

HORIZONS: tuple[int, ...] = (5, 10, 20)
COOLDOWN_HORIZON: int = max(HORIZONS)
MIN_EVENT_COUNT: int = 10

START_DATE: dt.date = dt.date(2013, 1, 1)
END_DATE: dt.date = dt.date(2026, 5, 26)


def load_tech_universe() -> pd.DataFrame:
    """取得科技業普通股清單（含產業別）。"""

    finmind = FinMindAPI()
    info = finmind.get_all_stock_info()
    tech = info[info["industry_category"].isin(TECH_INDUSTRIES)].copy()
    tech_ids = StockUtils.filter_common_stocks(tech["stock_id"].astype(str).tolist())
    tech = tech[tech["stock_id"].astype(str).isin(tech_ids)]
    tech = tech.drop_duplicates(subset=["stock_id"], keep="last")
    tech = tech[["stock_id", "stock_name", "industry_category"]].rename(
        columns={"industry_category": "industry"}
    )
    logger.info(f"科技業普通股 universe: {len(tech)} 檔")
    return tech.reset_index(drop=True)


def load_price_panel(
    stock_ids: Sequence[str],
    start_date: dt.date,
    end_date: dt.date,
    price_api: StockPriceAPI,
) -> pd.DataFrame:
    """批次讀取 price 表 OHLC。"""

    if not stock_ids:
        return pd.DataFrame()

    # SQL 一律在 DAO；研究腳本不直接拿 `price_api.conn` 下查詢
    panel: pd.DataFrame = price_api.dao.get_high_close_by_stocks(
        list(stock_ids), start_date, end_date
    )
    panel["date"] = pd.to_datetime(panel["date"]).dt.date
    panel["stock_id"] = panel["stock_id"].astype(str)
    logger.info(f"讀取 price 列數: {len(panel):,}")
    return panel


def add_new_high_and_forward(df: pd.DataFrame, horizons: Iterable[int]) -> pd.DataFrame:
    """標記創高事件並計算各 horizon 的後續收盤延續。"""

    out = df.sort_values("date").copy()
    prior_max = out["最高價"].cummax().shift(1)
    out["is_new_high"] = (out["最高價"] > prior_max) & prior_max.notna()
    out["event_high"] = out["最高價"]
    out["event_close"] = out["收盤價"]

    for n in horizons:
        fwd_close = out["收盤價"].shift(-n)
        out[f"fwd_close_{n}d"] = fwd_close
        out[f"continued_up_{n}d"] = fwd_close > out["event_close"]

    return out


def extract_stock_events(df: pd.DataFrame, horizons: Iterable[int]) -> pd.DataFrame:
    """從單股時間序列取出有效創高事件。"""

    enriched = add_new_high_and_forward(df, horizons)
    events = enriched[enriched["is_new_high"]].copy()
    if events.empty:
        return events

    max_horizon = max(horizons)
    valid = events[f"fwd_close_{max_horizon}d"].notna()
    for n in horizons:
        valid &= events[f"fwd_close_{n}d"].notna()
    events = events[valid]
    return events


def extract_non_overlapping_events(
    events: pd.DataFrame,
    horizon: int,
) -> pd.DataFrame:
    """創高後 horizon 日內不再計入新事件（依每檔股票分別去重）。"""

    if events.empty:
        return events.copy()

    kept_frames: list[pd.DataFrame] = []
    for _, group in events.sort_values("date").groupby("stock_id", sort=False):
        next_allowed_idx = 0
        keep_indices: list[int] = []
        for i, idx in enumerate(group.index):
            if i < next_allowed_idx:
                continue
            keep_indices.append(idx)
            next_allowed_idx = i + horizon + 1
        if keep_indices:
            kept_frames.append(group.loc[keep_indices])

    if not kept_frames:
        return events.iloc[0:0].copy()
    return pd.concat(kept_frames, ignore_index=False).sort_values(["stock_id", "date"])


def _prob_columns(horizons: Iterable[int]) -> list[str]:
    return [f"prob_{n}d" for n in horizons]


def aggregate_probabilities_by_stock(
    events: pd.DataFrame,
    universe: pd.DataFrame,
    horizons: Iterable[int],
    event_mode: str,
) -> pd.DataFrame:
    """逐檔彙總機率。"""

    if events.empty:
        cols = ["stock_id", "stock_name", "industry", "event_mode", "event_count"]
        cols += _prob_columns(horizons) + ["low_sample"]
        return pd.DataFrame(columns=cols)

    rows: list[dict] = []
    for stock_id, group in events.groupby("stock_id"):
        row: dict = {
            "stock_id": stock_id,
            "event_mode": event_mode,
            "event_count": len(group),
        }
        for n in horizons:
            col = f"continued_up_{n}d"
            row[f"prob_{n}d"] = (
                group[col].mean() if col in group.columns else float("nan")
            )
        row["low_sample"] = row["event_count"] < MIN_EVENT_COUNT
        rows.append(row)

    by_stock = pd.DataFrame(rows)
    by_stock = by_stock.merge(universe, on="stock_id", how="left")
    col_order = [
        "stock_id",
        "stock_name",
        "industry",
        "event_mode",
        "event_count",
        *_prob_columns(horizons),
        "low_sample",
    ]
    return by_stock[col_order].sort_values(
        ["event_count", "stock_id"], ascending=[False, True]
    )


def aggregate_probabilities_summary(
    events: pd.DataFrame,
    horizons: Iterable[int],
    event_mode: str,
) -> pd.DataFrame:
    """整體與分產業彙總機率。"""

    rows: list[dict] = []

    def _append_scope(scope: str, group: pd.DataFrame) -> None:
        if group.empty:
            return
        row: dict = {
            "scope": scope,
            "event_mode": event_mode,
            "event_count": len(group),
        }
        for n in horizons:
            row[f"prob_{n}d"] = group[f"continued_up_{n}d"].mean()
        rows.append(row)

    _append_scope("all_tech", events)
    if "industry" in events.columns:
        for industry, group in events.groupby("industry", sort=True):
            _append_scope(f"by_industry:{industry}", group)

    cols = ["scope", "event_mode", "event_count", *_prob_columns(horizons)]
    return pd.DataFrame(rows, columns=cols)


def build_event_detail(events: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """事件明細表（含產業）。"""

    detail = events.merge(universe, on="stock_id", how="left")
    keep_cols = [
        "stock_id",
        "stock_name",
        "industry",
        "date",
        "event_high",
        "event_close",
    ]
    for n in HORIZONS:
        keep_cols.extend([f"fwd_close_{n}d", f"continued_up_{n}d"])
    existing = [c for c in keep_cols if c in detail.columns]
    return detail[existing].sort_values(["stock_id", "date"])


def run_analysis() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """執行完整分析流程。"""

    universe = load_tech_universe()
    stock_ids = universe["stock_id"].astype(str).tolist()

    price_api = StockPriceAPI()
    panel = load_price_panel(stock_ids, START_DATE, END_DATE, price_api)
    if panel.empty:
        raise RuntimeError("price 資料為空，請確認資料庫已更新。")

    event_frames: list[pd.DataFrame] = []
    for stock_id, group in panel.groupby("stock_id", sort=False):
        events = extract_stock_events(group, HORIZONS)
        if not events.empty:
            events = events.assign(stock_id=stock_id)
            event_frames.append(events)

    if not event_frames:
        raise RuntimeError("未偵測到任何創高事件。")

    all_events_raw = pd.concat(event_frames, ignore_index=True)
    all_events = build_event_detail(all_events_raw, universe)
    all_events["event_mode"] = "all_events"

    non_overlap_raw = extract_non_overlapping_events(all_events_raw, COOLDOWN_HORIZON)
    non_overlap = build_event_detail(non_overlap_raw, universe)
    non_overlap["event_mode"] = "non_overlap"

    by_stock_all = aggregate_probabilities_by_stock(
        all_events_raw.merge(universe, on="stock_id"),
        universe,
        HORIZONS,
        "all_events",
    )
    by_stock_non = aggregate_probabilities_by_stock(
        non_overlap_raw.merge(universe, on="stock_id"),
        universe,
        HORIZONS,
        "non_overlap",
    )
    by_stock = pd.concat([by_stock_all, by_stock_non], ignore_index=True)

    summary_all = aggregate_probabilities_summary(
        all_events_raw.merge(universe, on="stock_id"), HORIZONS, "all_events"
    )
    summary_non = aggregate_probabilities_summary(
        non_overlap_raw.merge(universe, on="stock_id"), HORIZONS, "non_overlap"
    )
    summary = pd.concat([summary_all, summary_non], ignore_index=True)

    events_out = pd.concat([all_events, non_overlap], ignore_index=True)
    return events_out, by_stock, summary


def write_outputs(
    events: pd.DataFrame,
    by_stock: pd.DataFrame,
    summary: pd.DataFrame,
) -> Path:
    """寫入 CSV 並回傳 output 目錄。"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    events_path = OUTPUT_DIR / "tech_new_high_events.csv"
    by_stock_path = OUTPUT_DIR / "tech_new_high_prob_by_stock.csv"
    summary_path = OUTPUT_DIR / "tech_new_high_prob_summary.csv"

    events.to_csv(events_path, index=False, encoding="utf-8-sig")
    by_stock.to_csv(by_stock_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    logger.info(f"事件明細: {events_path} ({len(events):,} 列)")
    logger.info(f"逐檔機率: {by_stock_path} ({len(by_stock):,} 列)")
    logger.info(f"彙總機率: {summary_path} ({len(summary):,} 列)")

    non_overlap_summary = summary[summary["event_mode"] == "non_overlap"]
    if not non_overlap_summary.empty:
        overall = non_overlap_summary[non_overlap_summary["scope"] == "all_tech"].iloc[
            0
        ]
        logger.info(
            "整體 non_overlap 機率 — "
            f"5d={overall['prob_5d']:.2%}, "
            f"10d={overall['prob_10d']:.2%}, "
            f"20d={overall['prob_20d']:.2%} "
            f"(events={int(overall['event_count'])})"
        )

    return OUTPUT_DIR
