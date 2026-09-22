import datetime
from typing import Any, Callable, List, Optional, Sequence, Tuple

from loguru import logger

from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.futures_price_api import FuturesPriceAPI
from core.config import TW_FUTURES_DB_PATH
from core.config.settings import now_live
from core.dao.connection import DBConnection, connect_sqlite
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.datafeed.calendar import (
    BrokerContractCalendarSource,
    TradingCalendarSource,
    WeekendCalendarSource,
)
from core.managers.futures.position_manager import FuturesMarginConfig
from core.models import BaseQuote, PreOpenFuturesQuote
from core.strategies.base import BaseStrategy
from core.utils import (
    FUTURES_MULTIPLIER,
    ExecutionTiming,
    FuturesSession,
    Scale,
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

    def __init__(
        self,
        broker: Any,
        calendar_sources: Optional[Sequence[TradingCalendarSource]] = None,
        db_path: Any = TW_FUTURES_DB_PATH,
        now_provider: Callable[[], datetime.datetime] = now_live,
        margin_config: Optional[FuturesMarginConfig] = None,
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
        """

        super().__init__(broker, calendar_sources, now_provider)

        self.db_path: Any = db_path
        self.conn: Optional[DBConnection] = None
        self.futures_price: Optional[FuturesPriceAPI] = None
        self.margin: Optional[FuturesMarginAPI] = None
        self.margin_config: Optional[FuturesMarginConfig] = margin_config

    def setup(self, strategy: BaseStrategy) -> None:
        """建立歷史資料 API（唯讀）與交易日來源"""

        self.conn = connect_sqlite(self.db_path, read_only=True)
        self.futures_price = FuturesPriceAPI(conn=self.conn)
        self.margin = FuturesMarginAPI(conn=self.conn)
        self.inject_margin_api()

        if not self.calendar_sources:
            self.calendar_sources = [
                WeekendCalendarSource(),
                BrokerContractCalendarSource(self._broker_contract_update_date),
            ]

        strategy.setup_apis(self)

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

    def _broker_contract_update_date(self) -> Optional[datetime.date]:
        """券商合約檔的更新日期；取不到時回 None"""

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

    def get_latest_data_date(self) -> Optional[datetime.date]:
        """`futures_price_daily` 的最新交易日"""

        if self.conn is None:
            return None

        rows: List[Any] = self.conn.execute(
            "SELECT MAX(date) FROM futures_price_daily"
        ).fetchall()
        raw: Any = rows[0][0] if rows else None
        if not raw:
            return None
        return datetime.date.fromisoformat(str(raw))

    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        """
        - Description:
            依段落取得即時報價；`symbols` 是**契約代號**（`{分類}{YYYYMM}`）
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
                    date=now,
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

        代號格式是 `{分類}{YYYYMM}`；拆不開時略過該契約並記 warning，
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

    @staticmethod
    def _as_optional_float(contract: Any, field: str) -> Optional[float]:
        """取合約的浮點欄位；缺值回 None"""

        value: Any = getattr(contract, field, None)
        return float(value) if value else None

    def get_quotes(
        self, date: datetime.date, scale: Scale, adjusted: bool = False
    ) -> List[BaseQuote]:
        """歷史報價請走 API；今天的報價走 `get_live_quotes()`"""

        if date >= self._now().date():
            raise ValueError(
                f"{date} 不早於今天：歷史報價只到前一個交易日，"
                "今天的報價請用 get_live_quotes()"
            )

        raise NotImplementedError(
            "實盤不從歷史表逐日取報價；策略需要歷史資料時走 API（self.futures_price）"
        )

    def close(self) -> None:
        """關閉歷史資料連線；可重複呼叫"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None
