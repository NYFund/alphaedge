import datetime
import inspect
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesQuote,
    FuturesTradeRecord,
    StockAccount,
    StockOrder,
    StockPosition,
    StockTradeRecord,
)
from core.portfolio.signal import Signal
from core.strategies.futures import BaseFuturesStrategy
from core.strategies.stock import BaseStockStrategy
from core.utils import Action, PositionType, Scale
from tests.conftest import build_stock_quote

"""
Models 的對稱性：兩個市場都有的欄位只在基底宣告一次，訊號到訂單的欄位搬運只有一份

原本 `margin_used`、`margin`、`holding_days`、`entry_price`、`roi_on_capital` 在台股與期貨
各宣告一份，註解與預設值會各自漂移；`Signal → Order` 的六個欄位則抄了四份，
多一個欄位時只會改到其中幾份。
"""

DAY: datetime.date = datetime.date(2026, 9, 21)

# 兩個市場都有的欄位：只能由基底的 `__init__` 賦值
SHARED_FIELDS: Tuple[str, ...] = (
    "margin",
    "holding_days",
    "entry_price",
    "margin_used",
    "roi_on_capital",
)
SUBCLASSES: List[type] = [
    StockPosition,
    FuturesPosition,
    StockTradeRecord,
    FuturesTradeRecord,
    StockAccount,
    FuturesAccount,
]


@pytest.mark.parametrize("cls", SUBCLASSES, ids=lambda cls: cls.__name__)
def test_shared_fields_are_assigned_only_in_the_base(cls: type) -> None:
    """子類的 `__init__` 不可再對共同欄位賦值；賦兩次等於有兩份註解與兩個預設值"""

    if "__init__" not in cls.__dict__:
        return
    source: str = inspect.getsource(cls.__init__)
    offenders: List[str] = [
        field
        for field in SHARED_FIELDS
        if re.search(rf"self\.{field}\s*(:[^=]+)?=", source)
    ]

    assert offenders == [], f"{cls.__name__} 自己賦值了共同欄位：{offenders}"


def test_positions_and_accounts_still_carry_the_shared_fields() -> None:
    """欄位上移後兩個市場的實例都拿得到，預設值相同"""

    stock: StockPosition = StockPosition(stock_id="2330", price=100.0)
    futures: FuturesPosition = FuturesPosition(
        product="TX", expiry="202610", price=18000.0
    )

    assert (stock.margin, stock.holding_days) == (0.0, 0)
    assert (futures.margin, futures.holding_days) == (0.0, 0)
    assert StockAccount(1_000_000.0).margin_used == 0.0
    assert FuturesAccount(1_000_000.0).margin_used == 0.0


def test_entry_price_is_fixed_when_the_mark_price_moves() -> None:
    """`price` 會被逐日結算重設，`entry_price` 不動；台股兩者永遠相同"""

    futures: FuturesPosition = FuturesPosition(
        product="TX", expiry="202610", price=18000.0
    )
    futures.price = 18150.0

    assert futures.entry_price == 18000.0
    assert StockPosition(stock_id="2330", price=100.0).entry_price == 100.0


def test_futures_roi_on_capital_equals_roi() -> None:
    """期貨的報酬率分母本來就是保證金，`roi_on_capital` 不另算；台股由部位管理層傳入"""

    assert (
        FuturesTradeRecord(product="TX", expiry="202610", roi=5.25).roi_on_capital
        == 5.25
    )
    assert (
        StockTradeRecord(stock_id="2330", roi=5.25, roi_on_capital=12.5).roi_on_capital
        == 12.5
    )


def test_futures_account_has_no_empty_cost_override() -> None:
    """期貨沒有持有期間的計提費用，不需要覆寫交易成本的加總"""

    assert "update_transaction_cost" not in FuturesAccount.__dict__
    account: FuturesAccount = FuturesAccount(1_000_000.0)
    account.update_transaction_cost()
    assert account.total_transaction_cost == 0


