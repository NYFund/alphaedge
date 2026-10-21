import datetime
from typing import List, Optional

from core.datafeed.base import BaseDataFeed
from core.models import StockAccount, StockPosition, StockQuote
from core.portfolio.signal import Signal
from core.strategies.stock import BaseStockStrategy
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
    PositionType,
    Scale,
    TradeDirection,
)


class StockRoundTripRehearsalStrategy(BaseStockStrategy):
    """
    股票下單路徑演練策略（只供模擬環境演練，不是交易策略）

    買進條件（全部滿足）：
    - 報價日落在建倉期間（2026-10-22～2026-10-30）
    - 目前沒有 0050 部位
    - 收盤集合競價買 1 張 0050

    賣出條件：
    - 已有部位且報價日 ≥ 開倉日 + 1 日曆日，全數賣出（與 `MomentumStrategy1` 相同的隔日出場）

    停損條件：
    - 未實作（一律不回傳停損單）

    〈為什麼需要這支策略〉
    演練要驗「開倉成交 → 次一交易日平倉成交」走完一輪。示範用的 `MomentumStrategy1`
    挑當日漲幅 ≥ 9% 的強勢股，買單掛漲停價，收盤鎖漲停就排不到；被動等它成交
    不知要等多久。這支策略固定買流動性高、幾乎不會鎖漲停的 0050，
    讓開倉與平倉兩腿都經引擎在收盤集合競價實際成交。
    模擬環境實測過收盤集合競價會撮合 0050（跌停價賣單在 13:30 成交）。

    〈日期為什麼這樣排〉
    - 建倉從 10-22 開始：前一天以前的段落還在做期貨換月與平倉，這支策略要等那之後才上線，
      日期本身也擋住提早部署時就開始下單。
    - 建倉期間留到 10-30：收盤沒成交時隔天重試。期間內會反覆「買 → 隔天賣 → 再隔天買」，
      多跑幾輪無妨；過了期間只剩平倉，手上有部位照樣會被平掉。

    〈實盤執行〉
    - 開倉與平倉都在**尾盤段**（`AT_CLOSE`），與 `MomentumStrategy1` 同一個行程、同一個段落。
    - 狀態只看帳上部位與報價日，可由「當前帳戶部位」重建，沒有逐日累積的內部狀態。
    - 執行方式 `MARKET`：收盤集合競價時段不收市價單，執行層換成買掛漲停、賣掛跌停，
      成交價仍是競價結果。

    〈資金額度〉
    張數由部位建構器以 `live_capital` ÷ 可開檔數切出：45 萬、最多 3 檔，每檔 15 萬，
    0050 在 75～150 元之間都剛好 1 張。檔數取 3 而不是 1：每檔佔額度的比例
    （1 ÷ 檔數）不可超過單一標的曝險上限 35%，只開 1 檔就是 100%，帳戶層會把買單截斷。
    **只買一檔靠的是「持有中不再買」那道檢查**——拿掉的話，持有 1 檔時剩 2 檔可開、
    每檔 22.5 萬，會再買 2 張。

    逐單風控的金額上限有兩種口徑，這支策略兩種都過得了：以 `init_capital` 為基準
    （100 萬 × 20% ＝ 20 萬），或以 `live_capital` 為基準（45 萬 × 35% ＝ 15.75 萬）。
    1 張 0050 以漲停價掛單約 12 萬。後一種口徑的比率若調到 27% 以下，這張單就會被擋下，
    屆時要一起重訂 `live_capital`。

    〈回測〉
    建倉期間在 2026-10，回測區間內沒有交易；這支策略不提供回測意義，parity 補比也不比它。
    """

    SYMBOL: str = "0050"
    DEFAULT_MAX_HOLDINGS: int = 3
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2026, 10, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2026, 10, 31)

    # 建倉期間（含首尾）
    OPEN_FROM_DATE: datetime.date = datetime.date(2026, 10, 22)
    OPEN_UNTIL_DATE: datetime.date = datetime.date(2026, 10, 30)

    def __init__(self) -> None:
        super().__init__()
        self.strategy_name: str = "Stock-Round-Trip-Rehearsal"
        # 逐單風控以它為金額基準的口徑下才有作用；張數不看它（見 class docstring〈資金額度〉）
        self.init_capital: float = 1000000.0
        self.live_capital: float = 450000.0  # 實盤額度（元），每檔切 15 萬 ＝ 1 張 0050
        self.max_holdings: int = self.DEFAULT_MAX_HOLDINGS
        self.direction: TradeDirection = TradeDirection.LONG
        self.scale: Scale = Scale.DAY
        # 標的池只有 0050；不宣告的話實盤會以全市場為標的
        self.symbols: List[str] = [self.SYMBOL]

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 實盤：兩個鉤子都在尾盤段呼叫，與示範策略同一個段落
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        self.live_execution = ExecutionStyle.MARKET

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """宣告本策略要用的資料源；只用報價與帳上部位，不查歷史行情"""

        return None

    def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """開倉訊號：建倉期間內、沒有部位時買 0050；張數由 portfolio 層依 `live_capital` 決定"""

        if self.max_holdings == 0 or not stock_quotes:
            return []

        date: datetime.date = stock_quotes[0].date
        if not self.OPEN_FROM_DATE <= date <= self.OPEN_UNTIL_DATE:
            return []

        if self.account.check_has_position(self.SYMBOL):
            return []

        return [
            Signal(
                quote=stock_quote,
                action=Action.BUY,
                position_type=PositionType.LONG,
                order_price=stock_quote.cur_price,
                sizing_price=stock_quote.close,
            )
            for stock_quote in stock_quotes
            if stock_quote.stock_id == self.SYMBOL
        ]

    def generate_close_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """平倉訊號：持有至少一個日曆日後全數賣出；不受建倉期間限制"""

        signals: List[Signal] = []

        for stock_quote in stock_quotes:
            if not self.account.check_has_position(stock_quote.stock_id):
                continue

            position: Optional[StockPosition] = self.account.get_first_open_position(
                stock_quote.stock_id
            )
            if position is None:
                continue
            # 開倉當日不平：當日平倉是當沖，不是要驗的「次日平倉」
            if stock_quote.date < position.date + datetime.timedelta(days=1):
                continue

            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.SELL,
                    position_type=position.position_type,
                    order_price=stock_quote.cur_price,
                    volume=position.volume,
                )
            )

        return signals

    def generate_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """停損訊號：本策略未實作"""

        return []
