import datetime
from abc import abstractmethod
from typing import Any, Dict, List, Optional

from core.api.tw.financial_statement_api import FinancialStatementAPI
from core.api.tw.monthly_revenue_report_api import MonthlyRevenueReportAPI
from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.api.tw.stock_tick_api import StockTickAPI
from core.backtest.models.cost_model import CostConfig, ShortConstraint
from core.backtest.models.fill_model import FillConfig
from core.datafeed.base import BaseDataFeed
from core.models import StockAccount, StockOrder, StockQuote
from core.portfolio.construction import StockPortfolioConstructor
from core.portfolio.signal import Signal
from core.portfolio.sizing import BasePositionSizer, EqualWeightSizer
from core.strategies.base import BaseStrategy
from core.utils import (
    DayTradeUncoveredPolicy,
    InstrumentType,
    MarginCallPolicy,
    Market,
    ShortMethod,
)

"""BaseStockStrategy: 台股策略基底，補上信用交易設定與五個資料集"""


class BaseStockStrategy(BaseStrategy):
    """Stock Strategy Framework (Base Template)"""

    def __init__(self) -> None:
        super().__init__()

        """ === Strategy Setting === """
        self.market: Market = Market.TW  # 市場：台灣
        self.instrument_type: InstrumentType = InstrumentType.STOCK  # 商品：股票

        """
        === Short Setting ===

        台股信用交易專屬；方向白名單與執行順序屬市場與商品皆無關，已上移至 BaseStrategy
        （`enable_intraday` 與 `bar_execution_order` 的對應表見 `BaseStrategy.__init__`
        的〈Direction Setting〉區塊，推導由 `Backtester.get_execution_order()` 執行）。

        `enable_intraday` 在台股另有一項市場專屬效果：SHORT ＋ 當沖時
        `factory.build_cost_config()` 會強制 `ShortMethod.DAY_TRADE`（證交稅減半）。
        """
        self.short_method: ShortMethod = ShortMethod.MARGIN  # 放空管道
        self.cost_config: Optional[CostConfig] = None  # 成本參數（None 用預設）
        self.short_constraint: Optional[ShortConstraint] = None  # 放空可成交限制
        # 成交假設（滑價、成交量上限）；None 用預設（全部關閉）
        self.fill_config: Optional[FillConfig] = None
        self.max_holding_days: Optional[int] = None  # 留倉放空的最長持有曆日數
        # 連續無報價幾天後強制出場（停牌／下市）；None 為不處理，維持現況
        self.max_no_quote_days: Optional[int] = None
        self.day_trade_uncovered_policy: DayTradeUncoveredPolicy = (
            DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE  # 當沖日終未回補的處理
        )
        self.margin_call_policy: MarginCallPolicy = (
            MarginCallPolicy.FORCE_COVER  # 維持率追繳的處理
        )

        """
        === Position Sizing ===

        等權資金切分原本在五支策略內各寫一遍且已經漂移，收成單一實作。
        要換配置演算法（波動度加權等）時，在策略的 __init__ 覆寫本欄位即可。
        """
        self.sizer: BasePositionSizer = EqualWeightSizer()  # 部位大小模型

        """ === Datasets Setting=== """
        self.tick: Optional[StockTickAPI] = None  # Ticks data (Optional)
        self.price: Optional[StockPriceAPI] = None  # Day price data (Optional)
        self.chip: Optional[StockChipAPI] = None  # Chips data (Optional)
        self.mrr: Optional[MonthlyRevenueReportAPI] = (
            None  # Monthly Revenue Report data (Optional)
        )
        self.fs: Optional[FinancialStatementAPI] = (
            None  # Financial Statement data (Optional)
        )

    @abstractmethod
    def setup_account(self, account: StockAccount) -> None:
        """
        - Description:
            載入虛擬帳戶資訊
        """
        pass

    @abstractmethod
    def setup_apis(self, feed: BaseDataFeed) -> None:
        """
        - Description:
            宣告本策略要用的資料源

            實例一律由 DataFeed 統一持有，策略只做取用，不自行建立
            。
        - Parameter:
            - feed: BaseDataFeed
                引擎持有的資料源
        """
        pass

    def get_signal_close_map(
        self,
        stock_quotes: List[StockQuote],
        date: datetime.date,
    ) -> Dict[str, Any]:
        """
        - Description:
            取得**訊號用**的收盤價對照表，與 `StockQuote.signal_close` 成對使用

            為什麼要有這個方法：策略算漲跌幅時，「今日價格」來自 `StockQuote`、
            「昨日價格」來自 `StockPriceAPI`，是兩條不同的路徑。若只有一邊套用還原，
            比值會同時混用還原價與原始價，**比完全不還原更糟，而且不會報錯**。

            本方法讓兩邊由**同一個來源**（引擎傳進來的報價）決定要不要還原，
            呼叫端不需要、也不應該自己判斷目前是不是還原模式。

            用法固定成對：

            ```python
            close_map = self.get_signal_close_map(stock_quotes, yesterday)
            price_chg = quote.signal_close / close_map[quote.stock_id] - 1
            ```

            成交價、手續費、證交稅、漲跌停與檔位判定**一律走原始價**
            （`quote.close` 與 `self.price.get_close_map()`），不要用本方法。
        - Parameters:
            - stock_quotes: List[StockQuote]
                引擎傳入的當日報價；由其 `adj_close` 判定是否為還原模式
            - date: datetime.date
                要查詢的日期
        - Return:
            - Dict[str, Any]
                {stock_id: 收盤價}；還原模式下為還原價，否則為原始價
        """

        adjusted: bool = bool(stock_quotes) and stock_quotes[0].adj_close is not None

        if adjusted:
            return self.price.get_adjusted_close_map(date)
        return self.price.get_close_map(date)

    def make_portfolio_constructor(self) -> StockPortfolioConstructor:
        """
        台股的部位建構器：等權資金切分

        **每次組裝都重建**（理由見 `BaseStrategy.make_portfolio_constructor()`）：
        `max_holdings` 是策略在 `super().__init__()` 之後才填的，建一次存起來
        會永遠讀到 `None`。
        """

        return StockPortfolioConstructor(self.sizer, self.max_holdings)

    def build_close_orders(self, signals: List[Signal]) -> List[StockOrder]:
        """
        把平倉／停損訊號組成 `StockOrder`

        **只做欄位搬運**：平幾張、用什麼價、算在哪個方向，全部由策略在訊號裡
        決定（`MomentumStrategy1` 平第一筆部位、`ForeignSellShortDayTradeStrategy`
        合併同標的所有空單，後者逐筆送單會被 `close_position()` 的 FIFO 吃掉）。

        `short_method` 與 `is_day_trade` 不在此填，由 `StockCostModel.enrich_orders()`
        依成本設定補值。
        """

        orders: List[StockOrder] = []
        for signal in signals:
            # 訊號沒帶數量就是策略算出來無倉可平，略過而非下一張 0 張的單
            if not signal.volume or signal.volume <= 0:
                continue

            orders.append(
                StockOrder(
                    stock_id=signal.symbol,
                    date=signal.quote.date,
                    action=signal.action,
                    position_type=signal.position_type,
                    price=signal.order_price,
                    volume=signal.volume,
                )
            )
        return orders
