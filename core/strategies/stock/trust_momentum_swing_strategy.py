import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.datafeed.base import BaseDataFeed
from core.market.tw.market_calendar import MarketCalendar
from core.models import StockAccount, StockPosition, StockQuote
from core.portfolio.signal import Signal
from core.strategies.stock import BaseStockStrategy
from core.utils import Action, PositionType, Scale, Units


class TrustMomentumSwingStrategy(BaseStockStrategy):
    """
    投信認同的強勢股短波段（日線、只做多）

    買進條件（全部以 T−1 已收盤、已公布的資料判斷，T 日開盤買進）：
    - 大盤濾網：T−1 的 0050 還原收盤價 > 其 20 日均線
    - T−1 收盤相對 T−2 收盤漲幅 ≥ 門檻（預設 5%，含漲停）
    - T−1 投信買超 > 0（當日有投信實際掏錢買進）
    - T−1 成交金額 ≥ 門檻（預設 3 億元），收盤價 ≥ 門檻（預設 10 元）
    - T 日開盤相對 T−1 收盤的跳空 < 門檻（預設 9.5%）：開盤就鎖漲停的買不到
    - 候選多於剩餘檔數時，依 T−1 漲幅由高到低挑

    賣出條件：
    - 持有滿 `HOLDING_DAYS` 個交易日（預設 10）後，以當日收盤價出場

    停損條件：
    - **刻意不做**（一律不回傳停損單）。以 2020-01～2026-09 的組合模擬測 8%／10%／15%
      三種停損水位，**每一種都讓平均報酬變差**（10% 停損讓 2024-09～2026-03 的
      每筆平均報酬從 +2.95% 掉到 +2.16%）。這類剛噴出的股票常先洗一段才延續，
      停損砍掉的正是之後會獲利的部位；而且模擬還假設停損恰好成交在觸發價，
      真實表現只會更差。風險上界改由「最多持有 10 個交易日」與大盤濾網承擔。

    **為什麼是 T 日開盤、不是 T−1 收盤進場**：三大法人買賣超在收盤後才公布，
    T−1 收盤當下根本拿不到 T−1 的投信買超。若用 T−1 收盤進場，帳面報酬會多吃到
    隔夜跳空（樣本平均約 +0.3～1%），但那是未來函數，實盤做不到。

    **為什麼看投信而不是外資**：同樣是「漲 5% 以上」的樣本，投信當日買超那一組的
    10 日報酬中位數約 0%～+1%，沒有法人買超的那一組約 −0.2%～−1.3%；
    外資買超的篩選效果較弱（中位數仍多為負）。投信部位小、季底有作帳壓力，
    主動式 ETF 又每日揭露持股，買進後傾向連續加碼，這是短波段延續的來源。

    **為什麼要大盤濾網**：2024-09～2026-09 之間出現多次單日重挫（2025-04 關稅、
    2026-03 美伊衝突、2026-07 半導體急殺），強勢股在急殺段的 10 日報酬最差。
    組合模擬中，排序方式（漲幅／投信買超比重／隨機）對結果影響不大，
    有沒有大盤濾網才是主要差異，三個區間都一致。

    **研究結果（組合模擬：10 檔等權、扣來回成本 0.585%，每筆平均報酬）**：
    2020-01～2024-08 約 +0.8%、2024-09～2026-03 約 +3.0%、
    2026-04～2026-09（保留作驗證、未參與設計）約 +2.9%。
    勝率都在 47%～55% 之間，獲利來自右尾，單筆中位數在較早區間為負。
    """

    DEFAULT_MAX_HOLDINGS: int = 10
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2024, 9, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2026, 9, 24)

    # 買進參數
    MIN_SIGNAL_RETURN_PCT: float = 5.0  # 訊號日相對前一交易日的最小漲幅（%）
    MIN_TURNOVER: float = 3e8  # 訊號日最小成交金額（元）
    MIN_PRICE: float = 10.0  # 訊號日最小收盤價（元）
    MAX_OPEN_GAP_PCT: float = 9.5  # 進場日開盤跳空上限（%），超過視為開盤鎖漲停
    # 大盤濾網參數
    MARKET_PROXY_ID: str = "0050"  # 代表大盤的標的
    MARKET_MA_DAYS: int = 20  # 大盤均線天數（交易日）
    # 取均線要往回抓的曆日數：20 個交易日遇上春節連假也要夠用
    MARKET_LOOKBACK_CALENDAR_DAYS: int = 45
    # 出場參數
    HOLDING_DAYS: int = 10  # 持有交易日數
    # 交易日清單往回多抓幾個曆日，理由同 `MomentumStrategy1.CALENDAR_LOOKBACK_DAYS`：
    # 窗比日曆的回推上界小，清單查不到就會退回逐日查資料庫
    CALENDAR_LOOKBACK_DAYS: int = MarketCalendar.MAX_LOOKBACK_DAYS

    def __init__(self) -> None:
        super().__init__()
        self.strategy_name: str = "Trust-Momentum-Swing"
        self.position_type: PositionType = PositionType.LONG
        # 最短持有 10 個交易日，不會當沖；關掉避免被誤認為當沖策略
        self.enable_intraday: bool = False
        self.init_capital: float = 1000000.0
        self.max_holdings: int = self.DEFAULT_MAX_HOLDINGS
        self.scale: Scale = Scale.DAY

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 回測區間的交易日清單；`setup_apis()` 建一次
        self.trading_days: List[datetime.date] = []

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """
        - Description:
            宣告本策略要用的資料源；實例由 DataFeed 統一持有

            只支援日線：訊號建立在「前兩個交易日收盤」與「前一交易日投信買超」上，
            TICK 路徑不會掛 `self.price`，放行的話第一根 bar 才會崩。
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
        - Raise:
            - NotImplementedError
                `scale` 不是 `Scale.DAY`
        """

        if self.scale != Scale.DAY:
            raise NotImplementedError(
                f"{self.strategy_name} 只支援日線（Scale.DAY），目前的 scale 是 {self.scale}"
            )

        self.price = feed.price
        self.chip = feed.chip
        self.trading_days = self.build_trading_days()

    def build_trading_days(self) -> List[datetime.date]:
        """預先取好回測區間（含起始日前的回推窗）的交易日清單"""

        if self.price is None or not self.start_date or not self.end_date:
            return []

        return self.price.get_trading_days(
            self.start_date - datetime.timedelta(days=self.CALENDAR_LOOKBACK_DAYS),
            self.end_date,
        )

    def get_previous_trading_date(self, date: datetime.date) -> datetime.date:
        """
        - Description:
            取得前一個交易日；優先查預備好的清單，查不到才回頭問資料庫

            只在清單涵蓋的範圍內查清單：日期超出清單時平移會落到清單最後一天，
            拿錯的日期算漲幅而不會有任何錯誤。
        - Parameters:
            - date: datetime.date
                基準日
        - Return:
            - datetime.date
                前一個交易日
        """

        if self.trading_days and date <= self.trading_days[-1]:
            previous: Optional[datetime.date] = MarketCalendar.shift_trading_days(
                self.trading_days, date, offset=-1
            )
            if previous is not None:
                return previous

        return MarketCalendar.get_last_trading_date(api=self.price, date=date)

    def count_holding_days(
        self, open_date: datetime.date, date: datetime.date
    ) -> Optional[int]:
        """
        - Description:
            計算從開倉日到 `date` 經過幾個交易日（開倉日當天為 0）

            用交易日而不是曆日：連假會讓曆日持有期整段縮水。
        - Parameters:
            - open_date: datetime.date
                開倉日
            - date: datetime.date
                當前報價日
        - Return:
            - Optional[int]
                經過的交易日數；任一日期不在清單內時為 None（交由呼叫端退回曆日判斷）
        """

        try:
            return self.trading_days.index(date) - self.trading_days.index(open_date)
        except ValueError:
            return None

    def check_market_uptrend(self, signal_date: datetime.date) -> bool:
        """
        - Description:
            大盤濾網：`signal_date` 的 0050 還原收盤價是否站上其 20 日均線

            用還原價：0050 在 2025-06-18 做過 1 拆 4 分割，原始價會在該日腰斬，
            均線整段失真。資料不足 20 日時視為不通過，寧可少開倉也不要在不明狀態下進場。
        - Parameters:
            - signal_date: datetime.date
                訊號日（T−1）
        - Return:
            - bool
                站上均線為 True
        """

        series: pd.Series = self.price.get_adjusted_close_series(
            self.MARKET_PROXY_ID,
            signal_date - datetime.timedelta(days=self.MARKET_LOOKBACK_CALENDAR_DAYS),
            signal_date,
        ).dropna()

        if len(series) < self.MARKET_MA_DAYS:
            logger.warning(
                f"{self.MARKET_PROXY_ID} 在 {signal_date} 前的資料不足 "
                f"{self.MARKET_MA_DAYS} 日，本日不開倉"
            )
            return False

        latest: float = float(series.iloc[-1])
        moving_average: float = float(series.iloc[-self.MARKET_MA_DAYS :].mean())
        return latest > moving_average

    def collect_candidates(
        self, stock_quotes: List[StockQuote], signal_date: datetime.date
    ) -> List[Tuple[float, StockQuote]]:
        """
        - Description:
            依 `signal_date`（T−1）已公布的資料篩出候選，回傳（排序分數, 當日報價）
        - Parameters:
            - stock_quotes: List[StockQuote]
                進場日（T）的報價
            - signal_date: datetime.date
                訊號日（T−1）
        - Return:
            - List[Tuple[float, StockQuote]]
                排序分數為訊號日漲幅（%）
        """

        base_date: datetime.date = self.get_previous_trading_date(signal_date)

        # 漲幅是兩個歷史日的比值，兩邊都走 `get_signal_close_map()`，
        # 還原與否由同一個來源決定，不會一邊還原一邊原始價
        signal_close_map: Dict[str, Any] = self.get_signal_close_map(
            stock_quotes, signal_date
        )
        base_close_map: Dict[str, Any] = self.get_signal_close_map(
            stock_quotes, base_date
        )
        # 成交金額與跳空判定屬於「可不可以成交」，一律用原始價
        raw_close_map: Dict[str, Any] = self.price.get_close_map(signal_date)
        volume_map: Dict[str, int] = self.price.get_volume_lots_map(signal_date)
        trust_map: Dict[str, Any] = self.chip.get_trust_net_shares_map(signal_date)

        candidates: List[Tuple[float, StockQuote]] = []

        for quote in stock_quotes:
            stock_id: str = quote.stock_id
            if self.account.check_has_position(stock_id):
                continue

            signal_close: Any = signal_close_map.get(stock_id)
            base_close: Any = base_close_map.get(stock_id)
            raw_close: Any = raw_close_map.get(stock_id)
            trust_shares: Any = trust_map.get(stock_id)
            volume_lots: int = volume_map.get(stock_id, 0)

            # NaN 一定要先擋：`NaN < 門檻` 恆為 False，不擋會一路走成買進候選
            if any(
                value is None or pd.isna(value) or not value
                for value in (signal_close, base_close, raw_close)
            ):
                continue
            if trust_shares is None or pd.isna(trust_shares) or trust_shares <= 0:
                continue
            if volume_lots <= 0 or raw_close < self.MIN_PRICE:
                continue

            signal_return_pct: float = (signal_close / base_close - 1) * 100
            if signal_return_pct < self.MIN_SIGNAL_RETURN_PCT:
                continue

            turnover: float = raw_close * volume_lots * Units.LOT
            if turnover < self.MIN_TURNOVER:
                continue

            open_gap_pct: float = (quote.open / raw_close - 1) * 100
            if open_gap_pct >= self.MAX_OPEN_GAP_PCT:
                continue

            candidates.append((signal_return_pct, quote))

        return candidates

    def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """開倉訊號：T−1 漲幅達門檻且投信買超，T 日開盤買進；張數由 portfolio 層決定"""

        if not stock_quotes or self.max_holdings == 0:
            return []

        available_slots: int = self.max_holdings - self.account.get_position_count()
        if available_slots <= 0:
            return []

        signal_date: datetime.date = self.get_previous_trading_date(
            stock_quotes[0].date
        )
        if not self.check_market_uptrend(signal_date):
            return []

        candidates: List[Tuple[float, StockQuote]] = self.collect_candidates(
            stock_quotes, signal_date
        )

        # 先依分數挑滿剩餘檔數：引擎同一 bar 內依代號排序後才截斷，
        # 不在這裡挑的話，留下來的是代號小的，而不是訊號最強的
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected: List[StockQuote] = [
            quote for _, quote in candidates[:available_slots]
        ]

        for quote in selected:
            logger.info(
                f"股票 {quote.stock_id} 符合投信強勢條件（訊號日 {signal_date}）"
            )

        return [
            Signal(
                quote=quote,
                action=Action.BUY,
                position_type=PositionType.LONG,
                order_price=quote.open,  # 開盤進場：訊號在 T−1 收盤後才算得出來
                sizing_price=quote.open,
            )
            for quote in selected
        ]

    def generate_close_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """平倉訊號：持有滿 `HOLDING_DAYS` 個交易日後以收盤價出場"""

        signals: List[Signal] = []

        for quote in stock_quotes:
            positions: List[StockPosition] = self.account.get_positions(
                stock_id=quote.stock_id, position_type=PositionType.LONG
            )
            due_volume: int = 0
            for position in positions:
                holding_days: Optional[int] = self.count_holding_days(
                    position.date, quote.date
                )
                # 清單查不到（實盤日期超出回測區間）時以曆日 1.4 倍近似
                if holding_days is None:
                    holding_days = int((quote.date - position.date).days / 1.4)
                if holding_days >= self.HOLDING_DAYS:
                    due_volume += position.volume

            # 同一標的的多筆部位合併成一張單，逐筆送會被 FIFO 吃掉
            if due_volume <= 0:
                continue

            signals.append(
                Signal(
                    quote=quote,
                    action=Action.SELL,
                    position_type=PositionType.LONG,
                    order_price=quote.close,
                    volume=due_volume,
                )
            )

        return signals

    def generate_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """停損訊號：刻意不做停損（理由見 class docstring），固定回傳空列表"""

        return []
