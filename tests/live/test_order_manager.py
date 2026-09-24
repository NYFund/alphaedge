import datetime
from typing import Any, Callable, Dict, List, Optional

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.oms.order_manager import (
    MAX_ORDER_INDEX,
    MAX_RUN_INDEX,
    OrderManager,
    OrderStateError,
    to_base36,
)
from core.models import ExecutionReport, OrderStatusEvent, OrderTicket, StockOrder
from core.utils import Action, ExecutionTiming, LiveOrderStatus, StockPriceType

from .conftest import FakeBroker

"""
`OrderManager`：狀態機、先寫 DB 再送單、回報消化、重啟接管

三件事決定了實盤能不能從崩潰中恢復：

- **先寫 DB 再送單**：先送再寫的話，程式在兩者之間崩潰時本地什麼都沒有，
  那張單會變成無主部位——既不知道它存在，也查不到它屬於誰。
- **每張單帶著壓縮碼往返券商**：它可以經本地紀錄反查策略，策略代號卻反查不到
  是哪一張單。
- **查不到就不猜**：模糊比對到多筆時挑一個看起來最像的，會把兩張單的成交
  記到同一張上，而帳上的總量還是對的，對帳看不出來。
"""

TODAY: datetime.date = datetime.date(2026, 9, 19)
NOW: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)


@pytest.fixture
def degradations() -> List[str]:
    return []


@pytest.fixture
def oms(
    fake_broker: FakeBroker, dao: LiveTradeDAO, degradations: List[str]
) -> OrderManager:
    return OrderManager(
        broker=fake_broker,
        dao=dao,
        run_id="run1",
        run_index=1,
        on_degrade=degradations.append,
        now_provider=lambda: NOW,
    )


def make_order(
    symbol: str = "2330", volume: int = 2, price: float = 1000.0
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        action=Action.BUY,
        volume=volume,
        price=price,
        price_type=StockPriceType.LMT,
        timing=ExecutionTiming.AT_CLOSE,
    )


# === 壓縮碼 ===
def test_base36_is_fixed_width() -> None:
    """壓縮碼寬度固定：長度會變的話，解析時要先猜欄位邊界"""

    assert to_base36(0, 4) == "0000"
    assert to_base36(35, 4) == "000z"
    assert to_base36(36, 4) == "0010"
    assert len(to_base36(MAX_ORDER_INDEX - 1, 4)) == 4


def test_base36_wraps_instead_of_truncating() -> None:
    """
    超出寬度時取模而不是截斷

    截斷會讓兩個不同的數字壓成不同字串卻都不等於原值；取模則是明確的循環使用。
    """

    assert to_base36(MAX_ORDER_INDEX, 4) == to_base36(0, 4)


def test_custom_field_fits_six_ascii_characters(oms: OrderManager) -> None:
    """
    壓縮碼必須塞得進 6 個可列印 ASCII 字元

    那是券商欄位的硬限制，超過會讓整張單被擋下。
    """

    code: str = oms.compress("run1-0001")

    assert len(code) == 6
    assert all(" " <= char <= "~" for char in code)
    assert MAX_RUN_INDEX == 1296


def test_custom_field_differs_across_runs(
    fake_broker: FakeBroker, dao: LiveTradeDAO
) -> None:
    """
    不同 run 的同一個序號要壓出不同的碼

    否則重啟後兩次啟動的委託在券商端長得一模一樣，接管時分不出來。
    """

    first: OrderManager = OrderManager(fake_broker, dao, "run1", run_index=1)
    second: OrderManager = OrderManager(fake_broker, dao, "run2", run_index=2)

    assert first.compress("run1-0001") != second.compress("run2-0001")


