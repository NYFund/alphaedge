import datetime
from typing import Dict, List, Optional

from core.datafeed.base import BaseDataFeed
from core.models import FuturesAccount, FuturesQuote
from core.portfolio.signal import Signal
from core.strategies.futures import BaseFuturesStrategy
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
    PositionType,
    Scale,
    TradeDirection,
)


class FuturesRolloverRehearsalStrategy(BaseFuturesStrategy):
    """
    期貨換月演練策略（只供模擬環境演練，不是交易策略）

    買進條件（全部滿足）：
    - 報價日落在建倉期間（2026-10-14～2026-10-19）
    - 目前沒有台指期部位
    - 買 1 口台指期**近月**契約（10 月契約）

    賣出條件：
    - 報價日 ≥ 2026-10-21（換月已在 2026-10-20 尾盤段由引擎完成），平掉手上的部位

    停損條件：
    - 未實作（一律不回傳停損單）

    〈為什麼需要這支策略〉
    換月兩腿只會對「換月日持有舊月部位」的策略觸發。示範用的
    `MomentumFuturesStrategy` 要漲幅 ≥ 1% 才進場、持有 1 個曆日就出場，
    要撐過換月日只能剛好在前一個交易日出訊號；被動等不知要等到哪一個月。
    這支策略以固定日期建倉並撐過換月，確保 10 月契約的換月日有部位可換，
    同時讓期貨的 `MARKET`（範圍市價＋IOC）經引擎實際送出。

    〈日期為什麼這樣排〉
    - 10 月契約最後交易日是 2026-10-21，實盤在前 1 個交易日（10-20）換月。
    - 建倉從 10-14 開始而不是只在 10-19：IOC 可能沒成交，要留幾個交易日重試。
    - **10-20 當天不建倉**：引擎在尾盤段先換月、再收新訊號，那時近月已切到
      11 月契約，建出來的部位不會被換月。
    - 平倉從 10-21 開始：10-20 換月後新部位的開倉日是當天，隔一個交易日才出場，
      避免和換月兩腿擠在同一個段落。

    〈實盤執行〉
    - 開倉與平倉都在**尾盤段**（`AT_CLOSE`，期貨 13:30～13:44），與換月同一個段落。
    - 狀態只看帳上部位與報價日，可由「當前帳戶部位」重建，沒有逐日累積的內部狀態。
    - 執行方式 `MARKET`：尾盤段仍在連續交易時段，執行層送範圍市價＋IOC。

    〈資金額度〉
    與示範策略同為 300 萬：建倉前的保證金檢查以策略帳戶餘額比對每口原始保證金
    （台指期 2026-08 起 70.1 萬），口數再由 `max_lots` 壓在 1 口。

    〈回測〉
    建倉期間在 2026-10，回測區間內沒有交易；這支策略不提供回測意義。
    """

    DEFAULT_PRODUCTS: List[str] = ["TX"]
    DEFAULT_MAX_LOTS: int = 1
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2026, 10, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2026, 10, 31)

    # 建倉期間（含首尾）；最後一天必須早於換月日
    OPEN_FROM_DATE: datetime.date = datetime.date(2026, 10, 14)
    OPEN_UNTIL_DATE: datetime.date = datetime.date(2026, 10, 19)
    # 此日（含）起平倉；必須晚於換月日
    CLOSE_FROM_DATE: datetime.date = datetime.date(2026, 10, 21)

    def __init__(self) -> None:
        super().__init__()
        self.strategy_name: str = "Futures-Rollover-Rehearsal"
        self.init_capital: float = 3000000.0
        self.products: List[str] = self.DEFAULT_PRODUCTS
        self.max_lots: int = self.DEFAULT_MAX_LOTS
        self.direction: TradeDirection = TradeDirection.LONG
        self.scale: Scale = Scale.DAY

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 實盤：兩個鉤子都在尾盤段呼叫，與換月同一個段落
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        self.live_execution = ExecutionStyle.MARKET

    def setup_account(self, account: FuturesAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: FuturesAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """宣告本策略要用的資料源；只用報價與帳上部位，不查歷史行情"""

        self.margin = getattr(feed, "margin", None)
        self.calendar = getattr(feed, "calendar", None)

    def generate_open_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """開倉訊號：建倉期間內、沒有部位時買近月；口數由保證金與 `max_lots` 決定"""

        if self.max_lots == 0 or not quotes:
            return []

        day_quotes: List[FuturesQuote] = self.filter_session(quotes)
        if not day_quotes:
            return []

        date: datetime.date = self.normalize_quote_date(day_quotes[0].date)
        if not self.OPEN_FROM_DATE <= date <= self.OPEN_UNTIL_DATE:
            return []

        signals: List[Signal] = []
        open_lots: Dict[str, int] = self.get_open_lots()
        for product in self.products:
            if any(
                code.startswith(product) and lots != 0
                for code, lots in open_lots.items()
            ):
                continue

            quote: Optional[FuturesQuote] = self.select_near_month(day_quotes, product)
            if quote is None:
                continue

            signals.append(
                Signal(
                    quote=quote,
                    action=Action.BUY,
                    position_type=PositionType.LONG,
                    order_price=quote.close,
                )
            )
        return signals

    def generate_close_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """平倉訊號：換月日之後平掉所有部位；**訊號來源是帳上部位，不是報價**"""

        day_quotes: List[FuturesQuote] = self.filter_session(quotes)
        if not day_quotes:
            return []

        date: datetime.date = self.normalize_quote_date(day_quotes[0].date)
        if date < self.CLOSE_FROM_DATE:
            return []

        quote_by_contract: Dict[str, FuturesQuote] = {
            quote.contract_id: quote for quote in day_quotes
        }
        signals: List[Signal] = []
        for position in self.account.get_positions():
            quote: Optional[FuturesQuote] = quote_by_contract.get(position.contract_id)
            if quote is None:
                continue

            signals.append(
                Signal(
                    quote=quote,
                    action=Action.SELL
                    if position.position_type == PositionType.LONG
                    else Action.BUY,
                    position_type=position.position_type,
                    order_price=quote.close,
                    volume=position.volume,
                )
            )
        return signals

    def generate_stop_loss_signals(self, quotes: List[FuturesQuote]) -> List[Signal]:
        """停損訊號：本策略未實作"""

        return []
