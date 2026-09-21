import inspect
from abc import ABC
from typing import Callable, List, Set

import pytest

from core.broker.base import BaseBroker
from core.models import BaseQuote, BrokerAccountSnapshot, ExecutionReport, OrderTicket
from core.utils import LiveOrderStatus

from .conftest import FakeBroker

"""
券商閘道的**介面契約**：任何 `BaseBroker` 實作都要通過這一份

本檔刻意只驗介面層面的行為（連線、狀態轉移、回報入列、查無資料的處理、
訂閱上限），不驗某家券商的特有語意——那些留給各實作自己的測試。

`ShioajiBroker` 要以 `-m shioaji_sim` 在模擬環境跑同一份，
兩邊都通過才算介面成立。只靠假券商測出來的東西到了實盤不算數：
假券商永遠照著腳本走，而真券商會在你沒想到的地方回一個 None。
"""


def test_base_broker_is_abstract() -> None:
    """
    介面本身不可被實例化

    少了這道保險，忘記實作某個方法的子類別會在**盤中第一次呼叫它**時才炸，
    而那時已經有部位在場上。
    """

    assert issubclass(BaseBroker, ABC)
    with pytest.raises(TypeError):
        BaseBroker()  # type: ignore[abstract]


def test_every_abstract_method_is_implemented_by_fake() -> None:
    """
    `FakeBroker` 必須實作介面的每一個抽象方法

    它是其他所有實盤測試的地基：地基少一塊，上面那些測試驗的是一個
    真實世界不存在的券商。
    """

    abstract: Set[str] = set(BaseBroker.__abstractmethods__)
    missing: List[str] = sorted(
        name
        for name in abstract
        if getattr(FakeBroker, name, None) is getattr(BaseBroker, name, None)
    )

    assert missing == [], f"FakeBroker 未實作：{missing}"


@pytest.mark.parametrize("name", sorted(BaseBroker.__abstractmethods__))
def test_implementation_signature_matches_the_interface(name: str) -> None:
    """
    實作的參數名要與介面相同

    引擎一律以關鍵字傳參。參數改名不會讓 `issubclass` 失敗，只會在呼叫時
    丟一個 `unexpected keyword argument`——而那要等到真的送單才會發生。
    """

    expected = inspect.signature(getattr(BaseBroker, name))
    actual = inspect.signature(getattr(FakeBroker, name))

    assert list(actual.parameters) == list(expected.parameters)


# === 連線 ===
def test_calls_before_connect_are_rejected() -> None:
    """
    未連線時的呼叫一律拋出，**不可靜默排隊等重連**

    靜默排隊的後果是：委託在連線恢復後才一次送出，而那時的價格已經不是
    算訊號時的價格了。
    """

    broker: FakeBroker = FakeBroker()

    assert broker.is_connected() is False
    with pytest.raises(ConnectionError):
        broker.get_positions()


def test_close_is_idempotent(fake_broker: FakeBroker) -> None:
    """`close()` 要可重複呼叫：引擎以 `try/finally` 保證它一定跑到，可能跑兩次"""

    fake_broker.close()
    fake_broker.close()

    assert fake_broker.is_connected() is False


def test_connect_failure_raises_instead_of_returning_a_flag() -> None:
    """登入失敗要拋出，不回傳成功與否的旗標——沒人會記得檢查回傳值"""

    broker: FakeBroker = FakeBroker()
    broker.fail_connect = True

    with pytest.raises(ConnectionError):
        broker.connect()