# === 先寫 DB 再送單 ===
def test_order_is_persisted_before_it_reaches_the_broker(
    oms: OrderManager, dao: LiveTradeDAO, fake_broker: FakeBroker
) -> None:
    """
    送單途中崩潰時，本地要留得下 `PENDING_SUBMIT` 紀錄

    以「送單時拋例外」模擬崩潰：即使券商那邊沒有回應，DB 裡也必須有這張單，
    否則重啟後不知道它存不存在。
    """

    fake_broker.drop_connection_after = 0

    with pytest.raises(ConnectionError):
        oms.submit(make_order(), "MomentumStrategy1")

    rows: List[Dict[str, Any]] = dao.get_unfinished_orders(TODAY)
    persisted: Any = dao.conn.execute(
        "SELECT status FROM live_order WHERE client_order_id = 'run1-0001'"
    ).fetchone()

    assert persisted[0] == "FAILED"  # 送單失敗已反映，但紀錄留下來了
    assert rows == []


def test_failed_submit_is_not_retried(
    oms: OrderManager, fake_broker: FakeBroker
) -> None:
    """
    送單失敗**不自動重送**

    途中的例外分不出「沒送到」與「送到了但回應丟了」，重送有機會下成兩張。
    """

    fake_broker.drop_connection_after = 0

    with pytest.raises(ConnectionError):
        oms.submit(make_order(), "MomentumStrategy1")

    assert fake_broker.placed_count == 1


def test_successful_submit_records_broker_ids(
    oms: OrderManager, dao: LiveTradeDAO
) -> None:
    """送出成功後要回填券商編號與狀態"""

    ticket: OrderTicket = oms.submit(make_order(), "MomentumStrategy1")

    assert ticket.status is LiveOrderStatus.SUBMITTED
    assert ticket.broker_seqno is not None

    row: Any = dao.conn.execute(
        "SELECT strategy_name, custom_field, status FROM live_order"
    ).fetchone()
    assert row == (
        "MomentumStrategy1",
        oms.compress(ticket.client_order_id),
        "SUBMITTED",
    )


def test_dry_run_never_reaches_the_broker(
    fake_broker: FakeBroker, dao: LiveTradeDAO
) -> None:
    """
    `--dry-run` 走完整流程但不送出，而且**要落地並標記**

    不標記的話，盤後分不出「今天沒下單」與「今天是演練」。
    """

    oms: OrderManager = OrderManager(
        fake_broker, dao, "run1", run_index=1, dry_run=True, now_provider=lambda: NOW
    )
    oms.submit(make_order(), "MomentumStrategy1")

    assert fake_broker.placed_count == 0
    assert dao.conn.execute("SELECT dry_run FROM live_order").fetchone()[0] == 1


# === 狀態機 ===
@pytest.mark.parametrize(
    "start, target",
    [
        (LiveOrderStatus.PENDING_SUBMIT, LiveOrderStatus.SUBMITTED),
        (LiveOrderStatus.PENDING_SUBMIT, LiveOrderStatus.REJECTED),
        (LiveOrderStatus.SUBMITTED, LiveOrderStatus.PARTIALLY_FILLED),
        (LiveOrderStatus.SUBMITTED, LiveOrderStatus.FILLED),
        (LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.FILLED),
        (LiveOrderStatus.PARTIALLY_FILLED, LiveOrderStatus.CANCELLED),
    ],
)
def test_legal_transitions(
    oms: OrderManager, start: LiveOrderStatus, target: LiveOrderStatus
) -> None:
    """合法轉移要通過"""

    ticket: OrderTicket = OrderTicket(
        client_order_id="run1-0001", order=make_order(), status=start
    )
    oms.transition(ticket, target)

    assert ticket.status is target


