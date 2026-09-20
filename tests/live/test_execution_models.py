import datetime
from typing import Any, Dict

import pytest

from core.models.base.execution import (
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderTicket,
)
from core.models.futures.execution import (
    FuturesAccountSnapshot,
    FuturesOrderTicket,
    FuturesPositionSnapshot,
)
from core.models.futures.order import FuturesOrder
from core.models.stock.execution import StockOrderTicket, StockPositionSnapshot
from core.models.stock.order import StockOrder
from core.utils import (
    Action,
    FuturesOCType,
    LiveOrderStatus,
    PositionType,
    StockOrderCond,
    StockOrderLot,
)

"""
實盤委託、成交回報與帳務快照模型

這一層擋的是**型別混用**：回測沒有「已送出但還沒成交」的狀態，所有欄位都是
送出當下就定案。實盤相反，一張單的狀態會被券商的回報一路改寫，而回報不保證
順序、不保證不重複。模型若不把「策略要做什麼」和「這張單走到哪了」分開存，
成交結果會覆蓋掉原始意圖，parity 比對就失去比對對象。
"""


def test_ticket_keeps_order_and_status_separate() -> None:
    """成交進度改變時，原始訂單不得被改寫"""

    order: StockOrder = StockOrder(stock_id="2330", volume=5, price=1000.0)
    ticket: OrderTicket = OrderTicket(
        client_order_id="r1-0001", strategy_name="MomentumStrategy1", order=order
    )

    ticket.status = LiveOrderStatus.PARTIALLY_FILLED
    ticket.filled_volume = 2
    ticket.avg_fill_price = 1002.0

    assert ticket.order is order
    assert ticket.order.volume == 5  # 策略原本要 5 張，不因為只成交 2 張而變成 2
    assert ticket.order.price == 1000.0


def test_remaining_volume_never_goes_negative() -> None:
    """
    超額回報（重複推送同一筆成交）不得讓殘量變成負數

    殘量是段落結束時「要不要撤單」與「平倉單補不補」的依據，
    負數會讓這兩個判斷都走到錯的分支。
    """

    ticket: OrderTicket = OrderTicket(order=StockOrder(stock_id="2330", volume=3))
    ticket.filled_volume = 5

    assert ticket.remaining_volume == 0


def test_remaining_volume_without_order_is_zero() -> None:
    """沒有原始訂單時不得拋出：重啟接管會先建出只有券商編號的空殼 ticket"""

    assert OrderTicket().remaining_volume == 0


@pytest.mark.parametrize(
    "status, expected",
    [
        (LiveOrderStatus.PENDING_SUBMIT, False),
        (LiveOrderStatus.SUBMITTED, False),
        (LiveOrderStatus.PARTIALLY_FILLED, False),
        (LiveOrderStatus.FILLED, True),
        (LiveOrderStatus.CANCELLED, True),
        (LiveOrderStatus.REJECTED, True),
        (LiveOrderStatus.FAILED, True),
    ],
)
def test_terminal_states(status: LiveOrderStatus, expected: bool) -> None:
    """
    終態的定義是「不會再有回報進來」

    `PARTIALLY_FILLED` **不是**終態：它還會繼續成交或被撤掉。判成終態的話，
    段落結束時不會去撤它，殘量就會留到收盤變成非預期的隔夜部位。
    """

    assert OrderTicket(status=status).is_terminal is expected


def test_execution_report_dedup_key_identifies_the_same_fill() -> None:
    """
    去重鍵要能認出同一筆成交

    回報在斷線重連後會重推。以價格、數量比對是不行的——同一張單分批成交時，
    兩筆內容可能完全相同，靠內容去重會把真實的第二筆丟掉。
    """

    raw: Dict[str, Any] = {"seqno": "000123", "exchange_seq": "A1"}
    first: ExecutionReport = ExecutionReport(
        broker_seqno="000123",
        broker_trade_id="A1",
        symbol="2330",
        action=Action.BUY,
        price=1000.0,
        volume=1,
        raw=raw,
    )
    replay: ExecutionReport = ExecutionReport(
        broker_seqno="000123", broker_trade_id="A1", symbol="2330", volume=1
    )
    another_fill: ExecutionReport = ExecutionReport(
        broker_seqno="000123", broker_trade_id="A2", symbol="2330", volume=1
    )

    assert first.dedup_key == replay.dedup_key
    assert first.dedup_key != another_fill.dedup_key


