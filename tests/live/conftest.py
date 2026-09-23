import datetime
import itertools
from typing import Any, Callable, Dict, List, Optional, Set

import pytest

from core.broker.base import BaseBroker
from core.models import (
    BaseQuote,
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    ExecutionReport,
    FuturesAccountSnapshot,
    OrderStatusEvent,
    OrderTicket,
    StockOrder,
    StockQuote,
)
from core.utils import Action, LiveOrderStatus, PositionType, Scale

"""
`FakeBroker`：可腳本化的假券商，供實盤各層在不連網的情況下測試

**為什麼要有它**：實盤最難測的不是「一切正常」，而是延遲回報、部分成交、拒單、
斷線這四種。模擬環境跑不出這些——它只會照常成交。沒有假券商的話，這些路徑會
一直到正式環境才第一次被執行。

`FakeBroker` 與 `ShioajiBroker` 跑**同一份契約測試**（`test_broker_contract.py`），
所以它不是「比較寬鬆的替身」：兩者在介面層面必須表現一致，否則用它測過的東西
到了實盤不算數。
"""


class FakeBroker(BaseBroker):
    """
    測試用的假券商

    行為以屬性腳本化，不用 monkeypatch：
    - `reject_symbols`：這些標的一律拒單。
    - `fill_ratio`：送單後立刻成交的比例（0 為完全不成交、0.5 為部分成交）。
    - `defer_reports`：True 時回報先存著不入列，由 `release_reports()` 手動釋放，
      用來模擬「委託確認與成交回報亂序到達」。
    - `fail_connect`／`drop_connection_after`：連線失敗與中途斷線。
    """

    MAX_SUBSCRIPTIONS: int = 200  # 與券商「每連線最多 200 檔」對齊

    def __init__(self) -> None:
        super().__init__()

        self._connected: bool = False
        self._seqno: itertools.count = itertools.count(1)
        self._trade_seq: itertools.count = itertools.count(1)

        # 券商端狀態
        self.tickets: Dict[str, OrderTicket] = {}
        self.positions: List[BrokerPositionSnapshot] = []
        self.account: BrokerAccountSnapshot = BrokerAccountSnapshot(
            available_balance=1_000_000.0, total_equity=1_000_000.0
        )
        # 期貨保證金帳戶與股票是兩個子帳戶，各有各的錢。
        # **一定要是帶保證金欄位的型別**：送單前的保證金檢查讀 `available_margin`，
        # 回骨架型別的話它一律看到 0，每一張期貨開倉單都會被擋下來。
        # 預設與股票同額，要驗分派的測試自行改掉其中一邊
        self.futures_account: FuturesAccountSnapshot = FuturesAccountSnapshot(
            available_balance=1_000_000.0,
            total_equity=1_000_000.0,
            available_margin=1_000_000.0,
        )
        self.quotes: Dict[str, BaseQuote] = {}
        self.subscribed: Set[str] = set()

        # 腳本化開關
        self.reject_symbols: Set[str] = set()
        self.fill_ratio: float = 1.0
        self.defer_reports: bool = False
        self.fail_connect: bool = False
        self.drop_connection_after: Optional[int] = None

        # 觀測用
        self.placed_count: int = 0
        self.cancel_requests: List[str] = []
        self.pending_reports: List[Any] = []  # 成交回報與撤單回報

    # === 連線 ===
    def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("FakeBroker: 登入失敗（腳本化）")
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _require_connection(self) -> None:
        """未連線時一律拋出，不靜默排隊等重連"""

        if not self._connected:
            raise ConnectionError("FakeBroker: 尚未連線")

    # === 下單 ===
    def place_order(self, ticket: OrderTicket) -> OrderTicket:
        self._require_connection()

        self.placed_count += 1
        if (
            self.drop_connection_after is not None
            and self.placed_count > self.drop_connection_after
        ):
            self._connected = False
            raise ConnectionError("FakeBroker: 送單途中斷線（腳本化）")

        ticket.broker_seqno = f"{next(self._seqno):06d}"
        ticket.broker_order_id = f"A{ticket.broker_seqno}"
        self.tickets[ticket.client_order_id] = ticket

        symbol: str = ticket.order.symbol if ticket.order is not None else ""
        if symbol in self.reject_symbols:
            ticket.status = LiveOrderStatus.REJECTED
            ticket.reject_reason = "FakeBroker: 標的被腳本化為拒單"
            return ticket

        ticket.status = LiveOrderStatus.SUBMITTED
        self._maybe_fill(ticket)
        return ticket

    def _maybe_fill(self, ticket: OrderTicket) -> None:
        """依 `fill_ratio` 產生成交回報；比例為 0 時不產生任何回報"""

        if ticket.order is None or self.fill_ratio <= 0:
            return

        volume: int = int(ticket.order.volume * self.fill_ratio)
        if volume <= 0:
            return

        report: ExecutionReport = ExecutionReport(
            broker_seqno=ticket.broker_seqno or "",
            broker_trade_id=f"T{next(self._trade_seq):06d}",
            symbol=ticket.order.symbol,
            action=ticket.order.action,
            price=ticket.order.price,
            volume=volume,
            ts=datetime.datetime(2026, 9, 19, 13, 25),
            raw={"source": "FakeBroker"},
        )
        if self.defer_reports:
            self.pending_reports.append(report)
        else:
            self.execution_queue.put(report)

    def release_reports(self) -> None:
        """把延後的回報一次入列，用來模擬回報比委託確認晚到"""

        for report in self.pending_reports:
            self.execution_queue.put(report)
        self.pending_reports.clear()

    def cancel_order(self, ticket: OrderTicket) -> OrderTicket:
        self._require_connection()

        # 已是終態的委託不送撤單請求：那會拿到一個看起來很像真問題的券商錯誤
        if ticket.is_terminal:
            return ticket

        # 與真券商相同（2026-09-22 模擬環境實測）：撤單請求**不改本地狀態**，
        # 券商另推一筆 `op_type='Cancel'` 的委託回報，由 OMS 消化後才轉 CANCELLED。
        # 以前這裡直接改 ticket 的狀態，於是「撤單請求一送出就轉終態」這個 bug
        # 在假券商上永遠測不出來
        self.cancel_requests.append(ticket.client_order_id)
        event: OrderStatusEvent = OrderStatusEvent(
            broker_seqno=ticket.broker_seqno or "",
            broker_order_id=ticket.broker_order_id,
            op_type="Cancel",
            op_code="00",
            symbol=ticket.order.symbol if ticket.order is not None else "",
            custom_field=ticket.custom_field or "",
            exchange_ts=datetime.datetime(2026, 9, 19, 13, 26)
            + datetime.timedelta(seconds=len(self.cancel_requests)),
            raw={"source": "FakeBroker"},
        )
        if self.defer_reports:
            self.pending_reports.append(event)
        else:
            self.execution_queue.put(event)
        return ticket

    def update_order_price(self, ticket: OrderTicket, price: float) -> OrderTicket:
        self._require_connection()

        if ticket.is_terminal:
            raise ValueError("FakeBroker: 已終結的委託不可改價")
        if ticket.order is not None:
            ticket.order.price = price
        return ticket

    def refresh_order_status(self) -> List[OrderTicket]:
        self._require_connection()

        return list(self.tickets.values())

    # === 帳務 ===
    def get_positions(self) -> List[BrokerPositionSnapshot]:
        self._require_connection()

        return list(self.positions)

    def get_account(self) -> BrokerAccountSnapshot:
        self._require_connection()

        return self.account

    def get_futures_account(self) -> FuturesAccountSnapshot:
        self._require_connection()

        return self.futures_account

    # === 行情 ===
    def get_snapshots(self, symbols: List[str]) -> List[BaseQuote]:
        self._require_connection()

        # 查無資料的標的直接略過，與真實券商一致——呼叫端不可用位置索引對應
        return [self.quotes[symbol] for symbol in symbols if symbol in self.quotes]

    def subscribe_quotes(self, symbols: List[str]) -> None:
        self._require_connection()

        if len(self.subscribed | set(symbols)) > self.MAX_SUBSCRIPTIONS:
            raise ValueError(
                f"FakeBroker: 訂閱數超過單一連線上限 {self.MAX_SUBSCRIPTIONS}"
            )
        self.subscribed |= set(symbols)

    def unsubscribe_quotes(self, symbols: List[str]) -> None:
        self._require_connection()

        self.subscribed -= set(symbols)


