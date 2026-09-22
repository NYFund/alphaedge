import datetime
from typing import List, Optional

from core.datafeed.base import BaseDataFeed
from core.models import FuturesAccount, FuturesQuote
from core.portfolio.signal import Signal
from core.strategies.futures import BaseFuturesStrategy
from core.utils import Action, ExecutionTiming, LiveHook, PositionType, Scale


class MomentumFuturesStrategy(BaseFuturesStrategy):
    """
    台指期動能策略（日線，示範用）

    買進條件（全部滿足）：
    - 標的為 `products` 指定的商品（預設 TX），取**近月**契約
    - 當日收盤相對前一交易日收盤漲幅 ≥ 門檻（預設 1%）
    - 目前無未平倉部位

    賣出條件：
    - 已有部位且「報價日」≥ 開倉日 + 持有天數門檻（預設 1 個曆日）

    停損條件：
    - 未實作（一律不回傳停損單）

    **這支策略的用途是驗證期貨的介面能跑通，不是可用的交易邏輯**——
    門檻是隨手取的，沒有經過任何參數研究，不可當成可交易的策略。

    〈實盤執行〉
    - 開倉與平倉兩個鉤子都在**尾盤段**（`AT_CLOSE`，期貨 13:30～13:44）呼叫。
    - **狀態可由「歷史資料 ＋ 當前帳戶部位」重建**：昨收每次由期貨價格表查，
      是否已有部位與持有天數只看帳上部位，沒有逐日累積的內部狀態。
    - 標的池：未宣告 `symbols`，由實盤資料源補上 `products` 目前掛牌的各月份契約；
      挑哪個月份由 `select_near_month()` 依實盤版的換月規則決定
      （`LAST_TRADING_DAY` 在實盤改為最後交易日前 1 個交易日換月）。
    - 已知差異：換月比回測早一天；價格是快照價而非收盤價。
    """

    DEFAULT_PRODUCTS: List[str] = ["TX"]
    DEFAULT_MAX_LOTS: int = 2
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2024, 1, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2024, 12, 31)

    # 買進參數
    MIN_PRICE_CHANGE_PCT_FOR_SIGNAL: float = 1.0  # 相對昨收之最小漲幅（%）
    MIN_HOLDING_DAYS: int = 1  # 最少持有曆日數
    # 取前一交易日收盤時往回看幾個曆日；連假最長 9 天，取 15 留餘裕
    LOOKBACK_DAYS: int = 15

    def __init__(self) -> None:
        super().__init__()
        self.strategy_name: str = "Momentum-Futures"
        self.init_capital: float = 3000000.0
        self.products: List[str] = self.DEFAULT_PRODUCTS
        self.max_lots: int = self.DEFAULT_MAX_LOTS
        self.position_type: PositionType = PositionType.LONG
        self.scale: Scale = Scale.DAY

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 實盤：兩個鉤子都在尾盤段呼叫（見 class docstring〈實盤執行〉）
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }

    def setup_account(self, account: FuturesAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: FuturesAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """宣告本策略要用的資料源；實例由 DataFeed 統一持有"""

        self.futures_price = feed.futures_price
        self.margin = getattr(feed, "margin", None)
        self.calendar = getattr(feed, "calendar", None)

    def generate_open_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """開倉訊號：近月契約相對昨收漲幅達門檻且目前無部位；口數由保證金決定"""

        if self.max_lots == 0 or not quotes:
            return []

        # 日盤與夜盤是兩筆獨立行情，先過濾再挑契約
        day_quotes: List[FuturesQuote] = self.filter_session(quotes)

        candidates: List[FuturesQuote] = []
        for product in self.products:
            quote: Optional[FuturesQuote] = self.select_near_month(day_quotes, product)
            if quote is None:
                continue
            # 已有該商品的部位就不再開（本策略不加碼）
            if any(
                code.startswith(product) and lots != 0
                for code, lots in self.get_open_lots().items()
            ):
                continue
            if self.is_momentum(quote):
                candidates.append(quote)

        # 口數受保證金約束，由 portfolio 層換算，故不給 sizing_price
        return [
            Signal(
                quote=quote,
                action=Action.BUY
                if self.position_type == PositionType.LONG
                else Action.SELL,
                position_type=self.position_type,
                order_price=quote.close,
            )
            for quote in candidates
        ]

    def is_momentum(self, quote: FuturesQuote) -> bool:
        """當日收盤相對前一交易日收盤的漲幅是否達門檻"""

        if self.futures_price is None:
            return False

        date: datetime.date = self.normalize_quote_date(quote.date)
        # 取該契約在本日之前最近一個交易日的收盤
        series = self.futures_price.get_close_series(
            product=quote.product,
            expiry=quote.expiry,
            start_date=date - datetime.timedelta(days=self.LOOKBACK_DAYS),
            end_date=date,
            # **不可傳 self.session**：整併模式的 COMBINED 不是資料表裡的值，
            # 查歷史行情一律走日盤（見 `price_query_session`）
            session=self.price_query_session,
        )
        if len(series) < 2:
            return False

        previous_close: float = float(series.iloc[-2])
        if previous_close <= 0:
            return False

        change_pct: float = (quote.close / previous_close - 1) * 100
        return change_pct >= self.MIN_PRICE_CHANGE_PCT_FOR_SIGNAL

    def generate_close_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """平倉訊號：持有滿門檻天數即出場；**訊號來源是帳上部位，不是報價**"""

        day_quotes: List[FuturesQuote] = self.filter_session(quotes)
        quote_by_contract = {quote.contract_id: quote for quote in day_quotes}

        signals: List[Signal] = []
        for position in self.account.get_positions():
            quote: Optional[FuturesQuote] = quote_by_contract.get(position.contract_id)
            if quote is None:
                continue

            holding_days: int = (
                self.normalize_quote_date(quote.date)
                - self.normalize_quote_date(position.date)
            ).days
            if holding_days < self.MIN_HOLDING_DAYS:
                continue

            signals.append(
                Signal(
                    quote=quote,
                    action=Action.SELL
                    if position.position_type == PositionType.LONG
                    else Action.BUY,
                    # 方向沿用策略宣告，不看部位本身的方向
                    position_type=self.position_type,
                    order_price=quote.close,
                    volume=position.volume,
                )
            )

        return signals

    def generate_stop_loss_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """停損訊號：本策略未實作"""

        return []