@pytest.mark.parametrize(
    "start, target",
    [
        (LiveOrderStatus.FILLED, LiveOrderStatus.SUBMITTED),
        (LiveOrderStatus.CANCELLED, LiveOrderStatus.FILLED),
        (LiveOrderStatus.SUBMITTED, LiveOrderStatus.PENDING_SUBMIT),
        (LiveOrderStatus.REJECTED, LiveOrderStatus.SUBMITTED),
    ],
)
def test_illegal_transitions_raise_and_are_recorded(
    oms: OrderManager,
    dao: LiveTradeDAO,
    start: LiveOrderStatus,
    target: LiveOrderStatus,
) -> None:
    """
    非法轉移要拋出並留下紀錄

    靜默忽略會讓「已成交」被遲到的舊回報改回「已送出」，而引擎會繼續等它成交，
    段落結束時還會去撤一張早就不存在的單。
    """

    ticket: OrderTicket = OrderTicket(
        client_order_id="run1-0001", order=make_order(), status=start
    )

    with pytest.raises(OrderStateError):
        oms.transition(ticket, target)

    assert ticket.status is start
    assert dao.conn.execute("SELECT COUNT(*) FROM live_risk_event").fetchone()[0] == 1


def test_fill_before_confirmation_is_legal(oms: OrderManager) -> None:
    """
    成交回報可能比委託確認先到

    OMS 因此以「成交即代表已提交」處理——擋掉的話，那筆成交會被丟棄。
    """

    ticket: OrderTicket = OrderTicket(
        client_order_id="run1-0001",
        order=make_order(),
        status=LiveOrderStatus.PENDING_SUBMIT,
    )
    oms.transition(ticket, LiveOrderStatus.FILLED)

    assert ticket.status is LiveOrderStatus.FILLED


def test_transition_history_is_append_only(
    oms: OrderManager, dao: LiveTradeDAO
) -> None:
    """轉移歷史逐筆累加，是事後重建時序的唯一依據"""

    oms.submit(make_order(), "MomentumStrategy1")
    rows: List[Any] = dao.conn.execute(
        "SELECT seq, to_status FROM live_order_event ORDER BY seq"
    ).fetchall()

    assert [row[1] for row in rows] == ["PENDING_SUBMIT", "SUBMITTED"]


# === 回報消化 ===
def test_fills_update_the_ticket_and_are_persisted(
    oms: OrderManager, dao: LiveTradeDAO, fake_broker: FakeBroker
) -> None:
    """成交要更新委託並寫進 `live_fill`"""

    fake_broker.fill_ratio = 0.5
    ticket: OrderTicket = oms.submit(make_order(volume=4), "MomentumStrategy1")

    fills: List[ExecutionReport] = oms.drain_executions()

    assert len(fills) == 1
    assert ticket.filled_volume == 2
    assert ticket.status is LiveOrderStatus.PARTIALLY_FILLED
    assert dao.conn.execute("SELECT COUNT(*) FROM live_fill").fetchone()[0] == 1


def test_duplicate_fills_are_ignored(
    oms: OrderManager, fake_broker: FakeBroker, dao: LiveTradeDAO
) -> None:
    """
    重複推送的成交只算一次

    重複記一筆成交等於帳上多了一個不存在的部位。
    """

    ticket: OrderTicket = oms.submit(make_order(volume=2), "MomentumStrategy1")
    report: ExecutionReport = ExecutionReport(
        broker_seqno=ticket.broker_seqno or "",
        broker_trade_id="T999",
        symbol="2330",
        action=Action.BUY,
        price=1000.0,
        volume=1,
    )
    fake_broker.execution_queue.put(report)
    fake_broker.execution_queue.put(report)
    oms.drain_executions()

    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_fill WHERE broker_trade_id = 'T999'"
        ).fetchone()[0]
        == 1
    )


def test_average_fill_price_is_volume_weighted(
    oms: OrderManager, fake_broker: FakeBroker
) -> None:
    """
    成交均價以量加權

    直接取最後一筆的價格，會讓分批成交的成本整個偏掉，而 PnL 不會報錯。
    """

    fake_broker.fill_ratio = 0.0
    ticket: OrderTicket = oms.submit(make_order(volume=3), "MomentumStrategy1")
    for trade_id, price, volume in [("T1", 1000.0, 1), ("T2", 1006.0, 2)]:
        fake_broker.execution_queue.put(
            ExecutionReport(
                broker_seqno=ticket.broker_seqno or "",
                broker_trade_id=trade_id,
                symbol="2330",
                action=Action.BUY,
                price=price,
                volume=volume,
            )
        )
    oms.drain_executions()

    assert ticket.avg_fill_price == pytest.approx(1004.0)
    assert ticket.status is LiveOrderStatus.FILLED


