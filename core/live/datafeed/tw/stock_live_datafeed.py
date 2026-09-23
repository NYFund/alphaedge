import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd
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
from core.dao.connection import connect_sqlite
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
from core.utils import ExecutionTiming

"""
台股的實盤資料源：歷史資料到 T−1 由 `tw_stock.db` 提供，今天由券商提供

**API 物件與回測建的是同一批**（`StockPriceAPI` 等），策略的 `setup_apis(feed)`
因此不用改。差別只有兩個：連線是唯讀的，以及今天的報價來自券商快照而不是 DB。
"""


class TwStockLiveDataFeed(BaseLiveDataFeed):
    """台股實盤資料源"""

    LATEST_DATE_TABLE: str = "price"
    HISTORY_API_HINT: str = "self.price 等"
    # 交易日佐證用的合約：取一檔成交最活絡、不會下市的權值股即可，
    # 合約檔的更新日期全市場一致，換哪一檔都是同一個日期
    PROBE_SYMBOL: str = "2330"

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

        super().__init__(broker, calendar_sources, now_provider, db_path)

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
        self.fill_default_universe(strategy)

    def fill_default_universe(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            策略沒宣告 `symbols` 時，以「`price` 表最新交易日有行情的全部股票」為標的池

            **與回測每天的標的池相同**：回測餵給策略的是當天價格表上的每一檔，
            實盤若只抓策略宣告的幾檔，全市場掃描型的策略（例如漲幅選股）
            在實盤永遠拿不到報價、永遠不出訊號，而且不會有任何錯誤。
            以前一交易日為準（今天的資料要收盤後才進來）。
        - Parameters:
            - strategy: BaseStrategy
                本次要跑的策略
        """

        if getattr(strategy, "symbols", None):
            return

        name: str = type(strategy).__name__
        try:
            latest: Optional[datetime.date] = self.get_latest_data_date()
            frame: pd.DataFrame = (
                self.price.get(latest) if latest is not None else pd.DataFrame()
            )
        except Exception as exc:
            # 取不到預設標的池只影響「這支策略沒有標的」，不該讓整個資料源啟動失敗；
            # 資料過期或缺表會由新鮮度檢查以明確的錯誤擋下
            logger.warning(
                f"{name} 未宣告標的池，且查不到價格表（{exc}），本次沒有任何標的"
            )
            return

        if frame.empty:
            logger.warning(f"{name} 未宣告標的池，且價格表沒有資料，本次沒有任何標的")
            return

        strategy.symbols = sorted(frame["stock_id"].astype(str).unique())
        logger.info(
            f"{name} 未宣告標的池，以 {latest} 有行情的 {len(strategy.symbols)} 檔股票為標的"
        )

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

    def _probe_contract(self, resolver: Any) -> Optional[Any]:
        """交易日佐證取**股票**合約"""

        return resolver.resolve_stock(self.PROBE_SYMBOL)

    def _price_table_has_data(self, date: datetime.date) -> bool:
        """`price` 表當日有無資料"""

        if self.price is None:
            return False
        return not self.price.get(date).empty

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
