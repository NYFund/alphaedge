import datetime
import json
import queue
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import pytest

from core.broker.execution_dedup import ExecutionEventDeduplicator
from core.broker.tw.shioaji_execution_handler import ShioajiExecutionHandler
from core.models import ExecutionReport, OrderStatusEvent
from core.utils import Action, OrderState

"""
回報正規化與去重：亂序、重複都要得到同一個結果

實盤的回報有三個不能假設的性質——不保證順序、不保證不重複、不保證完整。
前兩者可以在程式裡處理，第三者只能靠盤後對帳。本檔驗前兩者。

另外驗一件容易被忽略的事：**回呼裡不可以拋例外**——代價寫在
`test_callback_swallows_exceptions` 的說明裡。
"""


def stock_deal(
    seqno: str = "000001",
    trade_id: str = "T001",
    price: float = 1000.0,
    quantity: int = 2,
    action: str = "Buy",
    ts: float = 1789000000.0,
) -> Dict[str, Any]:
    """一筆股票成交回報（欄位名與 Shioaji 的 `StockDeal` 對齊）"""

    return {
        "trade_id": trade_id,
        "seqno": seqno,
        "ordno": "AB123",
        "exchange_seq": "123456",
        "action": action,
        "code": "2330",
        "price": price,
        "quantity": quantity,
        "custom_field": "r1a001",
        "ts": ts,
    }


def order_event(
    seqno: str = "000001",
    op_type: str = "New",
    op_code: str = "00",
    exchange_ts: float = 1789000000.0,
) -> Dict[str, Any]:
    """一筆委託狀態回報（巢狀結構與 Shioaji 的 `StockOrder` 對齊）"""

    return {
        "operation": {"op_type": op_type, "op_code": op_code, "op_msg": ""},
        "order": {
            "id": "abc",
            "seqno": seqno,
            "ordno": "AB123",
            "action": "Buy",
            "price": 1000.0,
            "quantity": 2,
            "custom_field": "r1a001",
        },
        "status": {"id": "abc", "exchange_ts": exchange_ts, "order_quantity": 2},
        "contract": {"code": "2330", "exchange": "TSE"},
    }


@pytest.fixture
def event_queue() -> queue.Queue:
    return queue.Queue()


@pytest.fixture
def handler(event_queue: queue.Queue) -> ShioajiExecutionHandler:
    return ShioajiExecutionHandler(event_queue)


def drain(event_queue: queue.Queue) -> List[Any]:
    """取出佇列中的所有事件"""

    events: List[Any] = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    return events


# === 解析 ===
def test_stock_deal_becomes_an_execution_report(
    handler: ShioajiExecutionHandler,
) -> None:
    """成交回報轉成 `ExecutionReport`，原始訊息完整保留"""

    report: ExecutionReport = handler.parse(OrderState.StockDeal, stock_deal())

    assert isinstance(report, ExecutionReport)
    assert report.broker_seqno == "000001"
    assert report.broker_trade_id == "T001"
    assert report.symbol == "2330"
    assert report.action is Action.BUY
    assert report.volume == 2
    assert report.raw["exchange_seq"] == "123456"


def test_futures_deal_uses_the_same_parser(handler: ShioajiExecutionHandler) -> None:
    """期貨成交的共同欄位與股票相同；契約細節留在 `raw` 裡"""

    msg: Dict[str, Any] = stock_deal()
    msg.update({"code": "TXF202601", "delivery_month": "202601"})
    report: ExecutionReport = handler.parse(OrderState.FuturesDeal, msg)

    assert report.symbol == "TXF202601"
    assert report.raw["delivery_month"] == "202601"


def test_order_event_is_parsed_from_the_nested_payload(
    handler: ShioajiExecutionHandler,
) -> None:
    """委託回報是巢狀的：`operation`／`order`／`status`／`contract`"""

    event: OrderStatusEvent = handler.parse(OrderState.StockOrder, order_event())

    assert isinstance(event, OrderStatusEvent)
    assert event.broker_seqno == "000001"
    assert event.op_type == "New"
    assert event.symbol == "2330"
    assert event.custom_field == "r1a001"
    assert event.is_failure is False


