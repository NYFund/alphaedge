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

**與報表產生分開**：`StockBacktestReporter` 原本一個類別裝了四種關注點
（報表、指標、繪圖、對標序列），繪圖那一群是唯一可以整塊搬走而不動到其他三群的。

**渲染器持有報表物件當資料來源**：畫圖要的權益序列、對標價格與帳戶狀態都在報表端，
本步驟只搬繪圖程式碼、不動取值邏輯，所以用組合把資料留在原處。
真要把介面收窄成「只吃畫圖需要的那幾個序列」，得逐張圖確認取值時機，屬另一件事。
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

        # Plot Balance Curve
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

        # === 清理 benchmark_price 數據 ===
        # 移除缺失值和 0 值（0 值可能是數據錯誤或停牌），並確保索引唯一且排序
        # 注意：股票收盤價不可能是負數，所以不需要特別檢查負數
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

        # === 計算調整後價格（處理股票分割） ===
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

        # === 策略權益資料（口徑由 get_equity_series 統一決定）===
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.reporter.get_equity_series()

        # === 整理 DataFrame 用來繪圖 ===
        # 使用 benchmark 的所有交易日作為基準日期（確保日期對齊正確）
        # benchmark 的日期通常是完整的交易日曆，所以用它作為基準更合理
        all_dates: pd.Index = benchmark_net_worth.index.sort_values()

        # 將策略數據重新索引到 benchmark 的日期上，使用前向填充處理沒有交易的日期
        cumulative_balance_aligned: pd.Series = cumulative_balance.reindex(
            all_dates
        ).ffill()
        # 如果仍有 NaN（例如在第一次交易之前的日期），用初始資金填充
        if cumulative_balance_aligned.isna().any():
            cumulative_balance_aligned = cumulative_balance_aligned.fillna(
                self.reporter.account.init_capital
            )

        # benchmark_net_worth 已經在 all_dates 上（因為 all_dates 就是從它的 index 來的），直接使用即可
        benchmark_net_worth_aligned: pd.Series = benchmark_net_worth

        networth_df: pd.DataFrame = pd.DataFrame(
            {
                "Date": all_dates,
                "Strategy Net Worth": cumulative_balance_aligned.values,
                f"{self.reporter.benchmark} Net Worth": benchmark_net_worth_aligned.values,
            }
        )

        # 計算報酬率 (ROI)
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

        # === 繪製圖表 ===
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

        # === 清理 benchmark_price 數據 ===
        # 移除缺失值和 0 值（0 值可能是數據錯誤或停牌），並確保索引唯一且排序
        # 注意：股票收盤價不可能是負數，所以不需要特別檢查負數
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

        # === 計算調整後價格（處理股票分割） ===
        benchmark_price_adjusted: pd.Series = self.reporter.get_adjusted_price(
            benchmark_price_clean, self.reporter.benchmark
        )

        # === 計算 Benchmark 的 MDD (%) ===
        # 使用調整後價格計算 MDD，這樣可以正確處理股票分割
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

        # === 策略權益資料（口徑由 get_equity_series 統一決定）===
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.reporter.get_equity_series()

        # === 整理 DataFrame 用來繪圖 ===
        # 使用 benchmark 的所有交易日作為基準日期（確保日期對齊正確）
        # benchmark 的日期通常是完整的交易日曆，所以用它作為基準更合理
        all_dates: pd.Index = mdd_benchmark.index.sort_values()

        # 將策略數據重新索引到 benchmark 的日期上，使用前向填充處理沒有交易的日期
        cumulative_balance_aligned: pd.Series = cumulative_balance.reindex(
            all_dates
        ).ffill()
        # 如果仍有 NaN（例如在第一次交易之前的日期），用初始資金填充
        if cumulative_balance_aligned.isna().any():
            cumulative_balance_aligned = cumulative_balance_aligned.fillna(
                self.reporter.account.init_capital
            )

        # 在對齊後的日期上計算策略的 MDD。
        # **公式與 `metrics_summary.csv` 的 `Max Drawdown (%)` 共用同一個函式**：
        # 兩處各寫一份必然漂移（MDD 曾經就有 reporter 與前端兩份實作）
        mdd_balance: pd.Series = pd.Series(
            compute_drawdown_series(cumulative_balance_aligned.astype(float).tolist()),
            index=cumulative_balance_aligned.index,
        )

        # mdd_benchmark 已經在 all_dates 上（因為 all_dates 就是從它的 index 來的），直接使用即可
        mdd_benchmark_aligned: pd.Series = mdd_benchmark

        mdd_df: pd.DataFrame = pd.DataFrame(
            {
                "Date": all_dates,
                "Strategy MDD": mdd_balance.values,
                f"{self.reporter.benchmark} MDD": mdd_benchmark_aligned.values,
            }
        )

        # === 繪製圖表 ===
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

        # 設置圖表配置 (MDD)
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

        # 轉換 Exit Date 為 datetime 格式
        profit_df: pd.DataFrame = self.reporter.trading_report[
            ["Exit Date", "Realized PnL"]
        ].copy()
        profit_df["Exit Date"] = pd.to_datetime(profit_df["Exit Date"])

        # 群組並計算每日總損益
        daily_profit: pd.DataFrame = (
            profit_df.groupby(profit_df["Exit Date"].dt.date)["Realized PnL"]
            .sum()
            .reset_index()
            .rename(columns={"Exit Date": "Date", "Realized PnL": "Daily PnL"})
        )

        # 建立 bar chart
        fig: go.Figure = go.Figure()
        fig.add_trace(
            go.Bar(
                x=daily_profit["Date"],
                y=daily_profit["Daily PnL"],
                marker_color="green",
                name="Daily Profit",
            )
        )

        # 設置圖表配置
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

        `show` 不指定時跟隨 reporter 的設定（見 `resolve_show_figures()`）——
        舊版寫死 `True`，每跑一次回測就在瀏覽器彈出 5 個分頁。
        """

        # Layout setting
        fig.update_layout(
            title=title,
            xaxis_title=xaxis_title,
            yaxis_title=yaxis_title,
            xaxis=dict(
                showgrid=True,
                gridcolor="lightgrey",  # 黑色格線
                gridwidth=0.5,  # 可微調線條粗細
                zeroline=False,
            ),
            yaxis=dict(
                showgrid=True, gridcolor="lightgrey", gridwidth=0.5, zeroline=False
            ),
            plot_bgcolor="#f9f9f9",
            paper_bgcolor="white",
        )

        # Annotation setting
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

        # Show figure
        if self.reporter.show if show is None else show:
            fig.show(renderer="browser")

    def save_figure(self, fig: go.Figure, file_name: str = "") -> None:
        """
        - Description: 儲存回測報告
        - Parameters:
            - fig: go.Figure
                要儲存的圖表
            - file_name: str
                儲存檔案的名稱
        """

        if not file_name:
            raise ValueError("file_name 不能是空字串")

        # 決定輸出路徑
        if self.reporter.output_dir is not None:
            save_path: Path = self.reporter.output_dir / file_name
        else:
            save_path: Path = Path(file_name)

        # 確保資料夾存在
        save_path.parent.mkdir(parents=True, exist_ok=True)

        # 輸出圖片
        fig.write_image(str(save_path))
        logger.info(f"* Figure saved to: {save_path}")
