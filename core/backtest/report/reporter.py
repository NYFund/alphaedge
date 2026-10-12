from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from core.api.tw.stock_price_api import StockPriceAPI
from core.backtest.report.base import BaseBacktestReporter
from core.models.stock.record import StockTradeRecord
from core.strategies.stock import BaseStockStrategy

"""台股回測報表：對標 0050 的還原收盤價，交易明細含信用交易欄位"""


class StockBacktestReporter(BaseBacktestReporter):
    """
    台股回測報表

    與基底的差異只有三件事：對標讀 `0050` 的**還原**收盤價、交易明細多了
    借券費／利息／股利補償／融券方式這一組信用交易欄位、多空統計列出這些成本的合計。
    """

    # 對標標的
    benchmark: str = "0050"

    # 識別欄名 `Symbol` 與領域模型一致（`record.symbol`）；期貨那份是 `Contract ID`——
    # 它是 `{商品}{到期月}`，與「一檔股票」不是同一種東西，且前端以它判斷報表型別
    TRADING_REPORT_COLUMNS: Tuple[str, ...] = (
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
    )

    def __init__(
        self,
        strategy: BaseStockStrategy,
        output_dir: Optional[Path] = None,
        price: Optional[StockPriceAPI] = None,
        show: Optional[bool] = None,
    ) -> None:
        # `price` 由 Backtester 傳入 DataFeed 已開好的連線；未指定時
        # `load_benchmark_series()` 自行建立，並由 `close()` 負責關掉
        super().__init__(strategy, output_dir, price=price, show=show)

    def load_benchmark_series(self) -> pd.Series:
        """
        - Description:
            取 benchmark 的**還原**收盤價

            **benchmark 必須用還原價**：原始收盤價在除權息日有跳空，
            0050 這種年年配息的標的，用原始價當基準等於讓基準每年少賺一次配息，
            策略看起來永遠贏得比實際多。

            分割與減資已由 `corporate_action` 併入還原係數，
            `get_adjusted_close_series()` 一次處理完，之後不可再套一次分割調整——
            重複調整實測會讓 0050 的期間報酬從 1.82% 變成 303%。
        - Return:
            - pd.Series
                index 為 `datetime.date`、值為還原收盤價；查無資料時為空 Series
        """

        # 由呼叫端注入時共用同一條連線，`close()` 不會關掉別人的
        if self.price is None:
            self.price = StockPriceAPI()

        series: pd.Series = self.price.get_adjusted_close_series(
            stock_id=self.benchmark,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        if not series.empty:
            series.index = pd.to_datetime(series.index).date
        return series

    def build_trade_row(self, record: StockTradeRecord) -> Dict[str, Any]:
        """一筆已平倉紀錄 → 交易明細的一列（累計兩欄由基底補上）"""

        return {
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
            "Short Method": (record.short_method.value if record.short_method else ""),
            "Realized PnL": record.realized_pnl,
            "ROI": record.roi,
            "ROI on Capital": record.roi_on_capital,
        }

    def build_direction_row(
        self, position_type: str, group: pd.DataFrame
    ) -> Dict[str, Any]:
        """同一個方向的多空統計；列出信用交易的各項成本合計"""

        wins: pd.DataFrame = group[group["Realized PnL"] > 0]

        return {
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
