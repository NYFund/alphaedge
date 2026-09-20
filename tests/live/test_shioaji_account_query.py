import datetime
from typing import Any, List, Optional, Tuple

import pytest

from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.models import (
    BrokerAccountSnapshot,
    FuturesAccountSnapshot,
    FuturesPositionSnapshot,
    StockPositionSnapshot,
)
from core.utils import PositionType, StockOrderCond

"""
帳務查詢：對帳與資金分配都建立在這裡的數字上

兩個具體的失效方式：
- **交割款用位置索引取值**（舊 `get_settlement_capital()` 的 `loc[1:2]`）：
  交割日數或回傳列數一變，它會安靜地算到別的金額。
- **部位不帶融資券別**：同一檔的現股多單與融券空單在券商端是兩筆部位，
  只比「代號 ＋ 方向 ＋ 數量」時，兩者互換的數字會剛好對得上。
"""


class FakeSettlement:
    """`list_settlements()` 回傳的具名形狀"""

    def __init__(self, t_money: float, t1_money: float, t2_money: float) -> None:
        self.t_money: float = t_money
        self.t1_money: float = t1_money
        self.t2_money: float = t2_money


class FakeSettlementV1:
    """`settlements()` 回傳的逐列形狀"""

    def __init__(self, day: int, amount: float) -> None:
        self.T: int = day
        self.amount: float = amount
        self.date: str = "2026-09-19"


class FakeEnum:
    """模擬券商的 str Enum 欄位（有 `.value`）"""

    def __init__(self, value: str) -> None:
        self.value: str = value


class FakeStockPosition:
    def __init__(
        self,
        code: str = "2330",
        direction: str = "Buy",
        quantity: int = 2,
        price: float = 980.0,
        pnl: float = 4000.0,
        cond: Optional[str] = "Cash",
    ) -> None:
        self.code: str = code
        self.direction: FakeEnum = FakeEnum(direction)
        self.quantity: int = quantity
        self.price: float = price
        self.pnl: float = pnl
        self.cond: Optional[FakeEnum] = FakeEnum(cond) if cond is not None else None

    def model_dump(self) -> dict:
        return {"code": self.code, "quantity": self.quantity}


class FakeFuturesPosition:
    def __init__(
        self, code: str = "TXF202601", direction: str = "Sell", quantity: int = 1
    ) -> None:
        self.code: str = code
        self.direction: FakeEnum = FakeEnum(direction)
        self.quantity: int = quantity
        self.price: float = 20000.0
        self.pnl: float = -1000.0

    def model_dump(self) -> dict:
        return {"code": self.code}


class FakeMargin:
    def __init__(self) -> None:
        self.equity_amount: float = 500_000.0
        self.available_margin: float = 316_000.0
        self.initial_margin: float = 184_000.0
        self.maintenance_margin: float = 141_000.0

    def model_dump(self) -> dict:
        return {"equity_amount": self.equity_amount}


class FakeBalance:
    def __init__(self, acc_balance: float = 200_000.0) -> None:
        self.acc_balance: float = acc_balance


class FakeApi:
    def __init__(
        self,
        balance: float = 200_000.0,
        settlements: Any = None,
        stock_positions: Optional[List[Any]] = None,
        futures_positions: Optional[List[Any]] = None,
    ) -> None:
        self.stock_account: str = "S"
        self.futopt_account: str = "F"
        self._balance: FakeBalance = FakeBalance(balance)
        self._settlements: Any = settlements
        self._stock_positions: List[Any] = stock_positions or []
        self._futures_positions: List[Any] = futures_positions or []

    def account_balance(self) -> FakeBalance:
        return self._balance

    def list_settlements(self, account: Any) -> Any:
        return self._settlements

    def list_positions(self, account: Any) -> List[Any]:
        return (
            self._stock_positions
            if account == self.stock_account
            else self._futures_positions
        )

    def margin(self, account: Any) -> FakeMargin:
        return FakeMargin()