def test_unmatched_fill_is_still_recorded(
    oms: OrderManager, fake_broker: FakeBroker, dao: LiveTradeDAO
) -> None:
    """
    找不到委託的成交仍要寫進 DB

    丟掉它的話，帳上會少一筆真實發生的交易，而對帳只會說「數字對不上」。
    """

    fake_broker.execution_queue.put(
        ExecutionReport(
            broker_seqno="999999",
            broker_trade_id="TX",
            symbol="2330",
            action=Action.BUY,
            price=1000.0,
            volume=1,
        )
    )
    oms.drain_executions()

    assert dao.conn.execute("SELECT COUNT(*) FROM live_fill").fetchone()[0] == 1
    assert (
        dao.conn.execute("SELECT category FROM live_risk_event").fetchone()[0]
        == "FILL_UNMATCHED"
    )


def test_rejection_event_moves_ticket_to_rejected(
    oms: OrderManager, fake_broker: FakeBroker
) -> None:
    """券商的失敗回報要反映成拒單"""

    fake_broker.fill_ratio = 0.0
    ticket: OrderTicket = oms.submit(make_order(), "MomentumStrategy1")

    fake_broker.execution_queue.put(
        OrderStatusEvent(
            broker_seqno=ticket.broker_seqno or "",
            op_type="New",
            op_code="99",
            op_msg="餘額不足",
        )
    )
    oms.drain_executions()

    assert ticket.status is LiveOrderStatus.REJECTED
    assert ticket.reject_reason == "餘額不足"


def test_contradictory_event_does_not_drop_the_rest_of_the_queue(
    oms: OrderManager, fake_broker: FakeBroker, dao: LiveTradeDAO
) -> None:
    """
    一筆狀態矛盾的回報不可讓整個佇列停擺

    例外往上拋的話，排在它後面那些正常的成交就全丟了——而成交是實盤唯一可信的
    部位來源。矛盾本身已經寫進 `live_risk_event`。
    """

    fake_broker.fill_ratio = 1.0
    filled: OrderTicket = oms.submit(make_order(), "MomentumStrategy1")
    oms.drain_executions()

    # 已成交的單又收到拒單事件（矛盾），後面接一筆正常成交
    fake_broker.execution_queue.put(
        OrderStatusEvent(
            broker_seqno=filled.broker_seqno or "",
            op_type="New",
            op_code="99",
            op_msg="不該發生",
        )
    )
    other: OrderTicket = oms.submit(make_order(symbol="2317"), "MomentumStrategy1")
    fills: List[ExecutionReport] = oms.drain_executions()

    assert filled.status is LiveOrderStatus.FILLED  # 矛盾的那筆沒有改到它
    assert other.status is LiveOrderStatus.FILLED  # 後面的照樣處理
    assert len(fills) == 1
    assert (
        dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'ORDER_STATE_INVALID'"
        ).fetchone()[0]
        == 1
    )


# === 撤單 ===
def test_cancel_skips_terminal_orders(
    oms: OrderManager, fake_broker: FakeBroker
) -> None:
    """已終結的委託不送撤單請求"""

    fake_broker.fill_ratio = 1.0
    oms.submit(make_order(), "MomentumStrategy1")
    oms.drain_executions()

    assert oms.cancel_open_orders() == []
    assert fake_broker.cancel_requests == []


def test_cancel_filters_by_timing(oms: OrderManager, fake_broker: FakeBroker) -> None:
    """只撤指定段落送出的單：盤前掛的單不該在尾盤段被一起撤掉"""

    fake_broker.fill_ratio = 0.0
    open_order: StockOrder = make_order()
    open_order.timing = ExecutionTiming.AT_OPEN
    oms.submit(open_order, "MomentumStrategy1")
    oms.submit(make_order(symbol="2317"), "MomentumStrategy1")  # AT_CLOSE

    cancelled: List[OrderTicket] = oms.cancel_open_orders(ExecutionTiming.AT_CLOSE)

    assert [ticket.order.symbol for ticket in cancelled] == ["2317"]


