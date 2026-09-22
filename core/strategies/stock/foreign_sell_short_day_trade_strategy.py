# Python standard library
import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from core.backtest.datafeed.tw.market_calendar import MarketCalendar
from core.backtest.models.fill_model import FillConfig, VolumeCapPolicy
from core.datafeed.base import BaseDataFeed
from core.models import StockAccount, StockPosition, StockQuote
from core.portfolio.signal import Signal
from core.strategies.stock import BaseStockStrategy
from core.utils import Action, PositionType, Scale, Units


class ForeignSellShortDayTradeStrategy(BaseStockStrategy):
    """
    外資大賣強勢股當沖放空（日線）

    進出場都在同一個交易日，本質是**現股「先賣後買」當日沖銷**，不是跨日融券放空：
    免融券保證金、免借券費、證交稅走當沖減半。`enable_intraday=True` ＋
    `position_type=SHORT` 會讓引擎自動採用 `ShortMethod.DAY_TRADE` 與
    `OPEN_THEN_CLOSE`，同一根 bar 內的開平倉才成立。

    賣出（開倉）條件（全部以 T−1 已收盤的資料判斷，T 日開盤放空）：
    - T−1 外資賣超 ≥ 門檻（預設 1,000 張）
    - T−1 收盤相對 T−2 收盤漲幅 > 門檻（預設 8%）
    - T−1 成交量 ≥ 門檻（預設 1,000 張）

    回補（平倉）條件：
    - 同一交易日回補，不留倉；當日開的部位以**收盤價**平倉（尾盤近似）
    - 例外一：疑似全日鎖漲停者不送回補單，交由引擎判定並計入 `limit_up_cover_failed`
    - 例外二：**被迫留倉的部位改以開盤價回補**——當沖沖銷失敗轉成融券後，
      T+2 交割壓力與券商風控都要求盡早了結，不可能等到尾盤挑價（見 `get_cover_price()`）

    停損條件：
    - **刻意不做**（一律不回傳停損單）。理由不是「日線做不到」，而是**實測會虧**：
      以 2020~2025 的 1,086 筆當日進出、用各檔當日最高價回測各種停損水位，
      **每一個水位都讓總損益變差**——

      | 停損 | 觸發筆數 | 總損益變化 | 最差單筆 ROI |
      |-----:|---------:|-----------:|-------------:|
      | +3%  | 527 | **−354 萬**（吃掉 85% 獲利） | −3.00% |
      | +5%  | 291 | −249 萬 | −5.14% |
      | +7%  | 130 | −105 萬 | −7.29% |
      | +10% |  19 | −34 萬 | −10.16% |

      原因很直接：本策略放空的是**剛噴出的高波動股**，它們盤中常再衝高一段才拉回收低。
      停損砍掉的正是那些最終會獲利的部位，而這支策略的 edge 本來就集中在少數交易
      （最賺的 50 筆貢獻 72.1% 總損益），截斷右尾等於自斷手腳。
      上表的模擬還**假設停損恰好成交在觸發價**（不計滑價與檔位），
      也就是說真實的停損表現只會**比這更差**。
    - **風險上界不靠停損，靠三件事**：① 當日必平，損失以當日開盤到收盤的區間為限
      （實測當日進出的最差單筆 −12.36%）；② 漲跌停限制了單日的不利幅度；
      ③ 唯一會過夜的「鎖漲停轉融券留倉」路徑由 `max_holding_days`／`max_no_quote_days`
      封住（見 `__init__`）——**尾部風險確實在那條路上**：留倉部位的最差單筆是
      **−21.65%**，幾乎是當日進出最差值的兩倍。
    - **引擎的維持率追繳幫不上忙**：融券維持率 130% 要價格逆行約 **+46%** 才觸發
      （擔保價款 ＋ 90% 保證金 ÷ 市值），對本策略是極遙遠的後衛，實測 0 次。

    ---

    ## 解讀回測結果時務必一併看的五件事

    2020~2025 的實測（1,096 筆、勝率 59.8%、ROI 381.1%、CAGR 30.0%、MDD −11.4%、
    Sharpe 1.93）**不是穩健結論**，以下五項全部是**樂觀方向**的偏差或穩定性疑慮：

    1. **當日部位的回補價以收盤價近似尾盤**。`Scale.DAY` 沒有真正的尾盤價，落差未量化；
       要量化必須升級 `Scale.TICK`（tick 資料在 DolphinDB，非 `tw_stock.db`）。
       被迫留倉的部位已改用開盤價（見 `get_cover_price()`）。
    2. **未排除處置股／非當沖標的**。兩者都沒有資料源（見 `REJECT_BELOW_REFERENCE_OPEN`
       的說明），實際可交易的機會數會少於 1,096 筆。
    3. **部位大小以「全額買進」為基準等權切分**。現股當沖沖賣其實不需保證金，
       此假設讓名目曝險上界等於本金；改用實際資金佔用的話，同樣訊號可開的部位會大得多，
       **報酬與回撤會一起放大**。
    4. **開倉價為開盤價**。訊號在 T−1 收盤後即可算出，時序上沒有未來函數，
       但實務上要在開盤集合競價全部成交仍有執行風險。
    5. **報酬高度集中在少數交易**：最賺的 50 筆（4.6%）貢獻 **72.1%** 的總損益，
       最賺的 5 筆就佔 10.6%。這個 edge 是厚尾驅動而非廣泛穩定，
       **換區間或微調門檻可能大幅改變結論**。
    """

    DEFAULT_MAX_HOLDINGS: int = 5
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2020, 1, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2025, 12, 31)

    # 開倉訊號參數（皆為 T−1 的條件；使用者標註賣超門檻為「先暫定」，故全部可調）
    MIN_FOREIGN_NET_SELL_LOTS: int = 1000  # 外資最小賣超（張）
    MIN_PRICE_CHANGE_PCT_FOR_SIGNAL: float = 8.0  # 相對前一交易日之最小漲幅（%）
    MIN_VOLUME_LOTS: int = 1000  # 最小成交量（張）

    # 成交假設：大漲 8%+ 又被外資大賣的股票，隔日開盤缺口大、流動性可能驟降，
    # 以「不可能成交的開盤價」計損益會讓回測過度樂觀，故雙邊都給滑價並限量
    SLIPPAGE_BPS: float = 10.0  # 滑價基點（1 bps = 0.01%）
    MAX_VOLUME_SHARE: float = 0.05  # 單筆訂單不超過當日成交量的比例

    # 平盤下放空過濾（預設關閉）
    #
    # 台股自 2013/9/23 起**全面取消**平盤下放空限制，現行僅「處置股票」仍受限，
    # 而處置股清單目前沒有資料源，
    # 因此無法只對真正受限的標的套用。
    #
    # 引擎側的 `ShortConstraint.allow_below_reference` 目前**有定義、無呼叫端**，
    # 設了不會生效（`StockCostModel.check_unimplemented_constraints()` 會警告），
    # 所以本策略把這道過濾實作在策略層而不是交給引擎。
    #
    # 設為 True 等於把處置股規則套用到全市場，是**保守上界**而非真實規則，
    # 用來看「若限制真的存在，訊號還剩多少」。
    REJECT_BELOW_REFERENCE_OPEN: bool = False

    # 留倉部位的強制出場上限（只會作用在鎖漲停轉融券的部位，見 __init__）
    MAX_HOLDING_DAYS: int = 5  # 最長持有天數
    MAX_NO_QUOTE_DAYS: int = 3  # 連續無報價幾天後以最後可得價格出場（停牌／下市）

    # 交易日曆往前多取的曆日數：訊號需要 T−2，起始日前至少要有兩個交易日
    CALENDAR_LOOKBACK_DAYS: int = 30

    def __init__(self) -> None:
        super().__init__()

        # === 策略基本資訊 ===
        self.strategy_name: str = "Foreign-Sell-Short-Day-Trade"
        self.init_capital: float = 1000000.0
        self.max_holdings: int = self.DEFAULT_MAX_HOLDINGS
        self.scale: Scale = Scale.DAY

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # === 放空設定 ===
        # short_method 不設：當沖時由 factory 強制為 DAY_TRADE
        # cost_config 不設：設了會讓 factory 跳過 is_day_trade 的推導，當沖稅率減半失效
        #
        # short_constraint 不設：本策略維持券源檢核關閉，但這已是選擇而非限制。
        # `TwStockFillModel.check_short_borrowable()` 現在會跳過 `ShortMethod.DAY_TRADE`
        # ——沖賣是先賣後買、不需要券源——所以即使開啟也不會誤拒本策略的開倉單。
        # 仍維持關閉的理由：當日必平的沖賣本來就不吃券源，檢核對本策略沒有實際約束力，
        # 只會多一組每日融券餘額查詢。
        self.position_type: PositionType = PositionType.SHORT
        self.enable_intraday: bool = True  # 現股當沖沖賣

        # === 留倉部位的保險絲 ===
        #
        # 本策略當日必平，這兩個上限**只會作用在「鎖漲停補不到券而轉融券留倉」的部位**
        # ——也就是唯一會過夜的那條路徑，同時也是放空最致命的尾部風險。
        # 沒有上限的話，連續鎖漲停的部位會一路留到回測結束，虧損無界。
        #
        # 兩個值都刻意設得寬鬆（歷史最長留倉 4 天、無報價 0 次），
        # **不影響既有結果，只封住尾巴**。
        self.max_holding_days: int = self.MAX_HOLDING_DAYS
        self.max_no_quote_days: int = self.MAX_NO_QUOTE_DAYS

        # === 成交假設 ===
        self.fill_config: FillConfig = FillConfig(
            slippage_bps_buy=self.SLIPPAGE_BPS,
            slippage_bps_sell=self.SLIPPAGE_BPS,
            max_volume_share=self.MAX_VOLUME_SHARE,
            volume_cap_policy=VolumeCapPolicy.TRUNCATE,
        )

        # 交易日曆快取：訊號每天都要 T−1 與 T−2，逐日往前查資料庫會重複掃全市場
        self.trading_days: List[datetime.date] = []

        # 平盤價快取：同一根 bar 內開倉與平倉都要用，只留最近一天
        self.reference_price_date: Optional[datetime.date] = None
        self.reference_price_map: Dict[str, Any] = {}

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """宣告本策略要用的資料源；實例由 DataFeed 統一持有"""

        self.chip = feed.chip

        if self.scale == Scale.DAY:
            self.price = feed.price

    def get_signal_trading_days(
        self, date: datetime.date
    ) -> Optional[Tuple[datetime.date, datetime.date]]:
        """
        - Description:
            取得訊號要用的 T−1 與 T−2 兩個交易日

            以營業日平移而非曆日相減：連假會讓「昨天」整段位移到非交易日。
        - Parameters:
            - date: datetime.date
                當前交易日（T）
        - Return:
            - Optional[Tuple[datetime.date, datetime.date]]
                (T−1, T−2)；交易日資料不足以推算時為 None
        """

        if not self.trading_days:
            self.trading_days = self.price.get_trading_days(
                self.start_date - datetime.timedelta(days=self.CALENDAR_LOOKBACK_DAYS),
                self.end_date,
            )

        yesterday: Optional[datetime.date] = MarketCalendar.shift_trading_days(
            self.trading_days, date, -1
        )
        day_before: Optional[datetime.date] = MarketCalendar.shift_trading_days(
            self.trading_days, date, -2
        )

        if yesterday is None or day_before is None:
            return None
        return yesterday, day_before

    def get_reference_price_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得當日的**平盤價**對照表，即平盤下放空與漲跌停的判定基準

            平常就是 T−1 的原始收盤價（法規判定一律走原始價，不用還原價）；
            **除權息日則改用交易所另行公告的「開盤競價基準」**——那天沿用 T−1 收盤
            會讓整段區間偏移，配息越大偏得越多，且不會有任何錯誤。引擎的 `FillModel`
            走的是同一份資料（`TwStockDataFeed.get_price_limit_basis()`），
            兩邊口徑必須一致，否則策略與引擎會對同一根 bar 有不同的漲停價。

            同一根 bar 內開倉與平倉都會問到同一天，故快取最近一天的結果。
        - Parameters:
            - date: datetime.date
                當前交易日（T）
        - Return:
            - Dict[str, Any]
                {stock_id: 平盤價}；交易日資料不足以推算時為空 dict
        """

        if self.reference_price_date == date:
            return self.reference_price_map

        signal_days: Optional[Tuple[datetime.date, datetime.date]] = (
            self.get_signal_trading_days(date)
        )
        if signal_days is None:
            return {}

        reference_price_map: Dict[str, Any] = self.price.get_close_map(signal_days[0])
        # 除權息日以開盤競價基準覆蓋；非除權息日回傳空 dict，覆蓋後與原本相同
        reference_price_map.update(
            self.price.get_dividend_api().get_opening_reference_price_map(date)
        )

        self.reference_price_date = date
        self.reference_price_map = reference_price_map
        return self.reference_price_map

    def check_limit_up_locked(self, stock_quote: StockQuote) -> bool:
        """
        - Description:
            判定當日是否「一價到底且上漲」，即疑似全日鎖漲停、放空補不到券

            **這裡刻意只做形狀判定，不自己算漲停價**：漲停幅度有 7%／10% 兩段
            歷史（`PRICE_LIMIT_RATIO_LEGACY`），除權息日的基準又另行公告，
            權威判定在 `TwStockSettlementModel.check_limit_up_locked()`。
            本方法只負責「這根 bar 可疑，不要送回補單」，把最終認定交給引擎。

            誤判的代價為零：引擎若認定不是鎖漲停，會依
            `DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE` 以收盤價強制回補，
            與本策略自己送單的成交價相同，只是計入 `forced_cover_day_trade`。
        - Parameters:
            - stock_quote: StockQuote
                當日報價
        - Return:
            - bool
                True 表示疑似鎖漲停，本日不送回補單
        """

        if not (
            stock_quote.open == stock_quote.high == stock_quote.low == stock_quote.close
        ):
            return False

        reference_price: Optional[float] = self.to_float(
            self.get_reference_price_map(stock_quote.date).get(stock_quote.stock_id)
        )
        if reference_price is None:
            return False

        return stock_quote.close > reference_price

    @staticmethod
    def to_float(value: Any) -> Optional[float]:
        """把對照表取回的原始值轉為 float；缺資料或 NaN 回傳 None"""

        try:
            number: float = float(value)
        except (TypeError, ValueError):
            return None

        # NaN 不等於自己；缺資料與「有資料但值異常」在此都視為不可用
        if number != number:
            return None
        return number

    def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """開倉訊號：T−1 外資賣超且強勢，T 日以開盤價放空"""

        if not stock_quotes or self.max_holdings == 0:
            return []

        signal_days: Optional[Tuple[datetime.date, datetime.date]] = (
            self.get_signal_trading_days(stock_quotes[0].date)
        )
        if signal_days is None:
            logger.warning(f"{stock_quotes[0].date} 之前不足兩個交易日，略過開倉判斷")
            return []

        yesterday, day_before = signal_days

        # 外資買賣超單位是「股」，賣超為負值
        foreign_net_shares_map: Dict[str, Any] = self.chip.get_foreign_net_shares_map(
            yesterday
        )
        # 訊號用收盤價：T−1 與 T−2 由同一個來源決定要不要還原，不可只還原一邊
        yesterday_close_map: Dict[str, Any] = self.get_signal_close_map(
            stock_quotes, yesterday
        )
        day_before_close_map: Dict[str, Any] = self.get_signal_close_map(
            stock_quotes, day_before
        )
        yesterday_volume_map: Dict[str, int] = self.price.get_volume_lots_map(yesterday)
        # 平盤價只有開啟過濾時才用得到，不要每根 bar 都多打一次全市場查詢
        reference_price_map: Dict[str, Any] = (
            self.get_reference_price_map(stock_quotes[0].date)
            if self.REJECT_BELOW_REFERENCE_OPEN
            else {}
        )

        min_net_sell_shares: int = self.MIN_FOREIGN_NET_SELL_LOTS * Units.LOT

        open_positions: List[StockQuote] = []
        for stock_quote in stock_quotes:
            if self.account.check_has_position(stock_quote.stock_id):
                continue

            # a. T−1 外資賣超達門檻（賣超是負值，故取小於等於負門檻）
            net_shares: Optional[float] = self.to_float(
                foreign_net_shares_map.get(stock_quote.stock_id)
            )
            if net_shares is None or net_shares > -min_net_sell_shares:
                continue

            # b. T−1 收盤相對 T−2 收盤之漲幅（%）> MIN_PRICE_CHANGE_PCT_FOR_SIGNAL
            yesterday_close: Optional[float] = self.to_float(
                yesterday_close_map.get(stock_quote.stock_id)
            )
            day_before_close: Optional[float] = self.to_float(
                day_before_close_map.get(stock_quote.stock_id)
            )
            if not yesterday_close or not day_before_close:
                continue

            # 取整到小數第 4 位再比：浮點誤差會讓 108/100 算出 8.000000000000007，
            # 恰好落在門檻上的標的會不會被選中就變成看價格的二進位表示
            price_chg: float = round((yesterday_close / day_before_close - 1) * 100, 4)
            if price_chg <= self.MIN_PRICE_CHANGE_PCT_FOR_SIGNAL:
                continue

            # c. T−1 成交量 ≥ MIN_VOLUME_LOTS（張）
            if yesterday_volume_map.get(stock_quote.stock_id, 0) < self.MIN_VOLUME_LOTS:
                continue

            # d. 開盤價須有效才有得放空（成交價驗證在引擎側，這裡先擋掉缺資料）
            if stock_quote.open <= 0:
                continue

            # e.（可選）平盤下放空過濾
            if self.REJECT_BELOW_REFERENCE_OPEN:
                reference_price: Optional[float] = self.to_float(
                    reference_price_map.get(stock_quote.stock_id)
                )
                if reference_price is not None and stock_quote.open < reference_price:
                    continue

            logger.info(
                f"股票 {stock_quote.stock_id} {yesterday} 外資賣超 "
                f"{int(-net_shares / Units.LOT)} 張、漲幅 {round(price_chg, 2)}%"
            )
            open_positions.append(stock_quote)

        # 放空開倉：算量與下單都用當日開盤價（即成交價）。
        # 現股當沖沖賣不需保證金，等權切分是以「全額買進」為基準的保守假設：
        # 名目曝險上界即為初始本金，不會因為零保證金而放大到不可解釋的槓桿
        return [
            Signal(
                quote=stock_quote,
                action=Action.SELL,  # 放空開倉是賣出
                position_type=PositionType.SHORT,
                order_price=stock_quote.open,
                sizing_price=stock_quote.open,
            )
            for stock_quote in open_positions
        ]

    def generate_close_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """
        平倉訊號：當日以收盤價回補全部空單，不留倉

        **疑似全日鎖漲停的標的不送回補單**：引擎對平倉單不做價格合理性檢查，
        照送等於用「買不到的漲停價」記一筆回補，把放空最致命的尾部風險抹掉，
        而且 `limit_up_cover_failed` 會因為部位早被自己平掉而永遠是 0。
        改交給 `SettlementModel` 於收盤後判定與處理（見 `check_limit_up_locked()`）。
        """

        signals: List[Signal] = []
        for stock_quote in stock_quotes:
            if not self.account.check_has_position(
                stock_quote.stock_id, PositionType.SHORT
            ):
                continue

            if self.check_limit_up_locked(stock_quote):
                logger.warning(
                    f"股票 {stock_quote.stock_id} {stock_quote.date} 疑似全日鎖漲停，"
                    f"本日不送回補單，交由引擎判定"
                )
                continue

            # 同一標的的多筆部位合併成一張單：`close_position()` 本來就會 FIFO
            # 掃過該標的所有同向部位，逐筆送單會讓第一張就吃掉後面那筆的張數，
            # 後續訂單再以「持倉不足」警告收場
            positions: List[StockPosition] = self.account.get_positions(
                stock_quote.stock_id, PositionType.SHORT
            )
            cover_volume: int = sum(position.volume for position in positions)
            if cover_volume <= 0:
                continue

            signals.append(
                Signal(
                    quote=stock_quote,
                    action=Action.BUY,  # 放空平倉是買進回補
                    position_type=PositionType.SHORT,
                    order_price=self.get_cover_price(stock_quote, positions),
                    volume=cover_volume,
                )
            )

        return signals

    def generate_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """停損訊號：本策略未實作停損（理由見 class docstring），固定回傳空列表"""

        return []

    @staticmethod
    def get_cover_price(
        stock_quote: StockQuote, positions: List[StockPosition]
    ) -> float:
        """
        - Description:
            決定回補價：當日開的部位用收盤價，**被迫留倉的部位用開盤價**

            兩者的處境完全不同，用同一個價格會系統性高估績效：

            | 部位 | 處境 | 回補價 |
            |------|------|--------|
            | 當日開倉 | 照計畫等到尾盤出場 | **收盤價**（`Scale.DAY` 對尾盤的近似） |
            | 前一日以前開倉 | 當沖沖銷失敗、已被轉成融券 | **開盤價** |

            **為什麼留倉的不能用收盤價**：這個部位是「現股當沖沖賣沒補回來」才存在的，
            T+2 就要交割，券商的風控與交割壓力都要求盡早了結——不可能讓你悠哉等到尾盤，
            挑一個當天最好的價格出場。用收盤價等於給了這個部位一個它沒有的權利：
            **有權等待盤中跌回來**。

            實測這個差別很大：**整體 ROI 由 450.6% 降為 381.1%**、MDD 由 −9.99%
            惡化為 −11.43%，只因為 10 筆留倉部位換了回補價。
            最極端的是 2349（2024-07-09）——進場 18.10、開盤跳空 21.90、收盤跌回 18.05，
            用收盤價幾乎打平出場，用開盤價則虧 119,350。那個「跌回來」在現實中不屬於你。

            **已知簡化**：若當日開盤即漲停鎖死，實務上買方掛單未必成交，此時
            「以開盤價回補」仍偏樂觀。但一價到底的情形已由 `check_limit_up_locked()`
            先擋掉不送單，剩下的是開高走低這類**開盤確實有量**的情境，偏差有限。
        - Parameters:
            - stock_quote: StockQuote
                當日報價
            - positions: List[StockPosition]
                該標的目前的所有 SHORT 部位
        - Return:
            - float
                回補價
        """

        # 任一部位早於今日 → 整批視為被迫留倉（同一標的不會同時有兩種，
        # 因為持倉中就不會再開倉，見 `check_open_signal` 的 check_has_position）
        is_carried_over: bool = any(
            position.date < stock_quote.date for position in positions
        )

        return stock_quote.open if is_carried_over else stock_quote.close