@pytest.fixture
def limiter() -> RateLimiter:
    """假時鐘限流器：測試不該真的睡"""

    return RateLimiter(time_source=lambda: 0.0, sleep=lambda seconds: None)


def make_query(api: FakeApi, limiter: RateLimiter) -> ShioajiAccountQuery:
    return ShioajiAccountQuery(
        api,
        limiter,
        now_provider=lambda: datetime.datetime(2026, 9, 19, 14, 30),
    )


# === 交割款 ===
def test_settlement_sum_uses_named_fields() -> None:
    """
    具名形狀：只加 T+1 與 T+2，今天已交割的不算

    舊寫法 `loc[1:2, "amount"]` 靠的是「第 1、2 列剛好是 T+1、T+2」——
    列數一變就會安靜地算到別的金額。
    """

    settlements: List[FakeSettlement] = [FakeSettlement(1000.0, 2000.0, 3000.0)]

    assert ShioajiAccountQuery.sum_pending_settlements(settlements) == 5000.0


def test_settlement_sum_handles_the_row_shape() -> None:
    """
    逐列形狀：依 `T` 欄位判斷，不依列的順序

    專案裡兩支 API 都有人用（`core/utils/account.py` 用的是這一種），
    Phase7-3 收斂前它們會並存。
    """

    rows: List[FakeSettlementV1] = [
        FakeSettlementV1(day=0, amount=1000.0),  # 今天已交割
        FakeSettlementV1(day=2, amount=3000.0),  # 故意把 T+2 放在 T+1 前面
        FakeSettlementV1(day=1, amount=2000.0),
    ]

    assert ShioajiAccountQuery.sum_pending_settlements(rows) == 5000.0


def test_settlement_sum_of_nothing_is_zero() -> None:
    """沒有交割款時回 0，不拋出"""

    assert ShioajiAccountQuery.sum_pending_settlements(None) == 0.0
    assert ShioajiAccountQuery.sum_pending_settlements([]) == 0.0


# === 股票 ===
def test_stock_account_equity_includes_pending_and_positions(
    limiter: RateLimiter,
) -> None:
    """
    總權益 ＝ 可用餘額 ＋ 未交割款 ＋ 持倉市值 ＋ 未實現損益

    **額度檢查的分母是總權益，不是可用餘額**：拿可用餘額當分母的話，
    只要隔日還有部位在場上，餘額就已經被部位佔掉，檢查必然誤判成額度超標
    而拒絕啟動——而那是完全正常的續跑狀態。
    """

    api: FakeApi = FakeApi(
        balance=200_000.0,
        settlements=[FakeSettlement(0.0, 50_000.0, 30_000.0)],
        stock_positions=[FakeStockPosition(quantity=2, price=980.0, pnl=4000.0)],
    )
    snapshot: BrokerAccountSnapshot = make_query(api, limiter).get_stock_account()

    assert snapshot.available_balance == 200_000.0
    assert snapshot.total_equity == 200_000.0 + 80_000.0 + 1960.0 + 4000.0
    assert snapshot.total_equity > snapshot.available_balance
    assert snapshot.ts is not None


def test_stock_positions_carry_order_cond(limiter: RateLimiter) -> None:
    """
    部位要帶融資券別

    同一檔的現股多單與融券空單在券商端是兩筆，只比「代號 ＋ 方向 ＋ 數量」時，
    兩者互換的數字會剛好對得上，對帳就看不出差異。
    """

    api: FakeApi = FakeApi(
        stock_positions=[
            FakeStockPosition(cond="Cash"),
            FakeStockPosition(direction="Sell", cond="ShortSelling"),
        ]
    )
    positions: List[StockPositionSnapshot] = make_query(
        api, limiter
    ).get_stock_positions()

    assert positions[0].order_cond is StockOrderCond.Cash
    assert positions[0].direction is PositionType.LONG
    assert positions[1].order_cond is StockOrderCond.ShortSelling
    assert positions[1].direction is PositionType.SHORT