def test_raw_payload_defaults_are_not_shared() -> None:
    """
    `raw` 的預設值不可是同一個 dict

    可變預設值是 Python 的經典陷阱，而這裡的後果特別隱蔽：所有沒帶 raw 的回報
    會共用同一份原始訊息，事後追查時每一筆看起來都一樣。
    """

    first: ExecutionReport = ExecutionReport()
    second: ExecutionReport = ExecutionReport()
    first.raw["seqno"] = "000123"

    assert second.raw == {}


def test_stock_ticket_records_what_was_actually_sent() -> None:
    """
    委託條件記的是**送出的值**，不是策略意圖

    策略從頭到尾看不到 `order_cond`，它由送單前的轉換推導。推導結果不留在委託上，
    成本對不上時就回答不了「那張單到底是用現股還是融券送出去的」。
    """

    ticket: StockOrderTicket = StockOrderTicket(
        client_order_id="r1-0002",
        strategy_name="ForeignSellShortDayTradeStrategy",
        order=StockOrder(stock_id="2330", action=Action.SELL, volume=1),
        order_cond=StockOrderCond.Cash,
        order_lot=StockOrderLot.Common,
        day_trade_short=True,
    )

    assert ticket.order_cond is StockOrderCond.Cash
    assert ticket.day_trade_short is True
    # 現股當沖先賣：`Cash` 與 `day_trade_short` 缺一不可，少了後者會被當成賣持股而退單
    assert (ticket.order_cond, ticket.day_trade_short) == (StockOrderCond.Cash, True)


def test_stock_position_snapshot_carries_order_cond() -> None:
    """
    對帳要連融資券別一起比

    同一檔的現股多單與融券空單在券商端是兩筆部位，只比「代號 ＋ 方向 ＋ 數量」時，
    兩者互換的數字會剛好對得上。
    """

    cash: StockPositionSnapshot = StockPositionSnapshot(
        symbol="2330",
        direction=PositionType.LONG,
        volume=2,
        order_cond=StockOrderCond.Cash,
    )
    short: StockPositionSnapshot = StockPositionSnapshot(
        symbol="2330",
        direction=PositionType.SHORT,
        volume=2,
        order_cond=StockOrderCond.ShortSelling,
    )

    assert isinstance(cash, BrokerPositionSnapshot)
    assert (cash.symbol, cash.direction, cash.volume) != (
        short.symbol,
        short.direction,
        short.volume,
    )
    assert cash.order_cond != short.order_cond


def test_futures_ticket_and_snapshot_split_product_and_expiry() -> None:
    """
    換月期間同一商品會有兩個月份的部位，只看 symbol 會把兩者混成一件事
    """

    ticket: FuturesOrderTicket = FuturesOrderTicket(
        client_order_id="r1-0003",
        strategy_name="MomentumFuturesStrategy",
        order=FuturesOrder(product="TX", expiry="202601", volume=1),
        product="TX",
        expiry="202601",
        octype=FuturesOCType.Cover,
    )
    near: FuturesPositionSnapshot = FuturesPositionSnapshot(
        symbol="TX202601", product="TX", expiry="202601", volume=1
    )
    far: FuturesPositionSnapshot = FuturesPositionSnapshot(
        symbol="TX202602", product="TX", expiry="202602", volume=1
    )

    assert ticket.octype is FuturesOCType.Cover
    assert ticket.octype is not FuturesOCType.Auto  # Auto 的行為在換月時不透明
    assert near.product == far.product
    assert near.expiry != far.expiry


def test_account_snapshot_distinguishes_equity_from_available() -> None:
    """
    總權益與可用餘額是兩個數，額度檢查的分母是前者

    拿可用餘額當分母的話，只要隔日還有部位在場上，可用餘額就已經被部位佔掉，
    檢查必然誤判成額度超標而拒絕啟動——而那是完全正常的續跑狀態。
    """

    now: datetime.datetime = datetime.datetime(2026, 9, 19, 8, 30)
    snapshot: BrokerAccountSnapshot = BrokerAccountSnapshot(
        ts=now, available_balance=200_000.0, total_equity=1_000_000.0
    )

    assert snapshot.total_equity > snapshot.available_balance


def test_futures_account_snapshot_adds_margin_fields() -> None:
    """期貨能不能再開一口看的是可用保證金，不是帳戶餘額"""

    snapshot: FuturesAccountSnapshot = FuturesAccountSnapshot(
        available_balance=500_000.0,
        total_equity=500_000.0,
        initial_margin=184_000.0,
        maintenance_margin=141_000.0,
        available_margin=316_000.0,
    )

    assert isinstance(snapshot, BrokerAccountSnapshot)
    assert snapshot.available_margin == 316_000.0
