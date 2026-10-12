import datetime
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
from loguru import logger

from core.analysis.performance_metrics import (
    TRADING_DAYS_PER_YEAR,
    compute_annualized_information_ratio,
    compute_annualized_sharpe,
    compute_annualized_sortino,
    compute_annualized_volatility,
    compute_max_drawdown,
    compute_period_returns,
    compute_profit_factor,
    compute_win_loss_ratio,
)
from core.backtest.report.plotting import EquityChartRenderer
from core.config import resolve_show_figures
from core.models import BaseAccount, BaseTradeRecord
from core.strategies.base import BaseStrategy
from core.utils import FileEncoding

"""
回測報表的共用骨架：交易明細、多空統計、事件計數、績效指標、權益口徑、繪圖委派與存檔

**與市場有關的只有四件事**，由子類提供：
- 對標序列怎麼取：`load_benchmark_series()`（台股讀 0050 還原價，期貨讀連續合約）。
- 交易明細有哪些欄位：`TRADING_REPORT_COLUMNS` ＋ `build_trade_row()`。
- 多空統計有哪些欄位：`build_direction_row()`。
- 三個口徑註腳：`get_avg_roi_note()`、`get_benchmark_block_reason()`、`get_benchmark_note()`，
  預設空字串。

其餘一律放在這裡。期貨報表原本繼承台股報表，交易明細的排序與累計迴圈兩邊各抄一份，
在一邊修了排序鍵另一邊不會跟著修，而症狀是累積損益的加總順序不同、圖上取到的最後一筆
不同——不會有任何錯誤。

**前端以識別欄名判斷報表型別**（台股 `Symbol`、期貨 `Contract ID`），欄名與順序由子類的
欄位清單鎖住，基底只在每一列尾端補上累計損益與累計餘額。
"""