# === Signal → Order 只有一份 ===
def test_stock_order_from_signal_copies_every_field() -> None:
    """標的、日期、動作、方向、下單價抄自訊號，張數由呼叫端決定"""

    quote = build_stock_quote("2330", date=DAY, cur_price=100.0)
    signal: Signal = Signal(
        quote, Action.SELL, PositionType.SHORT, order_price=99.5, volume=7
    )

    order: StockOrder = StockOrder.from_signal(signal, 3)

    assert (order.stock_id, order.date, order.action, order.position_type) == (
        "2330",
        DAY,
        Action.SELL,
        PositionType.SHORT,
    )
    assert (order.price, order.volume) == (99.5, 3)


def test_futures_order_from_signal_copies_the_contract() -> None:
    """期貨多了契約兩欄，一樣從訊號的報價拿"""

    quote: FuturesQuote = FuturesQuote(
        product="TX", expiry="202610", scale=Scale.DAY, date=DAY, close=18000.0
    )
    signal: Signal = Signal(quote, Action.BUY, PositionType.LONG, order_price=18010.0)

    order: FuturesOrder = FuturesOrder.from_signal(signal, 2)

    assert (order.product, order.expiry, order.contract_id) == (
        "TX",
        "202610",
        "TX202610",
    )
    assert (order.date, order.action, order.price, order.volume) == (
        DAY,
        Action.BUY,
        18010.0,
        2,
    )


ORDER_BUILDERS: List[Path] = [
    Path(__file__).resolve().parents[1] / "core" / path
    for path in (
        "strategies/stock/base.py",
        "strategies/futures/base.py",
        "portfolio/construction.py",
    )
]


@pytest.mark.parametrize("path", ORDER_BUILDERS, ids=lambda path: path.name)
def test_order_builders_do_not_copy_signal_fields_by_hand(path: Path) -> None:
    """策略基底與部位建構層只能走 `from_signal()`，不可再手抄一份欄位"""

    source: str = path.read_text()

    assert "from_signal(" in source
    assert not re.search(r"\b(StockOrder|FuturesOrder)\(\n", source), (
        f"{path.name} 仍直接建構訂單"
    )


# === 策略基底的平倉：數量照訊號 ===
class CloseOnlyStock(BaseStockStrategy):
    """只為了呼叫 `build_close_orders()` 的台股策略"""

    def setup_account(self, account: StockAccount) -> None:
        self.account = account

    def setup_apis(self, feed=None) -> None:
        pass


class CloseOnlyFutures(BaseFuturesStrategy):
    """只為了呼叫 `build_close_orders()` 的期貨策略"""

    def setup_account(self, account: FuturesAccount) -> None:
        self.account = account

    def setup_apis(self, feed=None) -> None:
        pass


def test_close_orders_take_the_volume_from_the_signal() -> None:
    """兩個市場的平倉單張數／口數都照訊號給的量；沒帶量的訊號略過"""

    stock: CloseOnlyStock = CloseOnlyStock()
    stock.setup_account(StockAccount(1_000_000.0))
    futures: CloseOnlyFutures = CloseOnlyFutures()
    futures.setup_account(FuturesAccount(1_000_000.0))

    stock_quote = build_stock_quote("2330", date=DAY, cur_price=100.0)
    futures_quote: FuturesQuote = FuturesQuote(
        product="TX", expiry="202610", scale=Scale.DAY, date=DAY, close=18000.0
    )
    stock_orders: List[StockOrder] = stock.build_close_orders(
        [
            Signal(stock_quote, Action.SELL, PositionType.LONG, 99.0, volume=4),
            Signal(stock_quote, Action.SELL, PositionType.LONG, 99.0, volume=None),
        ]
    )
    futures_orders: List[FuturesOrder] = futures.build_close_orders(
        [Signal(futures_quote, Action.SELL, PositionType.LONG, 17990.0, volume=3)]
    )

    assert [order.volume for order in stock_orders] == [4]
    assert [(order.contract_id, order.volume) for order in futures_orders] == [
        ("TX202610", 3)
    ]
