import datetime
from abc import abstractmethod
from typing import Dict, List, Optional

from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.backtest.models.fill_model import FuturesFillConfig
from core.datafeed.base import BaseDataFeed
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig, FuturesRollPlanner
from core.models import FuturesAccount, FuturesOrder, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.portfolio.construction import (
    FuturesPortfolioConstructor,
    normalize_quote_date,
)
from core.portfolio.signal import Signal
from core.strategies.base import BaseStrategy
from core.utils import FuturesSession, InstrumentType, Market

"""
BaseFuturesStrategy: 台期貨策略基底

**與 `BaseStockStrategy` 的四個根本差異**，每一個都會讓沿用股票習慣的人寫錯：

1. **一天不只一個報價**
   同一天同一商品有多個到期月在交易（見 `FuturesPriceAPI` 的說明）。
   引擎傳進來的 `quotes` 是「當日所有契約」，**策略必須自己挑一個**。
   本基底提供 `select_near_month()` 作為預設政策，換月規則由 `roll_config` 決定，
   要別的政策就覆寫它。

2. **數量單位是口，且受保證金約束**
   股票是「用多少錢買多少股」，期貨是「繳多少保證金開幾口」。
   可開口數 = 可動用餘額 × 資金使用上限 ÷ 每口保證金，
   見 `calculate_max_lots()`——**不是用契約價值算**。

3. **沒有 `max_holdings`，只有口數上限**
   股票的 `max_holdings` 是「同時持有幾檔」；期貨通常只交易一兩個商品，
   限制的是**總口數**。故本基底用 `max_lots`，`max_holdings` 維持不設。

4. **沒有股票的信用交易設定**
   放空在期貨不需要券源、不需要借券費、也沒有平盤下限制——賣出開倉就是放空。
   `BaseStockStrategy` 的那一整組欄位在此都不存在。

---

**載入方式不需要特別處理**：`StrategyLoader.load_strategies()` 會逐一掃描
`core/strategies/` 底下的所有商品類別子套件，新增 `futures/` 就會自動被收錄，
`run.py --strategy <類別名>` 直接可用。
"""


