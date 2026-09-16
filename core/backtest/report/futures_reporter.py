import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.backtest.datafeed.tw.futures_roll import FuturesRollPlanner
from core.backtest.report.reporter import StockBacktestReporter
from core.config.schema import FuturesPriceColumn
from core.models.futures.record import FuturesTradeRecord
from core.strategies.futures import BaseFuturesStrategy
from core.utils import FuturesSession

"""FuturesBacktestReporter: 台期貨回測報表（交易明細與績效圖表）"""


class FuturesBacktestReporter(StockBacktestReporter):
    """
    期貨回測報表

    **為什麼繼承 `StockBacktestReporter`**：四張圖（資金曲線、對標曲線、MDD、
    每日損益）與權益口徑的判斷（`get_equity_series()`）本來就與商品類別無關，
    複製一份四百行繪圖程式碼只會讓兩邊各自漂移。真正與商品有關的只有三件事，
    本類別逐一覆寫：**交易明細欄位**、**多空統計欄位**、**對標的標的**。

    > 待美股加入時應把繪圖抽到 `BaseBacktestReporter`，屆時 baseline 本來就要
    > 重產。在那之前
    > 動它會讓台股報表跟著改，不划算。

    **對標序列是「近月拼接」，不是連續合約**：每個交易日取該商品最近到期月的
    收盤價接起來，換月當天會有一段展期價差造成的假跳空。真正的連續合約在
    `futures_continuous`；這條線只能當粗略參考，不可拿來算精確的對標報酬。
    """

    def __init__(
        self,
        strategy: BaseFuturesStrategy,
        output_dir: Optional[Path] = None,
        price: Optional[Any] = None,
        show: Optional[bool] = None,
    ) -> None:
        # 對標商品：策略交易的第一個商品（多商品策略以第一個為代表）
        self.benchmark_product: str = (
            strategy.products[0] if getattr(strategy, "products", None) else "TX"
        )
        # 對標序列一律取**日盤**：`COMBINED` 不是資料表裡的值（見 `FuturesSession`），
        # 直接拿去查會回空表，圖上只會出現一行「benchmark 數據異常」的警告
        session: FuturesSession = getattr(strategy, "session", FuturesSession.DAY)
        self.benchmark_session: FuturesSession = (
            FuturesSession.DAY if session == FuturesSession.COMBINED else session
        )

        # `price` 由 Backtester 統一傳入；期貨報表不查台股資料庫，
        # 收下只是為了讓兩個 reporter 的建構簽章一致
        super().__init__(strategy, output_dir, price=price, show=show)

    def setup(self) -> None:
        """建立對標序列（近月拼接的收盤價）；本 reporter 不碰台股資料庫"""

        self.benchmark: str = self.benchmark_product
        self.price = None  # 期貨報表不使用 StockPriceAPI

        futures_price: FuturesPriceAPI = FuturesPriceAPI()
        try:
            self.benchmark_price: pd.Series = self.build_near_month_close_series(
                futures_price
            )
        finally:
            futures_price.close()

    def build_near_month_close_series(
        self, futures_price: FuturesPriceAPI
    ) -> pd.Series:
        """
        - Description:
            建立對標序列：每個交易日取**最近到期月**的收盤價

            換月接點會有展期價差造成的假跳空，見 class docstring。
        - Parameters:
            - futures_price: FuturesPriceAPI
                行情 API
        - Return:
            - pd.Series
                index 為 `datetime.date`、值為收盤價；查無資料時為空 Series
        """

        df: pd.DataFrame = futures_price.get_range(
            self.start_date,
            self.end_date,
            product=self.benchmark_product,
            session=self.benchmark_session,
        )

        if df.empty:
            logger.warning(
                f"[Futures Report] 查無 {self.benchmark_product} 於 "
                f"{self.start_date} ~ {self.end_date} 的行情，本次不繪製對標曲線"
            )
            return pd.Series(dtype=float)

        # **先濾掉週契約**：`expiry` 可能是 `YYYYMM` 或 `YYYYMMWn`，
        # 字典序下 `202401W5` < `202402`，於是一月的週契約會贏過二月的月契約——
        # 一月月契約到期之後，近月序列會黏在快到期的週契約上。
        # 判準沿用 `FuturesRollPlanner.MONTHLY_EXPIRY_PATTERN`，與換月規則同一份。
        monthly: pd.DataFrame = df[
            df["expiry"]
            .astype(str)
            .str.match(FuturesRollPlanner.MONTHLY_EXPIRY_PATTERN)
        ]
        if monthly.empty:
            logger.warning(
                f"[Futures Report] {self.benchmark_product} 區間內只有週契約，"
                f"本次不繪製對標曲線"
            )
            return pd.Series(dtype=float)

        # 同一天多個到期月：字典序即時間序，取最小者為近月
        near_month: pd.DataFrame = monthly.sort_values(
            ["date", "expiry"]
        ).drop_duplicates(subset="date", keep="first")

        series: pd.Series = near_month[FuturesPriceColumn.CLOSE.value].astype(float)
        series.index = pd.to_datetime(near_month["date"]).dt.date
        return series

    def _get_adjusted_price(self, price_series: pd.Series, stock_id: str) -> pd.Series:
        """期貨沒有股票分割，對標價格原樣回傳（覆寫台股的分割調整）"""

        return price_series

    def generate_trading_report(self) -> pd.DataFrame:
        """
        - Description:
            生成期貨交易明細

            與台股報表的三個欄位差異：

            1. 識別欄是 **Contract ID**（`{product}{expiry}`）並拆出 Product／Expiry
               ——同一商品的不同到期月是不同契約，混在一欄看不出換月。
            2. 多了 **Multiplier**／**Margin**／**Settled PnL**：口數乘上乘數才是
               契約價值，而 `Realized PnL` 已包含逐日盯市各段（`Settled PnL`），
               只看進出場價會對不上。
            3. **沒有** Borrow Fee／Interest／Dividend Compensation／Short Method
               ——期貨賣出開倉就是放空，沒有這一整組信用交易欄位。
        - Return:
            - df: pd.DataFrame
                交易明細
        """

        report_columns: List[str] = [
            "Contract ID",
            "Product",
            "Expiry",
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
            "Multiplier",
            "Margin",
            "Commission",
            "Tax",
            "Transaction Cost",
            "Settled PnL",
            "Holding Days",
            "Realized PnL",
            "ROI",
            "Cumulative PnL",
            "Cumulative Balance",
        ]

        cumulative_pnl: float = 0.0
        cumulative_balance: float = self.account.init_capital

        # 排序規則與台股相同：以平倉日為主鍵、原始順序為次鍵
        # （SHORT 的 sell_date 是開倉日，時間軸一律用 exit_date）
        closed_records: List[FuturesTradeRecord] = [
            record
            for _, record in sorted(
                (
                    (index, record)
                    for index, record in enumerate(self.account.trade_records)
                    if record.is_closed
                ),
                key=lambda item: (
                    item[1].exit_date if item[1].exit_date else datetime.date.min,
                    item[0],
                ),
            )
        ]

        rows: List[Dict[str, Any]] = []
        for record in closed_records:
            cumulative_pnl += record.realized_pnl
            cumulative_balance += record.realized_pnl

            rows.append(
                {
                    "Contract ID": record.contract_id,
                    "Product": record.product,
                    "Expiry": record.expiry,
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
                    "Multiplier": record.multiplier,
                    "Margin": record.margin,
                    "Commission": record.commission,
                    "Tax": record.tax,
                    "Transaction Cost": record.transaction_cost,
                    "Settled PnL": record.settled_pnl,
                    "Holding Days": record.holding_days,
                    "Realized PnL": record.realized_pnl,
                    "ROI": record.roi,
                    "Cumulative PnL": cumulative_pnl,
                    "Cumulative Balance": cumulative_balance,
                }
            )

        df: pd.DataFrame = pd.DataFrame(rows, columns=report_columns)
        self.save_report(df, f"{self.strategy.strategy_name}_trading_report.csv")
        return df

    def generate_direction_summary(self) -> pd.DataFrame:
        """
        多空分開的績效統計（期貨口徑）

        **口數與保證金要分開看**：期貨的獲利能力與資金佔用由保證金決定，
        故列出 Total Lots 與 Total Margin，而非台股的借券費與利息。
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
                    "Total Lots": int(group["Buy Volume"].sum()),
                    "Win Rate (%)": round(len(wins) / len(group) * 100, 2),
                    "Total PnL": round(group["Realized PnL"].sum(), 2),
                    "Avg PnL": round(group["Realized PnL"].mean(), 2),
                    "Avg ROI (%)": round(group["ROI"].mean(), 2),
                    "Total Margin": round(group["Margin"].sum(), 2),
                    "Total Commission": round(group["Commission"].sum(), 2),
                    "Total Tax": round(group["Tax"].sum(), 2),
                    "Avg Holding Days": round(group["Holding Days"].mean(), 2),
                }
            )

        df: pd.DataFrame = pd.DataFrame(rows)
        self.save_report(df, f"{self.strategy.strategy_name}_direction_summary.csv")
        return df
