from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.models import OrderTicket, StockOrder
from core.utils import (
    Action,
    LiveOrderStatus,
    PositionType,
    ShortMethod,
    StockOrderLot,
    StockPriceType,
)

from .test_shioaji_broker import FakeApi, FakeContract, FakeSession

"""
台股委託送出前的券商端資格檢查：當沖與融券

- **先賣後買的當沖**只有合約 `day_trade=Yes` 可以；`OnlyBuy` 只能先買後賣，`No` 都不行
  （2026-09-22 模擬環境實測值域）。
- **融券賣出**先查券源，不足或查不到都不送——對應回測的 `rejected_no_borrow`。

不合格時在本地就回 REJECTED 並附原因，不送出、不佔下單額度：券商也會退，
但退單訊息看起來像別的問題。
"""


class EligibilityApi(FakeApi):
    """合約帶 `day_trade`，並可腳本化券源查詢"""

    def __init__(self, day_trade: Any, short_source: Optional[int]) -> None:
        super().__init__()
        contract: FakeContract = FakeContract("2330")
        contract.day_trade = day_trade
        self.Contracts.Stocks = SimpleNamespace(
            get=lambda code: contract if code == "2330" else None
        )
        self.short_source: Optional[int] = short_source
        self.source_queries: int = 0

    def short_stock_sources(self, contracts: List[Any], timeout: int) -> List[Any]:
        self.source_queries += 1
        if self.short_source is None:
            raise ConnectionError("券源查詢逾時")
        return [
            SimpleNamespace(code=contract.code, short_stock_source=self.short_source)
            for contract in contracts
        ]


def make_broker(day_trade: Any = "Yes", short_source: Optional[int] = 10) -> Any:
    limiter: RateLimiter = RateLimiter(time_source=lambda: 0.0, sleep=lambda s: None)
    api: EligibilityApi = EligibilityApi(day_trade, short_source)
    broker: ShioajiBroker = ShioajiBroker(FakeSession(api, limiter), limiter)
    broker.connect()
    return (broker, api)


def ticket(
    action: Action,
    position_type: PositionType,
    short_method: Optional[ShortMethod] = None,
    is_day_trade: bool = False,
    volume: int = 2,
) -> OrderTicket:
    return OrderTicket(
        client_order_id="run1-0001",
        custom_field="010001",
        strategy_name="Alpha",
        order=StockOrder(
            stock_id="2330",
            action=action,
            position_type=position_type,
            short_method=short_method,
            is_day_trade=is_day_trade,
            volume=volume,
            price=1000.0,
            price_type=StockPriceType.LMT,
        ),
    )


def short_day_trade() -> OrderTicket:
    return ticket(Action.SELL, PositionType.SHORT, ShortMethod.DAY_TRADE)


# === 當沖 ===
@pytest.mark.parametrize(
    ("day_trade", "allowed"),
    [("Yes", True), ("OnlyBuy", False), ("No", False), (None, False)],
)
def test_sell_first_day_trade_needs_both_ways(day_trade: Any, allowed: bool) -> None:
    """先賣後買只有 `Yes` 可以；認不得的值（None）照不允許處理"""

    broker, api = make_broker(day_trade=day_trade)

    result: OrderTicket = broker.place_order(short_day_trade())

    assert (result.status is not LiveOrderStatus.REJECTED) is allowed
    assert (len(api.placed) == 1) is allowed
    if not allowed:
        assert "不可先賣後買當沖" in result.reject_reason


@pytest.mark.parametrize(
    ("day_trade", "allowed"),
    [("Yes", True), ("OnlyBuy", True), ("No", False)],
)
def test_buy_first_day_trade_accepts_only_buy(day_trade: Any, allowed: bool) -> None:
    broker, api = make_broker(day_trade=day_trade)

    result: OrderTicket = broker.place_order(
        ticket(Action.BUY, PositionType.LONG, is_day_trade=True)
    )

    assert (len(api.placed) == 1) is allowed
    assert (result.status is LiveOrderStatus.REJECTED) is not allowed


def test_enum_day_trade_value_is_read_by_value() -> None:
    """真的合約欄位是 `DayTrade.Yes` 這種 Enum，要依值比對"""

    broker, api = make_broker(day_trade=SimpleNamespace(value="Yes"))

    broker.place_order(short_day_trade())

    assert len(api.placed) == 1


def test_regular_cash_order_is_not_checked() -> None:
    """一般現股買賣不看當沖欄位、不查券源"""

    broker, api = make_broker(day_trade="No", short_source=None)

    broker.place_order(ticket(Action.BUY, PositionType.LONG))

    assert len(api.placed) == 1
    assert api.source_queries == 0


# === 融券 ===
@pytest.mark.parametrize(
    ("source", "allowed"), [(2, True), (5, True), (1, False), (0, False)]
)
def test_margin_short_needs_enough_borrowable_shares(
    source: int, allowed: bool
) -> None:
    """券源（張）要不少於委託張數"""

    broker, api = make_broker(short_source=source)

    result: OrderTicket = broker.place_order(
        ticket(Action.SELL, PositionType.SHORT, ShortMethod.MARGIN, volume=2)
    )

    assert (len(api.placed) == 1) is allowed
    if not allowed:
        assert "券源不足" in result.reject_reason


def test_unknown_borrowable_shares_block_the_order() -> None:
    """查不到券源就不送：不知道借不借得到，等於把判斷交給券商退單"""

    broker, api = make_broker(short_source=None)

    result: OrderTicket = broker.place_order(
        ticket(Action.SELL, PositionType.SHORT, ShortMethod.MARGIN)
    )

    assert api.placed == []
    assert result.status is LiveOrderStatus.REJECTED
    assert "查不到券源" in result.reject_reason


def test_margin_short_cover_does_not_query_sources() -> None:
    """融券回補是買進，不需要券源"""

    broker, api = make_broker(short_source=0)

    broker.place_order(ticket(Action.BUY, PositionType.SHORT, ShortMethod.MARGIN))

    assert len(api.placed) == 1
    assert api.source_queries == 0


# === 盤中零股 ===
@pytest.mark.parametrize("volume", [0, 1000, 1500])
def test_odd_lot_volume_out_of_range_is_refused_at_construction(volume: int) -> None:
    """零股的單位是股，1～999；超出範圍時在建構當下就拋，不等到送單"""

    with pytest.raises(ValueError, match="盤中零股"):
        StockOrder(stock_id="2330", volume=volume, order_lot=StockOrderLot.IntradayOdd)


def test_odd_lot_order_is_not_sent_yet() -> None:
    """
    盤中零股在實盤還不能送

    金額換算、成本模型、部位管理、歸屬帳與對帳全部以「張」為單位，
    一張 500 股的零股單會在下游被當成 500 張。
    """

    broker, api = make_broker()
    order_ticket: OrderTicket = ticket(Action.BUY, PositionType.LONG)
    order_ticket.order = StockOrder(
        stock_id="2330",
        action=Action.BUY,
        volume=500,
        price=1000.0,
        price_type=StockPriceType.LMT,
        order_lot=StockOrderLot.IntradayOdd,
    )

    result: OrderTicket = broker.place_order(order_ticket)

    assert api.placed == []
    assert result.status is LiveOrderStatus.REJECTED
    assert "盤中零股尚未支援" in result.reject_reason
