"""
Overnight lead-signal strategy backtest module.

Uses TSM, ^SOX, and TWD=X as pre-open lead features to forecast same-day
2330.TW return and run a long-or-flat daily backtest.

Exports two PnL paths on the test window: (1) run_vectorized_continuous_backtest,
an idealized full-notional vectorization; (2) run_backtest_with_signal, a lot-based
path with cash and Taiwan stock fees/taxes.

Also writes baseline_comparison.csv for Ridge, OLS, logistic direction (IRLS),
and TSM-sign-only rules on the same test calendar.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf

from core.api.tw.stock_price_api import StockPriceAPI
from core.utils import Units
from core.utils.instrument import StockUtils
from strategy_lab.strategies.tsmc_overnight_signal.ridge import (
    ridge_fit_predict,
    tune_alpha,
)

# 此檔位於 strategy_lab/strategies/tsmc_overnight_signal/pipeline.py
# parents[3] = AlphaEdge 專案根目錄
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STRATEGY_DIR = Path(__file__).resolve().parent
_DEFAULT_OUTPUT_DIR = _STRATEGY_DIR / "output"

TRAIN_END = dt.date(2020, 12, 31)
VAL_START = dt.date(2021, 1, 1)
VAL_END = dt.date(2021, 12, 31)
TEST_START = dt.date(2022, 1, 1)

FEE_BUY: float = 0.001425
FEE_SELL_PLUS_TAX: float = 0.001425 + 0.003

# Plot export: reduce title/legend clipping in PNG
_PLOT_MARGIN = dict(l=72, r=108, t=100, b=80)


def _apply_figure_margins(fig: go.Figure) -> None:
    fig.update_layout(margin=_PLOT_MARGIN)


def _ols_fit_predict(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_pred: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Unregularized linear regression with intercept (OLS)."""
    n, p = X_train.shape
    X1 = np.c_[np.ones(n), X_train]
    coef, *_ = np.linalg.lstsq(X1, y_train, rcond=None)
    y_hat = np.c_[np.ones(len(X_pred)), X_pred] @ coef
    return coef, y_hat


def _logistic_irls_fit(
    X_design: np.ndarray,
    y_binary: np.ndarray,
    max_iter: int = 80,
    tol: float = 1e-9,
) -> np.ndarray:
    """
    Logistic regression via IRLS. X_design must include a leading column of ones (intercept).
    y_binary in {0, 1}.
    """
    n, p = X_design.shape
    beta = np.zeros(p, dtype=float)
    y = y_binary.astype(float)
    for _ in range(max_iter):
        eta = np.clip(X_design @ beta, -35.0, 35.0)
        pi = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(pi * (1.0 - pi), 1e-10, None)
        z = eta + (y - pi) / w
        xw = X_design * w[:, np.newaxis]
        hess = X_design.T @ xw + 1e-8 * np.eye(p)
        rhs = X_design.T @ (w * z)
        beta_new = np.linalg.solve(hess, rhs)
        if float(np.max(np.abs(beta_new - beta))) < tol:
            return beta_new
        beta = beta_new
    return beta


def _exec_with_pred_signal(
    exec_skel: pd.DataFrame,
    dates: np.ndarray,
    pred: np.ndarray,
    signal: np.ndarray,
) -> pd.DataFrame:
    """exec_skel: date, close_2330, r_2330 (test window)."""
    ps = pd.DataFrame(
        {
            "date": dates,
            "pred": pred.astype(float),
            "signal": signal.astype(int),
        }
    )
    ex = exec_skel.merge(ps, on="date", how="left")
    ex["pred"] = ex["pred"].fillna(0.0)
    ex["signal"] = ex["signal"].fillna(0).astype(int)
    return ex


def _evaluate_model_on_exec(
    name: str,
    exec_skel: pd.DataFrame,
    dates: np.ndarray,
    pred: np.ndarray,
    signal: np.ndarray,
) -> Dict[str, float]:
    ex = _exec_with_pred_signal(exec_skel, dates, pred, signal)
    ic = information_coefficient(
        ex["r_2330"].values.astype(float), ex["pred"].values.astype(float)
    )
    mr = summary_metrics(run_backtest_with_signal(ex))
    mv = summary_metrics(run_vectorized_continuous_backtest(ex))
    return {
        "model": name,
        "test_IC_pearson": float(ic) if not np.isnan(ic) else float("nan"),
        "vectorized_cum_return": float(mv["策略累積報酬"]),
        "realistic_cum_return": float(mr["策略累積報酬"]),
        "vectorized_sharpe": float(mv["策略Sharpe"]),
        "realistic_sharpe": float(mr["策略Sharpe"]),
        "vectorized_mdd_pct": float(mv["策略最大回撤%"]),
        "realistic_mdd_pct": float(mr["策略最大回撤%"]),
    }


