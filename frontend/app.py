from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd
import plotly.express as px
import streamlit as st

try:
    from config import RESULTS_ROOT
    from services.futures_metrics import (
        DATE_COLUMN,
        LOTS_COLUMN,
        MARGIN_COLUMN,
        build_exposure_series,
        is_futures_report,
        summarise_margin,
    )
    from services.metrics import (
        calc_sharpe_ratio,
        calc_sortino_ratio,
        extract_backtest_date_range,
    )
    from services.report_loader import (
        BacktestReport,
        build_equity_series,
        compute_daily_pnl,
        compute_daily_returns,
        compute_max_drawdown,
        extract_starting_capital,
        list_strategy_dirs,
        load_backtest_report,
        read_daily_equity,
        read_direction_summary,
        read_event_report,
        read_trading_report,
        sort_by_exit_date,
        summarise_overview,
        to_numeric,
    )
except ModuleNotFoundError:
    from frontend.config import RESULTS_ROOT
    from frontend.services.futures_metrics import (
        DATE_COLUMN,
        LOTS_COLUMN,
        MARGIN_COLUMN,
        build_exposure_series,
        is_futures_report,
        summarise_margin,
    )
    from frontend.services.metrics import (
        calc_sharpe_ratio,
        calc_sortino_ratio,
        extract_backtest_date_range,
    )
    from frontend.services.report_loader import (
        BacktestReport,
        build_equity_series,
        compute_daily_pnl,
        compute_daily_returns,
        compute_max_drawdown,
        extract_starting_capital,
        list_strategy_dirs,
        load_backtest_report,
        read_daily_equity,
        read_direction_summary,
        read_event_report,
        read_trading_report,
        sort_by_exit_date,
        summarise_overview,
        to_numeric,
    )


st.set_page_config(page_title="AlphaEdge Backtest Viewer", layout="wide")
_THEME_CSS_PATH = Path(__file__).resolve().parent / "static" / "theme.css"
if _THEME_CSS_PATH.exists():
    st.markdown(
        f"<style>{_THEME_CSS_PATH.read_text(encoding='utf-8')}</style>",
        unsafe_allow_html=True,
    )
st.title("Backtest Report")


