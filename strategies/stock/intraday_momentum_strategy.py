import datetime
from typing import Any, Dict, List, Optional, Set, Union

import pandas as pd
from loguru import logger

from core.datafeed.base import BaseDataFeed
from core.market.tw.instrument_spec import TwStockSpec
from core.market.tw.market_calendar import MarketCalendar
from core.models import PreOpenStockQuote, StockAccount, StockPosition, StockQuote
from core.models.fill_config import FillConfig, VolumeCapPolicy
from core.models.stock.trading_list import DayTradeListSnapshot
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
from core.utils.instrument import StockUtils


class IntradayMomentumStrategy(BaseStockStrategy):
    """
    盤中動能策略（回測為日 K 近似；實盤逐筆觸發）

    買進條件（全部滿足）：
    - 盤中相對平盤價（前一交易日收盤；除權息日為開盤競價基準）漲幅 ≥ 門檻（預設 9%）
    - 當日累計成交量 ≥ 門檻（預設 5,000 張）
    - 當日在現股當沖名單內（當天停損要能當沖賣出，不在名單就不進場）
    - 帳上沒有該檔部位

    賣出條件：
    - 進場隔天（下一個有報價的交易日）以**開盤價**出場

    停損條件：
    - **只看進場當天**：價格跌破平盤價 × (1 + 停損門檻)（預設 8%）立刻賣出，
      當天進出即現股當沖，證交稅走當沖減半

    〈日 K 近似〉日 K 看不出盤中先後，以下假設刻意偏向「看得出偏差方向」：
    - 進場：當日最高價 ≥ 觸發價即視為觸發，以觸發價成交；開盤就已在觸發價之上時以開盤價成交。
    - 成交量：用當日**總量**判斷，盤中觸發當下的累計量可能還沒到——**偏樂觀**。
    - 停損：依 K 棒方向推盤中路徑（OHLC 回測的常見慣例），跌破時以停損價出場：
      - 陽線（收盤 ≥ 開盤）視為「開 → 低 → 高 → 收」：最低價出現在拉漲、進場之前，
        進場後只看收盤是否跌破停損價。
      - 陰線（收盤 < 開盤）視為「開 → 高 → 低 → 收」：最低價出現在進場之後，最低價跌破即停損。
      漲到 9% 的股票多半開低走高，若一律拿最低價判斷，當天的低點幾乎都在進場前，
      會把九成以上的交易誤判成當天停損。開盤後先探底、拉上 9% 又跌回 8% 再收高的陽線會漏判——**偏樂觀**。
    - 成交量上限：單筆最多買當日成交量的 10%（超過截斷），避免複利放大後的部位大到市場吃不下。
    - 全日鎖漲停（一價到底）的買單由引擎拒絕，與實盤排不到隊一致。

    〈與實盤的已知差異〉
    - 引擎對當沖策略採「先開倉、再平倉」，隔天開盤出場的部位要等當天的新倉開完才賣：
      開倉時這些部位仍佔持倉名額、賣出款也還沒回到可用餘額——**偏保守**（實盤是開盤先賣、盤中才買）。
    - 同一檔隔天又觸發時，回測因為部位還在帳上而不進場；實盤在開盤賣出後盤中可以再進。

    〈實盤執行〉
    - 開倉與停損在盤中逐筆段落（`IMMEDIATE`）呼叫，每次只拿到一檔的一筆報價（`Scale.TICK`）：
      - 進場：**現價**相對平盤價漲幅 ≥ 門檻、當日累計量 ≥ 門檻、在當沖名單內，以現價為決策價買進；
        同一檔一天只進場一次（送出就算，沒成交也不再追）。
      - 停損：當天進場的部位，現價跌到停損價以下即賣出；送出後部位仍在（IOC 沒成交）時，
        隔 `STOP_LOSS_RETRY_SECONDS` 秒再送一次，不追價。
      - 逐筆不需要 K 棒路徑假設：盤中的先後順序就是報價到達的順序。
    - 隔天開盤出場在開盤段（`AT_OPEN`）呼叫：盤前沒有開盤價，以參考價為決策價，
      執行層換成開盤集合競價掛跌停＋ROD，成交價即開盤價。
    - 執行方式 `MARKET`（要成交）：盤中由執行層換成決策價加保護價的限價＋IOC，沒成交即作廢。
    - 標的池：券商單一連線的逐筆訂閱上限 200 檔，盤前由 `get_live_symbols()` 篩選
      「最近一個交易日在當沖名單內的一般股票（排除 ETF 與權證）中，成交量最大的前 `LIVE_UNIVERSE_SIZE` 檔」。
      **前一天冷門、當天才爆量的股票會漏掉**，這是與日 K 回測（全市場）最大的差異。
    - 當沖名單：當天那一份收盤後才入庫，盤中沿用最近一份（見實盤資料源的 `get_day_trade_list()`）。
    - 時區：逐筆報價與盤中成交的日期都帶時刻（`datetime`），比較開倉日前一律轉成日期。

    **TICK 級別回測不支援**：`setup_apis()` 會直接 `NotImplementedError`。
    """

    DEFAULT_MAX_HOLDINGS: int = 10
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2020, 5, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2025, 5, 31)

    # 進出場參數
    MIN_PRICE_CHANGE_PCT_FOR_ENTRY: float = 9.0  # 相對平盤價之進場漲幅（%）
    STOP_LOSS_PRICE_CHANGE_PCT: float = 8.0  # 跌破此漲幅即停損（%）
    MIN_VOLUME_LOTS: int = 5000  # 當日累計成交量門檻（張）
    MAX_VOLUME_SHARE: float = 0.1  # 單筆訂單不超過當日成交量的比例

    # 實盤參數
    LIVE_UNIVERSE_SIZE: int = (
        200  # 盤前篩選的標的數上限（檔）；等於券商單一連線的逐筆訂閱上限
    )
    STOP_LOSS_RETRY_SECONDS: int = 30  # 停損送出後部位仍在時，再送一次的間隔（秒）

    # 交易日清單往回多抓的曆日數；與日曆的最大回看天數對齊，
    # 否則第一根 bar 在清單裡查不到前一交易日、退回逐日查資料庫
    CALENDAR_LOOKBACK_DAYS: int = MarketCalendar.MAX_LOOKBACK_DAYS

    # 停損價要「嚴格低於」門檻：門檻剛好落在檔位上時往下退一檔。
    # 比最小檔位（0.01）小一個數量級，扣掉後捨去必定落在下一檔
    STRICT_BELOW_EPSILON: float = 0.001

    def __init__(self) -> None:
        super().__init__()

        self.strategy_name: str = "Intraday-Momentum"
        self.init_capital: float = 1000000.0
        self.max_holdings: int = self.DEFAULT_MAX_HOLDINGS
        self.scale: Scale = Scale.DAY
        self.direction: TradeDirection = TradeDirection.LONG
        # 進場當天停損就是現股當沖：引擎依此採先開後平，當天的新倉當天就會檢查停損
        self.allow_day_trade: bool = True

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 成交假設：只設成交量上限，不設滑價（觸發價、停損價本身就是對齊檔位後的成交價）
        self.fill_config: FillConfig = FillConfig(
            max_volume_share=self.MAX_VOLUME_SHARE,
            volume_cap_policy=VolumeCapPolicy.TRUNCATE,
        )

        # 檔位與幅度換算一律走台股規格，避免浮點相乘後對齊差一檔
        self.instrument: TwStockSpec = TwStockSpec()

        # 回測區間的交易日清單；`setup_apis()` 建一次
        self.trading_days: List[datetime.date] = []

        # 現股當沖名單由資料源提供（策略不自己建 API）
        self.feed: Optional[BaseDataFeed] = None

        # 平盤價快取：同一根 bar 內開倉與停損都要用，只留最近一天
        self.reference_price_date: Optional[datetime.date] = None
        self.reference_price_map: Dict[str, Any] = {}

        # 實盤（見 class docstring〈實盤執行〉）
        self.is_tick_triggered = True
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.IMMEDIATE,
            LiveHook.STOP_LOSS.value: ExecutionTiming.IMMEDIATE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
        }
        self.live_execution = ExecutionStyle.MARKET
        # 模擬環境的額度與檔數比照 `VolumeBreakoutMomentumStrategy`；正式環境要在上線前重新決定
        self.live_capital = 400000.0
        self.live_max_holdings = 3

        # 逐筆狀態：只對當天有效，換日時清掉。行程重啟會遺失——重啟後同一檔可能再進場一次，
        # 由「帳上已有部位就不進場」擋住已成交的那些
        self.live_trading_date: Optional[datetime.date] = None
        self.live_entered: Set[str] = set()
        self.stop_loss_sent_at: Dict[str, datetime.datetime] = {}

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """
        - Description:
            宣告本策略要用的資料源；實例由 DataFeed 統一持有
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
        - Raise:
            - NotImplementedError
                `scale` 不是 `Scale.DAY`（逐筆分支尚未實作）
        """

        if self.scale != Scale.DAY:
            raise NotImplementedError(
                f"{self.strategy_name} 目前只有日 K 近似版本（Scale.DAY），"
                f"逐筆分支尚未實作。目前的 scale 是 {self.scale}"
            )

        self.feed = feed
        self.price = feed.price
        self.trading_days = self.build_trading_days()

    def build_trading_days(self) -> List[datetime.date]:
        """預先取好回測區間（含起始日前的回看窗）的交易日清單"""

        if self.price is None or not self.start_date or not self.end_date:
            return []

        return self.price.get_trading_days(
            self.start_date - datetime.timedelta(days=self.CALENDAR_LOOKBACK_DAYS),
            self.end_date,
        )

    def get_previous_trading_date(self, date: datetime.date) -> datetime.date:
        """取得前一個交易日；清單涵蓋範圍內查清單，超出時（實盤）回頭問資料庫"""

        # 只在清單涵蓋的範圍內平移：超出時平移會落到清單最後一天，拿錯的昨收而且不報錯
        if self.trading_days and date <= self.trading_days[-1]:
            previous: Optional[datetime.date] = MarketCalendar(
                self.trading_days
            ).shift_trading_days(date, offset=-1)
            if previous is not None:
                return previous

        return MarketCalendar.previous_trading_day_from_api(self.price, date)

    def get_reference_price_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得當日的**平盤價**對照表，即漲幅、觸發價與停損價的基準

            用原始價而不是還原價：觸發與停損是拿當日最高／最低價（原始價）去比，
            基準必須同一個口徑。平常是前一交易日收盤；**除權息日改用交易所公告的
            開盤競價基準**——沿用前收會讓整段門檻偏高，配息越大偏得越多。
            這與看盤軟體顯示的「漲幅」、引擎判定漲跌停用的基準相同。
        - Parameters:
            - date: datetime.date
                當前交易日
        - Return:
            - Dict[str, Any]
                {stock_id: 平盤價}
        """

        if self.reference_price_date == date:
            return self.reference_price_map

        yesterday: datetime.date = self.get_previous_trading_date(date)
        reference_price_map: Dict[str, Any] = self.price.get_close_map(yesterday)
        # 非除權息日回傳空 dict，覆蓋後與原本相同
        reference_price_map.update(
            self.price.get_dividend_api().get_opening_reference_price_map(date)
        )

        self.reference_price_date = date
        self.reference_price_map = reference_price_map
        return self.reference_price_map

    def get_valid_reference_price(
        self, stock_quote: StockQuote, reference_price_map: Dict[str, Any]
    ) -> Optional[float]:
        """取得有效的平盤價；缺資料、NULL（讀進來是 NaN）或 0 時為 None"""

        reference_price: Any = reference_price_map.get(stock_quote.stock_id)
        # NaN 一定要擋：`high < NaN` 恆為 False，不擋會一路走成買進候選
        if reference_price is None or pd.isna(reference_price) or not reference_price:
            return None
        return float(reference_price)

    def get_entry_trigger_price(self, reference_price: float, stock_id: str) -> float:
        """進場觸發價：平盤價 × (1 + 進場門檻) 往上對齊檔位（第一個達到門檻的價位）"""

        return self.instrument.round_to_tick(
            self.instrument.scale_price(
                reference_price, self.MIN_PRICE_CHANGE_PCT_FOR_ENTRY / 100
            ),
            "up",
            stock_id,
        )

    def get_stop_loss_price(self, reference_price: float, stock_id: str) -> float:
        """停損價：**嚴格低於**平盤價 × (1 + 停損門檻) 的最高檔位（跌破才停損）"""

        threshold: float = self.instrument.scale_price(
            reference_price, self.STOP_LOSS_PRICE_CHANGE_PCT / 100
        )
        stop_price: float = self.instrument.round_to_tick(threshold, "down", stock_id)
        if stop_price >= threshold:
            stop_price = self.instrument.round_to_tick(
                threshold - self.STRICT_BELOW_EPSILON, "down", stock_id
            )
        return stop_price

    def get_day_trade_list(self, date: datetime.date) -> Optional[DayTradeListSnapshot]:
        """當日的現股當沖名單；資料源沒有名單時為 None"""

        if self.feed is None:
            return None
        return self.feed.get_day_trade_list(date)

    @staticmethod
    def to_date(value: Union[datetime.date, datetime.datetime]) -> datetime.date:
        """逐筆報價與盤中成交的日期帶時刻，日 K 與重建的部位只有日期；比較前一律轉成日期"""

        if isinstance(value, datetime.datetime):
            return value.date()
        return value

    def get_live_symbols(self, latest_date: datetime.date) -> Optional[List[str]]:
        """
        - Description:
            盤前篩選逐筆訂閱的標的：最近一個交易日在當沖名單內的一般股票中，
            成交量最大的前 `LIVE_UNIVERSE_SIZE` 檔

            不在當沖名單的標的本來就不進場，先排除才不會浪費訂閱名額；
            以前一天的成交量排序是「當天會不會爆量」最便宜的代理指標。
            **只留一般股票**（`StockUtils.filter_common_stocks()`，排除 ETF 與權證）：
            與回測的報價轉換同一份過濾，回測本來就看不到 ETF；成交量大的 ETF 幾乎不會漲 9%，
            留著只會佔掉訂閱名額。
        - Parameters:
            - latest_date: datetime.date
                歷史資料最新的交易日
        - Return:
            - Optional[List[str]]
                要訂閱的標的；沒有當沖名單時為空 list（不進場的日子不必訂閱）
        """

        day_trade_list: Optional[DayTradeListSnapshot] = self.get_day_trade_list(
            latest_date
        )
        if day_trade_list is None:
            logger.warning(f"{latest_date} 沒有現股當沖名單，盤中不訂閱任何標的")
            return []

        volumes: Dict[str, int] = self.price.get_volume_lots_map(latest_date)
        candidates: List[str] = [
            stock_id
            for stock_id in StockUtils.filter_common_stocks(list(volumes))
            if stock_id in day_trade_list.day_tradable
        ]
        candidates.sort(key=lambda stock_id: volumes[stock_id], reverse=True)
        return sorted(candidates[: self.LIVE_UNIVERSE_SIZE])

    def reset_live_state(self, date: datetime.date) -> None:
        """逐筆狀態換日：前一天的進場與停損紀錄不得延續到今天"""

        if self.live_trading_date == date:
            return

        self.live_trading_date = date
        self.live_entered = set()
        self.stop_loss_sent_at = {}

    def is_stop_loss_hit(self, stock_quote: StockQuote, stop_price: float) -> bool:
        """
        - Description:
            以日 K 推斷進場**之後**價格是否跌到停損價

            陽線的最低價出現在拉漲之前（開 → 低 → 高 → 收），那時還沒進場，
            進場後的最低點只能確定不高於收盤；陰線的最低價出現在最高價之後
            （開 → 高 → 低 → 收），也就是進場之後。
        - Parameters:
            - stock_quote: StockQuote
                進場當天的日 K
            - stop_price: float
                停損價
        - Return:
            - bool
                是否停損
        """

        if stock_quote.close >= stock_quote.open:
            return stock_quote.close <= stop_price
        return stock_quote.low <= stop_price

    def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """開倉訊號：盤中漲幅觸及門檻、量達門檻且可當沖，以觸發價（跳空時為開盤價）做多"""

        if self.max_holdings == 0 or not stock_quotes:
            return []

        if stock_quotes[0].scale == Scale.TICK:
            return self.generate_tick_open_signals(stock_quotes)

        date: datetime.date = stock_quotes[0].date
        reference_price_map: Dict[str, Any] = self.get_reference_price_map(date)

        # 不在名單就不進場；整天沒有名單時等於每一檔都不在名單——
        # 照常進場的話，當天停損可能根本賣不掉，回測卻照樣以停損價出場
        day_trade_list: Optional[DayTradeListSnapshot] = self.get_day_trade_list(date)
        if day_trade_list is None:
            logger.warning(f"{date} 沒有現股當沖名單，本日不進場")
            return []

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            # 同一檔不重複持有（回測與實盤的差異見 class docstring）
            if self.account.check_has_position(stock_quote.stock_id):
                continue

            if stock_quote.volume < self.MIN_VOLUME_LOTS:
                continue

            reference_price: Optional[float] = self.get_valid_reference_price(
                stock_quote, reference_price_map
            )
            if reference_price is None:
                continue

            trigger_price: float = self.get_entry_trigger_price(
                reference_price, stock_quote.stock_id
            )
            if stock_quote.high < trigger_price:
                continue

            if stock_quote.stock_id not in day_trade_list.day_tradable:
                continue

            # 開盤就跳空在觸發價之上時，第一筆能買到的就是開盤價
            entry_price: float = max(stock_quote.open, trigger_price)
            logger.info(
                f"股票 {stock_quote.stock_id} 盤中觸及 {trigger_price}"
                f"（平盤價 {reference_price}），進場價 {entry_price}"
            )

            # 算張數也用進場價：盤中進場時還不知道收盤價
            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.BUY,
                    position_type=PositionType.LONG,
                    order_price=entry_price,
                    sizing_price=entry_price,
                )
            )

        return signals

    def generate_tick_open_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """逐筆開倉：現價漲幅達門檻、累計量達門檻且可當沖，以現價為決策價做多"""

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            date: datetime.date = self.to_date(stock_quote.date)
            self.reset_live_state(date)

            stock_id: str = stock_quote.stock_id
            if stock_id in self.live_entered:
                continue
            if self.account.check_has_position(stock_id):
                continue
            # 實盤的成交量是券商的當日累計量（張）
            if stock_quote.volume < self.MIN_VOLUME_LOTS:
                continue

            reference_price: Optional[float] = self.get_valid_reference_price(
                stock_quote, self.get_reference_price_map(date)
            )
            if reference_price is None:
                continue

            trigger_price: float = self.get_entry_trigger_price(
                reference_price, stock_id
            )
            if stock_quote.close < trigger_price:
                continue

            day_trade_list: Optional[DayTradeListSnapshot] = self.get_day_trade_list(
                date
            )
            if day_trade_list is None or stock_id not in day_trade_list.day_tradable:
                continue

            # 送出就算進場過：之後每一筆報價都還在門檻之上，不擋的話會逐筆重送，
            # 被持倉上限擋下時也會逐筆寫風控事件
            self.live_entered.add(stock_id)
            logger.info(
                f"股票 {stock_id} 盤中現價 {stock_quote.close} 觸及 {trigger_price}"
                f"（平盤價 {reference_price}、累計量 {stock_quote.volume} 張），進場"
            )
            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.BUY,
                    position_type=PositionType.LONG,
                    order_price=stock_quote.close,
                    sizing_price=stock_quote.close,
                )
            )

        return signals

    def get_exit_price(self, stock_quote: StockQuote) -> float:
        """隔天出場的決策價：回測是開盤價；實盤開盤段在盤前，只有參考價"""

        if isinstance(stock_quote, PreOpenStockQuote):
            return stock_quote.reference_price
        return stock_quote.open

    def generate_close_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """平倉訊號：進場日之前開的部位，一律在當日開盤出場"""

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            date: datetime.date = self.to_date(stock_quote.date)
            positions: List[StockPosition] = [
                position
                for position in self.account.get_positions(
                    stock_id=stock_quote.stock_id, position_type=PositionType.LONG
                )
                if self.to_date(position.date) < date
            ]
            # 同一檔多筆部位合併成一張單：逐筆送會被 FIFO 吃掉後面那筆的張數
            volume: int = sum(position.volume for position in positions)
            if volume <= 0:
                continue

            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.SELL,
                    position_type=PositionType.LONG,
                    order_price=self.get_exit_price(stock_quote),
                    volume=volume,
                )
            )

        return signals

    def generate_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """停損訊號：只對當天進場的部位，依 K 棒方向判斷進場後是否跌破停損價，以停損價賣出"""

        if not stock_quotes:
            return []

        if stock_quotes[0].scale == Scale.TICK:
            return self.generate_tick_stop_loss_signals(stock_quotes)

        reference_price_map: Dict[str, Any] = self.get_reference_price_map(
            stock_quotes[0].date
        )

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            positions: List[StockPosition] = [
                position
                for position in self.account.get_positions(
                    stock_id=stock_quote.stock_id, position_type=PositionType.LONG
                )
                if position.date == stock_quote.date
            ]
            volume: int = sum(position.volume for position in positions)
            if volume <= 0:
                continue

            reference_price: Optional[float] = self.get_valid_reference_price(
                stock_quote, reference_price_map
            )
            if reference_price is None:
                continue

            stop_price: float = self.get_stop_loss_price(
                reference_price, stock_quote.stock_id
            )
            if not self.is_stop_loss_hit(stock_quote, stop_price):
                continue

            logger.warning(
                f"股票 {stock_quote.stock_id} 進場當天跌破 {stop_price}，停損"
            )
            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.SELL,
                    position_type=PositionType.LONG,
                    order_price=stop_price,
                    volume=volume,
                )
            )

        return signals

    def generate_tick_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """逐筆停損：當天進場的部位，現價跌到停損價以下即以現價為決策價賣出"""

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            now: datetime.datetime = stock_quote.date
            date: datetime.date = self.to_date(now)
            self.reset_live_state(date)

            stock_id: str = stock_quote.stock_id
            positions: List[StockPosition] = [
                position
                for position in self.account.get_positions(
                    stock_id=stock_id, position_type=PositionType.LONG
                )
                if self.to_date(position.date) == date
            ]
            volume: int = sum(position.volume for position in positions)
            if volume <= 0:
                continue

            reference_price: Optional[float] = self.get_valid_reference_price(
                stock_quote, self.get_reference_price_map(date)
            )
            if reference_price is None:
                continue

            stop_price: float = self.get_stop_loss_price(reference_price, stock_id)
            if stock_quote.close > stop_price:
                continue

            # 部位要等成交回報才會消失：剛送出的停損還在路上時，下一筆報價不可再送一張
            sent_at: Optional[datetime.datetime] = self.stop_loss_sent_at.get(stock_id)
            if (
                sent_at is not None
                and (now - sent_at).total_seconds() < self.STOP_LOSS_RETRY_SECONDS
            ):
                continue

            self.stop_loss_sent_at[stock_id] = now
            logger.warning(
                f"股票 {stock_id} 進場當天現價 {stock_quote.close} 跌到 {stop_price}，停損"
            )
            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.SELL,
                    position_type=PositionType.LONG,
                    order_price=stock_quote.close,
                    volume=volume,
                )
            )

        return signals
