import datetime
from typing import List

import pytest

from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.models import (
    BaseOrder,
    BaseQuote,
    FuturesAccount,
    FuturesQuote,
    StockAccount,
    StockPosition,
    StockQuote,
)
from core.portfolio.construction import (
    FuturesPortfolioConstructor,
    StockPortfolioConstructor,
)
from core.portfolio.signal import Signal
from core.strategies.futures import BaseFuturesStrategy
from core.strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from core.strategies.stock import BaseStockStrategy
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
from core.utils import Action, PositionType
from tests.conftest import (
    build_futures_quote,
    build_stock_quote,
)

"""
策略分層鉤子的測試：`check_*_signal()` 由基底提供，策略只給訊號

釘住三件會靜默出錯的事：

1. **基底路徑與舊路徑輸出相同的訂單**——分層只換位置，不換結果。
2. **部位建構器每次組裝都重建**。策略的 `max_holdings`／`max_lots` 在
   `super().__init__()` 之後才填，`margin_config` 更是由 `core/backtest/factory.py`
   在策略建構完成之後才注入。快取住的建構器會永遠看到舊值，而症狀是部位大小
   整段偏掉，沒有任何錯誤訊息。
3. **`account` 未載入時回傳空清單**，不是 `AttributeError`。
"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)
MULTIPLIER: int = 200


def make_stock_quote(stock_id: str, price: float) -> StockQuote:
    """組一筆日線報價；OHLC 一律沿用同一個價格"""

    return build_stock_quote(stock_id=stock_id, date=DAY_1, cur_price=price)


def make_futures_quote(expiry: str, close: float = 18000.0) -> FuturesQuote:
    """組一筆期貨日盤報價；OHLC 留 0（本檔的測試只看 cur_price 與 close）"""

    return build_futures_quote(
        expiry=expiry,
        date=DAY_1,
        close=close,
        open=0.0,
        high=0.0,
        low=0.0,
        multiplier=MULTIPLIER,
    )


def order_fields(orders: List[BaseOrder]) -> List[tuple]:
    """把訂單攤成可逐筆比對的欄位"""

    return [
        (o.symbol, o.date, o.action, o.position_type, o.price, o.volume) for o in orders
    ]


class LayeredMomentum(BaseStockStrategy):
    """
    只實作 Alpha 鉤子的示範策略

    **刻意直接繼承 `BaseStockStrategy`**：`MomentumStrategy1` 在 S6 之前仍自行
    覆寫 `check_open_signal()`，繼承它就會走到舊覆寫，驗不到基底路徑。

    **不要為了「本策略不平倉」補一個 `check_close_signal()` 空實作**：那會蓋掉基底
    的實作，平倉路徑就再也驗不到了。不實作的鉤子會在被呼叫時拋 `NotImplementedError`，
    而不是默默回空清單。
    """

    def __init__(self, quotes: List[StockQuote]) -> None:
        super().__init__()
        self.strategy_name: str = "Layered-Demo"
        self.max_holdings: int = 10
        self.candidates: List[StockQuote] = quotes

    def setup_account(self, account: StockAccount) -> None:
        """載入虛擬帳戶"""

        self.account = account

    def setup_apis(self, feed) -> None:
        """本示範策略不需要資料源"""

    def generate_open_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """開倉訊號：算量用收盤價、下單用 cur_price（與改寫前同一組欄位）"""

        return [
            Signal(
                quote=quote,
                action=Action.BUY,
                position_type=PositionType.LONG,
                order_price=quote.cur_price,
                sizing_price=quote.close,
            )
            for quote in self.candidates
        ]


class LayeredFuturesMomentum(BaseFuturesStrategy):
    """期貨版的示範策略；理由同 `LayeredMomentum`"""

    def __init__(self, quotes: List[FuturesQuote]) -> None:
        super().__init__()
        self.strategy_name: str = "Layered-Futures-Demo"
        self.max_lots: int = MomentumFuturesStrategy.DEFAULT_MAX_LOTS
        self.candidates: List[FuturesQuote] = quotes

    def setup_account(self, account: FuturesAccount) -> None:
        """載入虛擬帳戶"""

        self.account = account

    def setup_apis(self, feed) -> None:
        """本示範策略不需要資料源"""

    def generate_open_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """開倉訊號：口數由保證金決定，故不給 sizing_price"""

        return [
            Signal(
                quote=quote,
                action=Action.BUY,
                position_type=PositionType.LONG,
                order_price=quote.close,
            )
            for quote in self.candidates
        ]


# === 基底路徑產生的訂單 ===
def test_stock_base_open_path_builds_orders() -> None:
    """`check_open_signal()` 走基底：訊號經 portfolio 層換算成張數"""

    quotes: List[StockQuote] = [
        make_stock_quote("2330", 100.0),
        make_stock_quote("2317", 50.0),
    ]

    layered: LayeredMomentum = LayeredMomentum(quotes)
    layered.max_holdings = 2
    layered.setup_account(StockAccount(1_000_000.0))

    # 兩個名額均分 1,000,000 → 每檔 500,000
    assert order_fields(layered.check_open_signal(quotes)) == [
        ("2330", DAY_1, Action.BUY, PositionType.LONG, 100.0, 5),
        ("2317", DAY_1, Action.BUY, PositionType.LONG, 50.0, 10),
    ]


def test_futures_base_open_path_builds_orders() -> None:
    """期貨的基底路徑：口數由保證金決定"""

    quotes: List[FuturesQuote] = [make_futures_quote("202403")]

    layered: LayeredFuturesMomentum = LayeredFuturesMomentum(quotes)
    layered.max_lots = 10
    layered.setup_account(FuturesAccount(init_capital=3_000_000))

    # 比率模式每口 18000 × 200 × 0.1 ＝ 360,000；預算 1,500,000 → 4 口
    assert order_fields(layered.check_open_signal(quotes)) == [
        ("TX202403", DAY_1, Action.BUY, PositionType.LONG, 18000.0, 4)
    ]


# === 建構器每次重建：設定晚於 __init__ 才填 ===
def test_stock_constructor_reads_max_holdings_set_after_init() -> None:
    """`max_holdings` 是 `super().__init__()` 之後才填的，建構器必須讀到新值"""

    strategy: MomentumStrategy1 = MomentumStrategy1()

    strategy.max_holdings = 1
    assert strategy.make_portfolio_constructor().max_holdings == 1

    strategy.max_holdings = 7
    assert strategy.make_portfolio_constructor().max_holdings == 7


def test_futures_constructor_reads_margin_config_injected_by_factory() -> None:
    """
    `margin_config` 由 `core/backtest/factory.py` 在策略建構完成後才注入

    快取住建構器的話，這裡會拿到 `None` 而**靜默退回比率近似**——
    可開口數整段偏掉，且不會有任何錯誤訊息。
    """

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    assert strategy.make_portfolio_constructor().margin_config is None

    injected: FuturesMarginConfig = FuturesMarginConfig.default()
    strategy.margin_config = injected

    assert strategy.make_portfolio_constructor().margin_config is injected


def test_constructor_types_match_market() -> None:
    """兩個市場各自拿到對應的建構器"""

    assert isinstance(
        MomentumStrategy1().make_portfolio_constructor(), StockPortfolioConstructor
    )
    assert isinstance(
        MomentumFuturesStrategy().make_portfolio_constructor(),
        FuturesPortfolioConstructor,
    )


# === 守門 ===
def test_open_signal_without_account_returns_empty() -> None:
    """帳戶還沒載入時回傳空清單，不是 AttributeError"""

    layered: LayeredMomentum = LayeredMomentum([make_stock_quote("2330", 100.0)])

    assert layered.account is None
    assert layered.check_open_signal([]) == []


def test_strategy_without_signal_hook_raises_clearly() -> None:
    """沒實作 Alpha 鉤子又走基底路徑時，錯誤訊息要指得到是哪一支策略"""

    class _NoHook(LayeredMomentum):
        def generate_open_signals(self, quotes):
            return BaseStockStrategy.generate_open_signals(self, quotes)

    strategy: _NoHook = _NoHook([])
    strategy.setup_account(StockAccount(1_000_000.0))

    with pytest.raises(NotImplementedError, match="_NoHook"):
        strategy.check_open_signal([])


# === 平倉／停損：不經過 portfolio 層 ===
class LayeredClose(LayeredMomentum):
    """平倉訊號取自持倉的示範策略（對應 `MomentumStrategy1` 的 SELL 分支）"""

    def generate_close_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """平掉每檔的第一筆部位，價格用 cur_price——與改寫前同一組欄位"""

        signals: List[Signal] = []
        for quote in quotes:
            position = self.account.get_first_open_position(quote.symbol)
            if position is None:
                continue

            signals.append(
                Signal(
                    quote=quote,
                    action=Action.SELL,
                    position_type=position.position_type,
                    order_price=quote.cur_price,
                    volume=position.volume,
                )
            )
        return signals

    def generate_stop_loss_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """停損與平倉共用同一組訊號，只為驗證兩者走同一條組裝路徑"""

        return self.generate_close_signals(quotes)


def test_stock_close_path_uses_position_volume() -> None:
    """平倉單的張數取自持倉，**不經過 portfolio 層的資金切分**"""

    quotes: List[StockQuote] = [make_stock_quote("2330", 100.0)]

    layered: LayeredClose = LayeredClose(quotes)
    layered.setup_account(StockAccount(1_000_000.0))
    layered.account.positions.append(
        StockPosition(id=1, stock_id="2330", date=DAY_1, price=90.0, volume=4)
    )

    # 4 張全數出場；若誤走資金切分會變成「1,000,000 ÷ 100,000 ＝ 10 張」
    assert order_fields(layered.check_close_signal(quotes)) == [
        ("2330", DAY_1, Action.SELL, PositionType.LONG, 100.0, 4)
    ]


def test_stop_loss_shares_the_close_builder() -> None:
    """停損與平倉走同一條組裝路徑"""

    quotes: List[StockQuote] = [make_stock_quote("2330", 100.0)]

    layered: LayeredClose = LayeredClose(quotes)
    layered.setup_account(StockAccount(1_000_000.0))
    layered.account.positions.append(
        StockPosition(id=1, stock_id="2330", date=DAY_1, price=90.0, volume=4)
    )

    assert order_fields(layered.check_stop_loss_signal(quotes)) == order_fields(
        layered.check_close_signal(quotes)
    )


def test_close_signal_without_volume_is_skipped() -> None:
    """訊號沒帶數量代表無倉可平，略過而不是下一張 0 張的單"""

    quotes: List[StockQuote] = [make_stock_quote("2330", 100.0)]
    layered: LayeredClose = LayeredClose(quotes)
    layered.setup_account(StockAccount(1_000_000.0))

    signals: List[Signal] = [
        Signal(
            quote=quotes[0],
            action=Action.SELL,
            position_type=PositionType.LONG,
            order_price=100.0,
            volume=None,
        ),
        Signal(
            quote=quotes[0],
            action=Action.SELL,
            position_type=PositionType.LONG,
            order_price=100.0,
            volume=0,
        ),
    ]

    assert layered.build_close_orders(signals) == []


def test_close_signal_without_account_returns_empty() -> None:
    """帳戶還沒載入時平倉也回空清單"""

    layered: LayeredClose = LayeredClose([make_stock_quote("2330", 100.0)])

    assert layered.check_close_signal([]) == []
    assert layered.check_stop_loss_signal([]) == []
