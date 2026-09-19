import datetime
from typing import Callable, Dict, List, Optional, Set

import pytest

from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.models import StockAccount, StockOrder, StockQuote
from core.strategies.stock import BaseStockStrategy
from core.utils import Action, PositionType, Scale, ShortMethod

"""放空回測框架的測試 fixture：全部為純記憶體物件，不連資料庫"""


@pytest.fixture
def make_order() -> Callable[..., StockOrder]:
    """建立 StockOrder 的 factory"""

    def _make_order(
        stock_id: str = "2330",
        date: Optional[datetime.date] = None,
        action: Action = Action.BUY,
        position_type: PositionType = PositionType.LONG,
        price: float = 100.0,
        volume: int = 1,
        short_method: Optional[ShortMethod] = None,
        is_day_trade: bool = False,
    ) -> StockOrder:
        return StockOrder(
            stock_id=stock_id,
            date=date or datetime.date(2024, 1, 2),
            action=action,
            position_type=position_type,
            price=price,
            volume=volume,
            short_method=short_method,
            is_day_trade=is_day_trade,
        )

    return _make_order


class ScriptedStrategy(BaseStockStrategy):
    """
    測試用策略：依「日期 → 訂單清單」的腳本回傳訊號

    讓整合測試能精確控制「第幾天下什麼單」，不依賴任何真實訊號邏輯與資料庫。
    """

    def __init__(
        self,
        open_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
        close_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
        stop_loss_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
    ) -> None:
        super().__init__()

        self.strategy_name: str = "ScriptedStrategy"
        self.init_capital: float = 1000000.0
        self.max_holdings: int = 10
        self.scale: Scale = Scale.DAY
        self.start_date: datetime.date = datetime.date(2024, 1, 2)
        self.end_date: datetime.date = datetime.date(2024, 1, 31)

        self.open_script: Dict[datetime.date, List[StockOrder]] = open_script or {}
        self.close_script: Dict[datetime.date, List[StockOrder]] = close_script or {}
        self.stop_loss_script: Dict[datetime.date, List[StockOrder]] = (
            stop_loss_script or {}
        )

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed=None) -> None:
        """測試不使用任何資料 API"""

        pass

    def get_quote_date(self, stock_quotes: List[StockQuote]) -> Optional[datetime.date]:
        """取得這批報價的日期（tick 級別的 datetime 一律取其 date 部分）"""

        if not stock_quotes:
            return None

        date = stock_quotes[0].date
        return date.date() if isinstance(date, datetime.datetime) else date

    def check_open_signal(self, stock_quotes: List[StockQuote]) -> List[StockOrder]:
        """依腳本回傳當日開倉單"""

        return self.open_script.get(self.get_quote_date(stock_quotes), [])

    def check_close_signal(self, stock_quotes: List[StockQuote]) -> List[StockOrder]:
        """依腳本回傳當日平倉單"""

        return self.close_script.get(self.get_quote_date(stock_quotes), [])

    def check_stop_loss_signal(
        self, stock_quotes: List[StockQuote]
    ) -> List[StockOrder]:
        """依腳本回傳當日停損單"""

        return self.stop_loss_script.get(self.get_quote_date(stock_quotes), [])

    def calculate_position_size(
        self, stock_quotes: List[StockQuote], action: Action
    ) -> List[StockOrder]:
        """測試策略的下單張數由腳本直接指定，不需計算"""

        return []


class ScriptedDataFeed(TwStockDataFeed):
    """
    測試用資料源：停券日與除息股利改由腳本給定，完全不連資料庫

    引擎每根 bar 都會從 DataFeed 把這兩份資料推給 `SettlementModel`
    （`Backtester.execute_bar()`），因此腳本情境要驗停券強制回補與股利補償，
    只能從這一層注入——直接設 `SettlementModel` 的欄位會在下一根 bar 被覆寫。
    """

    def __init__(
        self,
        force_cover_script: Optional[Dict[datetime.date, Set[str]]] = None,
        cash_dividend_script: Optional[Dict[datetime.date, Dict[str, float]]] = None,
    ) -> None:
        super().__init__()

        self.force_cover_script: Dict[datetime.date, Set[str]] = (
            force_cover_script or {}
        )
        self.cash_dividend_script: Dict[datetime.date, Dict[str, float]] = (
            cash_dividend_script or {}
        )

    def get_force_cover_symbols(self, date: datetime.date) -> Set[str]:
        """依腳本回傳當日觸及融券最後回補日的標的"""

        return self.force_cover_script.get(date, set())

    def get_cash_dividend_map(self, date: datetime.date) -> Dict[str, float]:
        """依腳本回傳當日除息的每股現金股利"""

        return self.cash_dividend_script.get(date, {})


@pytest.fixture
def make_strategy() -> Callable[..., ScriptedStrategy]:
    """建立 ScriptedStrategy 的 factory，可覆寫任意設定欄位"""

    def _make_strategy(
        open_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
        close_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
        stop_loss_script: Optional[Dict[datetime.date, List[StockOrder]]] = None,
        **overrides,
    ) -> ScriptedStrategy:
        strategy: ScriptedStrategy = ScriptedStrategy(
            open_script=open_script,
            close_script=close_script,
            stop_loss_script=stop_loss_script,
        )

        for key, value in overrides.items():
            setattr(strategy, key, value)

        return strategy

    return _make_strategy


@pytest.fixture
def account() -> StockAccount:
    """初始資金 100 萬的虛擬帳戶"""

    return StockAccount(1000000.0)
