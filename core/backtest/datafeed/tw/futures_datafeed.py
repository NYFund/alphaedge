import datetime
from typing import Callable, Dict, List, Optional, Set

from loguru import logger

from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.api.tw.futures_stock_universe_api import FuturesStockUniverseAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.backtest.datafeed.base import BaseDataFeed
from core.backtest.datafeed.tw.futures_calendar import FuturesCalendar
from core.backtest.datafeed.tw.futures_roll import FuturesRollConfig
from core.config import TW_FUTURES_DB_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.managers.futures.position_manager import FuturesMarginConfig
from core.models import FuturesQuote
from core.strategies.base import BaseStrategy
from core.utils import FuturesSession, Scale
from core.utils.constant import FUTURES_MULTIPLIER

"""TwFuturesDataFeed: 台期貨資料源（tw_futures.db 的日行情與保證金 ＋ 交易日判定）"""


class TwFuturesDataFeed(BaseDataFeed):
    """
    台期貨資料源

    與 `TwStockDataFeed` 的三個結構差異，每一個都直接影響回測正確性：

    1. **一天不只一個報價**：同一天同一商品有多個到期月在交易，本 feed 一律把
       當日**所有契約**轉出（換月是策略的政策，見 `FuturesQuoteAdapter`）。
    2. **必須指定交易時段**：日盤與夜盤是兩筆獨立行情，而同一契約兩個時段的
       `symbol` 完全相同（`{product}{expiry}`）。兩者混在同一根 bar 傳進引擎，
       `quote_map` 會互相覆蓋、訊號也會被算兩次。故本 feed 只取**策略宣告的那一個
       時段**（`strategy.session`），不提供「兩個都拿」的選項。
    3. **沒有還原價**：期貨沒有除權息，`get_quotes()` 的 `adjusted` 參數一律忽略。

    **交易日取自行情表當日有資料的日子**（颱風假、補行交易日自動涵蓋）；
    結算日與夜盤跨日由 `FuturesCalendar` 處理。
    """

    # 日曆往回測結束日之後多取的曆日數：末段契約的最後交易日可能落在區間外
    CALENDAR_LOOKAHEAD_DAYS: int = 45

    def __init__(
        self,
        margin_config: Optional[FuturesMarginConfig] = None,
        roll_config: Optional[FuturesRollConfig] = None,
    ) -> None:
        # 單次回測共用一條 SQLite 連線：行情與保證金查的是同一個 DB 檔
        self.conn: Optional[DBConnection] = None

        # 本次回測的保證金設定；`setup()` 會把建好的 API 注入其中，
        # 讓策略層與部位管理層**共用同一個查表來源**（見 `inject_margin_api()`）
        self.margin_config: Optional[FuturesMarginConfig] = margin_config

        # 本次回測的換月設定；`setup()` 會把建好的日曆注入其中，
        # 讓策略挑合約與結算模型轉倉共用同一份規則
        self.roll_config: Optional[FuturesRollConfig] = roll_config

        self.futures_price: Optional[FuturesPriceAPI] = None  # 期貨日行情
        self.margin: Optional[FuturesMarginAPI] = None  # 保證金歷史
        # 股期標的池：**股期的乘數是會隨除權息調整的契約單位**，不在
        # `FUTURES_MULTIPLIER` 裡，必須逐日查表（見 `resolve_multiplier()`）
        self.universe: Optional[FuturesStockUniverseAPI] = None

        # 股期保證金要用**標的證券**的收盤價，而那在另一個 DB 檔（`tw_stock.db`）。
        # **延遲建立**：只跑指數期貨的回測一輩子不會用到它，沒有理由先開一條連線
        self.stock_conn: Optional[DBConnection] = None
        self.stock_price: Optional[StockPriceAPI] = None
        # {交易日: {股票代號: 收盤價}}，一天只查一次全市場
        self.underlying_close_cache: Dict[datetime.date, Dict[str, float]] = {}

        # 本次回測的商品與時段（由策略宣告）
        self.products: List[str] = []
        self.session: FuturesSession = FuturesSession.DAY

        # 回測區間與期貨交易日曆（整場只建一次）
        self.start_date: Optional[datetime.date] = None
        self.end_date: Optional[datetime.date] = None
        self.calendar: Optional[FuturesCalendar] = None

    def setup(self, strategy: BaseStrategy) -> None:
        """建立資料 API 並記下策略宣告的商品與時段"""

        # **先確保目錄存在再連線**：全新環境（CI、剛 clone 的機器）沒有
        # `data/db/`，`connect_sqlite()` 會丟出
        # `unable to open database file`——那個訊息完全看不出是「目錄不存在」。
        # 與各 loader 同一種作法；此時開出來的是空 DB，查詢一律回空結果
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(TW_FUTURES_DB_PATH)

        self.futures_price = FuturesPriceAPI(conn=self.conn)
        self.margin = FuturesMarginAPI(conn=self.conn)
        self.universe = FuturesStockUniverseAPI(conn=self.conn)

        self.products = list(getattr(strategy, "products", []) or [])
        self.session = getattr(strategy, "session", FuturesSession.DAY)
        self.start_date = strategy.start_date
        self.end_date = strategy.end_date

        self.calendar = self.build_calendar()
        self.inject_margin_api()

        # 換月規則要用日曆算最後交易日，故在日曆建好之後才注入
        if self.roll_config is not None:
            self.roll_config.calendar = self.calendar

        if not self.products:
            logger.warning(
                "[Futures DataFeed] 策略未宣告 products，將載入當日所有商品的報價"
            )

    def inject_margin_api(self) -> None:
        """
        - Description:
            把本 feed 建立的 `FuturesMarginAPI` 注入保證金設定

            **保證金設定是策略層與部位管理層共用的同一個物件**（由 factory 建立
            並回寫給策略），故只要在此注入一次，兩邊算出來的每口保證金就一定一致。
            兩處不一致的後果是「策略算得出口數、部位管理層卻開不進去」，
            而且不會有任何錯誤訊息。

            `use_api=False`（比率近似）時不注入——那是使用者明確表態要降級，
            此時發出警告，避免有人以為自己在跑查表模式。
        """

        if self.margin_config is None:
            return

        if not self.margin_config.use_api:
            logger.warning(
                f"[Futures DataFeed] 保證金採比率近似"
                f"（契約價值 × {self.margin_config.initial_margin_ratio:.0%}），"
                f"跨年份誤差實測 +143% ~ −38%，僅適合跑通流程"
            )
            return

        if self.margin_config.api is None:
            self.margin_config.api = self.margin

        self.check_margin_coverage()

    def check_margin_coverage(self) -> None:
        """
        - Description:
            回測起始日早於保證金資料涵蓋範圍時**在開跑前就講清楚**

            查表模式查不到保證金會直接 raise（那是刻意的，見 `FuturesMarginConfig`），
            但那會發生在迴圈跑到第一筆開倉訊號的時候——使用者看到的是跑了一半
            才中斷。保證金資料只回溯到 2020-03（更早的公告附件是掃描影像，
            取不到數值），本檢查在 `setup()` 就把
            「這段期間查不到」說明白，並指出可用的替代做法。
        """

        if self.margin_config is None or self.start_date is None:
            return

        for product in self.products:
            covered: Optional[Dict[str, str]] = (
                self.margin_config.api.get_covered_date_range(product)
            )
            if covered is None:
                logger.warning(
                    f"[Futures DataFeed] {product} 在保證金表內沒有任何資料，"
                    f"開倉時會中止。改用 `FuturesMarginConfig.ratio()` 可跑通流程"
                )
                continue

            if str(self.start_date) < covered["earliest"]:
                logger.warning(
                    f"[Futures DataFeed] 回測起始日 {self.start_date} 早於 {product} "
                    f"的保證金涵蓋範圍（{covered['earliest']} 起），該段一開倉就會中止。"
                    f"要回測更早的期間請明確改用 `FuturesMarginConfig.ratio()`"
                    f"（比率近似，TX 實測跨年份誤差 +143% ~ −38%）"
                )

    def is_market_open(self, date: datetime.date) -> bool:
        """
        期貨開盤日判定：交給 `FuturesCalendar`（判準是「當日表內有行情」）

        **不沿用股票 calendar**：期貨的休市日與現貨不完全相同，且最後交易日
        遇休市要順延到**期貨自己**的下一個開盤日。日曆整場只建一次
        （回測期間資料表不會變動）。
        """

        if self.futures_price is None:
            return False

        if self.calendar is None:
            self.calendar = self.build_calendar()

        return self.calendar.is_trading_day(date)

    def build_calendar(self) -> FuturesCalendar:
        """
        - Description:
            建立本次回測的期貨交易日曆

            **以策略宣告的第一個商品為準**：不同商品的掛牌期間不同，用「任一商品
            有資料」會把該策略根本不交易的商品的交易日也算進來。

            **結束日往後多取 `CALENDAR_LOOKAHEAD_DAYS` 天**：落在回測末段的契約，
            其最後交易日可能在 `end_date` 之後，只取回測區間本身會讓
            `get_last_trading_date()` 因為「日曆涵蓋不到」而失準（換月規則會用到）。
        - Return:
            - FuturesCalendar
        """

        if self.start_date is None or self.end_date is None:
            return FuturesCalendar()

        # **多商品要取聯集**：舊版只看 `products[0]`，
        # 於是第一個商品停止交易（下市、尚未上市、資料缺一段）的那些日子，
        # 整場回測都會被判定為休市——連還在交易的其他商品都跟著停擺。
        #
        # 這份日曆同時餵給 `roll_config.calendar`（結算日順延），聯集在那裡
        # 一樣是對的：順延依的是**交易所休市日**，不是個別商品的暫停交易。
        # 單一商品的判準仍可用 `FuturesCalendar.from_api(product=...)`。
        end: datetime.date = self.end_date + datetime.timedelta(
            days=self.CALENDAR_LOOKAHEAD_DAYS
        )
        if not self.products:
            return FuturesCalendar.from_api(
                self.futures_price, self.start_date, end, product=None
            )

        trading_days: Set[datetime.date] = set()
        for product in self.products:
            trading_days |= set(
                self.futures_price.get_trading_days(
                    self.start_date, end, product=product
                )
            )

        return FuturesCalendar(trading_days)

    def get_quotes(
        self,
        date: datetime.date,
        scale: Scale,
        adjusted: bool = False,
    ) -> List[FuturesQuote]:
        """
        - Description:
            取得當日**所有契約**的報價（僅策略宣告的商品與時段）

            `adjusted` 一律忽略：期貨沒有除權息還原的概念。
        - Parameters:
            - date: datetime.date
                交易日
            - scale: Scale
                報價級別；**目前僅支援 DAY**，期貨 Tick 回測尚未實作
            - adjusted: bool
                期貨不適用，僅為對齊介面
        - Return:
            - List[FuturesQuote]
                當日報價；查無資料時為空 list
        """

        if scale == Scale.TICK:
            logger.warning("[Futures DataFeed] 期貨 Tick 級別回測尚未實作，本日無報價")
            return []

        quotes: List[FuturesQuote] = []
        resolver: Callable[[str], int] = self.build_multiplier_resolver(date)

        # products 為空時以 [None] 跑一輪，代表「不過濾商品」
        for product in self.products or [None]:
            if self.session == FuturesSession.COMBINED:
                quotes.extend(
                    FuturesQuoteAdapter.convert_to_combined_quotes(
                        self.futures_price,
                        date,
                        self.get_night_session_date(date),
                        product=product,
                        multiplier_resolver=resolver,
                    )
                )
                continue

            quotes.extend(
                FuturesQuoteAdapter.convert_to_day_quotes(
                    self.futures_price,
                    date,
                    product=product,
                    session=self.session,
                    multiplier_resolver=resolver,
                )
            )

        return quotes

    def resolve_multiplier(self, product: str, date: datetime.date) -> int:
        """
        - Description:
            解出該商品在該日的乘數

            **兩種來源不可互相取代**：指數期貨的乘數是常數（`FUTURES_MULTIPLIER`），
            股票期貨的「契約單位」則會隨除權息被交易所調整，必須查**當時**的快照。
            拿今天的契約單位回測歷史，除權息之後那一段的 PnL 就會整段偏掉。
        - Parameters:
            - product: str
                商品代碼
            - date: datetime.date
                交易日（股期的契約單位隨日期變動）
        - Return:
            - int
                乘數（股期為契約單位股數）
        - Raises:
            - KeyError
                兩個來源都查不到——**刻意讓它中斷**，理由同 `FUTURES_MULTIPLIER`：
                乘數猜錯只會讓 PnL 靜默偏掉，中斷比靜默錯誤好查
        """

        if product in FUTURES_MULTIPLIER:
            return FUTURES_MULTIPLIER[product]

        contract_size: Optional[int] = (
            self.universe.get_contract_size(product, date)
            if self.universe is not None
            else None
        )
        if contract_size is None:
            raise KeyError(
                f"{product} 的乘數在 FUTURES_MULTIPLIER 與 futures_stock_universe "
                f"都查不到（{date}）。指數期貨請登錄常數，股期請先更新標的池"
            )

        return contract_size

    def build_multiplier_resolver(self, date: datetime.date) -> Callable[[str], int]:
        """把 `resolve_multiplier()` 綁定日期後交給 adapter（adapter 不持有連線）"""

        return lambda product: self.resolve_multiplier(product, date)

    def resolve_underlying_price(
        self, product: str, date: datetime.date
    ) -> Optional[float]:
        """
        - Description:
            解出股票期貨**標的證券**在該日的收盤價

            股期的保證金來源只給比例，每口金額得自己算：
            `標的股價 × 契約單位 × 適用比例`。那個「標的股價」在 `tw_stock.db`，
            與期貨行情不同檔，故由本 feed 負責跨庫取值——**連線一律由 DataFeed
            持有**，部位管理層不自行開連線。

            **取未還原的收盤價**：保證金是拿當時真實的市價去算的，
            還原價是為了訊號連續性而回推的數字，拿它算保證金會整段偏掉。

            指數期貨不會走到這裡（`calculate_margin()` 先查得到金額表就回傳了）。
        - Parameters:
            - product: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                交易日
        - Return:
            - Optional[float]
                標的證券當日收盤價；標的或價格查不到時為 None
        """

        if self.universe is None:
            return None

        underlying: Optional[Dict[str, str]] = self.universe.get_underlying(
            product, date
        )
        if not underlying or not underlying.get("underlying_stock_id"):
            return None

        close_map: Dict[str, float] = self.get_underlying_close_map(date)
        price: Optional[float] = close_map.get(underlying["underlying_stock_id"])

        return float(price) if price else None

    def get_underlying_close_map(self, date: datetime.date) -> Dict[str, float]:
        """
        當日全市場收盤價對照表（逐日快取）

        一天只查一次：同一天可能有多檔股期要算保證金，逐檔查會變成 N 次全表掃描。
        `tw_stock.db` 的連線在此**首次需要時才開**。
        """

        if date in self.underlying_close_cache:
            return self.underlying_close_cache[date]

        if self.stock_price is None:
            TW_STOCK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.stock_conn = connect_sqlite(TW_STOCK_DB_PATH)
            self.stock_price = StockPriceAPI(conn=self.stock_conn)

        close_map: Dict[str, float] = self.stock_price.get_close_map(date)
        self.underlying_close_cache[date] = close_map
        return close_map

    def get_night_session_date(self, date: datetime.date) -> Optional[datetime.date]:
        """
        - Description:
            取得要與當日日盤整併的那一段夜盤所在的日期（＝**當日**）

            夜盤 15:00 開盤、次日 05:00 收盤，制度上屬於次一交易日——星期五晚上
            那一段屬於星期一。**行情表存的是它所屬的交易日、不是它開始的曆日**：
            TX 第一筆 `night` 列是 2017-05-16（夜盤制度 2017-05-15 晚上上線），
            且 2023 年起近月樣本中「同日期夜盤開盤 vs 前一交易日日盤收盤」的差距
            中位數 15 點、對「同日期日盤收盤」則是 158 點。故整併時直接取同一天。

            **2017-05-15 之前沒有夜盤**，此時回傳 None，整併結果等於日盤本身。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Optional[datetime.date]
                同一個交易日；日曆未建立或無夜盤制度時為 None
        """

        if self.calendar is None or not self.calendar.has_night_session(date):
            return None

        return date

    def close(self) -> None:
        """關閉資料連線（回測結束時由引擎呼叫）"""

        for api in (self.futures_price, self.margin, self.universe, self.stock_price):
            if api is not None:
                api.close()

        if self.conn is not None:
            self.conn.close()
            self.conn = None

        # 標的股價那一條是延遲開的，只跑指數期貨時根本沒建立
        if self.stock_conn is not None:
            self.stock_conn.close()
            self.stock_conn = None