def _render_info_card(label: str, value: str) -> None:
    st.markdown(
        f"""
        <div class="ae-info-card">
            <div class="ae-info-label">{label}</div>
            <div class="ae-info-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_strategy_overview(report: BacktestReport, df: pd.DataFrame) -> None:
    starting_capital = extract_starting_capital(df)
    start_date, end_date = extract_backtest_date_range(df)

    if start_date is not None and end_date is not None:
        date_range = f"{start_date.date()} ~ {end_date.date()}"
    else:
        date_range = "N/A"

    if starting_capital is None:
        starting_capital_text = "N/A"
    else:
        starting_capital_text = f"{starting_capital:,.2f}"

    c1, c2, c3 = st.columns(3)
    with c1:
        _render_info_card("策略名稱", report.strategy_name)
    with c2:
        _render_info_card("策略起始資金", starting_capital_text)
    with c3:
        _render_info_card("回測日期區間", date_range)


def _render_metrics(
    df: pd.DataFrame,
    direction_summary: pd.DataFrame,
    equity: pd.Series,
) -> None:
    """
    關鍵指標**一律取自 reporter 落地的 CSV**

    總覽四個數字來自 `direction_summary.csv`；權益相關（MDD、Sharpe、Sortino）
    來自 `daily_equity.csv`。前端不再自行重算任何一條公式。
    """

    overview = summarise_overview(df, direction_summary)
    cumulative_balance = to_numeric(df, "Cumulative Balance")

    daily_returns = compute_daily_returns(equity)
    sharpe_ratio = calc_sharpe_ratio(daily_returns)
    sortino_ratio = calc_sortino_ratio(daily_returns)
    max_drawdown = compute_max_drawdown(equity)

    def _fmt_ratio(value: float) -> str:
        return f"{value:.3f}" if pd.notna(value) else "N/A"

    def _fmt_number(value: float | None, suffix: str = "") -> str:
        return "N/A" if value is None else f"{value:,.2f}{suffix}"

    st.markdown("##### 交易概況")
    r1c1, r1c2, r1c3 = st.columns(3)
    r1c1.metric("總交易數", overview["trade_count"])
    r1c2.metric("勝率", _fmt_number(overview["win_rate"], "%"))
    r1c3.metric(
        "獲利筆數 / 虧損筆數",
        f"{overview['win_count']} / {overview['loss_count']}",
    )

    st.divider()

    st.markdown("##### 損益與資產")
    r2c1, r2c2, r2c3 = st.columns(3)
    r2c1.metric("總已實現損益", _fmt_number(overview["total_pnl"]))
    # `ROI` 欄本來就是百分比，**不再乘 100**
    r2c2.metric("平均 ROI", _fmt_number(overview["avg_roi"], "%"))
    r2c3.metric(
        "最後累積資產",
        (
            f"{float(cumulative_balance.iloc[-1]):,.2f}"
            if not cumulative_balance.empty
            else "N/A"
        ),
    )

    st.divider()

    st.markdown("##### 風險調整報酬")
    if equity.empty:
        st.info(
            "找不到 `daily_equity.csv`，本區塊無法計算。"
            "風險指標的樣本必須是**日報酬**——用每筆交易的報酬代替並乘 √252，"
            "等於宣稱「一年有 252 筆交易」，會得到一個看起來合理的錯數字。"
        )
    else:
        r3c1, r3c2, r3c3 = st.columns(3)
        r3c1.metric("Sharpe Ratio", _fmt_ratio(sharpe_ratio))
        r3c2.metric("Sortino Ratio", _fmt_ratio(sortino_ratio))
        r3c3.metric("最大回撤", _fmt_number(max_drawdown, "%"))
        st.caption(
            "以 `daily_equity.csv` 的**盯市權益**為樣本（含未實現損益）。"
            "Information Ratio 需要基準的日報酬序列，報表目前沒有輸出這一欄，故不顯示。"
        )


def _render_direction_summary(direction_summary: pd.DataFrame) -> None:
    """多空分開的績效統計——混在一起會看不出策略靠哪一邊賺錢"""

    st.markdown("##### 多空分開統計")
    if direction_summary.empty:
        st.info("找不到 `direction_summary.csv`。")
        return
    st.dataframe(direction_summary, width="stretch", hide_index=True)


def _render_event_report(event_report: pd.DataFrame) -> None:
    """
    尾部事件計數

    強制回補、斷頭、拒單這些是放空策略的尾部風險，**被平均進總績效就看不見了**，
    必須單獨列出。
    """

    st.markdown("##### 事件計數")
    if event_report.empty:
        st.info("找不到 `event_report.csv`。")
        return

    counts = event_report[to_numeric(event_report, "Count") > 0]
    if counts.empty:
        st.success("回測期間沒有任何強制回補、斷頭或拒單事件。")
        st.caption(f"（已檢視 {len(event_report)} 種事件，全部為 0）")
        return
    st.dataframe(counts, width="stretch", hide_index=True)
    st.caption(f"僅列出計數不為 0 者；報表共記錄 {len(event_report)} 種事件。")


def _render_futures_metrics(df: pd.DataFrame) -> None:
    """
    期貨專屬指標：**保證金佔用與口數曝險**

    股票看「投入多少錢、值多少錢」，期貨看「佔用多少保證金、留了幾口」——
    契約價值本身不佔用資金，用股票那組指標看期貨只會看到一堆與風險無關的數字。
    """

    starting_capital = extract_starting_capital(df)
    summary = summarise_margin(df, starting_capital)

    def _fmt(value, suffix: str = "", digits: int = 2) -> str:
        if value is None:
            return "N/A"
        if isinstance(value, int):
            return f"{value:,}{suffix}"
        return f"{value:,.{digits}f}{suffix}"

    st.markdown("##### 保證金與曝險（期貨專屬）")
    r1c1, r1c2, r1c3 = st.columns(3)
    # **峰值而不是總和**：把每筆的保證金加起來會得到一個沒有意義的巨大數字
    # （同一筆錢用了幾十次），真正該看的是「最多同時押了多少」
    r1c1.metric("峰值佔用保證金", _fmt(summary["峰值佔用保證金"]))
    r1c2.metric("峰值未平倉口數", _fmt(summary["峰值未平倉口數"]))
    r1c3.metric("資金使用率", _fmt(summary["資金使用率"], suffix="%"))

    r2c1, r2c2, r2c3 = st.columns(3)
    r2c1.metric("平均每筆保證金", _fmt(summary["平均每筆保證金"]))
    r2c2.metric("總交易口數", _fmt(summary["總交易口數"]))
    # 期貨的 ROI 分母是保證金，故這個平均值才是「資金效率」
    r2c3.metric("平均保證金報酬率", _fmt(summary["平均保證金報酬率"], suffix="%"))


def _render_futures_exposure_chart(df: pd.DataFrame) -> None:
    """保證金佔用與未平倉口數的時間序列（雙軸階梯圖）"""

    exposure = build_exposure_series(df)
    if exposure.empty:
        st.info("交易明細不足以推導曝險曲線。")
        return

    theme = _get_chart_theme()
    figure = px.line(
        exposure,
        x=DATE_COLUMN,
        y=MARGIN_COLUMN,
        markers=True,
        title="佔用保證金",
    )
    # **階梯線不是美觀選擇**：保證金在兩筆交易之間是不變的，用直線內插會讓
    # 圖上出現從未發生過的中間值
    figure.update_traces(line_shape="hv")
    figure.update_layout(paper_bgcolor=theme["paper_bg"], plot_bgcolor=theme["plot_bg"])
    st.plotly_chart(figure, width="stretch")

    lots_figure = px.line(
        exposure, x=DATE_COLUMN, y=LOTS_COLUMN, markers=True, title="未平倉口數"
    )
    lots_figure.update_traces(line_shape="hv")
    lots_figure.update_layout(
        paper_bgcolor=theme["paper_bg"], plot_bgcolor=theme["plot_bg"]
    )
    st.plotly_chart(lots_figure, width="stretch")


def _get_chart_theme() -> dict[str, str]:
    return {
        "paper_bg": "rgba(0,0,0,0)",
        "plot_bg": "rgba(0,0,0,0)",
        "grid": "var(--ae-border)",
        "text": "var(--ae-text)",
    }


def _render_interactive_charts(df: pd.DataFrame, equity: pd.Series) -> None:
    """
    資產曲線與每日損益一律走 `daily_equity.csv`

    舊版拿「依 `Sell Date` 排序的 `Cumulative Balance`」畫這兩張圖，有兩個問題：
    `Sell Date` 對 SHORT 是**開倉日**，曲線不是依平倉順序長出來的；而已實現
    損益的累積餘額只在平倉那天才有節點，持倉期間的逆勢被整段抹平——
    那正是留倉放空最大的風險來源。
    """

    chart_theme = _get_chart_theme()

    def _style(fig) -> None:
        fig.update_layout(
            paper_bgcolor=chart_theme["paper_bg"],
            plot_bgcolor=chart_theme["plot_bg"],
            font=dict(color=chart_theme["text"]),
            margin=dict(l=20, r=20, t=56, b=20),
            xaxis=dict(showgrid=True, gridcolor=chart_theme["grid"], gridwidth=1),
            yaxis=dict(showgrid=True, gridcolor=chart_theme["grid"], gridwidth=1),
        )

    if equity.empty:
        st.info(
            "找不到 `daily_equity.csv`，無法畫盯市口徑的資產曲線與每日損益。"
            "改以交易明細的累積餘額作圖會**低估最大回撤**——"
            "已實現損益只在平倉那天才有節點，持倉期間的逆勢看不見。"
        )
        return

    equity_df = pd.DataFrame({"Date": list(equity.index), "Equity": equity.to_numpy()})
    line_fig = px.line(equity_df, x="Date", y="Equity", title="資產曲線（盯市）")
    _style(line_fig)
    st.plotly_chart(line_fig, width="stretch")

    daily_pnl = compute_daily_pnl(equity)
    if not daily_pnl.empty:
        pnl_df = pd.DataFrame(
            {"Date": list(daily_pnl.index), "Daily PnL": daily_pnl.to_numpy()}
        )
        bar_fig = px.bar(pnl_df, x="Date", y="Daily PnL", title="每日損益（盯市）")
        _style(bar_fig)
        st.plotly_chart(bar_fig, width="stretch")


def _render_chart_images(chart_paths: Dict[str, Path | None]) -> None:
    for chart_name, chart_path in chart_paths.items():
        st.subheader(chart_name)
        if chart_path is None:
            st.warning(f"找不到 `{chart_name}` 圖檔。")
            continue
        st.image(str(chart_path), caption=chart_path.name, width="stretch")


strategy_dirs = list_strategy_dirs(RESULTS_ROOT)
if not strategy_dirs:
    st.error(
        f"找不到任何回測結果資料夾。請確認路徑 `{RESULTS_ROOT}` 是否存在，"
        "或設定環境變數 `ALPHAEDGE_RESULTS_DIR`。"
    )
    st.stop()

with st.sidebar:
    st.header("設定")
    selected = st.selectbox(
        "策略資料夾",
        options=strategy_dirs,
        format_func=lambda path: path.name,
        index=0,
    )
    st.caption(f"結果根目錄：`{RESULTS_ROOT}`")

report: BacktestReport = load_backtest_report(selected)

if report.csv_path is None:
    st.error(f"`{selected.name}` 中找不到 trading report CSV。")
    st.stop()

df = read_trading_report(report.csv_path)

# reporter 另外落地的三份 CSV：前端只讀不算，指標一律以這些為準
direction_summary = read_direction_summary(report.direction_summary_path)
event_report = read_event_report(report.event_report_path)
daily_equity = read_daily_equity(report.daily_equity_path)
equity = build_equity_series(daily_equity, extract_starting_capital(df))

overview_tab, detail_tab, chart_tab, image_tab, download_tab = st.tabs(
    ["總覽", "交易明細", "圖表", "圖片", "下載"]
)

with overview_tab:
    st.subheader("策略摘要")
    _render_strategy_overview(report, df)
    st.divider()
    st.subheader("關鍵指標")
    _render_metrics(df, direction_summary, equity)
    st.divider()
    _render_direction_summary(direction_summary)
    st.divider()
    _render_event_report(event_report)
    # 期貨報表才有保證金與口數欄位；以**欄位**判斷而不是策略名稱
    if is_futures_report(df):
        st.divider()
        _render_futures_metrics(df)
    st.caption(f"報表檔案：`{report.csv_path.name}`")

with detail_tab:
    st.subheader("交易報表")
    # 一律依**平倉日**排序：`Sell Date` 對 SHORT 是開倉日
    detail_df = sort_by_exit_date(df)
    # 期貨的識別欄是 Contract ID（`{商品}{到期月}`），股票是 Symbol。
    # **`Stock ID` 是舊欄名**，保留在候選裡是為了讓改名前產出的結果資料夾照樣打得開
    stock_col = next(
        (
            column
            for column in ("Symbol", "Contract ID", "Stock ID")
            if column in detail_df.columns
        ),
        None,
    )
    if stock_col:
        stock_ids = sorted(detail_df[stock_col].dropna().astype(str).unique().tolist())
        selected_stock = st.multiselect("依股票代號／契約篩選", options=stock_ids)
        filtered_df = (
            detail_df[detail_df[stock_col].astype(str).isin(selected_stock)]
            if selected_stock
            else detail_df
        )
    else:
        filtered_df = detail_df
    st.dataframe(filtered_df, width="stretch", height=480)

with chart_tab:
    st.subheader("圖表")
    _render_interactive_charts(df, equity)
    if is_futures_report(df):
        st.divider()
        st.subheader("保證金與曝險")
        _render_futures_exposure_chart(df)

with image_tab:
    st.subheader("回測輸出圖片")
    _render_chart_images(report.chart_paths)

with download_tab:
    st.subheader("下載")
    st.download_button(
        label="下載 trading report CSV",
        data=report.csv_path.read_bytes(),
        file_name=report.csv_path.name,
        mime="text/csv",
    )
    for chart_name, chart_path in report.chart_paths.items():
        if chart_path is None:
            continue
        st.download_button(
            label=f"下載 {chart_name}",
            data=chart_path.read_bytes(),
            file_name=chart_path.name,
            mime="image/png",
            key=f"download-{chart_name}",
        )
