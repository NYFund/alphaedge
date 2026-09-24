from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd
import plotly.graph_objects as go
from loguru import logger

from core.backtest.analysis.performance_metrics import compute_drawdown_series

if TYPE_CHECKING:
    from core.backtest.report.reporter import StockBacktestReporter

"""
回測圖表的產生與輸出

**繪圖與報表產生分屬兩個類別**：`StockBacktestReporter` 負責報表、指標與對標序列，
本檔只負責把那些資料畫成圖。

**渲染器以組合持有報表物件當資料來源**：權益序列、對標價格與帳戶狀態的取值時機
都由報表端決定（例如口徑一律走 `get_equity_series()`）。介面刻意不收窄成
「只吃畫圖需要的那幾個序列」，避免取值時機散進繪圖端而與報表口徑分歧。
"""


class EquityChartRenderer:
    """把回測結果畫成圖並輸出檔案"""

    def __init__(self, reporter: "StockBacktestReporter") -> None:
        """
        - Parameters:
            - reporter: StockBacktestReporter
                資料來源；圖表要的權益序列與對標價格都由它提供
        """

        self.reporter: StockBacktestReporter = reporter

    def plot_balance_curve(self) -> None:
        """繪製總資金曲線圖（總資金隨時間變化）"""

        equity: pd.Series
        basis: str
        equity, basis = self.reporter.get_equity_series()

        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=list(equity.index),
                y=equity.values,
                mode="lines",
                line=dict(color="blue", width=2),
            )
        )

        self.set_figure_config(
            fig,
            title=f"Balance Curve ({basis})",
            xaxis_title="Date",
            yaxis_title="Equity",
        )
        self.save_figure(
            fig, f"{self.reporter.strategy.strategy_name}_balance_curve.png"
        )

    def plot_balance_and_benchmark_curve(self) -> None:
        """繪製總資金 & benchmark 曲線圖"""

        # 剔除缺失值與 0（資料錯誤或停牌），並讓索引唯一且遞增；
        # 收盤價不可能為負，故不另外檢查負數
        benchmark_price_clean: pd.Series = self.reporter.benchmark_price.copy()
        benchmark_price_clean = benchmark_price_clean[
            benchmark_price_clean.notna() & (benchmark_price_clean > 0)
        ]
        benchmark_price_clean = benchmark_price_clean.sort_index()
        benchmark_price_clean = benchmark_price_clean[
            ~benchmark_price_clean.index.duplicated(keep="last")
        ]

        if len(benchmark_price_clean) == 0:
            logger.warning("benchmark_price 數據異常，無法繪製 benchmark 曲線")
            return

        # 還原價；除權息、分割與減資都已併入還原係數，此處不再另外調整
        benchmark_price_adjusted: pd.Series = self.reporter.get_adjusted_price(
            benchmark_price_clean, self.reporter.benchmark
        )

        # === Benchmark 淨值曲線 ===
        benchmark_net_worth: pd.Series = (
            benchmark_price_adjusted
            / benchmark_price_adjusted.iloc[0]
            * self.reporter.account.init_capital
        )
        # 加入初始資金節點
        benchmark_net_worth = pd.concat(
            [
                pd.Series(
                    self.reporter.account.init_capital,
                    index=[self.reporter.origin_date],
                ),
                benchmark_net_worth,
            ]
        )

        # 權益口徑統一由 `get_equity_series()` 決定
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.reporter.get_equity_series()

        # 以 benchmark 的日期為基準對齊：它是完整的交易日曆，策略只在有交易的
        # 日子有節點，反過來對齊會漏掉沒交易的日子
        all_dates: pd.Index = benchmark_net_worth.index.sort_values()

        # 沒交易的日子沿用前一日權益；首次交易之前沒有前值，補初始資金
        cumulative_balance_aligned: pd.Series = cumulative_balance.reindex(
            all_dates
        ).ffill()
        if cumulative_balance_aligned.isna().any():
            cumulative_balance_aligned = cumulative_balance_aligned.fillna(
                self.reporter.account.init_capital
            )

        benchmark_net_worth_aligned: pd.Series = benchmark_net_worth

        networth_df: pd.DataFrame = pd.DataFrame(
            {
                "Date": all_dates,
                "Strategy Net Worth": cumulative_balance_aligned.values,
                f"{self.reporter.benchmark} Net Worth": benchmark_net_worth_aligned.values,
            }
        )

        strategy_roi: float = round(
            (cumulative_balance.iloc[-1] / self.reporter.account.init_capital - 1)
            * 100,
            2,
        )
        benchmark_roi: float = round(
            (benchmark_price_adjusted.iloc[-1] / benchmark_price_adjusted.iloc[0] - 1)
            * 100,
            2,
        )

        roi_text: str = (
            f"Strategy Total ROI(%): {strategy_roi}%\n"
            f"{self.reporter.benchmark} Total ROI(%): {benchmark_roi}%\n"
            f"Equity basis: {basis}"
        )
        # 對標序列的口徑註腳（期貨有連續合約與近月拼接兩種，不可混著看）
        benchmark_note: str = self.reporter.get_benchmark_note()
        if benchmark_note:
            roi_text += f"\nBenchmark series: {benchmark_note}"

        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=networth_df["Date"],
                y=networth_df["Strategy Net Worth"],
                mode="lines",
                name="Strategy Net Worth",
                line=dict(color="blue", width=2),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=networth_df["Date"],
                y=networth_df[f"{self.reporter.benchmark} Net Worth"],
                mode="lines",
                name=f"{self.reporter.benchmark} Net Worth",
                line=dict(color="red", width=2),
            )
        )

        self.set_figure_config(
            fig,
            title=f"Strategy vs {self.reporter.benchmark} Net Worth "
            f"({self.reporter.start_date.strftime('%Y/%m/%d')} ~ {self.reporter.end_date.strftime('%Y/%m/%d')})",
            xaxis_title="Date",
            yaxis_title="Net Worth",
            fig_text=roi_text,
        )
        self.save_figure(fig, f"{self.reporter.strategy.strategy_name}_networth.png")

    def plot_balance_mdd(self) -> None:
        """繪製總資金 Max Drawdown"""

        # 剔除缺失值與 0（資料錯誤或停牌），並讓索引唯一且遞增；
        # 收盤價不可能為負，故不另外檢查負數
        benchmark_price_clean: pd.Series = self.reporter.benchmark_price.copy()
        benchmark_price_clean = benchmark_price_clean[
            benchmark_price_clean.notna() & (benchmark_price_clean > 0)
        ]
        benchmark_price_clean = benchmark_price_clean.sort_index()
        benchmark_price_clean = benchmark_price_clean[
            ~benchmark_price_clean.index.duplicated(keep="last")
        ]

        if len(benchmark_price_clean) == 0:
            logger.warning("benchmark_price 數據異常，無法繪製 benchmark MDD")
            return

        # 還原價；除權息、分割與減資都已併入還原係數，此處不再另外調整
        benchmark_price_adjusted: pd.Series = self.reporter.get_adjusted_price(
            benchmark_price_clean, self.reporter.benchmark
        )

        # 以還原價計算 benchmark 的 MDD（%）；用原始價的話除權息跳空會被
        # 當成真實回撤
        mdd_benchmark: pd.Series = (
            benchmark_price_adjusted / benchmark_price_adjusted.cummax() - 1
        ) * 100

        # 加入初始資金節點
        mdd_benchmark = pd.concat(
            [
                pd.Series(0.0, index=[self.reporter.origin_date]),
                mdd_benchmark,
            ]  # 起點 MDD 為 0%
        )

        # 權益口徑統一由 `get_equity_series()` 決定
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.reporter.get_equity_series()

        # 以 benchmark 的日期為基準對齊：它是完整的交易日曆，策略只在有交易的
        # 日子有節點，反過來對齊會漏掉沒交易的日子
        all_dates: pd.Index = mdd_benchmark.index.sort_values()

        # 沒交易的日子沿用前一日權益；首次交易之前沒有前值，補初始資金
        cumulative_balance_aligned: pd.Series = cumulative_balance.reindex(
            all_dates
        ).ffill()
        if cumulative_balance_aligned.isna().any():
            cumulative_balance_aligned = cumulative_balance_aligned.fillna(
                self.reporter.account.init_capital
            )

        # **公式與 `metrics_summary.csv` 的 `Max Drawdown (%)` 共用同一個函式**：
        # 圖與報表各寫一份必然漂移
        mdd_balance: pd.Series = pd.Series(
            compute_drawdown_series(cumulative_balance_aligned.astype(float).tolist()),
            index=cumulative_balance_aligned.index,
        )

        mdd_benchmark_aligned: pd.Series = mdd_benchmark

        mdd_df: pd.DataFrame = pd.DataFrame(
            {
                "Date": all_dates,
                "Strategy MDD": mdd_balance.values,
                f"{self.reporter.benchmark} MDD": mdd_benchmark_aligned.values,
            }
        )

        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=mdd_df["Date"],
                y=mdd_df["Strategy MDD"],
                mode="lines",
                name="Strategy MDD",
                line=dict(color="blue", width=2),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=mdd_df["Date"],
                y=mdd_df[f"{self.reporter.benchmark} MDD"],
                mode="lines",
                name=f"{self.reporter.benchmark} MDD",
                line=dict(color="red", width=2),
            )
        )

        self.set_figure_config(
            fig,
            title=f"MDD ({self.reporter.start_date.strftime('%Y/%m/%d')} ~ {self.reporter.end_date.strftime('%Y/%m/%d')})",
            xaxis_title="Date",
            yaxis_title="MDD (%)",
            fig_text=f"Equity basis: {basis}",
        )
        self.save_figure(fig, f"{self.reporter.strategy.strategy_name}_mdd.png")

    def plot_everyday_profit(self) -> None:
        """
        繪製每天的利潤（已實現口徑：依平倉日分組的 Realized PnL）

        與 `plot_everyday_equity_change()` 的語意不同，兩張圖並存不可互相取代：
        本圖只在平倉當天有數值，持倉期間一律為 0。
        """

        profit_df: pd.DataFrame = self.reporter.trading_report[
            ["Exit Date", "Realized PnL"]
        ].copy()
        profit_df["Exit Date"] = pd.to_datetime(profit_df["Exit Date"])

        daily_profit: pd.DataFrame = (
            profit_df.groupby(profit_df["Exit Date"].dt.date)["Realized PnL"]
            .sum()
            .reset_index()
            .rename(columns={"Exit Date": "Date", "Realized PnL": "Daily PnL"})
        )

        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Bar(
                x=daily_profit["Date"],
                y=daily_profit["Daily PnL"],
                marker_color="green",
                name="Daily Profit",
            )
        )

        self.set_figure_config(
            fig,
            title=f"Everyday Profit ({self.reporter.EQUITY_BASIS_REALIZED_ONLY})",
            xaxis_title="Date",
            yaxis_title="Daily PnL",
        )
        self.save_figure(
            fig, f"{self.reporter.strategy.strategy_name}_everyday_profit.png"
        )

    def plot_everyday_equity_change(self) -> None:
        """
        - Description:
            繪製每日權益變化（盯市口徑）

            逐日權益的**差分**是「含未實現變動的當日損益」，與
            `plot_everyday_profit()` 的「已實現損益依平倉日分組」語意不同：
            持倉期間被軋的那幾天，本圖會有負值，那張圖是 0。

            沒有 `daily_equity` 時本圖會退化成與已實現口徑那張完全重複，
            故直接跳過而非畫一張誤導的圖。
        """

        if not self.reporter.daily_equity:
            logger.info("* 無 daily_equity，跳過每日權益變化圖（盯市口徑）")
            return

        equity: pd.Series
        equity, _ = self.reporter.get_equity_series()

        # 差分：第一筆是相對初始資金的變化，故 dropna 之後長度等於交易日數
        equity_change: pd.Series = equity.diff().dropna()

        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Bar(
                x=list(equity_change.index),
                y=equity_change.values,
                marker_color="steelblue",
                name="Daily Equity Change",
            )
        )

        self.set_figure_config(
            fig,
            title=f"Everyday Equity Change ({self.reporter.EQUITY_BASIS_MARK_TO_MARKET})",
            xaxis_title="Date",
            yaxis_title="Daily Equity Change",
        )
        self.save_figure(
            fig, f"{self.reporter.strategy.strategy_name}_everyday_equity_change.png"
        )

    def set_figure_config(
        self,
        fig: go.Figure,
        title: str = "",
        xaxis_title: str = "",
        yaxis_title: str = "",
        fig_text: str = "",
        show: Optional[bool] = None,
    ) -> None:
        """
        設置繪圖配置

        `show` 不指定時跟隨 reporter 的設定（`resolve_show_figures()`）；
        無條件開圖會讓批次回測一次在瀏覽器彈出數十個分頁。
        """

        fig.update_layout(
            title=title,
            xaxis_title=xaxis_title,
            yaxis_title=yaxis_title,
            xaxis=dict(
                showgrid=True,
                gridcolor="lightgrey",
                gridwidth=0.5,
                zeroline=False,
            ),
            yaxis=dict(
                showgrid=True, gridcolor="lightgrey", gridwidth=0.5, zeroline=False
            ),
            plot_bgcolor="#f9f9f9",
            paper_bgcolor="white",
        )

        if fig_text != "":
            fig.add_annotation(
                xref="paper",
                yref="paper",
                x=1,
                y=1,
                text=fig_text.replace("\n", "<br>"),
                showarrow=False,
                font=dict(
                    size=self.reporter.CHART_FONT_SIZE,
                    color="white",
                ),
                align="left",
                bordercolor="black",
                borderwidth=1,
                borderpad=5,
                bgcolor="black",
                opacity=0.5,
            )

        if self.reporter.show if show is None else show:
            fig.show(renderer="browser")

    def save_figure(self, fig: go.Figure, file_name: str = "") -> None:
        """
        - Description:
            儲存回測報告
        - Parameters:
            - fig: go.Figure
                要儲存的圖表
            - file_name: str
                儲存檔案的名稱
        """

        if not file_name:
            raise ValueError("file_name 不能是空字串")

        if self.reporter.output_dir is not None:
            save_path: Path = self.reporter.output_dir / file_name
        else:
            save_path: Path = Path(file_name)

        save_path.parent.mkdir(parents=True, exist_ok=True)

        fig.write_image(str(save_path))
        logger.info(f"* Figure saved to: {save_path}")