def fetch_panel(start: dt.date, end: dt.date) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    2330 收盤與報酬：與 core/backtest 一致，使用 StockPriceAPI（SQLite）。
    美股特徵：yfinance（TSM、^SOX、TWD=X）。
    """
    tw_id = "2330"
    price_api = StockPriceAPI()
    tw_df = price_api.get_stock_price(tw_id, start, end)
    if tw_df.empty:
        raise RuntimeError(
            f"No DB price rows for {tw_id} between {start} and {end}. "
            "Populate data/db price table to match backtester."
        )
    tw_df = tw_df.sort_values("date").reset_index(drop=True)
    tw_df["date"] = pd.to_datetime(tw_df["date"]).dt.normalize()
    tw_px = tw_df.set_index("date")["收盤價"].astype(float).sort_index()
    tw_px.index = pd.to_datetime(tw_px.index).tz_localize(None).normalize()

    us_tickers = ["TSM", "^SOX", "TWD=X"]
    data = yf.download(
        us_tickers,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if data.empty:
        raise RuntimeError("yfinance returned empty dataset for US tickers.")
    close_us = (
        data["Close"].copy()
        if isinstance(data.columns, pd.MultiIndex)
        else data[["Close"]].copy()
    )
    if not isinstance(data.columns, pd.MultiIndex):
        close_us.columns = us_tickers[:1]
    close_us = close_us.sort_index()
    close_us.index = pd.to_datetime(close_us.index).tz_localize(None).normalize()
    rets = close_us.pct_change()
    us_calendar = close_us["TSM"].dropna().index

    close_df = pd.DataFrame({"2330.TW": tw_px})

    rows = []
    for ts in tw_px.index:
        ts_d = pd.Timestamp(ts).normalize()
        prev_us = us_calendar[us_calendar < ts_d]
        if len(prev_us) == 0:
            continue
        us_d = prev_us.max()
        try:
            r_tsm = float(rets.loc[us_d, "TSM"])
            r_sox = float(rets.loc[us_d, "^SOX"])
            r_fx = float(rets.loc[us_d, "TWD=X"])
        except (KeyError, ValueError):
            continue
        if any(np.isnan([r_tsm, r_sox, r_fx])):
            continue
        loc = tw_px.index.get_indexer([ts_d], method="pad")[0]
        if loc <= 0:
            continue
        r_tw = float(tw_px.iloc[loc] / tw_px.iloc[loc - 1] - 1.0)
        if np.isnan(r_tw):
            continue
        rows.append(
            {
                "date": ts_d.date(),
                "r_tsm_us": r_tsm,
                "r_sox_us": r_sox,
                "r_twd": r_fx,
                "r_2330": r_tw,
                "close_2330": float(tw_px.iloc[loc]),
            }
        )

    panel = pd.DataFrame(rows).dropna()
    panel = (
        panel.drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    return close_df, panel


def run_backtest_with_signal(
    exec_df: pd.DataFrame, init_capital: float = 1_000_000.0
) -> pd.DataFrame:
    pred = exec_df["pred"].values.astype(float)
    signal = exec_df["signal"].values.astype(int)
    close = exec_df["close_2330"].values.astype(float)
    r = exec_df["r_2330"].values.astype(float)

    # Event-driven aligned simulation: close-price execution, integer lots, TW fee/tax.
    cash = float(init_capital)
    hold_lots = 0
    equity_series = []
    pos_series = []
    strat_r = []

    def max_buyable_lots(balance: float, price: float) -> int:
        lots = int(balance // (price * Units.LOT))
        while lots > 0:
            comm = StockUtils.calculate_transaction_commission(price=price, volume=lots)
            need = lots * price * Units.LOT + comm
            if need <= balance:
                return lots
            lots -= 1
        return 0

    prev_equity = init_capital
    for i in range(len(exec_df)):
        px = close[i]
        sig = signal[i]

        if sig == 1 and hold_lots == 0:
            buy_lots = max_buyable_lots(cash, px)
            if buy_lots > 0:
                buy_comm = StockUtils.calculate_transaction_commission(
                    price=px, volume=buy_lots
                )
                cash -= buy_lots * px * Units.LOT + buy_comm
                hold_lots = buy_lots
        elif sig == 0 and hold_lots > 0:
            sell_comm = StockUtils.calculate_transaction_commission(
                price=px, volume=hold_lots
            )
            sell_tax = StockUtils.calculate_transaction_tax(price=px, volume=hold_lots)
            cash += hold_lots * px * Units.LOT - (sell_comm + sell_tax)
            hold_lots = 0

        equity = cash + hold_lots * px * Units.LOT
        equity_series.append(equity)
        pos_series.append(1.0 if hold_lots > 0 else 0.0)
        day_r = equity / prev_equity - 1.0 if prev_equity > 0 else 0.0
        strat_r.append(day_r)
        prev_equity = equity

    equity_s = np.array(equity_series) / init_capital
    bh_equity = (1.0 + r).cumprod()
    dd_s = equity_s / np.maximum.accumulate(equity_s) - 1.0
    dd_bh = bh_equity / np.maximum.accumulate(bh_equity) - 1.0

    out = exec_df[["date"]].copy()
    out["pred"] = pred
    out["position"] = np.array(pos_series)
    out["r_strategy"] = np.array(strat_r)
    out["r_buyhold"] = r
    out["equity_strategy"] = equity_s
    out["equity_buyhold"] = bh_equity
    out["dd_strategy"] = dd_s
    out["dd_buyhold"] = dd_bh
    return out


def run_vectorized_continuous_backtest(exec_df: pd.DataFrame) -> pd.DataFrame:
    """
    Idealized daily backtest: full-notional long/flat, same-day signal times close-to-close
    return, with fees charged only when the discrete position toggles.

    This path intentionally abstracts lot size, partial fills, and cash drag. It is closer
    to a research notebook vectorization and can materially overstate implementable PnL
    versus the lot-based simulation in run_backtest_with_signal (see report reconciliation).
    """
    pred = exec_df["pred"].values.astype(float)
    signal = exec_df["signal"].values.astype(int)
    r = exec_df["r_2330"].values.astype(float)

    wealth = 1.0
    prev_sig = 0
    equity_series: list[float] = []
    strat_r: list[float] = []
    pos_series: list[float] = []
    prev_w = 1.0

    for i in range(len(exec_df)):
        sig = int(signal[i])
        if sig == 1 and prev_sig == 0:
            wealth *= 1.0 - FEE_BUY
        elif sig == 0 and prev_sig == 1:
            wealth *= 1.0 - FEE_SELL_PLUS_TAX
        wealth *= 1.0 + float(sig) * r[i]
        day_r = wealth / prev_w - 1.0 if prev_w > 0 else 0.0
        strat_r.append(day_r)
        equity_series.append(wealth)
        pos_series.append(1.0 if sig == 1 else 0.0)
        prev_w = wealth
        prev_sig = sig

    r_arr = r
    bh_equity = (1.0 + r_arr).cumprod()
    eq = np.array(equity_series, dtype=float)
    dd_s = eq / np.maximum.accumulate(eq) - 1.0
    dd_bh = bh_equity / np.maximum.accumulate(bh_equity) - 1.0

    out = exec_df[["date"]].copy()
    out["pred"] = pred
    out["position"] = np.array(pos_series)
    out["r_strategy"] = np.array(strat_r, dtype=float)
    out["r_buyhold"] = r_arr
    out["equity_strategy"] = eq
    out["equity_buyhold"] = bh_equity
    out["dd_strategy"] = dd_s
    out["dd_buyhold"] = dd_bh
    return out


def information_coefficient(y: np.ndarray, y_hat: np.ndarray) -> float:
    if len(y) < 3:
        return float("nan")
    return float(np.corrcoef(y, y_hat)[0, 1])


def annualized_sharpe(daily_r: np.ndarray, rf_daily: float = 0.02 / 252) -> float:
    x = daily_r - rf_daily
    s = float(np.std(x, ddof=1))
    if s < 1e-12:
        return float("nan")
    return float(np.sqrt(252) * np.mean(x) / s)


def max_drawdown_pct(dd: np.ndarray) -> float:
    return float(np.min(dd) * 100.0)


def save_fig(fig: go.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # scale≈3 improves PNG sharpness for Word/PDF (~print-friendly dpi band)
    fig.write_image(str(path), scale=3)
    fig.write_html(str(path.with_suffix(".html")))


def plot_equity_curve(
    bt: pd.DataFrame, out_dir: Path, title_suffix: str, basename: str = "equity_curve"
) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=bt["equity_strategy"],
            name="Overnight Signal Strategy",
            line=dict(color="#2563eb", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=bt["equity_buyhold"],
            name="Buy & Hold 2330.TW",
            line=dict(color="#94a3b8", width=2, dash="dash"),
        )
    )
    fig.update_layout(
        title=dict(text=f"Equity curve · {title_suffix}", x=0.02, xanchor="left"),
        xaxis_title="Date",
        yaxis_title="Equity (Start = 1)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="center", x=0.5),
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / f"{basename}.png")


def plot_mdd(
    bt: pd.DataFrame, out_dir: Path, title_suffix: str, basename: str = "mdd_underwater"
) -> None:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=bt["dd_strategy"] * 100.0,
            name="Strategy MDD (%)",
            fill="tozeroy",
            line=dict(color="#dc2626"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=bt["dd_buyhold"] * 100.0,
            name="2330 B&H MDD (%)",
            line=dict(color="#cbd5e1", dash="dot"),
        )
    )
    fig.update_layout(
        title=dict(text=f"Drawdown · {title_suffix}", x=0.02, xanchor="left"),
        xaxis_title="Date",
        yaxis_title="Drawdown (%)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="center", x=0.5),
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / f"{basename}.png")


def plot_rolling_sharpe(
    bt: pd.DataFrame, out_dir: Path, window: int = 63, basename: str = "rolling_sharpe"
) -> None:
    rs = bt["r_strategy"].astype(float)
    rb = bt["r_buyhold"].astype(float)

    def roll_sharpe(r: pd.Series) -> pd.Series:
        m = r.rolling(window, min_periods=max(20, window // 3)).mean()
        s = r.rolling(window, min_periods=max(20, window // 3)).std(ddof=1)
        return np.sqrt(252) * m / s

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=roll_sharpe(rs),
            name=f"Strategy Rolling Sharpe ({window}d)",
            line=dict(color="#2563eb"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=bt["date"],
            y=roll_sharpe(rb),
            name=f"B&H Rolling Sharpe ({window}d)",
            line=dict(color="#94a3b8", dash="dash"),
        )
    )
    fig.add_hline(y=0, line_dash="dot", line_color="#ccc")
    fig.update_layout(
        title=dict(text="Rolling Sharpe (annualized)", x=0.02, xanchor="left"),
        xaxis_title="Date",
        yaxis_title="Sharpe",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="center", x=0.5),
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / f"{basename}.png")


def plot_rolling_ic(
    panel_with_pred: pd.DataFrame,
    out_dir: Path,
    window: int = 126,
    basename: str = "rolling_ic",
) -> None:
    """Rolling information coefficient to monitor potential alpha decay."""
    df = panel_with_pred.sort_values("date").reset_index(drop=True)
    ic_roll = (
        df["pred"].rolling(window, min_periods=max(30, window // 4)).corr(df["r_2330"])
    )
    fig = go.Figure(
        go.Scatter(
            x=df["date"], y=ic_roll, name="Rolling IC", line=dict(color="#0d9488")
        )
    )
    fig.add_hline(y=0, line_dash="dot", line_color="#ccc")
    fig.update_layout(
        title=dict(text=f"Rolling IC ({window}d)", x=0.02, xanchor="left"),
        xaxis_title="Date",
        yaxis_title="IC",
        template="plotly_white",
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / f"{basename}.png")


def plot_ic_by_year(panel_with_pred: pd.DataFrame, out_dir: Path) -> None:
    """Yearly IC for signal effectiveness by year."""
    df = panel_with_pred.copy()
    df["year"] = pd.to_datetime(df["date"]).dt.year
    ics = []
    for y, g in df.groupby("year"):
        ics.append(
            {
                "year": y,
                "IC": information_coefficient(g["r_2330"].values, g["pred"].values),
            }
        )
    ic_df = pd.DataFrame(ics)
    fig = go.Figure(data=go.Bar(x=ic_df["year"], y=ic_df["IC"], marker_color="#7c3aed"))
    fig.update_layout(
        title=dict(text="IC by year (Pearson)", x=0.02, xanchor="left"),
        xaxis_title="Year",
        yaxis_title="IC",
        template="plotly_white",
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / "ic_by_year.png")
    ic_df.to_csv(out_dir / "ic_by_year.csv", index=False)


def plot_monthly_returns_heatmap(
    bt: pd.DataFrame, out_dir: Path, basename: str = "monthly_returns_heatmap"
) -> None:
    d = pd.to_datetime(bt["date"])
    mret = (
        bt.assign(ym=d.dt.to_period("M"))
        .groupby("ym")["r_strategy"]
        .apply(lambda s: (1 + s).prod() - 1)
    )
    pivot = mret.reset_index()
    pivot["year"] = pivot["ym"].dt.year
    pivot["month"] = pivot["ym"].dt.month
    mat = pivot.pivot(index="year", columns="month", values="r_strategy") * 100.0
    fig = go.Figure(
        data=go.Heatmap(
            z=mat.values,
            x=[f"M{m}" for m in mat.columns],
            y=mat.index.astype(str),
            colorscale="RdYlGn",
            zmid=0,
            colorbar=dict(title="%"),
        )
    )
    fig.update_layout(
        title=dict(text="Monthly return heatmap (%)", x=0.02, xanchor="left"),
        template="plotly_white",
        font=dict(size=13),
    )
    _apply_figure_margins(fig)
    save_fig(fig, out_dir / f"{basename}.png")


def ic_t_stat(ic: float, n: int) -> float:
    """t-statistic for Pearson correlation IC under approximate normality (diagnostic only)."""
    if n < 3 or np.isnan(ic) or abs(ic) >= 1.0:
        return float("nan")
    return float(ic * np.sqrt(n - 2) / np.sqrt(max(1e-12, 1.0 - ic * ic)))


def yearly_diagnostics(bt: pd.DataFrame, exec_for_ic: pd.DataFrame) -> pd.DataFrame:
    """Per-calendar-year strategy vs buy-hold, position switches, within-year strategy MDD, and IC."""
    # bt already carries pred; merge only realized r for IC (avoid duplicate pred_* columns).
    d = bt.merge(exec_for_ic[["date", "r_2330"]], on="date", how="left")
    d["year"] = pd.to_datetime(d["date"]).dt.year
    d["pos_prev"] = d["position"].shift(1).fillna(0.0)
    # Half a "switch" per day the discrete position flips 0<->1 (sum ≈ round-trip count proxy).
    d["pos_switch_half"] = (d["position"] != d["pos_prev"]).astype(float) * 0.5

    rows = []
    for y, g in d.groupby("year"):
        rs = g["r_strategy"].astype(float).values
        rb = g["r_buyhold"].astype(float).values
        strat_ret = float(np.prod(1.0 + rs) - 1.0)
        bh_ret = float(np.prod(1.0 + rb) - 1.0)
        dd_y = g["dd_strategy"].astype(float).values
        mdd_y = float(np.min(dd_y) * 100.0) if len(dd_y) else float("nan")
        switches = float(g["pos_switch_half"].sum())
        ic_val = information_coefficient(
            g["r_2330"].values.astype(float), g["pred"].values.astype(float)
        )
        rows.append(
            {
                "year": int(y),
                "IC": ic_val,
                "strategy_cum_return": strat_ret,
                "benchmark_cum_return": bh_ret,
                "approx_round_trips": switches,
                "strategy_mdd_pct": mdd_y,
            }
        )
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True)


def ic_pnl_gap_metrics(exec_df: pd.DataFrame, bt: pd.DataFrame) -> pd.DataFrame:
    """Explain tension between ranking IC and realistic PnL (threshold, costs, cash gaps)."""
    m = exec_df.merge(
        bt[["date", "position", "r_strategy", "r_buyhold"]], on="date", how="inner"
    )
    r = m["r_2330"].astype(float).values
    pred = m["pred"].astype(float).values
    sig = m["signal"].astype(int).values
    pos = m["position"].astype(float).values
    wanted_long = sig == 1
    held_long = pos > 0.5
    gap_long = wanted_long & ~held_long
    gap_arr = np.asarray(gap_long, dtype=bool)
    bh = m["r_buyhold"].astype(float).values

    def safe_mean(mask: np.ndarray) -> float:
        mask = np.asarray(mask, dtype=bool)
        if not mask.any():
            return float("nan")
        return float(np.mean(r[mask]))

    rows = [
        ("mean_market_ret_when_pred_positive", safe_mean(pred > 0)),
        ("mean_market_ret_when_pred_nonpositive", safe_mean(pred <= 0)),
        ("mean_market_ret_when_holding_long", safe_mean(held_long)),
        ("mean_market_ret_when_flat", safe_mean(~held_long)),
        ("share_days_pred_positive", float(np.mean(pred > 0))),
        ("share_days_signal_long_but_flat_cash_gap", float(np.mean(gap_arr))),
        ("count_days_signal_long_but_flat", float(np.sum(gap_arr))),
        (
            "mean_benchmark_ret_on_cash_gap_days",
            float(np.mean(bh[gap_arr])) if gap_arr.any() else float("nan"),
        ),
    ]

    mdt = pd.to_datetime(m["date"])
    y2026 = mdt.dt.year == 2026
    if y2026.any():
        sub = m.loc[y2026]
        ps = sub["position"].astype(float).values
        chg = np.sum(np.abs(np.diff(np.r_[ps[0], ps])) > 0.5)
        rows.extend(
            [
                ("y2026_days_in_sample", float(len(sub))),
                (
                    "y2026_days_pred_positive",
                    float(np.sum(sub["pred"].astype(float).values > 0)),
                ),
                ("y2026_days_held_long", float(np.sum(ps > 0.5))),
                ("y2026_position_change_events", float(chg)),
            ]
        )

    return pd.DataFrame(rows, columns=["metric", "value"])


def placebo_feature_shift_ic(
    panel_test: pd.DataFrame, coef: np.ndarray, x_cols: List[str]
) -> pd.DataFrame:
    """
    If IC is driven by incorrect calendar alignment, mis-shifted features should destroy correlation.
    Lag 1 TW row: pair today's TW return with previous row's US features (stale).
    Lead 1 TW row: pair with next row's US features (misaligned).
    """
    pt = panel_test.sort_values("date").reset_index(drop=True)
    y = pt["r_2330"].astype(float).values
    X = pt[x_cols].astype(float).values
    n = len(y)
    pred0 = np.c_[np.ones(n), X] @ coef
    ic0 = information_coefficient(y, pred0)

    Xlag = pt[x_cols].shift(1).astype(float).values
    m1 = np.all(np.isfinite(Xlag), axis=1) & np.isfinite(y)
    ic_lag = information_coefficient(y[m1], (np.c_[np.ones(m1.sum()), Xlag[m1]] @ coef))

    Xlead = pt[x_cols].shift(-1).astype(float).values
    m2 = np.all(np.isfinite(Xlead), axis=1) & np.isfinite(y)
    ic_lead = information_coefficient(
        y[m2], (np.c_[np.ones(m2.sum()), Xlead[m2]] @ coef)
    )

    return pd.DataFrame(
        {
            "variant": [
                "baseline_correct_alignment",
                "placebo_US_features_lagged_1_TW_row",
                "placebo_US_features_led_1_TW_row",
            ],
            "IC_pearson": [ic0, ic_lag, ic_lead],
        }
    )


def capital_sensitivity_analysis(
    exec_df: pd.DataFrame, caps: List[float]
) -> pd.DataFrame:
    """Realistic cumulative return vs initial cash (same signals); exposes lot-capital interaction."""
    rows = []
    for cap in caps:
        bt = run_backtest_with_signal(exec_df, init_capital=float(cap))
        sm = summary_metrics(bt)
        rows.append(
            {
                "initial_capital_twd": float(cap),
                "realistic_cum_return": float(sm["策略累積報酬"]),
                "realistic_sharpe": float(sm["策略Sharpe"]),
                "realistic_mdd_pct": float(sm["策略最大回撤%"]),
            }
        )
    return pd.DataFrame(rows)


def ridge_threshold_sweep(
    exec_skel: pd.DataFrame,
    dates_t: np.ndarray,
    pred_vec: np.ndarray,
    thresholds: List[float],
) -> pd.DataFrame:
    """Realistic PnL vs forecast threshold (same Ridge scores); tau in decimal return units."""
    rows = []
    for tau in thresholds:
        sig = (pred_vec > tau).astype(int)
        ex = _exec_with_pred_signal(exec_skel, dates_t, pred_vec, sig)
        mr = summary_metrics(run_backtest_with_signal(ex, init_capital=1_000_000.0))
        rows.append(
            {
                "threshold_tau": tau,
                "threshold_label": f"{tau:.6f}",
                "realistic_cum_return": float(mr["策略累積報酬"]),
                "realistic_sharpe": float(mr["策略Sharpe"]),
                "realistic_mdd_pct": float(mr["策略最大回撤%"]),
            }
        )
    return pd.DataFrame(rows)


def write_sanity_checks_csv(
    out: Path,
    ic_test: float,
    n_test: int,
    gap_share: float,
    placebo_ic_baseline: Optional[float] = None,
    placebo_ic_lag: Optional[float] = None,
    placebo_ic_lead: Optional[float] = None,
) -> None:
    """Structured sanity checks with explicit status (not only risks)."""
    placebo_txt = ""
    if (
        placebo_ic_baseline is not None
        and placebo_ic_lag is not None
        and placebo_ic_lead is not None
    ):
        placebo_txt = (
            f"Baseline IC {placebo_ic_baseline:.4f}; lag-1 feature placebo {placebo_ic_lag:.4f}; "
            f"lead-1 feature placebo {placebo_ic_lead:.4f}."
        )
    rows = [
        {
            "check": "Look-ahead (features vs TW date)",
            "status": "Pass (by construction)",
            "result": "Each TW row uses the last completed U.S. session strictly before the TW calendar date.",
            "remaining_risk": "Manually audit several matched U.S.–Taiwan calendar dates in future work.",
        },
        {
            "check": "Test-set IC magnitude (sanity)",
            "status": "Computed — unusually high for daily equity",
            "result": f"Pearson IC ≈ {ic_test:.4f} over n={n_test} days.",
            "remaining_risk": "High IC can reflect alignment artifacts; cross-check with placebo shifts below and raw calendars.",
        },
        {
            "check": "Alignment placebo (±1 TW-row feature shift)",
            "status": "Computed" if placebo_txt else "Pending",
            "result": placebo_txt or "See placebo_ic_alignment.csv after pipeline run.",
            "remaining_risk": "If placebo ICs do not collapse vs baseline, revisit merge logic and holidays.",
        },
        {
            "check": "Cash vs desired long (execution gap)",
            "status": "Measured",
            "result": f"Share of days with signal long but flat ≈ {gap_share:.2%}.",
            "remaining_risk": "Integer lots + commission can block full-notional replication.",
        },
        {
            "check": "Survivorship (single-name TSMC)",
            "status": "Acknowledged",
            "result": "Single liquid mega-cap; no universe screening bias in the narrow sense.",
            "remaining_risk": "Results may not transfer to smaller names or crisis liquidity regimes.",
        },
        {
            "check": "Adjusted prices (yfinance auto_adjust)",
            "status": "Acknowledged",
            "result": "Adjusted series reinvest dividends into prices; daily returns are comparable for prediction but differ from cash-account cash flows.",
            "remaining_risk": "Trade-level PnL can be biased vs adjusted closes (typically smoother long-run paths); prefer raw tradable prints plus explicit dividends for ticket-level simulation.",
        },
    ]
    pd.DataFrame(rows).to_csv(out / "sanity_checks.csv", index=False)


def summary_metrics(bt: pd.DataFrame) -> pd.Series:
    rs = bt["r_strategy"].values.astype(float)
    rb = bt["r_buyhold"].values.astype(float)
    years = max((bt["date"].max() - bt["date"].min()).days / 365.25, 0.25)
    total_s = float(bt["equity_strategy"].iloc[-1] - 1.0)
    total_b = float(bt["equity_buyhold"].iloc[-1] - 1.0)
    ann_s = (1 + total_s) ** (1 / years) - 1 if years > 0 else np.nan
    ann_b = (1 + total_b) ** (1 / years) - 1 if years > 0 else np.nan
    vol_s = float(np.std(rs, ddof=1) * np.sqrt(252))
    calmar = ann_s / abs(max_drawdown_pct(bt["dd_strategy"].values) / 100.0 + 1e-9)
    win = (
        float(np.mean(rs[bt["position"].values > 0] > 0))
        if (bt["position"] > 0).any()
        else np.nan
    )
    return pd.Series(
        {
            "區間天數": len(bt),
            "策略累積報酬": total_s,
            "B&H累積報酬": total_b,
            "策略年化報酬": ann_s,
            "B&H年化報酬": ann_b,
            "策略年化波動": vol_s,
            "策略Sharpe": annualized_sharpe(rs),
            "B&H_Sharpe": annualized_sharpe(rb),
            "策略最大回撤%": max_drawdown_pct(bt["dd_strategy"].values),
            "B&H最大回撤%": max_drawdown_pct(bt["dd_buyhold"].values),
            "卡瑪比率(估)": calmar,
            "做多日勝率": win,
        }
    )


def main(
    output_dir: Optional[Path] = None,
    data_start: dt.date = dt.date(2020, 1, 1),
    data_end: Optional[dt.date] = None,
) -> Path:
    data_end = data_end or dt.date.today()
    out = Path(output_dir or _DEFAULT_OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)

    close_df, panel = fetch_panel(data_start, data_end)
    panel = panel[panel["date"] >= data_start].reset_index(drop=True)

    x_cols = ["r_tsm_us", "r_sox_us", "r_twd"]
    train = panel[(panel["date"] <= TRAIN_END)].copy()
    val = panel[(panel["date"] >= VAL_START) & (panel["date"] <= VAL_END)].copy()
    test = panel[panel["date"] >= TEST_START].copy()

    X_tr, y_tr = (
        train[x_cols].values.astype(float),
        train["r_2330"].values.astype(float),
    )
    X_va, y_va = val[x_cols].values.astype(float), val["r_2330"].values.astype(float)
    alpha = tune_alpha(X_tr, y_tr, X_va, y_va, np.logspace(-4, 3, 30))

    fit_mask = panel["date"] <= VAL_END
    X_fit = panel.loc[fit_mask, x_cols].values.astype(float)
    y_fit = panel.loc[fit_mask, "r_2330"].values.astype(float)
    coef, _ = ridge_fit_predict(X_fit, y_fit, X_fit, alpha)

    panel_test = test.reset_index(drop=True)
    Xt = panel_test[x_cols].values.astype(float)
    _, pred_test = ridge_fit_predict(X_fit, y_fit, Xt, alpha)
    signal_df = panel_test[["date", "r_2330", "close_2330"]].copy()
    signal_df["pred"] = pred_test
    signal_df["signal"] = (signal_df["pred"] > 0.0).astype(int)

    _, pred_ols = _ols_fit_predict(X_fit, y_fit, Xt)
    sig_ols = (pred_ols > 0.0).astype(int)

    y_up_fit = (y_fit > 0.0).astype(float)
    X1_fit = np.c_[np.ones(len(X_fit)), X_fit]
    beta_log = _logistic_irls_fit(X1_fit, y_up_fit)
    X1_t = np.c_[np.ones(len(Xt)), Xt]
    eta_log = np.clip(X1_t @ beta_log, -35.0, 35.0)
    pred_logit_score = eta_log.astype(float)
    sig_log = ((1.0 / (1.0 + np.exp(-eta_log))) >= 0.5).astype(int)

    r_tsm_t = panel_test["r_tsm_us"].values.astype(float)
    pred_tsm_only = r_tsm_t.copy()
    sig_tsm = (r_tsm_t > 0.0).astype(int)

    # Align with event-driven loop: evaluate every TW trading day.
    tw_px = close_df["2330.TW"].dropna().sort_index()
    tw_ret = tw_px.pct_change()
    exec_skel = pd.DataFrame(
        {
            "date": tw_px.index.date,
            "close_2330": tw_px.values,
            "r_2330": tw_ret.values,
        }
    )
    exec_skel = exec_skel[
        (exec_skel["date"] >= TEST_START) & (exec_skel["date"] <= data_end)
    ]
    exec_skel = exec_skel.dropna(subset=["r_2330", "close_2330"]).copy()

    dates_t = panel_test["date"].values
    baseline_rows: List[Dict[str, float]] = [
        _evaluate_model_on_exec(
            "Ridge", exec_skel, dates_t, pred_test, (pred_test > 0.0).astype(int)
        ),
        _evaluate_model_on_exec(
            "OLS", exec_skel, dates_t, pred_ols.astype(float), sig_ols
        ),
        _evaluate_model_on_exec(
            "Logistic (direction)", exec_skel, dates_t, pred_logit_score, sig_log
        ),
        _evaluate_model_on_exec(
            "TSM sign only", exec_skel, dates_t, pred_tsm_only, sig_tsm
        ),
    ]
    pd.DataFrame(baseline_rows).to_csv(out / "baseline_comparison.csv", index=False)

    exec_df = exec_skel.merge(
        signal_df[["date", "pred", "signal"]],
        on="date",
        how="left",
    )
    exec_df["pred"] = exec_df["pred"].fillna(0.0)
    exec_df["signal"] = exec_df["signal"].fillna(0).astype(int)

    bt_real = run_backtest_with_signal(exec_df)
    bt_vec = run_vectorized_continuous_backtest(exec_df)

    # metrics_summary.csv = lot-based / realistic execution (primary for conclusions)
    summary_metrics(bt_real).to_csv(out / "metrics_summary.csv", header=["value"])
    summary_metrics(bt_vec).to_csv(
        out / "metrics_vectorized_summary.csv", header=["value"]
    )

    coef_names = ["intercept", "r_tsm_us", "r_sox_us", "r_twd"]
    pd.DataFrame({"feature": coef_names, "coefficient": coef.astype(float)}).to_csv(
        out / "ridge_coefficients.csv", index=False
    )

    ic_panel = exec_df[["date", "r_2330", "pred"]].copy()
    ic_test = information_coefficient(
        ic_panel["r_2330"].values.astype(float), ic_panel["pred"].values.astype(float)
    )
    n_test = len(ic_panel)
    pd.DataFrame(
        {
            "metric": ["test_IC_pearson", "test_IC_t_stat_approx", "test_n_days"],
            "value": [ic_test, ic_t_stat(ic_test, n_test), float(n_test)],
        }
    ).to_csv(out / "signal_ic_test.csv", index=False)

    yearly_diagnostics(bt_real, exec_df).to_csv(
        out / "yearly_diagnostics.csv", index=False
    )

    gap_tbl = ic_pnl_gap_metrics(exec_df, bt_real)
    gap_tbl.to_csv(out / "ic_pnl_gap.csv", index=False)
    gap_share_row = gap_tbl.loc[
        gap_tbl["metric"] == "share_days_signal_long_but_flat_cash_gap", "value"
    ]
    gap_share = float(gap_share_row.iloc[0]) if len(gap_share_row) else 0.0

    placebo_df = placebo_feature_shift_ic(panel_test, coef, x_cols)
    placebo_df.to_csv(out / "placebo_ic_alignment.csv", index=False)
    write_sanity_checks_csv(
        out,
        ic_test,
        n_test,
        gap_share,
        float(placebo_df["IC_pearson"].iloc[0]),
        float(placebo_df["IC_pearson"].iloc[1]),
        float(placebo_df["IC_pearson"].iloc[2]),
    )
    capital_sensitivity_analysis(
        exec_df, [1_000_000.0, 3_000_000.0, 5_000_000.0, 10_000_000.0]
    ).to_csv(out / "capital_sensitivity.csv", index=False)

    # Threshold robustness (predicted daily return must exceed tau to go long); tau in decimal return units.
    tau_list = [0.0, 0.0005, 0.001, 0.002]
    ridge_threshold_sweep(exec_skel, dates_t, pred_test, tau_list).to_csv(
        out / "threshold_robustness_ridge.csv", index=False
    )

    sm_bh = summary_metrics(bt_real)
    pd.DataFrame(
        [
            {
                "benchmark": "2330.TW buy-and-hold (test window)",
                "test_cumulative_return": float(sm_bh["B&H累積報酬"]),
            },
            {
                "benchmark": "Cash (flat, zero daily return)",
                "test_cumulative_return": 0.0,
            },
        ]
    ).to_csv(out / "passive_benchmarks.csv", index=False)

    last_dt = bt_real["date"].max()
    pd.Series(
        {
            "ridge_alpha": alpha,
            "data_start": str(data_start),
            "data_end_requested": str(data_end),
            "train_end": str(TRAIN_END),
            "val": f"{VAL_START}~{VAL_END}",
            "test_start": str(TEST_START),
            "test_end_effective_last_tw_close": str(last_dt),
            "fee_buy": FEE_BUY,
            "fee_sell_plus_tax": FEE_SELL_PLUS_TAX,
            "initial_capital_twd": 1_000_000.0,
            "shares_per_lot_tw_common_stock": float(Units.LOT),
            "odd_lot_modeled": "No (integer lots only)",
        }
    ).to_csv(out / "run_meta.csv", header=["value"])

    title_suffix = f"{TEST_START}–{last_dt}"
    plot_equity_curve(bt_real, out, title_suffix, basename="equity_curve_realistic")
    plot_mdd(bt_real, out, title_suffix, basename="mdd_underwater_realistic")
    plot_rolling_sharpe(bt_real, out, basename="rolling_sharpe_realistic")
    plot_ic_by_year(ic_panel, out)
    plot_rolling_ic(ic_panel, out, basename="rolling_ic")
    plot_monthly_returns_heatmap(
        bt_real, out, basename="monthly_returns_heatmap_realistic"
    )

    plot_equity_curve(
        bt_vec, out, title_suffix + " · vec (diag)", basename="equity_curve_vectorized"
    )
    plot_mdd(
        bt_vec,
        out,
        title_suffix + " · vec (diag)",
        basename="mdd_underwater_vectorized",
    )
    plot_monthly_returns_heatmap(
        bt_vec, out, basename="monthly_returns_heatmap_vectorized"
    )

    bt_real.to_csv(out / "backtest_daily.csv", index=False)
    bt_vec.to_csv(out / "backtest_vectorized_daily.csv", index=False)
    return out


if __name__ == "__main__":
    p = main()
    print(f"Done. Output: {p}")