class BaseFuturesStrategy(BaseStrategy):
    """Futures Strategy Framework (Base Template)"""

    def __init__(self) -> None:
        super().__init__()

        """ === Strategy Setting === """
        self.market: Market = Market.TW  # 市場：台灣
        self.instrument_type: InstrumentType = InstrumentType.FUTURE  # 商品：期貨

        """
        === Futures Setting ===

        `products` 是本策略要交易的商品代碼；引擎傳進來的報價可能含多個商品，
        策略自己過濾。乘數一律查 `FUTURES_MULTIPLIER`，不要寫死在策略裡。
        """
        self.products: List[str] = []  # 要交易的商品代碼（Ex: ["TX"]）
        # 交易時段。**日盤與夜盤是兩筆獨立行情**，混用會讓同一契約一天出現兩筆報價。
        # 設為 `FuturesSession.COMBINED` 則由 DataFeed 整併成單一序列
        # （前一交易日夜盤 ＋ 當日日盤，跨盤別跳空保留在 bar 內）
        self.session: FuturesSession = FuturesSession.DAY
        self.max_lots: int = 0  # 總口數上限（0 表示不開倉）
        # 期貨限制的是**總口數**不是持倉檔數，故明確解除引擎的檔數上限。
        # `BaseStrategy` 的預設值已於 2026-09-03 由 0 改為 None，
        # 此處保留是為了讓「期貨不用檔數上限」這件事在基底裡看得見
        self.max_holdings: Optional[int] = None
        # 單次開倉最多動用可動用餘額的比例；保證金交易若不設限，
        # 一次就能把帳戶壓到追繳邊緣
        self.max_capital_usage: float = 0.5

        """ === Cost & Margin ===

        兩者皆為 None 時走各自的預設：成本帶市場常見費率、保證金查表（API 由 DataFeed 注入）。
        要改用比率近似須明確宣告 `FuturesMarginConfig.ratio()`，
        固定比率跨年份的誤差實測為 +143% ~ −38%（TX：2020 年高估、2026 年低估，會變號）。
        """
        self.cost_config: Optional[FuturesCostConfig] = None
        self.margin_config: Optional[FuturesMarginConfig] = None
        # 換月設定；**策略挑合約與結算模型轉倉共用這一份**（見 `FuturesRollConfig`）
        self.roll_config: Optional[FuturesRollConfig] = None
        # 成交假設（滑價、成交量上限）；None 時全部關閉，成交價即策略給的委託價。
        # **真正擋住型別錯誤的是 factory 的檢查，不是這行標註**：期貨的滑價以
        # 跳動點表達，寫成基底的 `FillConfig(slippage_bps_buy=...)` 一樣跑得完，
        # 只是假設默默換成基點，故 `build_tw_futures_backtester()` 會當場拋 TypeError
        self.fill_config: Optional[FuturesFillConfig] = None

        """ === Datasets Setting === """
        self.futures_price: Optional[FuturesPriceAPI] = None  # 期貨行情
        self.margin: Optional[FuturesMarginAPI] = None  # 保證金（可選）
        # 期貨交易日曆（結算日、最後交易日、交易時段）；由 DataFeed 提供
        self.calendar: Optional[FuturesCalendar] = None

    @property
    def price_query_session(self) -> FuturesSession:
        """
        查**歷史行情**時要用的時段

        **`COMBINED` 不是資料表裡的值**（見 `FuturesSession`），它是報價層的組合
        結果。拿它去查 `FuturesPriceAPI` 只會得到空結果——而空結果在策略裡通常
        表現為「訊號永遠不成立」，不會有任何錯誤訊息。
        2026-09-02 實測：示範策略在整併模式下整場零交易，成因就是這個。
        """

        return (
            FuturesSession.DAY
            if self.session == FuturesSession.COMBINED
            else self.session
        )

    # === 契約選擇：與結算模型的轉倉共用同一份換月規則 ===
    def select_near_month(
        self, quotes: List[FuturesQuote], product: str
    ) -> Optional[FuturesQuote]:
        """
        - Description:
            挑出該商品當日的**當家契約**

            **規則由 `roll_config` 決定**：撐到最後交易日／提前 N 個
            交易日／未沖銷量交叉。這份規則同時被 `TwFuturesSettlementModel`
            用來轉倉——**兩處必須是同一個設定物件**，否則會出現「訊號在次月、
            部位還在近月」這種不會報錯的錯配。

            尚未注入日曆時（純記憶體測試、或 DataFeed 還沒 setup）退回
            「取最近的到期月」，即最單純的近月政策。

            ⚠️ 預設規則之下，**近月在最後交易日當天仍是近月**；要提早避開結算日
            請改用 `FuturesRollRule.DAYS_BEFORE_EXPIRY`。
        - Parameters:
            - quotes: List[FuturesQuote]
                當日所有契約的報價
            - product: str
                商品代碼
        - Return:
            - Optional[FuturesQuote]
                當家契約的報價；該商品當日無報價時為 None
        """

        candidates: List[FuturesQuote] = [
            quote for quote in quotes if quote.product == product
        ]
        if not candidates:
            return None

        planner: Optional[FuturesRollPlanner] = (
            self.roll_config.build_planner() if self.roll_config else None
        )
        if planner is None:
            # 到期月為 `YYYYMM`，字典序即時間序；週契約帶 W 尾碼排在同月月契約之後
            return min(candidates, key=lambda quote: quote.expiry)

        active: Optional[str] = planner.resolve_active_expiry(
            self.normalize_quote_date(candidates[0].date),
            [quote.expiry for quote in candidates],
            {quote.expiry: quote.open_interest for quote in candidates},
        )
        if active is None:
            return min(candidates, key=lambda quote: quote.expiry)

        return next(
            (quote for quote in candidates if quote.expiry == active),
            min(candidates, key=lambda quote: quote.expiry),
        )

    # === 口數計算：保證金約束，不是資金約束 ===
    def make_portfolio_constructor(self) -> FuturesPortfolioConstructor:
        """
        期貨的部位建構器：保證金約束

        **每次組裝都重建**（理由見 `BaseStrategy.make_portfolio_constructor()`）：
        `max_lots` 在 `super().__init__()` 之後才填，`margin_config` 更是由
        `core/backtest/factory.py` 在策略建構完成之後才注入——建一次存起來會
        永遠看到 `None`，於是**靜默退回比率近似**而不是查表。
        """

        return FuturesPortfolioConstructor(
            max_lots=self.max_lots,
            max_capital_usage=self.max_capital_usage,
            margin_config=self.margin_config,
            log_context=self.strategy_name,
        )

    def calculate_max_lots(self, quote: FuturesQuote) -> int:
        """
        - Description:
            以**保證金**算出這筆訂單最多能開幾口

            公式本體在 `FuturesPortfolioConstructor`：股票是「用多少錢買多少股」，
            期貨是「繳多少保證金開幾口」，拿契約價值去除可動用餘額會嚴重低估
            可開口數（TX 一口契約價值 900 萬、保證金只有 70 萬）。

            **本方法保留為可覆寫的鉤子**，供仍走舊路徑的策略與子類客製使用。
        - Parameters:
            - quote: FuturesQuote
                目標契約的報價
        - Return:
            - int
                可開口數；帳戶或報價不足以計算時為 0
        """

        if self.account is None:
            return 0

        return self.make_portfolio_constructor().calculate_max_lots(quote, self.account)

    def get_margin_per_lot(self, quote: FuturesQuote) -> float:
        """
        取得每口原始保證金

        委派給 `FuturesPortfolioConstructor`，與 `FuturesPositionManager` 的
        比率模式一致——兩處若不一致，策略算出來的口數會開不進去（或開得太少）。
        """

        return self.make_portfolio_constructor().get_margin_per_lot(quote)

    @staticmethod
    def normalize_quote_date(date) -> datetime.date:
        """Tick 級別的報價日期會是 datetime，統一取其日期部分"""

        return normalize_quote_date(date)

    # === 結算日：期貨獨有，沒有日曆就只能靠報價消失才發現 ===
    def get_trading_days_to_expiry(self, quote: FuturesQuote) -> Optional[int]:
        """
        該契約距離最後交易日還有幾個**交易日**（當日為 0）

        沒有日曆時回傳 None——**不可當成「還很久」**：那會讓策略在結算日照常開倉，
        隔天契約就不再有報價，部位只能靠結算模型的權宜出場收尾。
        """

        if self.calendar is None:
            return None

        return self.calendar.get_trading_days_to_expiry(
            self.normalize_quote_date(quote.date), quote.expiry
        )

    def filter_session(self, quotes: List[FuturesQuote]) -> List[FuturesQuote]:
        """
        只留下本策略指定時段的報價

        **日盤與夜盤是兩筆獨立行情**，不過濾的話同一契約一天會出現兩筆，
        訊號會被算兩次。
        """

        return [quote for quote in quotes if quote.session == self.session]

    def get_open_lots(self) -> Dict[str, int]:
        """目前各契約的淨口數（多為正、空為負）"""

        return {} if self.account is None else self.account.get_open_lots()

    # === 抽象介面（對齊 BaseStockStrategy）===
    @abstractmethod
    def setup_account(self, account: FuturesAccount) -> None:
        """載入虛擬帳戶資訊"""
        pass

    @abstractmethod
    def setup_apis(self, feed: BaseDataFeed) -> None:
        """
        宣告本策略要用的資料源

        實例一律由 DataFeed 統一持有，策略只做取用，不自行建立。
        """
        pass

    def build_close_orders(self, signals: List[Signal]) -> List[FuturesOrder]:
        """
        把平倉／停損訊號組成 `FuturesOrder`

        **只做欄位搬運**：期貨的平倉訊號來自帳上部位（`position.volume`、
        多單賣出、空單買進），數量與方向都已由策略決定。
        """

        orders: List[FuturesOrder] = []
        for signal in signals:
            # 訊號沒帶數量就是策略算出來無倉可平，略過而非下一張 0 口的單
            if not signal.volume or signal.volume <= 0:
                continue

            quote: FuturesQuote = signal.quote
            orders.append(
                FuturesOrder(
                    product=quote.product,
                    expiry=quote.expiry,
                    date=quote.date,
                    action=signal.action,
                    position_type=signal.position_type,
                    price=signal.order_price,
                    volume=signal.volume,
                )
            )
        return orders