def test_cancel_failure_does_not_stop_the_rest(
    oms: OrderManager, fake_broker: FakeBroker
) -> None:
    """
    一張撤不掉不該讓後面幾張留在場上

    段落結束時要把能撤的都撤掉。
    """

    fake_broker.fill_ratio = 0.0
    oms.submit(make_order(), "MomentumStrategy1")
    oms.submit(make_order(symbol="2317"), "MomentumStrategy1")

    original: Callable[..., Any] = fake_broker.cancel_order
    calls: List[int] = []

    def flaky(ticket: OrderTicket) -> OrderTicket:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("撤單失敗")
        return original(ticket)

    fake_broker.cancel_order = flaky  # type: ignore[assignment]
    cancelled: List[OrderTicket] = oms.cancel_open_orders()

    assert len(calls) == 2
    assert len(cancelled) == 1


# === 重啟接管 ===
def test_recover_matches_by_seqno(
    oms: OrderManager, dao: LiveTradeDAO, fake_broker: FakeBroker
) -> None:
    """有券商序號的委託直接對應"""

    fake_broker.fill_ratio = 0.0
    ticket: OrderTicket = oms.submit(make_order(), "MomentumStrategy1")

    fresh: OrderManager = OrderManager(
        fake_broker, dao, "run2", run_index=2, now_provider=lambda: NOW
    )
    recovered: List[OrderTicket] = fresh.recover(TODAY)

    assert len(recovered) == 1
    assert recovered[0].broker_seqno == ticket.broker_seqno


def test_recover_marks_unknown_orders_failed_without_resubmitting(
    oms: OrderManager, dao: LiveTradeDAO, fake_broker: FakeBroker
) -> None:
    """
    完整刷新後仍查不到的標為 `FAILED`，**不自動重送**

    重送有機會在券商端已經有一張單的情況下再下一張。
    """

    dao.upsert_order(
        {
            "client_order_id": "run1-0009",
            "run_id": "run1",
            "strategy_name": "MomentumStrategy1",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 2,
            "status": "PENDING_SUBMIT",
            "custom_field": "010009",
            "created_at": NOW,
        }
    )
    before: int = fake_broker.placed_count
    recovered: List[OrderTicket] = oms.recover(TODAY)

    assert recovered[0].status is LiveOrderStatus.FAILED
    assert fake_broker.placed_count == before


def test_ambiguous_fuzzy_match_degrades_instead_of_guessing(
    oms: OrderManager,
    dao: LiveTradeDAO,
    fake_broker: FakeBroker,
    degradations: List[str],
) -> None:
    """模糊比對到多筆時**不猜**，一律標成 `FAILED` 並降級（理由見模組說明）"""

    fake_broker.fill_ratio = 0.0
    oms.submit(make_order(), "MomentumStrategy1")
    oms.submit(make_order(), "MomentumStrategy1")  # 同標的同價量，刻意製造歧義

    dao.upsert_order(
        {
            "client_order_id": "run1-0009",
            "run_id": "run1",
            "strategy_name": "MomentumStrategy1",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 1000.0,
            "volume": 2,
            "status": "PENDING_SUBMIT",
            "custom_field": "010009",
            "created_at": NOW,
        }
    )

    fresh: OrderManager = OrderManager(
        fake_broker,
        dao,
        "run2",
        run_index=2,
        on_degrade=degradations.append,
        now_provider=lambda: NOW,
    )
    recovered: List[OrderTicket] = fresh.recover(TODAY)
    ambiguous: Optional[OrderTicket] = next(
        (t for t in recovered if t.client_order_id == "run1-0009"), None
    )

    assert ambiguous is not None
    assert ambiguous.status is LiveOrderStatus.FAILED
    assert any("模糊比對" in reason for reason in degradations)
