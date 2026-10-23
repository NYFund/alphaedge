from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from loguru import logger

from core.api.tw.futures_continuous_api import FuturesContinuousAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.backtest.report.base import BaseBacktestReporter
from core.config.schema import FuturesPriceColumn
from core.market.tw.futures_roll import FuturesRollPlanner
from core.models.futures.record import FuturesTradeRecord
from core.strategies.futures import BaseFuturesStrategy
from core.utils import FuturesRollRule, FuturesSession

"""FuturesBacktestReporter: 台期貨回測報表（交易明細與績效圖表）"""


class FuturesBacktestReporter(BaseBacktestReporter):
    """
    期貨回測報表

    與基底的差異只有三件事：**對標序列**（連續合約）、**交易明細欄位**
    （`Contract ID`、乘數、保證金、逐日結算損益）、**多空統計欄位**（口數與保證金）。
    四張圖與權益口徑的判斷與商品類別無關，一律沿用基底。

    **對標序列優先讀連續合約**（`futures_continuous`，`BACKWARD` 調整、換月規則
    對齊策略的 `roll_config.rule`）：換月價差已經調整掉，接點沒有假跳空，
    對標與策略實際轉倉的時點也一致。

    `futures_continuous` 查不到該商品、該區間或該組設定時，**退回近月拼接**
    （每個交易日取最近到期月的收盤價）並記 warning——那條線在換月當天有展期
    價差造成的假跳空，只能當粗略參考。圖表註腳會標明實際採用的是哪一種，
    **兩種口徑的曲線不可混著看**。
    """

    # 圖表註腳用的序列種類標示
    CONTINUOUS_SERIES_LABEL: str = "連續合約"
    NEAR_MONTH_SERIES_LABEL: str = "近月拼接"

    # 與台股報表的三個欄位差異：
    # 1. 識別欄是 **Contract ID**（`{product}{expiry}`）並拆出 Product／Expiry
    #    ——同一商品的不同到期月是不同契約，混在一欄看不出換月；前端以它判斷報表型別。
    # 2. 多了 **Multiplier**／**Margin**／**Settled PnL**：口數乘上乘數才是契約價值，
    #    而 `Realized PnL` 已包含逐日盯市各段（`Settled PnL`），只看進出場價會對不上。
    # 3. **沒有** Borrow Fee／Interest／Dividend Compensation／Short Method
    #    ——期貨賣出開倉就是放空，沒有這一整組信用交易欄位。
    TRADING_REPORT_COLUMNS: Tuple[str, ...] = (
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
    )

    def __init__(
        self,
        strategy: BaseFuturesStrategy,
        output_dir: Optional[Path] = None,
        price: Optional[Any] = None,
        show: Optional[bool] = None,
    ) -> None:
        # 對標的換月規則與策略實際轉倉共用同一份設定：不一致的話，
        # 兩條曲線在換月那幾天比的是不同的東西
        roll_config = getattr(strategy, "roll_config", None)
        self.benchmark_roll_rule: FuturesRollRule = getattr(
            roll_config, "rule", FuturesRollRule.LAST_TRADING_DAY
        )

        # 對標商品：策略交易的第一個商品（多商品策略以第一個為代表）
        self.benchmark_product: str = (
            strategy.products[0] if getattr(strategy, "products", None) else "TX"
        )
        self.benchmark: str = self.benchmark_product

        # 對標序列一律取**日盤**：`COMBINED` 不是資料表裡的值（見 `FuturesSession`），
        # 直接拿去查會回空表，圖上只會出現一行「benchmark 數據異常」的警告
        session: FuturesSession = getattr(strategy, "session", FuturesSession.DAY)
        self.benchmark_session: FuturesSession = (
            FuturesSession.DAY if session == FuturesSession.COMBINED else session
        )

        # 實際採用的序列種類，供圖表註腳標示（兩種口徑不可混著看）
        self.benchmark_series_kind: str = self.CONTINUOUS_SERIES_LABEL

        # `price` 由 Backtester 統一傳入；期貨報表不查台股資料庫，收下只是為了讓
        # 兩個 reporter 的建構簽章一致，一律不持有，`close()` 因此沒有東西要關
        super().__init__(strategy, output_dir, price=None, show=show)

    def load_benchmark_series(self) -> pd.Series:
        """建立對標序列：連續合約優先，查不到才退回近月拼接"""

        self.benchmark_series_kind = self.CONTINUOUS_SERIES_LABEL
        series: pd.Series = self.build_continuous_close_series()
        if not series.empty:
            return series

        self.benchmark_series_kind = self.NEAR_MONTH_SERIES_LABEL
        futures_price: FuturesPriceAPI = FuturesPriceAPI()
        try:
            return self.build_near_month_close_series(futures_price)
        finally:
            futures_price.close()

    def build_continuous_close_series(self) -> pd.Series:
        """
        - Description:
            讀 `futures_continuous` 的收盤價序列（`BACKWARD` 調整）

            **換月規則對齊策略自己的 `roll_config.rule`**：對標與策略實際轉倉的
            時點不一致的話，兩條曲線在換月那幾天比的是不同的東西。

            這是衍生表，`--target futures_continuous` 沒跑過、或那一組
            （商品，調整方式，換月規則）沒建過就會是空的——此時回空 Series，
            由 `load_benchmark_series()` 決定退回近月拼接，**不在此自行換一組設定**。
        - Return:
            - pd.Series
                index 為交易日、值為調整後收盤價；查無資料時為空 Series
        """

        api: FuturesContinuousAPI = FuturesContinuousAPI()
        try:
            series: pd.Series = api.get_close_series(
                self.benchmark_product,
                self.start_date,
                self.end_date,
                session=self.benchmark_session,
                roll_rule=self.benchmark_roll_rule,
            )
        finally:
            api.close()

        if series.empty:
            logger.warning(
                f"[Futures Report] futures_continuous 查無 "
                f"{self.benchmark_product}／{self.benchmark_session.value}／"
                f"{self.benchmark_roll_rule.value} 於 {self.start_date} ~ "
                f"{self.end_date} 的序列，對標改用近月拼接"
                f"（換月接點有展期價差造成的假跳空，只能當粗略參考）。"
                f"要用連續合約請先跑 `--target futures_continuous` 建出該組設定"
            )

        return series

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

    def get_benchmark_block_reason(self) -> str:
        """
        對標退回近月拼接時不算 IR

        近月拼接在換月當天有展期價差造成的**假跳空**，那幾天的基準日報酬是假的，
        而 IR 是逐日相減算出來的——被那幾天帶偏之後，數字看起來合理但沒有意義。
        連續合約已經把價差調整掉，那條路徑照算。
        """

        if self.benchmark_series_kind == self.CONTINUOUS_SERIES_LABEL:
            return ""

        return (
            f"對標為{self.NEAR_MONTH_SERIES_LABEL}，換月接點的基準日報酬含展期"
            f"假跳空，IR 會被帶偏；請先跑 `--target futures_continuous` 建出"
            f"{self.benchmark_roll_rule.value} 的連續合約"
        )

    def get_avg_roi_note(self) -> str:
        """期貨的 ROI 分母是**保證金**，與台股的名目報酬率不可混讀"""

        return "保證金報酬率（分母為已繳保證金，非契約價值）"

    def get_benchmark_note(self) -> str:
        """標明對標曲線用的是連續合約還是近月拼接——兩種口徑不可混著看"""

        if self.benchmark_series_kind == self.CONTINUOUS_SERIES_LABEL:
            return (
                f"{self.CONTINUOUS_SERIES_LABEL}"
                f"（BACKWARD／{self.benchmark_roll_rule.value}）"
            )

        return f"{self.NEAR_MONTH_SERIES_LABEL}（換月接點有展期價差造成的假跳空）"

    def build_trade_row(self, record: FuturesTradeRecord) -> Dict[str, Any]:
        """一筆已平倉紀錄 → 交易明細的一列（累計兩欄由基底補上）"""

        return {
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
        }

    def build_direction_row(
        self, position_type: str, group: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        同一個方向的多空統計（期貨口徑）

        **口數與保證金要分開看**：期貨的獲利能力與資金佔用由保證金決定，
        故列出 Total Lots 與 Total Margin，而非台股的借券費與利息。
        """

        wins: pd.DataFrame = group[group["Realized PnL"] > 0]

        return {
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