def test_failed_operation_is_flagged(handler: ShioajiExecutionHandler) -> None:
    """
    `op_code` 不是 `"00"` 就是失敗

    只看有沒有收到回報是不夠的——被拒的單同樣會有回報，差別只在這個代碼。
    """

    event: OrderStatusEvent = handler.parse(
        OrderState.StockOrder, order_event(op_code="99")
    )

    assert event.is_failure is True


def test_missing_nested_sections_do_not_raise(
    handler: ShioajiExecutionHandler,
) -> None:
    """
    缺欄位不可拋出

    不同操作別帶的欄位不一樣，硬取會在某個分支拋 KeyError，
    而那會發生在券商的執行緒裡。
    """

    event: OrderStatusEvent = handler.parse(OrderState.StockOrder, {})

    assert event.broker_seqno == ""
    assert event.op_type == ""


def test_unknown_state_returns_none(handler: ShioajiExecutionHandler) -> None:
    """不認得的回報種類回 None 並記 warning，不拋出"""

    assert handler.parse("SOMETHING_NEW", {}) is None


# === 時戳 ===
def test_timestamp_is_timezone_aware(handler: ShioajiExecutionHandler) -> None:
    """
    時戳一律轉成台北時區的 aware datetime

    當成 naive 本地時間的話，主機時區是 UTC 時整條時間軸會平移八小時，
    而段落判定全建立在它上面。
    """

    report: ExecutionReport = handler.parse(OrderState.StockDeal, stock_deal())

    assert report.ts is not None
    assert report.ts.tzinfo is not None
    assert report.ts.utcoffset() == datetime.timedelta(hours=8)


def test_unparsable_timestamp_becomes_none(handler: ShioajiExecutionHandler) -> None:
    """壞掉的時戳不可讓整筆回報消失：欄位留 None，其餘照常"""

    report: ExecutionReport = handler.parse(
        OrderState.StockDeal, stock_deal(ts="not-a-timestamp")
    )

    assert report.ts is None
    assert report.volume == 2


def test_clock_check_runs_once_on_the_first_timestamped_report(
    event_queue: queue.Queue,
) -> None:
    """
    以第一筆帶時戳的回報做一次時鐘檢查

    登入時只拿得到合約檔的日期（沒有秒），秒級偏差要等回報進來才驗得到。
    每筆都驗則是白做工——時鐘不會在盤中自己跳。
    """

    seen: List[datetime.datetime] = []
    handler: ShioajiExecutionHandler = ShioajiExecutionHandler(
        event_queue, on_first_report_ts=seen.append
    )

    handler.on_order_event(OrderState.StockDeal, stock_deal())
    handler.on_order_event(OrderState.StockDeal, stock_deal(trade_id="T002"))

    assert len(seen) == 1


# === 回呼的健壯性 ===
def test_callback_swallows_exceptions(
    handler: ShioajiExecutionHandler, event_queue: queue.Queue
) -> None:
    """
    回呼絕不可拋出

    它跑在 Shioaji 的執行緒上。拋出去那條執行緒就死了，之後所有回報靜默消失，
    而程式看起來還活著、部位也還在場上。寧可丟掉一筆有問題的回報。
    """

    handler.on_order_event(OrderState.StockDeal, stock_deal(action="不存在的動作"))

    assert drain(event_queue) == []


def test_callback_only_enqueues(
    handler: ShioajiExecutionHandler, event_queue: queue.Queue
) -> None:
    """回呼只做轉換與入列：重運算留給主執行緒"""

    handler.on_order_event(OrderState.StockDeal, stock_deal())
    events: List[Any] = drain(event_queue)

    assert len(events) == 1
    assert isinstance(events[0], ExecutionReport)


# === 去重與重放 ===
def test_duplicate_fills_are_dropped() -> None:
    """同一筆成交重推時只留第一次"""

    dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()
    first: ExecutionReport = ExecutionReport(broker_seqno="1", broker_trade_id="A")
    replay: ExecutionReport = ExecutionReport(broker_seqno="1", broker_trade_id="A")

    assert dedup.is_new(first) is True
    assert dedup.is_new(replay) is False


