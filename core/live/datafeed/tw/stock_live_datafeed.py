import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from core.api.tw.financial_statement_api import FinancialStatementAPI
from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.api.tw.monthly_revenue_report_api import MonthlyRevenueReportAPI
from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_dividend_api import StockDividendAPI
from core.api.tw.stock_margin_api import StockMarginAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.config import TW_STOCK_DB_PATH
from core.config.settings import now_live
from core.dao.connection import DBConnection, connect_sqlite
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.datafeed.calendar import (
    BrokerContractCalendarSource,
    OfficialHolidayCalendarSource,
    PriceTableCalendarSource,
    TradingCalendarSource,
    WeekendCalendarSource,
)
from core.models import BaseQuote, PreOpenStockQuote
from core.strategies.base import BaseStrategy
from core.utils import ExecutionTiming, Scale

"""
台股的實盤資料源：歷史資料到 T−1 由 `tw_stock.db` 提供，今天由券商提供

**API 物件與回測建的是同一批**（`StockPriceAPI` 等），策略的 `setup_apis(feed)`
因此不用改。差別只有兩個：連線是唯讀的，以及今天的報價來自券商快照而不是 DB。
"""


class TwStockLiveDataFeed(BaseLiveDataFeed):
    """台股實盤資料源"""

    def __init__(
        self,
        broker: Any,
        calendar_sources: Optional[Sequence[TradingCalendarSource]] = None,
        db_path: Any = TW_STOCK_DB_PATH,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立台股實盤資料源
        - Parameters:
            - broker: Any
                券商閘道
            - calendar_sources: Optional[Sequence[TradingCalendarSource]]
                交易日來源；None 時建立預設組合
                （官方開休市日曆 ＋ 週末 ＋ 券商合約檔 ＋ price 表）
            - db_path: Any
                歷史資料庫路徑
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        super().__init__(broker, calendar_sources, now_provider)

        self.db_path: Any = db_path
        self.conn: Optional[DBConnection] = None
        self.price: Optional[StockPriceAPI] = None
        self.dividend: Optional[StockDividendAPI] = None
        self.chip: Optional[StockChipAPI] = None
        self.margin: Optional[StockMarginAPI] = None
        self.mrr: Optional[MonthlyRevenueReportAPI] = None
        self.fs: Optional[FinancialStatementAPI] = None
        # 官方開休市日曆（交易日判定的主來源）；與歷史資料同庫，共用唯讀連線
        self.market_holiday: Optional[MarketHolidayAPI] = None

    def setup(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            建立歷史資料 API（**唯讀**）與交易日來源

            唯讀的兩個理由：實盤行程不該寫研究庫；以及唯讀連線不會與背景 ETL
            搶寫入鎖——尾盤那 4 分鐘被一個 ETL 擋住是很難查的那種事故。

            **建立的 API 必須與回測資料源完全相同**（少了 `tick`，那是盤中的事）。
            少建一個的話，用到它的策略會在 `setup_apis()` 當場 `AttributeError`，
            而那個訊息只會說「物件沒有某個屬性」，完全看不出是實盤資料源漏建了。
            `tests/live/test_live_datafeed.py` 有一條測試直接比對兩邊的屬性集合。
        - Parameters:
            - strategy: BaseStrategy
                本次要跑的策略
        """

        self.conn = connect_sqlite(self.db_path, read_only=True)
        self.dividend = StockDividendAPI(conn=self.conn)
        self.price = StockPriceAPI(conn=self.conn, dividend_api=self.dividend)
        self.chip = StockChipAPI(conn=self.conn)
        self.margin = StockMarginAPI(conn=self.conn)
        self.mrr = MonthlyRevenueReportAPI(conn=self.conn)
        self.fs = FinancialStatementAPI(conn=self.conn)
        self.market_holiday = MarketHolidayAPI(conn=self.conn)

        if not self.calendar_sources:
            self.calendar_sources = self.build_default_calendar_sources()

        strategy.setup_apis(self)

    def build_default_calendar_sources(self) -> List[TradingCalendarSource]:
        """
        預設的交易日來源組合

        官方開休市日曆是主來源；它的年度未入庫時，平日只剩券商合約檔一個佐證。
        官方日曆與合約檔衝突時 `resolve_trading_day()` 拒絕啟動，不偏袒任一方。
        """

        return [
            OfficialHolidayCalendarSource(self.market_holiday),
            WeekendCalendarSource(),
            BrokerContractCalendarSource(self._broker_contract_update_date),
            PriceTableCalendarSource(self._price_table_has_data),
        ]

    def _broker_contract_update_date(self) -> Optional[datetime.date]:
        """券商合約檔的更新日期；取不到時回 None（不猜）"""

        resolver: Any = getattr(self.broker, "resolver", None)
        if resolver is None:
            return None
        try:
            contract: Any = resolver.resolve_stock("2330")
        except Exception as exc:
            logger.debug(f"取合約檔更新日期失敗：{exc}")
            return None

        raw: Any = getattr(contract, "update_date", None)
        if isinstance(raw, datetime.date):
            return raw
        try:
            return datetime.date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    def _price_table_has_data(self, date: datetime.date) -> bool:
        """`price` 表當日有無資料"""

        if self.price is None:
            return False
        return not self.price.get(date).empty

    def get_latest_data_date(self) -> Optional[datetime.date]:
        """`price` 表的最新交易日"""

        if self.price is None:
            return None

        rows: List[Any] = self.conn.execute("SELECT MAX(date) FROM price").fetchall()
        raw: Any = rows[0][0] if rows else None
        if not raw:
            return None
        return datetime.date.fromisoformat(str(raw))

    # === 即時報價 ===
    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        """
        - Description:
            依段落取得即時報價

            開盤段回 `PreOpenStockQuote`——**盤前不存在當日 OHLC**，讀了就拋出。
            填成參考價的話，以漲幅判斷的策略會永遠算出 0%，訊號默默不成立。
        - Parameters:
            - timing: ExecutionTiming
                執行段落
            - symbols: Sequence[str]
                股票代號
        - Return:
            - List[BaseQuote]
                報價；查無資料的標的不會出現
        """

        if timing is ExecutionTiming.AT_OPEN:
            return self._build_pre_open_quotes(symbols)

        return list(self.broker.get_snapshots(list(symbols)))

    def _build_pre_open_quotes(self, symbols: Sequence[str]) -> List[BaseQuote]:
        """由券商合約檔的參考價與漲跌停組出盤前報價"""

        today: datetime.date = self._now().date()
        quotes: List[BaseQuote] = []

        for symbol in symbols:
            contract: Optional[Any] = self._resolve_contract(symbol)
            if contract is None:
                continue

            reference: float = float(getattr(contract, "reference", 0.0) or 0.0)
            if reference <= 0:
                # 參考價是盤前唯一可用的價格，沒有它這檔就不能下單。
                # **略過而不是給 0**：0 會讓「市價語意換算」算出 0 元的限價
                logger.warning(f"{symbol} 盤前取不到參考價，本段落略過此標的")
                continue

            quotes.append(
                PreOpenStockQuote(
                    stock_id=symbol,
                    date=today,
                    reference_price=reference,
                    limit_up=self._as_optional_float(contract, "limit_up"),
                    limit_down=self._as_optional_float(contract, "limit_down"),
                )
            )
        return quotes

    def _resolve_contract(self, symbol: str) -> Optional[Any]:
        """取得合約；查不到只記 warning 並略過該檔，不中斷整段"""

        resolver: Any = getattr(self.broker, "resolver", None)
        if resolver is None:
            return None
        try:
            return resolver.resolve_stock(symbol)
        except LookupError as exc:
            logger.warning(f"盤前取不到 {symbol} 的合約，本段落略過：{exc}")
            return None

    @staticmethod
    def _as_optional_float(contract: Any, field: str) -> Optional[float]:
        """取合約的浮點欄位；缺值回 None（不填 0，0 會被當成一個真實價格）"""

        value: Any = getattr(contract, field, None)
        return float(value) if value else None

    def get_price_limit_basis(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得漲跌停基準價

            實盤**一律用交易所公告值**（合約檔的 `reference`）而不是公式推算：
            除權息日的基準是另行公告的開盤競價基準，沿用前收會讓整段區間偏移。

            本方法由引擎在需要時針對特定標的呼叫；未指定標的時回空 dict
            （逐檔掃全市場合約沒有意義，也吃限流）。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 基準價}`
        """

        return {}

    def get_quotes(
        self, date: datetime.date, scale: Scale, adjusted: bool = False
    ) -> List[BaseQuote]:
        """
        - Description:
            歷史報價（T−1 以前）；今天的報價請走 `get_live_quotes()`

            **今天一律拒絕**：`price` 表要到收盤後才有今天的資料，
            這裡若靜默回空 list，策略會以為今天全市場都沒有報價。
        - Parameters:
            - date: datetime.date
                交易日
            - scale: Scale
                報價級別
            - adjusted: bool
                是否附上還原價
        - Return:
            - List[BaseQuote]
                該日報價
        - Raise:
            - ValueError
                查詢今天或未來的日期
        """

        if date >= self._now().date():
            raise ValueError(
                f"{date} 不早於今天：歷史報價只到前一個交易日，"
                "今天的報價請用 get_live_quotes()"
            )

        raise NotImplementedError(
            "實盤不從歷史表逐日取報價；策略需要歷史資料時走 API（self.price 等）"
        )

    def close(self) -> None:
        """關閉歷史資料連線；可重複呼叫"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None
