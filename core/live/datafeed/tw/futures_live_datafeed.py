import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.config import TW_FUTURES_DB_PATH, TW_STOCK_DB_PATH
from core.config.settings import now_live
from core.dao.connection import DBConnection, connect_sqlite
from core.live.datafeed.base import BaseLiveDataFeed, RollPlan
from core.live.datafeed.calendar import (
    BrokerContractCalendarSource,
    OfficialHolidayCalendarSource,
    TradingCalendarSource,
    WeekendCalendarSource,
)
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig, FuturesRollPlanner
from core.models import BaseQuote, FuturesOrder, PreOpenFuturesQuote
from core.strategies.base import BaseStrategy
from core.utils import (
    FUTURES_MULTIPLIER,
    Action,
    ExecutionTiming,
    FuturesPriceType,
    FuturesSession,
    OrderType,
    PositionType,
)

"""
台期貨的實盤資料源

與股票版的兩個差異：
- 歷史資料在 `tw_futures.db`。
- **標的是契約不是商品**：同一個商品同時有多個到期月在交易，要哪一個由換月規則
  決定（策略層的 `select_near_month()`），這一層只負責「給定契約代號就取得報價」。
"""


def split_contract_id(symbol: str) -> Tuple[str, str]:
    """
    把契約代號拆成商品與到期月份；拆不開時回 `(symbol, "")`

    `FuturesOrder.symbol` 是 `f"{product}{expiry}"`，本函式是它的反向。
    **放模組層而不是掛在資料源上**：帳戶重建也要用同一條規則
    （`live_position_lot` 只記 symbol），各寫一份會在換月時分岔。
    """

    if len(symbol) > 6 and symbol[-6:].isdigit():
        return (symbol[:-6], symbol[-6:])
    return (symbol, "")


