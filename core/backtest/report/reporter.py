import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
from loguru import logger

from core.api.tw.stock_price_api import StockPriceAPI
from core.backtest.analysis.performance_metrics import (
    TRADING_DAYS_PER_YEAR,
    compute_annualized_information_ratio,
    compute_annualized_sharpe,
    compute_annualized_sortino,
    compute_annualized_volatility,
    compute_drawdown_series,
    compute_max_drawdown,
    compute_period_returns,
    compute_profit_factor,
    compute_win_loss_ratio,
)
from core.backtest.report.base import BaseBacktestReporter
from core.config import resolve_show_figures
from core.models.stock.record import StockTradeRecord
from core.strategies.stock import BaseStockStrategy
from core.utils import FileEncoding

"""Generates performance reports based on backtest results"""


class StockBacktestReporter(BaseBacktestReporter):
    """Generates visual reports based on backtest results"""

    CHART_FONT_SIZE: int = 15

    # 權益曲線的兩種口徑，標註在圖上避免不同期報表被混著看
    EQUITY_BASIS_MARK_TO_MARKET: str = "Mark-to-market"  # 逐日盯市（含未實現損益）
    EQUITY_BASIS_REALIZED_ONLY: str = "Realized only"  # 只認已實現損益（MDD 會被低估）

    def __init__(
        self,
        strategy: BaseStockStrategy,
        output_dir: Optional[Path] = None,
        price: Optional[StockPriceAPI] = None,
        show: Optional[bool] = None,
    ) -> None:
        super().__init__(strategy, output_dir)

        # 由 Backtester 傳入 DataFeed 已經開好的連線；未指定時自行建立並由
        # `close()` 負責關掉（舊版每跑一次回測就多一條不再使用的連線）
        self.price: Optional[StockPriceAPI] = price

        # 畫完是否在瀏覽器開圖。舊版寫死 True，於是每跑一次回測就彈出 5 個分頁，
        # 批次跑參數掃描時等於一次開幾十個
        self.show: bool = resolve_show_figures() if show is None else show

        # Backtest date
        self.start_date: datetime.date = self.strategy.start_date  # Backtest start date
        self.end_date: datetime.date = self.strategy.end_date  # Backtest end date

        # 起始前一天，用來當作初始資金節點
        self.origin_date: datetime.date = self.start_date - datetime.timedelta(days=1)

        # Benchmark
        self.benchmark: str = "0050"  # Benchmark stock

        # Benchmark price
        self.benchmark_price: Optional[pd.Series] = None

        # Trading report
        self.trading_report: Optional[pd.DataFrame] = None  # Trading report

        self.setup()

    def setup(self) -> None:
        """
        - Description:
            建立資料連線並取 benchmark 的**還原**收盤價

            **benchmark 必須用還原價**：原始收盤價在除權息日有跳空，
            0050 這種年年配息的標的，用原始價當基準等於讓基準每年少賺一次配息，
            策略看起來永遠贏得比實際多。

            分割與減資已由 `corporate_action` 併入還原係數，
            `get_adjusted_close_series()` 一次處理完，這裡不可再套一次分割調整。
        """

        # Price data；`conn` 由呼叫端注入時共用同一條連線，close() 不會關掉別人的
        if self.price is None:
            self.price = StockPriceAPI()

        # Benchmark price（還原價）
        self.benchmark_price: pd.Series = self.price.get_adjusted_close_series(
            stock_id=self.benchmark,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        if not self.benchmark_price.empty:
            self.benchmark_price.index = pd.to_datetime(self.benchmark_price.index).date

    def close(self) -> None:
        """
        關閉 reporter 自己開的資料連線

        不關的話，每跑一次回測就多一條不再使用的 SQLite 連線；
        由呼叫端注入的連線不歸 reporter 關（`StockPriceAPI` 的 `owns_conn` 語意）。
        """

        if self.price is not None:
            self.price.close()

    def _get_adjusted_price(self, price_series: pd.Series, stock_id: str) -> pd.Series:
        """
        benchmark 的還原價（**分割已由還原係數涵蓋，本方法不再另外調整**）

        2026-09-13 之前這裡會再套一次 `stock_split.apply_split_adjustment()`，
        因為當時的還原係數只認除權息、不含分割。`corporate_action` 表上線後
        分割與減資都進了累乘係數，**再套一次就是重複調整**——實測 0050 會從
        1.82% 變成 303%。本方法保留只是為了讓兩處呼叫端不必各自改。
        """

        return price_series

    def generate_trading_report(self) -> pd.DataFrame:
        """生成回測報告"""

        report_columns: List[str] = [
            "Symbol",
            "Position Type",
            "Entry Date",
            "Entry Price",
            "Exit Date",
            "Exit Price",
            "Buy Date",
            "Buy Price",
            "Buy Volume",
            "Sell Date",
            "Sell Price",
            "Sell Volume",
            "Commission",
            "Tax",
            "Transaction Cost",
            "Borrow Fee",
            "Interest",
            "Dividend Compensation",
            "Margin",
            "Holding Days",
            "Short Method",
            "Realized PnL",
            "ROI",
            "ROI on Capital",
            "Cumulative PnL",
            "Cumulative Balance",
        ]

        # Initialize cumulative values for PnL and Balance
        cumulative_pnl: float = 0.0
        cumulative_balance: float = self.account.init_capital

        # 過濾出已平倉的交易記錄（只有已平倉的記錄才有完整的買賣資訊）
        # 確保交易記錄按 exit_date（平倉日）排序（對於 tick 級別回測，同一天可能有多筆交易）
        # 排序確保累積值的計算順序正確，以及繪圖時 groupby().last() 能取得正確的最後一筆
        # 此排序邏輯對 tick 和 day 級別回測都適用
        #
        # 排序邏輯：
        # 1. 主要排序：按 exit_date（平倉日期；SHORT 的 sell_date 是開倉日，不可用）
        # 2. 次要排序：保持 trade_records 的原始添加順序（使用索引）
        #    原因：trade_records 是按平倉順序添加的，而 id 是按開倉順序生成的
        #    使用原始順序可以確保同一天內的多筆交易按實際平倉時間順序排列
        closed_records_with_index: List[Tuple[int, StockTradeRecord]] = [
            (i, r) for i, r in enumerate(self.account.trade_records) if r.is_closed
        ]
        sorted_records: List[StockTradeRecord] = [
            r
            for _, r in sorted(
                closed_records_with_index,
                key=lambda x: (
                    x[1].exit_date if x[1].exit_date else datetime.date.min,
                    x[0],  # 使用原始索引作為次要排序鍵，保持平倉順序
                ),
            )
        ]

        # Generate trading report
        rows: List[Dict[str, Any]] = []
        for record in sorted_records:
            cumulative_pnl += record.realized_pnl
            cumulative_balance += record.realized_pnl

            row: Dict[str, Any] = {
                # 欄名與領域模型的識別欄一致（`record.symbol`）；
                # 期貨那份維持 `Contract ID`——它是 `{商品}{到期月}`，
                # 與「一檔股票」不是同一種東西，且前端以它判斷報表型別
                "Symbol": record.symbol,
                "Position Type": record.position_type.value,
                "Entry Date": record.entry_date,
                "Entry Price": record.entry_price,
                "Exit Date": record.exit_date,
                "Exit Price": record.exit_price,
                "Buy Date": record.buy_date,
                "Buy Price": record.buy_price,
                "Buy Volume": record.buy_volume,
                "Sell Date": record.sell_date,
                "Sell Price": record.sell_price,
                "Sell Volume": record.sell_volume,
                "Commission": record.commission,
                "Tax": record.tax,
                "Transaction Cost": record.transaction_cost,
                "Borrow Fee": record.borrow_fee,
                "Interest": record.interest,
                "Dividend Compensation": record.dividend_compensation,
                "Margin": record.margin,
                "Holding Days": record.holding_days,
                "Short Method": (
                    record.short_method.value if record.short_method else ""
                ),
                "Realized PnL": record.realized_pnl,
                "ROI": record.roi,
                "ROI on Capital": record.roi_on_capital,
                "Cumulative PnL": cumulative_pnl,
                "Cumulative Balance": cumulative_balance,
            }
            rows.append(row)

        # Convert to DataFrame
        df: pd.DataFrame = pd.DataFrame(rows, columns=report_columns)
        self.save_report(df, f"{self.strategy.strategy_name}_trading_report.csv")
        return df

    def generate_direction_summary(self) -> pd.DataFrame:
        """
        - Description:
            產生多空分開的績效統計

            多空的成本結構與風險型態完全不同（放空有借券費、保證金與無限虧損風險），
            混在同一組數字裡會看不出策略到底靠哪一邊賺錢。
        - Return:
            - df: pd.DataFrame
                以 Position Type 分組的統計表
        """

        if self.trading_report is None or self.trading_report.empty:
            return pd.DataFrame()

        rows: List[Dict[str, Any]] = []
        for position_type, group in self.trading_report.groupby("Position Type"):
            wins: pd.DataFrame = group[group["Realized PnL"] > 0]

            rows.append(
                {
                    "Position Type": position_type,
                    "Trades": len(group),
                    "Win Rate (%)": round(len(wins) / len(group) * 100, 2),
                    "Total PnL": round(group["Realized PnL"].sum(), 2),
                    "Avg PnL": round(group["Realized PnL"].mean(), 2),
                    "Avg ROI (%)": round(group["ROI"].mean(), 2),
                    "Total Commission": round(group["Commission"].sum(), 2),
                    "Total Tax": round(group["Tax"].sum(), 2),
                    "Total Borrow Fee": round(group["Borrow Fee"].sum(), 2),
                    "Total Interest": round(group["Interest"].sum(), 2),
                    "Total Dividend Compensation": round(
                        group["Dividend Compensation"].sum(), 2
                    ),
                    "Avg Holding Days": round(group["Holding Days"].mean(), 2),
                }
            )

        df: pd.DataFrame = pd.DataFrame(rows)
        self.save_report(df, f"{self.strategy.strategy_name}_direction_summary.csv")
        return df

    def generate_event_report(self, event_counts: Dict[str, int]) -> pd.DataFrame:
        """
        - Description:
            輸出回測期間的事件計數（強制回補、斷頭、拒單等）

            這些是放空策略的尾部風險，被平均進總績效就看不見了，必須單獨列出。
        - Parameters:
            - event_counts: Dict[str, int]
                Backtester.event_counts
        - Return:
            - df: pd.DataFrame
        """

        df: pd.DataFrame = pd.DataFrame(
            [{"Event": key, "Count": value} for key, value in event_counts.items()]
        )
        self.save_report(df, f"{self.strategy.strategy_name}_event_report.csv")
        return df

    # 指標報表的欄位；**長表**而非寬表，新增指標不必改欄位結構
    METRICS_COLUMNS: List[str] = ["Metric", "Value", "Note"]

    def generate_metrics_summary(self) -> pd.DataFrame:
        """
        - Description:
            輸出整體績效指標（`<策略>_metrics_summary.csv`）

            **不開前端也看得到，且只有一份計算**：Sharpe／Sortino／MDD 原本只存在
            於前端與 MDD 圖，公式散在兩處。本方法一律呼叫
            `core/backtest/analysis/performance_metrics.py` 的純函式。

            **格式是長表**（`Metric`／`Value`／`Note`）：新增指標不必改欄位結構，
            前端也能逐列直接顯示；`Note` 放口徑說明，避免兩種口徑的數字被混讀。

            **`Equity Basis` 為 `Realized only` 時，波動度與 Sharpe／Sortino 留空**：
            那個口徑只在平倉那天才有節點，拿它的「逐筆報酬」乘 √252 等於宣稱
            一年有 252 筆交易。留空比給一個看起來合理的錯數字好。
        - Return:
            - pd.DataFrame
                長表格式的指標；沒有已平倉交易時為空表
        """

        if self.trading_report is None or self.trading_report.empty:
            logger.warning("[Metrics] 沒有已平倉交易，略過整體績效指標")
            return pd.DataFrame(columns=self.METRICS_COLUMNS)

        equity: pd.Series
        basis: str
        equity, basis = self.get_equity_series()

        rows: List[Dict[str, Any]] = self.build_trade_metrics()
        rows.extend(self.build_equity_metrics(equity, basis))
        rows.extend(self.build_benchmark_metrics(equity, basis))
        rows.append({"Metric": "Equity Basis", "Value": basis, "Note": ""})

        df: pd.DataFrame = pd.DataFrame(rows, columns=self.METRICS_COLUMNS)
        self.save_report(df, f"{self.strategy.strategy_name}_metrics_summary.csv")
        return df

    def build_trade_metrics(self) -> List[Dict[str, Any]]:
        """
        逐筆交易統計

        **整體勝率與 `Avg ROI` 必須等於 `direction_summary` 依 `Trades` 加權合併
        的結果**：同一個數字在兩張報表上不一致，讀的人無從判斷哪個對。
        """

        pnls: List[float] = self.trading_report["Realized PnL"].astype(float).tolist()
        wins: int = sum(1 for value in pnls if value > 0)
        losses: int = sum(1 for value in pnls if value < 0)

        return [
            {"Metric": "Trades", "Value": len(pnls), "Note": ""},
            {"Metric": "Win Count", "Value": wins, "Note": ""},
            {
                "Metric": "Loss Count",
                "Value": losses,
                "Note": "平盤出場不計入勝敗任一邊",
            },
            {
                "Metric": "Win Rate (%)",
                "Value": round(wins / len(pnls) * 100, 2),
                "Note": "",
            },
            {
                "Metric": "Win/Loss Ratio",
                "Value": compute_win_loss_ratio(pnls),
                "Note": "獲利筆數 ÷ 虧損筆數；零虧損筆數時留空",
            },
            {
                "Metric": "Profit Factor",
                "Value": compute_profit_factor(pnls),
                "Note": "總獲利 ÷ |總虧損|；零虧損筆數時留空（不是 0）",
            },
            {
                "Metric": "Avg ROI (%)",
                "Value": round(self.trading_report["ROI"].astype(float).mean(), 2),
                "Note": self.get_avg_roi_note(),
            },
            {
                "Metric": "Avg Holding Days",
                "Value": round(
                    self.trading_report["Holding Days"].astype(float).mean(), 2
                ),
                "Note": "曆日，非交易日",
            },
            {"Metric": "Total PnL", "Value": round(sum(pnls), 2), "Note": ""},
        ]

    def build_equity_metrics(
        self, equity: pd.Series, basis: str
    ) -> List[Dict[str, Any]]:
        """
        權益序列衍生的指標

        口徑為 `Realized only` 時，以日報酬為樣本的三項一律留空並在 `Note`
        說明原因——留空比給一個看起來合理的錯數字好。
        """

        values: List[float] = equity.astype(float).tolist()
        returns: List[float] = compute_period_returns(values)

        skip: bool = basis == self.EQUITY_BASIS_REALIZED_ONLY
        skip_note: str = (
            f"{self.EQUITY_BASIS_REALIZED_ONLY} 口徑只在平倉日有節點，"
            f"以它算日頻指標等於宣稱一年有 {TRADING_DAYS_PER_YEAR} 筆交易"
        )

        return [
            {
                "Metric": "Final Equity",
                "Value": round(values[-1], 2) if values else None,
                "Note": basis,
            },
            {
                "Metric": "Max Drawdown (%)",
                "Value": compute_max_drawdown(values),
                "Note": f"自歷史高點的最大跌幅（負值）；口徑 {basis}",
            },
            {
                "Metric": "Annualized Volatility (%)",
                "Value": None if skip else compute_annualized_volatility(returns),
                "Note": skip_note if skip else "",
            },
            {
                "Metric": "Sharpe Ratio",
                "Value": None if skip else compute_annualized_sharpe(returns),
                "Note": skip_note if skip else "",
            },
            {
                "Metric": "Sortino Ratio",
                "Value": None if skip else compute_annualized_sortino(returns),
                "Note": skip_note if skip else "",
            },
        ]

    def build_benchmark_metrics(
        self, equity: pd.Series, basis: str
    ) -> List[Dict[str, Any]]:
        """
        - Description:
            對標相關指標：`Benchmark` 與 `Information Ratio`

            **兩條序列一定要先依日期對齊**：`compute_annualized_information_ratio()`
            對長度不同直接 `ValueError`，而長度湊得起來不代表日期對得起來
            ——自作主張對齊只會讓錯位的比較看起來很正常。策略首日沒有對應的
            基準報酬，自然落在交集之外。

            `Realized only` 口徑不算 IR，理由同波動度與 Sharpe。
            對標序列本身不可信時（見 `get_benchmark_block_reason()`）同樣留空。
        - Parameters:
            - equity: pd.Series
                策略權益序列（index 為交易日）
            - basis: str
                權益口徑
        - Return:
            - List[Dict[str, Any]]
                `Benchmark` 與 `Information Ratio` 兩列
        """

        rows: List[Dict[str, Any]] = [
            {"Metric": "Benchmark", "Value": self.benchmark, "Note": ""}
        ]

        reason: str = self.get_benchmark_block_reason()
        if basis == self.EQUITY_BASIS_REALIZED_ONLY:
            reason = (
                f"{self.EQUITY_BASIS_REALIZED_ONLY} 口徑只在平倉日有節點，"
                f"與基準日報酬不同頻，無法逐日相減"
            )

        if reason:
            rows.append({"Metric": "Information Ratio", "Value": None, "Note": reason})
            return rows

        strategy_returns: List[float]
        benchmark_returns: List[float]
        strategy_returns, benchmark_returns = self.align_daily_returns(equity)

        rows.append(
            {
                "Metric": "Information Ratio",
                "Value": compute_annualized_information_ratio(
                    strategy_returns, benchmark_returns
                ),
                "Note": f"相對 {self.benchmark} 的年化主動報酬 ÷ 追蹤誤差",
            }
        )
        return rows

    def align_daily_returns(self, equity: pd.Series) -> Tuple[List[float], List[float]]:
        """
        把策略權益與對標價格對齊到**同一組日期**後各自轉成日報酬

        先取日期交集再算報酬，不是先各算報酬再截長度：後者在任一邊缺某一天時
        會讓之後的每一期都錯開一格，而長度仍然可能剛好相同。
        """

        if self.benchmark_price is None or self.benchmark_price.empty:
            return ([], [])

        benchmark: pd.Series = self.benchmark_price.copy()
        benchmark = benchmark[benchmark.notna() & (benchmark > 0)].sort_index()
        benchmark = benchmark[~benchmark.index.duplicated(keep="last")]

        common: pd.Index = equity.index.intersection(benchmark.index).sort_values()
        if len(common) < 2:
            return ([], [])

        return (
            compute_period_returns(equity.reindex(common).astype(float).tolist()),
            compute_period_returns(benchmark.reindex(common).astype(float).tolist()),
        )

    def get_benchmark_block_reason(self) -> str:
        """
        對標序列不可信時回傳原因字串（IR 因此留空）；台股的還原價一律可信

        期貨覆寫它：對標退回近月拼接時，換月接點有展期價差造成的假跳空，
        那幾天的基準日報酬是假的，算出來的 IR 會被那幾天帶偏。
        """

        return ""

    def get_avg_roi_note(self) -> str:
        """`Avg ROI` 的口徑說明；台股是名目報酬率，期貨覆寫為保證金報酬率"""

        return ""

    def get_equity_series(self) -> Tuple[pd.Series, str]:
        """
        - Description:
            權益序列的唯一入口：三張權益圖與 MDD 都吃這一條

            `daily_equity` 有值時採**逐日盯市**（含未實現損益）；沒有時退回
            「已實現損益的累積餘額」。後者只在平倉那天才有節點，持倉期間的
            逆勢會被整段抹平——那正是留倉放空最大的風險來源，MDD 因此被低估。

            把口徑判斷收斂在這裡，避免四張圖各判一次而彼此不一致。
        - Return:
            - series: pd.Series
                index 為 `datetime.date`、值為權益；起點補上 `origin_date` → 初始資金
            - basis: str
                本次採用的口徑，供圖上標註
        """

        basis: str
        series: pd.Series

        if self.daily_equity:
            equity_df: pd.DataFrame = pd.DataFrame(self.daily_equity)
            series = (
                equity_df.groupby(pd.to_datetime(equity_df["Date"]).dt.date)["Equity"]
                .last()
                .astype(float)
            )
            basis = self.EQUITY_BASIS_MARK_TO_MARKET

        else:
            balance_df: pd.DataFrame = self.trading_report[
                ["Exit Date", "Cumulative Balance"]
            ].copy()
            # 依日期取每日最後一筆，避免一天多筆交易造成重複節點
            series = (
                balance_df.groupby(pd.to_datetime(balance_df["Exit Date"]).dt.date)[
                    "Cumulative Balance"
                ]
                .last()
                .astype(float)
            )
            basis = self.EQUITY_BASIS_REALIZED_ONLY

        # 加入初始資金節點，讓曲線從回測起始前一天開始
        init_row: pd.Series = pd.Series(
            float(self.account.init_capital), index=[self.origin_date]
        )
        series = pd.concat([init_row, series]).sort_index()

        return series, basis

    def plot_balance_curve(self) -> None:
        """繪製總資金曲線圖（總資金隨時間變化）"""

        equity: pd.Series
        basis: str
        equity, basis = self.get_equity_series()

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
        self.save_figure(fig, f"{self.strategy.strategy_name}_balance_curve.png")

    def get_benchmark_note(self) -> str:
        """
        對標序列的口徑註腳；台股只有一種（還原價），故不標示

        期貨覆寫它：連續合約與近月拼接是兩種口徑，圖上不標的話，
        換月接點有沒有假跳空完全看不出來，見 `FuturesBacktestReporter`。
        """

        return ""

    def plot_balance_and_benchmark_curve(self) -> None:
        """繪製總資金 & benchmark 曲線圖"""

        # === 清理 benchmark_price 數據 ===
        # 移除缺失值和 0 值（0 值可能是數據錯誤或停牌），並確保索引唯一且排序
        # 注意：股票收盤價不可能是負數，所以不需要特別檢查負數
        benchmark_price_clean: pd.Series = self.benchmark_price.copy()
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
        benchmark_price_adjusted: pd.Series = self._get_adjusted_price(
            benchmark_price_clean, self.benchmark
        )

        # === Benchmark 淨值曲線 ===
        benchmark_net_worth: pd.Series = (
            benchmark_price_adjusted
            / benchmark_price_adjusted.iloc[0]
            * self.account.init_capital
        )
        # 加入初始資金節點
        benchmark_net_worth = pd.concat(
            [
                pd.Series(self.account.init_capital, index=[self.origin_date]),
                benchmark_net_worth,
            ]
        )

        # === 策略權益資料（口徑由 get_equity_series 統一決定）===
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.get_equity_series()

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
                self.account.init_capital
            )

        # benchmark_net_worth 已經在 all_dates 上（因為 all_dates 就是從它的 index 來的），直接使用即可
        benchmark_net_worth_aligned: pd.Series = benchmark_net_worth

        networth_df: pd.DataFrame = pd.DataFrame(
            {
                "Date": all_dates,
                "Strategy Net Worth": cumulative_balance_aligned.values,
                f"{self.benchmark} Net Worth": benchmark_net_worth_aligned.values,
            }
        )

        # 計算報酬率 (ROI)
        strategy_roi: float = round(
            (cumulative_balance.iloc[-1] / self.account.init_capital - 1) * 100, 2
        )
        benchmark_roi: float = round(
            (benchmark_price_adjusted.iloc[-1] / benchmark_price_adjusted.iloc[0] - 1)
            * 100,
            2,
        )

        roi_text: str = (
            f"Strategy Total ROI(%): {strategy_roi}%\n"
            f"{self.benchmark} Total ROI(%): {benchmark_roi}%\n"
            f"Equity basis: {basis}"
        )
        # 對標序列的口徑註腳（期貨有連續合約與近月拼接兩種，不可混著看）
        benchmark_note: str = self.get_benchmark_note()
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
                y=networth_df[f"{self.benchmark} Net Worth"],
                mode="lines",
                name=f"{self.benchmark} Net Worth",
                line=dict(color="red", width=2),
            )
        )

        self.set_figure_config(
            fig,
            title=f"Strategy vs {self.benchmark} Net Worth "
            f"({self.start_date.strftime('%Y/%m/%d')} ~ {self.end_date.strftime('%Y/%m/%d')})",
            xaxis_title="Date",
            yaxis_title="Net Worth",
            fig_text=roi_text,
        )
        self.save_figure(fig, f"{self.strategy.strategy_name}_networth.png")

    def plot_balance_mdd(self) -> None:
        """繪製總資金 Max Drawdown"""

        # === 清理 benchmark_price 數據 ===
        # 移除缺失值和 0 值（0 值可能是數據錯誤或停牌），並確保索引唯一且排序
        # 注意：股票收盤價不可能是負數，所以不需要特別檢查負數
        benchmark_price_clean: pd.Series = self.benchmark_price.copy()
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
        benchmark_price_adjusted: pd.Series = self._get_adjusted_price(
            benchmark_price_clean, self.benchmark
        )

        # === 計算 Benchmark 的 MDD (%) ===
        # 使用調整後價格計算 MDD，這樣可以正確處理股票分割
        mdd_benchmark: pd.Series = (
            benchmark_price_adjusted / benchmark_price_adjusted.cummax() - 1
        ) * 100

        # 加入初始資金節點
        mdd_benchmark = pd.concat(
            [pd.Series(0.0, index=[self.origin_date]), mdd_benchmark]  # 起點 MDD 為 0%
        )

        # === 策略權益資料（口徑由 get_equity_series 統一決定）===
        cumulative_balance: pd.Series
        basis: str
        cumulative_balance, basis = self.get_equity_series()

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
                self.account.init_capital
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
                f"{self.benchmark} MDD": mdd_benchmark_aligned.values,
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
                y=mdd_df[f"{self.benchmark} MDD"],
                mode="lines",
                name=f"{self.benchmark} MDD",
                line=dict(color="red", width=2),
            )
        )

        # 設置圖表配置 (MDD)
        self.set_figure_config(
            fig,
            title=f"MDD ({self.start_date.strftime('%Y/%m/%d')} ~ {self.end_date.strftime('%Y/%m/%d')})",
            xaxis_title="Date",
            yaxis_title="MDD (%)",
            fig_text=f"Equity basis: {basis}",
        )
        self.save_figure(fig, f"{self.strategy.strategy_name}_mdd.png")

    def plot_everyday_profit(self) -> None:
        """
        繪製每天的利潤（已實現口徑：依平倉日分組的 Realized PnL）

        與 `plot_everyday_equity_change()` 的語意不同，兩張圖並存不可互相取代：
        本圖只在平倉當天有數值，持倉期間一律為 0。
        """

        # 轉換 Exit Date 為 datetime 格式
        profit_df: pd.DataFrame = self.trading_report[
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
            title=f"Everyday Profit ({self.EQUITY_BASIS_REALIZED_ONLY})",
            xaxis_title="Date",
            yaxis_title="Daily PnL",
        )
        self.save_figure(fig, f"{self.strategy.strategy_name}_everyday_profit.png")

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

        if not self.daily_equity:
            logger.info("* 無 daily_equity，跳過每日權益變化圖（盯市口徑）")
            return

        equity: pd.Series
        equity, _ = self.get_equity_series()

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
            title=f"Everyday Equity Change ({self.EQUITY_BASIS_MARK_TO_MARKET})",
            xaxis_title="Date",
            yaxis_title="Daily Equity Change",
        )
        self.save_figure(
            fig, f"{self.strategy.strategy_name}_everyday_equity_change.png"
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
                    size=self.CHART_FONT_SIZE,
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
        if self.show if show is None else show:
            fig.show(renderer="browser")

    def save_report(self, df: pd.DataFrame, file_name: str = "") -> None:
        """儲存回測報告"""
        if not file_name:
            raise ValueError("file_name 不能是空字串")

        # 決定輸出路徑
        if self.output_dir is not None:
            save_path: Path = self.output_dir / file_name
        else:
            save_path: Path = Path(file_name)

        # 確保資料夾存在
        save_path.parent.mkdir(parents=True, exist_ok=True)

        # 輸出 CSV 檔案
        df.to_csv(save_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"* Report saved to: {save_path}")

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
        if self.output_dir is not None:
            save_path: Path = self.output_dir / file_name
        else:
            save_path: Path = Path(file_name)

        # 確保資料夾存在
        save_path.parent.mkdir(parents=True, exist_ok=True)

        # 輸出圖片
        fig.write_image(str(save_path))
        logger.info(f"* Figure saved to: {save_path}")