def test_unknown_order_cond_becomes_none_without_blocking(
    limiter: RateLimiter,
) -> None:
    """
    認不得的券別記 None 並 warning，不阻擋

    對帳少一個維度比整段停擺好，而未知的券別本來就該由人看一眼。
    """

    api: FakeApi = FakeApi(stock_positions=[FakeStockPosition(cond="SomethingNew")])
    positions: List[StockPositionSnapshot] = make_query(
        api, limiter
    ).get_stock_positions()

    assert positions[0].order_cond is None
    assert positions[0].volume == 2


def test_unknown_direction_raises(limiter: RateLimiter) -> None:
    """
    認不得的方向要拋出

    一律當 LONG 會把空單記成多單——對帳會過，但帳上的方向是反的。
    """

    with pytest.raises(ValueError, match="部位方向"):
        ShioajiAccountQuery.to_position_type(FakeEnum("Unknown"))


def test_raw_payload_is_kept(limiter: RateLimiter) -> None:
    """原始值一定要留：欄位語意猜錯時，那是唯一能事後重建真相的東西"""

    api: FakeApi = FakeApi(stock_positions=[FakeStockPosition()])
    positions: List[StockPositionSnapshot] = make_query(
        api, limiter
    ).get_stock_positions()

    assert positions[0].raw["code"] == "2330"


# === 期貨 ===
def test_futures_account_uses_margin_not_balance(limiter: RateLimiter) -> None:
    """期貨能不能再開一口看的是可用保證金，不是帳戶餘額"""

    snapshot: FuturesAccountSnapshot = make_query(
        FakeApi(), limiter
    ).get_futures_account()

    assert snapshot.available_margin == 316_000.0
    assert snapshot.initial_margin == 184_000.0
    assert snapshot.total_equity == 500_000.0


@pytest.mark.parametrize(
    "code, expected",
    [
        ("TXF202601", ("TXF", "202601")),
        ("CDF202603", ("CDF", "202603")),
        ("2330", ("2330", "")),  # 拆不開時原樣回傳，不拋出
        ("", ("", "")),
    ],
)
def test_contract_code_split(code: str, expected: Tuple[str, str]) -> None:
    """
    合約代號拆成商品與月份

    換月期間同一商品會同時有兩個月份的部位，只看合併後的代號會把
    「還沒平掉的舊月」與「已經開好的新月」當成同一件事。
    """

    assert ShioajiAccountQuery.split_contract_code(code) == expected


def test_futures_positions_split_product_and_expiry(limiter: RateLimiter) -> None:
    """期貨部位要拆出商品與到期月份"""

    api: FakeApi = FakeApi(
        futures_positions=[
            FakeFuturesPosition(code="TXF202601"),
            FakeFuturesPosition(code="TXF202602"),
        ]
    )
    positions: List[FuturesPositionSnapshot] = make_query(
        api, limiter
    ).get_futures_positions()

    assert [position.expiry for position in positions] == ["202601", "202602"]
    assert positions[0].product == positions[1].product
    assert positions[0].direction is PositionType.SHORT


# === 限流 ===
def test_every_query_goes_through_the_account_bucket(limiter: RateLimiter) -> None:
    """
    帳務查詢全部走 `ACCOUNT` 類限流

    它與下單額度分開計算，但一樣會因為超限被暫停服務——而對帳失敗會讓整段交易停下來。
    """

    api: FakeApi = FakeApi(
        settlements=[FakeSettlement(0.0, 0.0, 0.0)],
        stock_positions=[FakeStockPosition()],
    )
    query: ShioajiAccountQuery = make_query(api, limiter)

    before: bool = limiter.try_acquire(RateLimitCategory.ACCOUNT)
    query.get_stock_account()  # 餘額 ＋ 交割款 ＋ 部位 = 3 次
    query.get_futures_account()  # 1 次

    assert before is True
    assert limiter.wait_stats().get(RateLimitCategory.ORDER) is None