# === 下單與回報 ===
def test_place_order_assigns_broker_ids_and_pushes_fill(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """送出後要補上券商編號，成交回報進入 queue"""

    ticket: OrderTicket = fake_broker.place_order(make_ticket())

    assert ticket.broker_seqno is not None
    assert ticket.status is LiveOrderStatus.SUBMITTED

    reports: List[ExecutionReport] = fake_broker.drain_execution_queue()
    assert len(reports) == 1
    assert reports[0].broker_seqno == ticket.broker_seqno


def test_rejected_order_carries_a_reason_and_no_fill(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """
    拒單要有原因，而且不得產生任何成交回報

    拒單沒帶原因時，盤後只看得到「這張單沒成交」，分不出是被券商退還是沒撮合到。
    """

    fake_broker.reject_symbols.add("2330")
    ticket: OrderTicket = fake_broker.place_order(make_ticket())

    assert ticket.status is LiveOrderStatus.REJECTED
    assert ticket.reject_reason
    assert fake_broker.drain_execution_queue() == []


def test_partial_fill_leaves_remaining_volume(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """部分成交後殘量要算得出來：它是段落結束時撤不撤單的依據"""

    fake_broker.fill_ratio = 0.5
    ticket: OrderTicket = fake_broker.place_order(make_ticket(volume=4))
    reports: List[ExecutionReport] = fake_broker.drain_execution_queue()

    ticket.filled_volume = sum(report.volume for report in reports)

    assert ticket.filled_volume == 2
    assert ticket.remaining_volume == 2
    assert ticket.is_terminal is False


def test_reports_can_arrive_after_the_order_confirmation(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """
    回報晚到要能重現

    實盤的成交回報甚至可能**比委託確認先到**，OMS 因此必須以「成交即代表已提交」
    處理。測不出亂序的假券商，等於沒測到這條路徑。
    """

    fake_broker.defer_reports = True
    fake_broker.place_order(make_ticket())

    assert fake_broker.drain_execution_queue() == []

    fake_broker.release_reports()
    assert len(fake_broker.drain_execution_queue()) == 1


def test_drain_is_non_blocking_when_queue_is_empty(fake_broker: FakeBroker) -> None:
    """
    佇列空的時候要立刻回空清單

    阻塞式取用會讓「沒有回報」和「程式卡住」在外部看起來一模一樣，
    而段落迴圈還要在這段時間檢查時限與 kill switch。
    """

    assert fake_broker.drain_execution_queue() == []


def test_cancel_is_a_no_op_for_terminal_orders(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """已成交或已拒的委託不送撤單請求"""

    ticket: OrderTicket = make_ticket()
    ticket.status = LiveOrderStatus.FILLED
    fake_broker.cancel_order(ticket)

    assert fake_broker.cancel_requests == []
    assert ticket.status is LiveOrderStatus.FILLED


def test_cancel_moves_open_order_to_cancelled(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """未成交的委託撤單後轉入 CANCELLED，並且真的送出了請求"""

    fake_broker.fill_ratio = 0.0
    ticket: OrderTicket = fake_broker.place_order(make_ticket())
    fake_broker.cancel_order(ticket)

    assert ticket.status is LiveOrderStatus.CANCELLED
    assert fake_broker.cancel_requests == [ticket.client_order_id]


def test_update_price_on_terminal_order_raises(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """
    已終結的委託不可改價

    平倉單追價時最容易踩到這條：剛好在改價前成交了，改價請求送出去只會
    拿到一個難讀的券商錯誤。
    """

    ticket: OrderTicket = make_ticket()
    ticket.status = LiveOrderStatus.FILLED

    with pytest.raises(ValueError):
        fake_broker.update_order_price(ticket, price=1010.0)


def test_disconnect_mid_session_surfaces_as_error(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """斷線要當場拋出並反映在 `is_connected()` 上，不可假裝送出去了"""

    fake_broker.drop_connection_after = 1
    fake_broker.place_order(make_ticket())

    with pytest.raises(ConnectionError):
        fake_broker.place_order(make_ticket())

    assert fake_broker.is_connected() is False


def test_refresh_order_status_returns_broker_side_orders(
    fake_broker: FakeBroker, make_ticket: Callable[..., OrderTicket]
) -> None:
    """恢復流程要能從券商端把當日委託撈回來"""

    fake_broker.place_order(make_ticket())
    fake_broker.place_order(make_ticket())

    assert len(fake_broker.refresh_order_status()) == 2


# === 帳務與行情 ===
def test_account_snapshot_shape(fake_broker: FakeBroker) -> None:
    """帳務查詢回傳的是正規化物件，不是券商原生型別或 dict"""

    account: BrokerAccountSnapshot = fake_broker.get_account()

    assert isinstance(account, BrokerAccountSnapshot)
    assert account.total_equity >= account.available_balance


def test_snapshots_skip_unknown_symbols(fake_broker: FakeBroker) -> None:
    """
    查無資料的標的直接略過，回傳筆數與請求筆數**不保證相同**

    呼叫端因此一律以 symbol 對應。用位置索引的話，停牌一檔就會讓後面所有
    標的的報價集體錯位，而且每一筆看起來都是合法報價。
    """

    quotes: List[BaseQuote] = fake_broker.get_snapshots(["2330", "9999"])

    assert [quote.symbol for quote in quotes] == ["2330"]


def test_subscription_limit_raises_before_subscribing(fake_broker: FakeBroker) -> None:
    """
    超過訂閱上限要在訂閱**之前**拋出

    先訂到滿再失敗的話，前面那幾檔會訂閱成功、後面的默默訂不到，
    於是策略只收得到一部分標的的行情，而它不會知道。
    """

    with pytest.raises(ValueError):
        fake_broker.subscribe_quotes(
            [f"{index:04d}" for index in range(FakeBroker.MAX_SUBSCRIPTIONS + 1)]
        )

    assert fake_broker.subscribed == set()