def test_separate_fills_of_the_same_order_are_kept() -> None:
    """
    同一張單的第二筆成交不是重複

    分批成交時兩筆的價格與數量可能完全相同，靠內容去重會把真實的第二筆丟掉——
    帳上就少了一半的部位。
    """

    dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()

    assert dedup.is_new(ExecutionReport(broker_seqno="1", broker_trade_id="A")) is True
    assert dedup.is_new(ExecutionReport(broker_seqno="1", broker_trade_id="B")) is True


def test_order_events_dedup_on_operation_and_timestamp() -> None:
    """
    委託事件以 `(序號, 操作別, 交易所時戳)` 去重

    同一張單的兩次改價是兩個事件，只看序號與操作別會把第二次吃掉。
    """

    dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()
    first_ts: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 25)
    second_ts: datetime.datetime = datetime.datetime(2026, 9, 19, 13, 27)

    assert (
        dedup.is_new(
            OrderStatusEvent(
                broker_seqno="1", op_type="UpdatePrice", exchange_ts=first_ts
            )
        )
        is True
    )
    assert (
        dedup.is_new(
            OrderStatusEvent(
                broker_seqno="1", op_type="UpdatePrice", exchange_ts=first_ts
            )
        )
        is False
    )
    assert (
        dedup.is_new(
            OrderStatusEvent(
                broker_seqno="1", op_type="UpdatePrice", exchange_ts=second_ts
            )
        )
        is True
    )


def test_fill_and_order_event_with_the_same_key_do_not_collide() -> None:
    """
    成交與委託事件即使鍵值長得像也不可互相吃掉

    兩者的鍵都以序號開頭。不把型別納入鍵的話，一筆成交會讓同序號的委託事件
    被當成重複丟掉。
    """

    dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()

    assert dedup.is_new(ExecutionReport(broker_seqno="1", broker_trade_id="")) is True
    assert dedup.is_new(OrderStatusEvent(broker_seqno="1", op_type="")) is True


def replay(
    handler: ShioajiExecutionHandler,
    event_queue: queue.Queue,
    lines: List[Tuple[Any, Dict[str, Any]]],
) -> Tuple[int, int]:
    """重放一批回報，回傳 (去重後的成交筆數, 成交總量)"""

    dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()
    for stat, msg in lines:
        handler.on_order_event(stat, msg)

    fills: int = 0
    volume: int = 0
    for event in drain(event_queue):
        if isinstance(event, ExecutionReport) and dedup.is_new(event):
            fills += 1
            volume += event.volume
    return (fills, volume)


def test_replay_is_order_and_duplication_invariant(
    handler: ShioajiExecutionHandler, event_queue: queue.Queue
) -> None:
    """
    同一份回報打亂順序、每筆重複兩次，最後的成交彙總不變

    這是實盤最常見的兩種雜訊：斷線重連後的重推、以及成交回報比委託確認先到。
    """

    lines: List[Tuple[Any, Dict[str, Any]]] = [
        (OrderState.StockOrder, order_event()),
        (OrderState.StockDeal, stock_deal(trade_id="T001", quantity=1)),
        (OrderState.StockDeal, stock_deal(trade_id="T002", quantity=1)),
        (OrderState.StockDeal, stock_deal(seqno="000002", trade_id="T003", quantity=3)),
    ]
    baseline: Tuple[int, int] = replay(handler, event_queue, lines)

    noisy: List[Tuple[Any, Dict[str, Any]]] = lines * 2
    random.Random(20260919).shuffle(noisy)

    assert replay(handler, event_queue, noisy) == baseline
    assert baseline == (3, 5)


# === 錄製 ===
def test_recording_appends_jsonl(event_queue: queue.Queue, tmp_path: Path) -> None:
    """
    錄製模式把原始回報存成 JSONL

    這是唯一能把「當天到底收到什麼」重現出來的方式——實盤的回報不可能重來一次。
    """

    record: Path = tmp_path / "nested" / "reports.jsonl"
    handler: ShioajiExecutionHandler = ShioajiExecutionHandler(
        event_queue, record_path=record
    )

    handler.on_order_event(OrderState.StockDeal, stock_deal())
    handler.on_order_event(OrderState.StockDeal, stock_deal(trade_id="T002"))

    lines: List[str] = record.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["msg"]["trade_id"] == "T001"