@pytest.fixture
def fake_broker() -> FakeBroker:
    """已連線的 `FakeBroker`，帶一檔報價"""

    broker: FakeBroker = FakeBroker()
    broker.connect()
    broker.quotes["2330"] = StockQuote(
        stock_id="2330",
        scale=Scale.DAY,
        date=datetime.date(2026, 9, 19),
        cur_price=1000.0,
        volume=5000,
        open=990.0,
        high=1005.0,
        low=985.0,
        close=1000.0,
    )
    broker.positions.append(
        BrokerPositionSnapshot(
            symbol="2330", direction=PositionType.LONG, volume=2, avg_price=980.0
        )
    )
    return broker


@pytest.fixture
def make_ticket() -> Callable[..., OrderTicket]:
    """建立 `OrderTicket` 的 factory；預設是一張 2330 的買單"""

    counter: itertools.count = itertools.count(1)

    def _make(
        symbol: str = "2330",
        action: Action = Action.BUY,
        volume: int = 2,
        price: float = 1000.0,
        strategy_name: str = "MomentumStrategy1",
    ) -> OrderTicket:
        return OrderTicket(
            client_order_id=f"run1-{next(counter):04d}",
            strategy_name=strategy_name,
            order=StockOrder(
                stock_id=symbol, action=action, volume=volume, price=price
            ),
            created_at=datetime.datetime(2026, 9, 19, 13, 25),
        )

    return _make
