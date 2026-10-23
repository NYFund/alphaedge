import datetime
from typing import Callable, Optional, Union

from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.utils import TimeUtils

"""期貨保證金模型：每口原始／維持保證金只有這一份查表（對應 NautilusTrader 的 MarginModel）"""


class FuturesMarginModel:
    """
    期貨保證金模型

    **查表只有這一份**：部位管理層算應繳保證金、部位建構層算可開口數、實盤送單前的保證金
    檢查都用同一個實例。原本兩份查表的判準不同——部位管理層依序查金額表、比例表（個股期貨），
    都查不到才拋錯；部位建構層只查金額表、查不到就回 0——結果是查表模式下個股期貨的 sizing
    永遠 0 口，部位管理層卻開得進去，而且不會有任何錯誤訊息。

    查表模式下**有兩張表，分表依據是「金額 vs 比例」而不是「指數 vs 股票」**：
    指數期貨與 ETF 期貨給每口固定金額（`futures_margin_history`），個股期貨給適用比例
    ＋ 級距（`stock_futures_margin_rate_history`），算式需要標的股價，由注入的 resolver
    提供（它在 `tw_stock.db`，跨庫取值屬 DataFeed 的職責）。

    `config.api` 由 DataFeed 在 `setup()` 時注入同一個設定物件，本模型每次呼叫才讀它，
    不在建構時複製。
    """

    def __init__(
        self,
        config: Optional[FuturesMarginConfig] = None,
        underlying_price_resolver: Optional[
            Callable[[str, datetime.date], Optional[float]]
        ] = None,
    ) -> None:
        self.config: FuturesMarginConfig = config or FuturesMarginConfig.default()
        # 股期保證金要用標的證券的價格；未注入時比例表那條路走不通（回 None）
        self.underlying_price_resolver: Optional[
            Callable[[str, datetime.date], Optional[float]]
        ] = underlying_price_resolver

    def initial_margin_per_lot(
        self,
        product: Optional[str],
        date: Optional[Union[datetime.date, datetime.datetime]],
        price: float,
        multiplier: int,
    ) -> float:
        """
        - Description:
            每口原始保證金

            比率模式（沒有 API）：契約價值 × 比率。查表模式：先查金額表，查不到再走比例表；
            兩者皆無就拋錯——**刻意不退回近似值**：靜默套一個比率會讓資金效率與可開口數
            整段偏掉卻毫無徵兆，中斷比靜默錯誤好查。
        - Parameters:
            - product: Optional[str]
                商品代碼；查表模式必填
            - date: Optional[Union[datetime.date, datetime.datetime]]
                交易日；查表模式必填（保證金隨日期變動）
            - price / multiplier: float, int
                成交價與契約乘數；比率模式用它們算契約價值
        - Return:
            - float
                每口原始保證金
        - Raise:
            - ValueError
                查表模式下沒給商品或日期，或查不到該商品在該日的保證金
        """

        if self.config.api is None:
            return price * multiplier * self.config.initial_margin_ratio

        if product is None or date is None:
            raise ValueError("查表模式必須提供 product 與 date——保證金隨商品與日期變動")

        query_date: datetime.date = TimeUtils.to_date(date)
        per_lot: Optional[float] = self.config.api.get_initial_margin(
            product, query_date, fallback_to_earliest=self.config.fallback_to_earliest
        )
        if per_lot is None:
            per_lot = self.stock_futures_margin_per_lot(product, query_date)

        if per_lot is None:
            covered = self.config.api.get_covered_date_range(product)
            raise ValueError(
                f"查無 {product} 在 {query_date} 生效的保證金"
                f"（金額表涵蓋 {covered}，比例表也查不到）。"
                f"**刻意不退回近似值**：靜默套一個比率會讓資金效率與可開口數"
                f"整段偏掉卻毫無徵兆。保證金資料只回溯到 2020-03，"
                f"要回測更早的期間請明確改用 `FuturesMarginConfig.ratio()`"
            )

        return float(per_lot)

    def maintenance_margin_per_lot(
        self, product: str, date: Union[datetime.date, datetime.datetime]
    ) -> Optional[float]:
        """
        每口維持保證金；比率模式或查不到時為 None（呼叫端退回已繳原始保證金當門檻，偏嚴格）

        **維持保證金與原始保證金是兩個獨立的公告值，不可用比率互推**
        （TX 2024-10-31：原始 338,000、維持 259,000，比值 0.766，但那個比值本身也會隨公告變動）。
        """

        if self.config.api is None:
            return None

        query_date: datetime.date = TimeUtils.to_date(date)
        per_lot: Optional[float] = self.config.api.get_maintenance_margin(
            product, query_date, fallback_to_earliest=self.config.fallback_to_earliest
        )
        if per_lot is None:
            per_lot = self.stock_futures_margin_per_lot(
                product, query_date, maintenance=True
            )
        return None if per_lot is None else float(per_lot)

    def stock_futures_margin_per_lot(
        self, product: str, date: datetime.date, maintenance: bool = False
    ) -> Optional[float]:
        """
        走比例表算股期的每口保證金 ＝ 標的股價 × 契約單位 × 適用比例

        未注入 resolver 或取不到價（純記憶體測試、標的當日無成交）、沒有 API 時回 None，
        由呼叫端決定要中斷還是退回既有門檻。
        """

        if self.config.api is None or self.underlying_price_resolver is None:
            return None

        price: Optional[float] = self.underlying_price_resolver(product, date)
        if not price:
            return None

        calculate = (
            self.config.api.calculate_stock_futures_maintenance_margin
            if maintenance
            else self.config.api.calculate_stock_futures_margin
        )
        return calculate(
            product, date, price, fallback_to_earliest=self.config.fallback_to_earliest
        )