class BaseBacktestReporter(ABC):
    """回測報表的共用骨架；市場差異見模組說明與各子類"""

    CHART_FONT_SIZE: int = 15

    # 權益曲線的兩種口徑，標註在圖上避免不同期報表被混著看
    EQUITY_BASIS_MARK_TO_MARKET: str = "Mark-to-market"  # 逐日盯市（含未實現損益）
    EQUITY_BASIS_REALIZED_ONLY: str = "Realized only"  # 只認已實現損益（MDD 會被低估）

    # 指標報表的欄位；**長表**而非寬表，新增指標不必改欄位結構
    METRICS_COLUMNS: List[str] = ["Metric", "Value", "Note"]

    # 交易明細的欄位與順序；子類必填，且要以 `CUMULATIVE_COLUMNS` 收尾
    TRADING_REPORT_COLUMNS: Tuple[str, ...] = ()
    # 由基底在每一列尾端補上的累計欄位
    CUMULATIVE_COLUMNS: Tuple[str, str] = ("Cumulative PnL", "Cumulative Balance")

    # 對標標的（台股為代號、期貨為商品）；子類以類別屬性宣告，
    # 或在呼叫 `super().__init__()` 之前設好
    benchmark: str = ""

    def __init__(
        self,
        strategy: BaseStrategy,
        output_dir: Optional[Path] = None,
        price: Optional[Any] = None,
        show: Optional[bool] = None,
    ) -> None:
        # 欄位清單少填或漏掉累計兩欄時，交易明細會變成一張沒有欄位的空表而不是錯誤；
        # 在建構時就擋下，不等到報表產出
        if tuple(self.TRADING_REPORT_COLUMNS[-2:]) != self.CUMULATIVE_COLUMNS:
            raise TypeError(
                f"{type(self).__name__}.TRADING_REPORT_COLUMNS 必須以 "
                f"{self.CUMULATIVE_COLUMNS} 收尾"
            )

        self.strategy: BaseStrategy = strategy
        self.account: BaseAccount = self.strategy.account
        self.output_dir: Optional[Path] = output_dir

        # 取對標序列用的資料 API。由呼叫端注入時共用同一條連線，`close()` 不會關掉別人的；
        # 自建的由 `close()` 負責關掉。用不到的市場（期貨讀自己的連續合約表）維持 None
        self.price: Optional[Any] = price

        # 畫完是否在瀏覽器開圖。預設交由環境變數決定：一次回測會產生五張圖，
        # 批次跑參數掃描時無條件開圖等於一次彈出幾十個分頁
        self.show: bool = resolve_show_figures() if show is None else show

        self.start_date: datetime.date = self.strategy.start_date
        self.end_date: datetime.date = self.strategy.end_date

        # 起始前一天，用來當作初始資金節點
        self.origin_date: datetime.date = self.start_date - datetime.timedelta(days=1)

        self.trading_report: Optional[pd.DataFrame] = None  # 交易明細表
        self.benchmark_price: pd.Series = pd.Series(dtype=float)

        # 逐日權益（含未實現損益）；由 `Backtester.generate_backtest_report()` 注入。
        # 只認已實現損益的曲線會把持倉期間的逆勢整段抹平，MDD 因此被低估
        self.daily_equity: Optional[List[Dict[str, Any]]] = None

        # 繪圖交給渲染器；報表端只負責備資料
        self.renderer: EquityChartRenderer = EquityChartRenderer(self)

        self.setup()

    def setup(self) -> None:
        """取對標序列；怎麼取由子類的 `load_benchmark_series()` 決定"""

        self.benchmark_price = self.load_benchmark_series()

    @abstractmethod
    def load_benchmark_series(self) -> pd.Series:
        """
        - Description:
            取對標的價格序列

            **序列必須是可以直接比較的口徑**：台股要用還原價（原始價在除權息日有跳空），
            期貨要用換月價差已調整掉的連續合約。渲染器拿到就直接畫，不再做任何調整。
        - Return:
            - pd.Series
                index 為 `datetime.date`、值為價格；查無資料時為空 Series
        """

    def close(self) -> None:
        """
        關閉 reporter 自己開的資料連線

        不關的話，每跑一次回測就多一條不再使用的 SQLite 連線；
        由呼叫端注入的連線不歸 reporter 關（`StockPriceAPI` 的 `owns_conn` 語意）。
        """

        if self.price is not None:
            self.price.close()

    # === 交易明細 ===
    @abstractmethod
    def build_trade_row(self, record: BaseTradeRecord) -> Dict[str, Any]:
        """
        - Description:
            把一筆已平倉紀錄轉成交易明細的一列（不含累計兩欄，由基底補上）
        - Parameters:
            - record: BaseTradeRecord
                已平倉的交易紀錄
        - Return:
            - Dict[str, Any]
                欄名 → 值；欄名要出現在 `TRADING_REPORT_COLUMNS`
        """

    def sorted_closed_records(self) -> List[BaseTradeRecord]:
        """
        - Description:
            只取已平倉紀錄（未平倉沒有完整的買賣資訊），並依「平倉順序」排序

            累積損益與餘額的加總順序、以及繪圖時 `groupby().last()` 取到的那一筆，
            都依賴這個順序正確。
            1. 主鍵 `exit_date`：SHORT 的 `sell_date` 是開倉日，不可拿來當平倉日。
            2. 次鍵為原始索引：`trade_records` 依平倉順序附加，而 `id` 依開倉順序
               生成，用索引才能讓同一天的多筆維持實際平倉先後（TICK 回測常見）。
        - Return:
            - List[BaseTradeRecord]
                依平倉順序排好的已平倉紀錄
        """

        closed_records_with_index: List[Tuple[int, BaseTradeRecord]] = [
            (index, record)
            for index, record in enumerate(self.account.trade_records)
            if record.is_closed
        ]
        return [
            record
            for _, record in sorted(
                closed_records_with_index,
                key=lambda item: (
                    item[1].exit_date if item[1].exit_date else datetime.date.min,
                    item[0],
                ),
            )
        ]

    def generate_trading_report(self) -> pd.DataFrame:
        """生成交易明細（`<策略>_trading_report.csv`）；欄位與順序由 `TRADING_REPORT_COLUMNS` 鎖住"""

        cumulative_pnl: float = 0.0
        cumulative_balance: float = self.account.init_capital

        rows: List[Dict[str, Any]] = []
        for record in self.sorted_closed_records():
            cumulative_pnl += record.realized_pnl
            cumulative_balance += record.realized_pnl

            row: Dict[str, Any] = self.build_trade_row(record)
            row[self.CUMULATIVE_COLUMNS[0]] = cumulative_pnl
            row[self.CUMULATIVE_COLUMNS[1]] = cumulative_balance
            rows.append(row)

        df: pd.DataFrame = pd.DataFrame(rows, columns=list(self.TRADING_REPORT_COLUMNS))
        self.save_report(df, f"{self.strategy.strategy_name}_trading_report.csv")
        return df

    # === 多空統計 ===
    @abstractmethod
    def build_direction_row(
        self, position_type: str, group: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        - Description:
            把同一個方向的交易明細彙整成多空統計的一列；欄位順序即報表順序
        - Parameters:
            - position_type: str
                方向（`Position Type` 欄的值）
            - group: pd.DataFrame
                該方向的交易明細
        - Return:
            - Dict[str, Any]
                欄名 → 值
        """

    def generate_direction_summary(self) -> pd.DataFrame:
        """
        - Description:
            產生多空分開的績效統計

            多空的成本結構與風險型態完全不同（放空有借券費、保證金與無限虧損風險），
            混在同一組數字裡會看不出策略到底靠哪一邊賺錢。
        - Return:
            - df: pd.DataFrame
                以 Position Type 分組的統計表；沒有已平倉交易時為空表
        """

        if self.trading_report is None or self.trading_report.empty:
            return pd.DataFrame()

        rows: List[Dict[str, Any]] = [
            self.build_direction_row(position_type, group)
            for position_type, group in self.trading_report.groupby("Position Type")
        ]

        df: pd.DataFrame = pd.DataFrame(rows)
        self.save_report(df, f"{self.strategy.strategy_name}_direction_summary.csv")
        return df

    # === 事件計數 ===
    def generate_event_report(self, event_counts: Dict[str, int]) -> pd.DataFrame:
        """
        - Description:
            輸出回測期間的事件計數（強制回補、斷頭、拒單等）

            這些是放空策略的尾部風險，被平均進總績效就看不見了，必須單獨列出。
            列出哪些 key 由 factory 建立計數器時依市場決定。
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

    # === 績效指標 ===
    def generate_metrics_summary(self) -> pd.DataFrame:
        """
        - Description:
            輸出整體績效指標（`<策略>_metrics_summary.csv`）

            **不開前端也看得到，且只有一份計算**：Sharpe／Sortino／MDD 一律呼叫
            `core/analysis/performance_metrics.py` 的純函式，
            不在報表與前端各寫一份——兩份實作必然漂移。

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
            *self.build_slippage_metrics(sum(pnls)),
        ]

    def build_slippage_metrics(self, total_pnl: float) -> List[Dict[str, Any]]:
        """
        - Description:
            滑價吃掉的價差總額與它佔損益的比例

            **單獨列出金額而非只標示有無開滑價**：一支策略的績效若有三成被滑價
            吃掉，調參數的人必須看得到這個量級才判斷得出這組假設的影響。

            **不併進交易成本**：滑價是內含在成交價裡的，損益早就反映了它，
            加進 `total_transaction_cost` 等於重複計算（見
            `BaseAccount.total_slippage_cost`）。

            分母取 `|Total PnL|`：虧損的策略也該看得到比例，而負數分母會讓
            「滑價佔比」出現看不懂的負號。
        - Parameters:
            - total_pnl: float
                已實現損益總額
        - Return:
            - List[Dict[str, Any]]
                `Slippage Cost` 與 `Slippage Cost / |Total PnL| (%)` 兩列
        """

        cost: float = round(getattr(self.account, "total_slippage_cost", 0.0), 2)
        share: Optional[float] = (
            round(cost / abs(total_pnl) * 100, 2) if total_pnl else None
        )

        return [
            {
                "Metric": "Slippage Cost",
                "Value": cost,
                "Note": "策略委託、強制出場與換月轉倉的價差總額；**不計入交易成本**"
                "（已內含在成交價裡）",
            },
            {
                "Metric": "Slippage Cost / |Total PnL| (%)",
                "Value": share,
                "Note": "損益為 0 時留空；分母取絕對值，虧損策略同樣看得到比例",
            },
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

    # === 口徑註腳（市場差異的 hook；預設不標示） ===
    def get_benchmark_block_reason(self) -> str:
        """
        對標序列不可信時回傳原因字串（IR 因此留空）；預設一律可信

        期貨覆寫它：對標退回近月拼接時，換月接點有展期價差造成的假跳空，
        那幾天的基準日報酬是假的，算出來的 IR 會被那幾天帶偏。
        """

        return ""

    def get_avg_roi_note(self) -> str:
        """`Avg ROI` 的口徑說明；台股是名目報酬率不另標示，期貨覆寫為保證金報酬率"""

        return ""

    def get_benchmark_note(self) -> str:
        """
        對標序列的口徯註腳；只有一種口徑的市場不標示

        期貨覆寫它：連續合約與近月拼接是兩種口徑，圖上不標的話，
        換月接點有沒有假跳空完全看不出來。
        """

        return ""

    # === 權益序列 ===
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

    # === 繪圖：委派給 `EquityChartRenderer` ===
    def plot_balance_curve(self) -> None:
        """繪製總資金曲線圖"""

        self.renderer.plot_balance_curve()

    def plot_balance_and_benchmark_curve(self) -> None:
        """繪製策略與對標的淨值曲線"""

        self.renderer.plot_balance_and_benchmark_curve()

    def plot_balance_mdd(self) -> None:
        """繪製最大回撤圖"""

        self.renderer.plot_balance_mdd()

    def plot_everyday_profit(self) -> None:
        """繪製每日損益圖"""

        self.renderer.plot_everyday_profit()

    def plot_everyday_equity_change(self) -> None:
        """繪製每日權益變化圖"""

        self.renderer.plot_everyday_equity_change()

    def set_figure_config(self, fig: "go.Figure", **kwargs: object) -> None:
        """統一圖表樣式；實作在 `EquityChartRenderer`"""

        self.renderer.set_figure_config(fig, **kwargs)

    def save_figure(self, fig: "go.Figure", file_name: str = "") -> None:
        """輸出圖檔；實作在 `EquityChartRenderer`"""

        self.renderer.save_figure(fig, file_name)

    # === 存檔 ===
    def save_report(self, df: pd.DataFrame, file_name: str = "") -> None:
        """儲存回測報告"""

        if not file_name:
            raise ValueError("file_name 不能是空字串")

        if self.output_dir is not None:
            save_path: Path = self.output_dir / file_name
        else:
            save_path: Path = Path(file_name)

        save_path.parent.mkdir(parents=True, exist_ok=True)

        df.to_csv(save_path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"* Report saved to: {save_path}")