def test_recording_failure_does_not_break_the_callback(
    event_queue: queue.Queue, tmp_path: Path
) -> None:
    """
    錄製失敗不可影響交易

    錄不到只是少了重放素材；錄製把回呼弄死才是真的事故。
    """

    blocked: Path = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    handler: ShioajiExecutionHandler = ShioajiExecutionHandler(
        event_queue, record_path=blocked / "reports.jsonl"
    )

    handler.on_order_event(OrderState.StockDeal, stock_deal())

    events: List[Union[ExecutionReport, OrderStatusEvent]] = drain(event_queue)
    assert len(events) == 1


# === 以實測回報釘住欄位名（2026-09-21 台北 13:13，模擬環境）===
# 帳號相關欄位換成假值；其餘欄位名、型別與結構照抄實際推播
REAL_STOCK_ORDER_EVENT: Dict[str, Any] = {
    "contract": {
        "code": "2330",
        "currency": "TWD",
        "exchange": "TSE",
        "security_type": "STK",
    },
    "event_id": "v1:SO:A1EGn1NBh:BOnXGyd:2",
    "operation": {"op_code": "00", "op_msg": "", "op_type": "New"},
    "order": {
        "account": {
            "account_id": "0000000",
            "account_type": "S",
            "broker_id": "XXXX",
            "person_id": "",
            "signed": True,
        },
        "action": "Buy",
        "custom_field": "",
        "id": "0087E4",
        "order_cond": "Cash",
        "order_lot": "Common",
        "order_type": "ROD",
        "ordno": "0EEC64",
        "price": 2215.0,
        "price_type": "LMT",
        "quantity": 1,
        "seqno": "0087E4",
    },
    "status": {
        "cancel_quantity": 0,
        "exchange_ts": 1789967612.079455,
        "id": "0087E4",
        "modified_price": 0,
        "order_quantity": 1,
        "web_id": "137",
    },
}


def test_real_order_event_fields_are_parsed() -> None:
    """
    欄位名原本取自文件，**這條以實際推播核對**

    parser 取值走 `.get()`，欄位名錯了只會靜靜變成空字串——委託序號變空的話，
    成交回報就對不回委託，而那筆成交會被記成「無法歸屬」。
    """

    event = ShioajiExecutionHandler(queue.Queue()).parse_order_event(
        REAL_STOCK_ORDER_EVENT
    )

    assert event.broker_seqno == "0087E4"
    assert event.broker_order_id == "0EEC64"
    assert (event.op_type, event.op_code) == ("New", "00")
    assert event.symbol == "2330"


def test_real_exchange_ts_is_seconds_not_nanoseconds() -> None:
    """
    委託回報的 `exchange_ts` 是**浮點秒**

    與 `Snapshot.ts`（奈秒）不同。兩條路徑的時戳不可共用一套換算：
    秒當成奈秒會得到 1970 年，奈秒當成秒則是一個遙遠的未來。
    """

    event = ShioajiExecutionHandler(queue.Queue()).parse_order_event(
        REAL_STOCK_ORDER_EVENT
    )

    assert event.exchange_ts is not None
    assert (event.exchange_ts.year, event.exchange_ts.month, event.exchange_ts.day) == (
        2026,
        9,
        21,
    )
    assert (event.exchange_ts.hour, event.exchange_ts.minute) == (13, 13)


def test_real_callback_does_not_carry_person_id() -> None:
    """
    實際推播的 `person_id` 是空字串

    記下來是因為錄製會把原始訊息整份落地：若哪天券商開始帶身分證字號，
    錄製檔就成了敏感資料，要跟著改成刮過再存。
    """

    account: Dict[str, Any] = REAL_STOCK_ORDER_EVENT["order"]["account"]

    assert account["person_id"] == ""