class TwFuturesLiveDataFeed(BaseLiveDataFeed):
    """台期貨實盤資料源"""

    LATEST_DATE_TABLE: str = "futures_price_daily"
    HISTORY_API_HINT: str = "self.futures_price"
    # 交易日佐證用的商品：台指期是成交最活絡、掛牌從不中斷的指數期貨。
    # **必須是期貨合約**：期貨與證券的開休市日不保證一致，
    # 拿股票合約來佐證，兩邊不一致的那天會誤判為開市
    PROBE_PRODUCT: str = "TX"

    # 實盤換月日曆的範圍（曆日）：往回取資料庫的實際交易日，往後取平日扣掉官方休市日。
    # 往後 70 天涵蓋「當月 ＋ 次月」的最後交易日，換月判定只看得到這麼遠
    ROLL_CALENDAR_LOOKBACK_DAYS: int = 400
    ROLL_CALENDAR_LOOKAHEAD_DAYS: int = 70

    def __init__(
        self,
        broker: Any,
        calendar_sources: Optional[Sequence[TradingCalendarSource]] = None,
        db_path: Any = TW_FUTURES_DB_PATH,
        now_provider: Callable[[], datetime.datetime] = now_live,
        margin_config: Optional[FuturesMarginConfig] = None,
        roll_config: Optional[FuturesRollConfig] = None,
        stock_db_path: Any = TW_STOCK_DB_PATH,
    ) -> None:
        """
        - Description:
            建立台期貨實盤資料源
        - Parameters:
            - broker: Any
                券商閘道
            - calendar_sources: Optional[Sequence[TradingCalendarSource]]
                交易日來源
            - db_path: Any
                歷史資料庫路徑
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - margin_config: Optional[FuturesMarginConfig]
                策略與部位管理共用的保證金設定；查表模式下由本資料源注入保證金表
            - roll_config: Optional[FuturesRollConfig]
                策略的換月設定（實盤版）；本資料源注入日曆，並依它決定何時轉倉
            - stock_db_path: Any
                官方開休市日曆所在的資料庫（`market_holiday` 表在 `tw_stock.db`）
        """

        super().__init__(broker, calendar_sources, now_provider, db_path)

        self.futures_price: Optional[FuturesPriceAPI] = None
        self.margin: Optional[FuturesMarginAPI] = None
        self.margin_config: Optional[FuturesMarginConfig] = margin_config
        self.roll_config: Optional[FuturesRollConfig] = roll_config
        # 官方開休市日曆在 `tw_stock.db`，與期貨歷史資料不同庫，另開一條唯讀連線
        self.stock_db_path: Any = stock_db_path
        self.stock_conn: Optional[DBConnection] = None
        self.market_holiday: Optional[MarketHolidayAPI] = None

    def setup(self, strategy: BaseStrategy) -> None:
        """
        建立歷史資料 API（唯讀）與交易日來源

        期貨沿用 TWSE 公告的開休市日曆當主來源（期貨市場的休市日原則上與證券市場相同）；
        它在 `tw_stock.db`，另開一條唯讀連線。
        """

        self.conn = connect_sqlite(self.db_path, read_only=True)
        self.stock_conn = connect_sqlite(self.stock_db_path, read_only=True)
        self.futures_price = FuturesPriceAPI(conn=self.conn)
        self.margin = FuturesMarginAPI(conn=self.conn)
        self.market_holiday = MarketHolidayAPI(conn=self.stock_conn)
        self.inject_margin_api()
        self.inject_roll_calendar()

        if not self.calendar_sources:
            self.calendar_sources = [
                OfficialHolidayCalendarSource(self.market_holiday),
                WeekendCalendarSource(),
                BrokerContractCalendarSource(self._broker_contract_update_date),
            ]

        strategy.setup_apis(self)
        self.fill_default_contracts(strategy)

    def fill_default_contracts(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            策略沒宣告 `symbols` 時，以 `products` 目前掛牌的各月份契約為標的

            策略的 `select_near_month()` 要在「當天所有月份」裡挑當家契約（回測也是
            這樣餵的），只給近月的話換月判定就沒有東西可挑。月份取自券商合約檔，
            排除連續月別名。
        - Parameters:
            - strategy: BaseStrategy
                本次要跑的策略
        """

        if getattr(strategy, "symbols", None):
            return

        resolver: Any = getattr(self.broker, "resolver", None)
        products: List[str] = list(getattr(strategy, "products", []) or [])
        if resolver is None or not products:
            return

        symbols: List[str] = []
        for product in products:
            try:
                expiries: List[str] = resolver.list_index_futures_expiries(product)
            except Exception as exc:
                logger.warning(f"取不到 {product} 的掛牌月份：{exc}")
                continue
            symbols.extend(f"{product}{expiry}" for expiry in expiries)

        strategy.symbols = symbols
        logger.info(f"{type(strategy).__name__} 未宣告標的池，以 {symbols} 為標的")

    def inject_margin_api(self) -> None:
        """
        - Description:
            把保證金表注入共用的保證金設定

            與回測的 `TwFuturesDataFeed.inject_margin_api()` 同一件事：不注入的話，
            查表模式會**靜默退回「契約價值 × 10%」的近似**，送單前的保證金檢查與
            成交後的開倉判斷都跟回測對不上。明確宣告比率模式（`use_api=False`）時不注入。
        """

        if self.margin_config is None or not self.margin_config.use_api:
            return
        if self.margin_config.api is None:
            self.margin_config.api = self.margin

    def inject_roll_calendar(self) -> None:
        """
        - Description:
            建一份實盤用的期貨日曆，注入換月設定

            換月規則要算「距最後交易日還有幾個交易日」，回測的日曆涵蓋整段回測區間；
            實盤的資料庫只到前一個交易日，**未來的交易日取平日再扣掉官方休市日**
            （`ROLL_CALENDAR_LOOKAHEAD_DAYS` 天）。過去的交易日取資料庫，
            讓已發生的休市照實計入。

            官方日曆**只涵蓋已入庫的年度**：落在未入庫年度的日子（例如 12 月公告前的
            明年一月）仍以平日近似，國定假日排除不了——距到期日之間夾著假日時會多算
            一天、換月可能晚一天。
        """

        if self.roll_config is None or self.roll_config.calendar is not None:
            return

        today: datetime.date = self._now().date()
        past: List[datetime.date] = self.futures_price.get_trading_days(
            today - datetime.timedelta(days=self.ROLL_CALENDAR_LOOKBACK_DAYS),
            today - datetime.timedelta(days=1),
        )
        horizon: List[datetime.date] = [
            today + datetime.timedelta(days=offset)
            for offset in range(self.ROLL_CALENDAR_LOOKAHEAD_DAYS)
        ]
        # 休市日只存在於已入庫的年度，未入庫年度的日子扣不到任何一天，自然退回平日近似
        closures: Set[datetime.date] = (
            self.market_holiday.get_closures(horizon[0], horizon[-1])
            if self.market_holiday is not None
            else set()
        )
        future: List[datetime.date] = [
            day for day in horizon if day.weekday() < 5 and day not in closures
        ]
        self.roll_config.calendar = FuturesCalendar(past + future)

    def plan_rolls(
        self, positions: Sequence[Any], today: datetime.date
    ) -> List[RollPlan]:
        """
        - Description:
            今天要轉倉的期貨部位：換月規則選出的當家契約比部位的契約遠時，
            平舊月、以相同方向與口數開新月

            沿用回測 `TwFuturesSettlementModel.roll_positions()` 的兩條規則：
            **只往遠月換**（當家契約不比部位遠就不動）、**週契約不轉**。
            兩腿都以範圍市價（`MKP`）＋ `IOC` 送出：換月要的是「換過去」，
            平倉腿沒有立即成交就由券商取消，開倉腿隨之放棄（次日再換）。
            價格欄位放快照價，只給風控當參考，券商端不看。
        - Parameters:
            - positions: Sequence[Any]
                該策略的部位
            - today: datetime.date
                交易日
        - Return:
            - List[RollPlan]
                轉倉計畫；換月設定停用或沒有需要轉的部位時為空
        """

        planner: Optional[FuturesRollPlanner] = (
            self.roll_config.build_planner()
            if self.roll_config is not None and self.roll_config.enabled
            else None
        )
        resolver: Any = getattr(self.broker, "resolver", None)
        if planner is None or resolver is None:
            return []

        plans: List[RollPlan] = []
        for position in positions:
            if getattr(position, "is_closed", False) or not getattr(
                position, "expiry", ""
            ):
                continue
            if not planner.MONTHLY_EXPIRY_PATTERN.match(position.expiry):
                continue

            active: Optional[str] = planner.resolve_active_expiry(
                today, resolver.list_index_futures_expiries(position.product)
            )
            if active is None or active <= position.expiry:
                continue

            plan: Optional[RollPlan] = self._build_roll_plan(position, active)
            if plan is not None:
                plans.append(plan)
        return plans

    def _build_roll_plan(self, position: Any, active: str) -> Optional[RollPlan]:
        """組兩腿；取不到報價時不換（沒有參考價，風控與保證金檢查都做不了）"""

        product: str = position.product
        try:
            old_contract: Any = self.broker.resolver.resolve_index_futures(
                product, position.expiry
            )
            new_contract: Any = self.broker.resolver.resolve_index_futures(
                product, active
            )
            quotes: List[BaseQuote] = self.broker.get_futures_snapshots(
                [old_contract, new_contract]
            )
        except Exception as exc:
            logger.opt(exception=True).warning(
                f"[Roll] {position.symbol} 應換到 {active}，但取不到合約或報價：{exc}"
            )
            return None

        prices: Dict[str, float] = {quote.symbol: quote.cur_price for quote in quotes}
        old_price: Optional[float] = prices.get(f"{product}{position.expiry}")
        new_price: Optional[float] = prices.get(f"{product}{active}")
        if not old_price or not new_price:
            logger.warning(
                f"[Roll] {position.symbol} 應換到 {active}，但快照缺價，本次不換"
            )
            return None

        position_type: PositionType = position.position_type
        opening: Action = (
            Action.BUY if position_type is PositionType.LONG else Action.SELL
        )
        closing: Action = Action.SELL if opening is Action.BUY else Action.BUY

        def leg(expiry: str, action: Action, price: float) -> FuturesOrder:
            return FuturesOrder(
                product=product,
                expiry=expiry,
                date=self._now(),
                action=action,
                position_type=position_type,
                price=price,
                volume=position.volume,
                order_type=OrderType.IOC,
                price_type=FuturesPriceType.MKP,
            )

        return RollPlan(
            close_order=leg(position.expiry, closing, old_price),
            open_order=leg(active, opening, new_price),
            reason=f"{product}{position.expiry} → {product}{active}（{position.volume} 口）",
        )

    def _probe_contract(self, resolver: Any) -> Optional[Any]:
        """
        交易日佐證取**期貨**合約：先問掛牌月份，再取最近的一個

        **不可寫死到期月**：寫死的那個月一交割就永遠查不到合約，
        平日的交易日判定會靜默失去唯一佐證。查不到掛牌月份時回 None（不猜）。
        """

        expiries: List[str] = resolver.list_index_futures_expiries(self.PROBE_PRODUCT)
        if not expiries:
            return None
        return resolver.resolve_index_futures(self.PROBE_PRODUCT, expiries[0])

    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        """
        - Description:
            依段落取得即時報價；`symbols` 是**契約代號**（`{商品}{YYYYMM}`）
        - Parameters:
            - timing: ExecutionTiming
                執行段落
            - symbols: Sequence[str]
                契約代號
        - Return:
            - List[BaseQuote]
                報價
        """

        contracts: List[Any] = [
            contract
            for contract in (self._resolve_contract(symbol) for symbol in symbols)
            if contract is not None
        ]

        if timing is ExecutionTiming.AT_OPEN:
            return self._build_pre_open_quotes(contracts)

        return list(self.broker.get_futures_snapshots(contracts))

    def _build_pre_open_quotes(self, contracts: Sequence[Any]) -> List[BaseQuote]:
        """由合約檔的參考價與漲跌停組出盤前報價"""

        now: datetime.datetime = self._now()
        quotes: List[BaseQuote] = []

        for contract in contracts:
            reference: float = float(getattr(contract, "reference", 0.0) or 0.0)
            if reference <= 0:
                logger.warning("盤前取不到期貨參考價，本段落略過此契約")
                continue

            symbol: str = str(getattr(contract, "symbol", ""))
            product, expiry = split_contract_id(symbol)
            quotes.append(
                PreOpenFuturesQuote(
                    product=product,
                    expiry=expiry,
                    # 與股票盤前一致（那邊取的就是 `self._now().date()`）：
                    # 同一個段落的兩個市場不該給出不同型別的日期
                    date=now.date(),
                    reference_price=reference,
                    session=self._resolve_session(now),
                    multiplier=self._resolve_multiplier(product, contract),
                    limit_up=self._as_optional_float(contract, "limit_up"),
                    limit_down=self._as_optional_float(contract, "limit_down"),
                )
            )
        return quotes

    def _resolve_contract(self, symbol: str) -> Optional[Any]:
        """
        以契約代號取得合約

        代號格式是 `{商品}{YYYYMM}`（與 `FuturesOrder.symbol` 相同，Ex: `TX202610`）；
        拆不開時略過該契約並記 warning，
        不中斷整段——一個代號打錯不該讓其他契約也收不到報價。
        """

        resolver: Any = getattr(self.broker, "resolver", None)
        if resolver is None:
            return None

        product, expiry = split_contract_id(symbol)
        if not expiry:
            logger.warning(f"契約代號 {symbol} 拆不出到期月份，本段落略過")
            return None

        try:
            return resolver.resolve_index_futures(product, expiry)
        except LookupError as exc:
            logger.warning(f"取不到契約 {symbol}，本段落略過：{exc}")
            return None

    @staticmethod
    def _resolve_session(moment: datetime.datetime) -> FuturesSession:
        """
        依時刻判定日盤或夜盤

        日盤 08:45~13:45、夜盤 15:00~次日 05:00；兩段之間的空檔歸日盤——
        那段時間沒有行情，但歸夜盤會讓收盤後的快照被記成次一交易日的帳。
        """

        hour: int = moment.hour
        return FuturesSession.NIGHT if hour >= 15 or hour < 5 else FuturesSession.DAY

    @staticmethod
    def _resolve_multiplier(product: str, contract: Any) -> int:
        """
        取契約乘數

        指數期貨查登錄表；查不到改用合約的欄位（股期的乘數會隨除權息調整，
        寫死必錯）。兩邊都沒有回 0——**猜一個值會讓 PnL 靜默偏掉**。
        """

        # 合約的 `symbol` 前綴是 Shioaji 分類（TXF），登錄表的鍵是 TAIFEX 代碼（TX），
        # 故先試合約自己的欄位再退回登錄表
        for field in ("multiplier", "unit"):
            value: Any = getattr(contract, field, None)
            if value:
                return int(value)

        if product in FUTURES_MULTIPLIER:
            return int(FUTURES_MULTIPLIER[product])

        logger.warning(f"取不到 {product} 的契約乘數，PnL 將無法計算")
        return 0

    def close(self) -> None:
        """
        關閉兩條唯讀連線；可重複呼叫

        期貨比其他市場多一條：官方開休市日曆的 `market_holiday` 表在
        `tw_stock.db`，與期貨歷史資料不同庫。漏關它會在每天重跑的行程裡
        一天洩一條連線。
        """

        super().close()

        if self.stock_conn is not None:
            self.stock_conn.close()
            self.stock_conn = None
