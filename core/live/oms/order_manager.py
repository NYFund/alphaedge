import datetime
import itertools
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from loguru import logger

from core.broker.base import BaseBroker
from core.broker.execution_dedup import ExecutionEventDeduplicator
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.models import BaseOrder, ExecutionReport, OrderStatusEvent, OrderTicket
from core.utils import ExecutionTiming, LiveOrderStatus

"""
OrderManager：委託狀態機、送單、回報消化與重啟接管

三條規則撐起整個恢復能力：

1. **先寫 DB 再送單。** 程式在 `place_order()` 前後崩潰時，重啟後靠 `live_order` 的
   `PENDING_SUBMIT` 紀錄去券商查詢接管，不會因為「不知道送出去了沒」而重送。
   反過來（先送再寫）崩潰時本地什麼都沒有，那張單會變成無主部位。
2. **每張單都帶著自己的識別碼往返券商。** `custom_field` 只有 6 個字元，
   放不下完整的 client order id，故放壓縮碼——它可以經本地紀錄反查策略，
   策略代號卻反查不到是哪一張單。
3. **查不到就不猜。** 恢復時模糊比對到多筆一律標為需人工確認並送降級事件，
   不挑一個看起來最像的。猜錯的後果是把兩張單的成交記到同一張上。

狀態機只允許往前走，非法轉移一律拋出並寫 `live_risk_event`——
靜默忽略會讓「已成交」被改回「已送出」，而引擎會繼續等它成交。
"""

# base36 的字母表；壓縮碼用它把數字塞進 6 個可列印 ASCII 字元
_BASE36_DIGITS: str = "0123456789abcdefghijklmnopqrstuvwxyz"

# 壓縮碼的欄位寬度：run 序號 2 碼 ＋ 委託序號 4 碼
RUN_CODE_WIDTH: int = 2
ORDER_CODE_WIDTH: int = 4

# 容量：1,296 次 run × 1,679,616 張委託
MAX_RUN_INDEX: int = 36**RUN_CODE_WIDTH
MAX_ORDER_INDEX: int = 36**ORDER_CODE_WIDTH


def to_base36(value: int, width: int) -> str:
    """
    - Description:
        把非負整數轉成固定寬度的 base36 字串

        **超出寬度時取模而不是截斷**：截斷會讓 1296 與 2592 壓成不同的字串卻
        都不等於原值，取模則是明確的「循環使用」，而循環的前提（同一天不會用滿）
        寫在 `OrderManager` 的 docstring 裡。
    - Parameters:
        - value: int
            待轉換的數字
        - width: int
            輸出寬度
    - Return:
        - str
            base36 字串
    """

    remainder: int = value % (36**width)
    digits: List[str] = []
    for _ in range(width):
        remainder, index = divmod(remainder, 36)
        digits.append(_BASE36_DIGITS[index])
    return "".join(reversed(digits))


class OrderStateError(RuntimeError):
    """非法的委託狀態轉移"""


class OrderManager:
    """
    - Description:
        委託管理

        **一個帳戶只有一個 OrderManager**：回報 callback 是 session 級的，
        分成多個 OMS 就得先把回報分派出去，那等於在更下層再做一次歸屬。
        多策略的歸屬靠每張委託上的 `strategy_name`，不是靠多個 OMS。
    """

    # 狀態轉移表。**只往前，不回頭**：允許回頭的話，一筆遲到的舊回報會把
    # 「已成交」改回「已送出」，而引擎會繼續等一張早就成交的單
    TRANSITIONS: Dict[LiveOrderStatus, Set[LiveOrderStatus]] = {
        LiveOrderStatus.PENDING_SUBMIT: {
            LiveOrderStatus.SUBMITTED,
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.FAILED,
            # 成交回報可能比委託確認先到，OMS 以「成交即代表已提交」處理
            LiveOrderStatus.PARTIALLY_FILLED,
            LiveOrderStatus.FILLED,
        },
        LiveOrderStatus.SUBMITTED: {
            LiveOrderStatus.PARTIALLY_FILLED,
            LiveOrderStatus.FILLED,
            LiveOrderStatus.CANCELLED,
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.FAILED,
        },
        LiveOrderStatus.PARTIALLY_FILLED: {
            LiveOrderStatus.PARTIALLY_FILLED,
            LiveOrderStatus.FILLED,
            LiveOrderStatus.CANCELLED,
        },
        LiveOrderStatus.FILLED: set(),
        LiveOrderStatus.CANCELLED: set(),
        LiveOrderStatus.REJECTED: set(),
        LiveOrderStatus.FAILED: set(),
    }

    # 恢復時模糊比對的送單時間窗（秒）
    FUZZY_MATCH_WINDOW_SECONDS: int = 600

    def __init__(
        self,
        broker: BaseBroker,
        dao: LiveTradeDAO,
        run_id: str,
        run_index: int = 0,
        dry_run: bool = False,
        on_degrade: Optional[Callable[[str], None]] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立委託管理器
        - Parameters:
            - broker: BaseBroker
                券商閘道
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_id: str
                本次啟動的識別碼（寫進 `live_order.run_id`）
            - run_index: int
                本次啟動的序號，用來組壓縮碼。同一個交易日內不可重複，
                否則兩次啟動的委託會壓出相同的碼，恢復時就分不出來
            - dry_run: bool
                走完整流程但不真的送出
            - on_degrade: Optional[Callable[[str], None]]
                降級回呼；OMS **不自己改交易模式**，只送事件給風控
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
        """

        self.broker: BaseBroker = broker
        self.dao: LiveTradeDAO = dao
        self.run_id: str = run_id
        self.run_index: int = run_index
        self.dry_run: bool = dry_run
        self._on_degrade: Optional[Callable[[str], None]] = on_degrade
        self._now: Callable[[], datetime.datetime] = now_provider

        self._sequence: itertools.count = itertools.count(1)
        self._event_sequence: Dict[str, itertools.count] = {}
        self._dedup: ExecutionEventDeduplicator = ExecutionEventDeduplicator()

        # client_order_id → ticket；本行程內的委託索引
        self.tickets: Dict[str, OrderTicket] = {}

    # === 識別碼 ===
    def next_client_order_id(self) -> str:
        """產生下一個 client order id：`{run_id}-{序號}`"""

        return f"{self.run_id}-{next(self._sequence):04d}"

    def compress(self, client_order_id: str) -> str:
        """
        - Description:
            把 client order id 壓成 6 個字元放進 `custom_field`

            格式是「run 序號 2 碼 ＋ 委託序號 4 碼」的 base36，容量
            1,296 次 run × 1,679,616 張委託。**這 6 個字元是唯一能讓委託
            帶著自己的識別碼往返券商的欄位**（FIX 的 `ClOrdID`），
            所以不拿去放策略代號——壓縮碼反查得到策略，反之不然。
        - Parameters:
            - client_order_id: str
                完整識別碼
        - Return:
            - str
                6 字元壓縮碼
        """

        order_index: int = int(client_order_id.rsplit("-", 1)[-1])
        return to_base36(self.run_index, RUN_CODE_WIDTH) + to_base36(
            order_index, ORDER_CODE_WIDTH
        )

    # === 送單 ===
    def submit(self, order: BaseOrder, strategy_name: str) -> OrderTicket:
        """
        - Description:
            送出一張委託：**先寫 DB 再呼叫 broker**

            先送再寫的話，程式在兩者之間崩潰時本地什麼都沒有，
            那張單會變成無主部位——既不知道它存在，也查不到它屬於誰。
        - Parameters:
            - order: BaseOrder
                已通過前處理與風控的訂單
            - strategy_name: str
                歸屬的策略
        - Return:
            - OrderTicket
                送出後的委託
        """

        client_order_id: str = self.next_client_order_id()
        order.client_order_id = client_order_id

        ticket: OrderTicket = OrderTicket(
            client_order_id=client_order_id,
            strategy_name=strategy_name,
            order=order,
            status=LiveOrderStatus.PENDING_SUBMIT,
            created_at=self._now(),
            dry_run=self.dry_run,
        )
        self.tickets[client_order_id] = ticket
        self._persist(ticket)
        self._append_event(ticket, None, LiveOrderStatus.PENDING_SUBMIT, "submit")

        if self.dry_run:
            logger.info(f"[dry-run] 不送出：{self.broker.describe_order(order)}")
            return ticket

        try:
            self.broker.place_order(ticket)
        except Exception as exc:
            # **不在這裡重送**：送出途中的例外分不出「沒送到」與「送到了但回應丟了」，
            # 重送有機會下成兩張。標為 FAILED 交給恢復流程去券商核對
            self.transition(ticket, LiveOrderStatus.FAILED, reason=str(exc))
            logger.opt(exception=True).error(f"送單失敗：{client_order_id}")
            raise

        self._sync_after_broker(ticket)
        return ticket

    def _sync_after_broker(self, ticket: OrderTicket) -> None:
        """券商回應後把狀態與編號寫回 DB；狀態轉移仍走狀態機"""

        broker_status: LiveOrderStatus = ticket.status
        ticket.status = LiveOrderStatus.PENDING_SUBMIT
        self.transition(ticket, broker_status, reason=ticket.reject_reason)

    # === 狀態機 ===
    def transition(
        self,
        ticket: OrderTicket,
        new_status: LiveOrderStatus,
        reason: Optional[str] = None,
        op_type: str = "",
    ) -> None:
        """
        - Description:
            狀態轉移；非法轉移拋出並寫 `live_risk_event`

            **靜默忽略是不行的**：那會讓「已成交」被遲到的舊回報改回「已送出」，
            而引擎會繼續等一張早就成交的單，段落結束時還會去撤它。
        - Parameters:
            - ticket: OrderTicket
                目標委託
            - new_status: LiveOrderStatus
                新狀態
            - reason: Optional[str]
                原因（拒單訊息等）
            - op_type: str
                觸發來源，寫進轉移歷史
        - Raise:
            - OrderStateError
                非法轉移
        """

        old_status: LiveOrderStatus = ticket.status
        if (
            new_status is old_status
            and new_status is not LiveOrderStatus.PARTIALLY_FILLED
        ):
            return

        if new_status not in self.TRANSITIONS.get(old_status, set()):
            message: str = (
                f"非法的委託狀態轉移：{ticket.client_order_id} "
                f"{old_status.value} → {new_status.value}"
            )
            self._write_risk_event("ORDER_STATE_INVALID", message, ticket)
            raise OrderStateError(message)

        ticket.status = new_status
        ticket.reject_reason = reason or ticket.reject_reason
        ticket.updated_at = self._now()
        self._persist(ticket)
        self._append_event(ticket, old_status, new_status, op_type)

    # === 回報 ===
    def drain_executions(self) -> List[ExecutionReport]:
        """
        - Description:
            消化券商回報佇列：更新委託、寫成交明細

            **不假設順序**：成交回報可能比委託確認先到，狀態機因此允許
            `PENDING_SUBMIT → PARTIALLY_FILLED／FILLED`。
        - Return:
            - List[ExecutionReport]
                本次新增的成交（已去重），交給帳戶同步
        """

        # 單筆事件造成的狀態矛盾在迴圈內就地處理：直接呼叫 `transition()` 時
        # 仍然拋出（那是程式邏輯錯誤），但佇列消化不可因為一筆而停擺

        new_fills: List[ExecutionReport] = []
        for event in self.broker.drain_execution_queue():
            fill: Optional[ExecutionReport] = self.apply_event(event)
            if fill is not None:
                new_fills.append(fill)
        return new_fills

    def apply_event(self, event: Any) -> Optional[ExecutionReport]:
        """
        - Description:
            處理**單一筆**回報

            與 `drain_executions()` 拆開，是因為盤中事件迴圈的行情與回報共用
            一個 queue：那條路徑一次只拿得到一筆，沒有「整批」可以消化。
            日頻那條仍走 `drain_executions()`，兩者共用同一份判定。
        - Parameters:
            - event: Any
                `ExecutionReport` 或 `OrderStatusEvent`
        - Return:
            - Optional[ExecutionReport]
                新成交；重複、非成交或狀態矛盾時為 None
        """

        if not self._dedup.is_new(event):
            return None

        try:
            if isinstance(event, ExecutionReport):
                return event if self._apply_fill(event) else None
            if isinstance(event, OrderStatusEvent):
                self._apply_order_event(event)
        except OrderStateError as exc:
            # **單筆跳過，不中斷整個迴圈**：一筆狀態矛盾的回報（例如已成交的單
            # 又收到拒單事件）若讓例外往上拋，佇列裡後面那些正常的成交就全丟了。
            # 矛盾本身已經寫進 `live_risk_event`，該知道的人會知道
            logger.warning(f"回報與本地狀態矛盾，已跳過本筆：{exc}")
        return None

    def _apply_fill(self, report: ExecutionReport) -> bool:
        """把一筆成交套進對應的委託；找不到委託時記事件但仍保留成交"""

        ticket: Optional[OrderTicket] = self._find_by_seqno(report.broker_seqno)
        if ticket is None:
            self._write_risk_event(
                "FILL_UNMATCHED",
                f"成交回報找不到對應委託（seqno={report.broker_seqno}）；"
                "該筆成交仍會寫入，但無法歸屬到策略",
                None,
            )

        filled: int = (ticket.filled_volume if ticket else 0) + report.volume
        is_new: bool = self.dao.insert_fill(
            {
                "broker_seqno": report.broker_seqno,
                "broker_trade_id": report.broker_trade_id,
                "client_order_id": ticket.client_order_id if ticket else None,
                "strategy_name": ticket.strategy_name if ticket else None,
                "symbol": report.symbol,
                "action": report.action.value,
                "price": report.price,
                "volume": report.volume,
                "filled_at": report.ts,
                "raw_json": str(report.raw),
            }
        )

        if ticket is not None and is_new:
            self._update_average_price(ticket, report)
            ticket.filled_volume = filled
            target: LiveOrderStatus = (
                LiveOrderStatus.FILLED
                if ticket.order is not None and filled >= ticket.order.volume
                else LiveOrderStatus.PARTIALLY_FILLED
            )
            self.transition(ticket, target, op_type="fill")
        return is_new

    @staticmethod
    def _update_average_price(ticket: OrderTicket, report: ExecutionReport) -> None:
        """以成交量加權更新均價；**先算再累加已成交量**，順序反了會少算最後一筆"""

        total: int = ticket.filled_volume + report.volume
        if total <= 0:
            return
        ticket.avg_fill_price = (
            ticket.avg_fill_price * ticket.filled_volume + report.price * report.volume
        ) / total

    def _apply_order_event(self, event: OrderStatusEvent) -> None:
        """把委託狀態事件套進對應的委託"""

        ticket: Optional[OrderTicket] = self._find_by_seqno(
            event.broker_seqno
        ) or self.tickets.get(self._expand(event.custom_field))
        if ticket is None:
            return

        if event.is_failure:
            self.transition(
                ticket,
                LiveOrderStatus.REJECTED,
                reason=event.op_msg,
                op_type=event.op_type,
            )
        elif event.op_type.lower().startswith("cancel"):
            self.transition(ticket, LiveOrderStatus.CANCELLED, op_type=event.op_type)

    # === 撤單 ===
    def cancel_open_orders(
        self, timing: Optional[ExecutionTiming] = None
    ) -> List[OrderTicket]:
        """
        - Description:
            撤掉尚未終結的委託

            **撤單失敗只記錄不拋出**：段落結束時要把能撤的都撤掉，
            其中一張撤不掉（例如剛好成交了）不該讓後面幾張留在場上。
        - Parameters:
            - timing: Optional[ExecutionTiming]
                只撤這個段落送出的單；None 代表全部
        - Return:
            - List[OrderTicket]
                實際送出撤單請求的委託
        """

        cancelled: List[OrderTicket] = []
        for ticket in list(self.tickets.values()):
            if ticket.is_terminal:
                continue
            if (
                timing is not None
                and getattr(ticket.order, "timing", None) is not timing
            ):
                continue
            try:
                self.broker.cancel_order(ticket)
                self.transition(ticket, LiveOrderStatus.CANCELLED, op_type="cancel")
                cancelled.append(ticket)
            except Exception as exc:
                logger.opt(exception=True).warning(
                    f"撤單失敗（繼續處理其餘委託）：{ticket.client_order_id}：{exc}"
                )
        return cancelled

    def refresh_from_broker(self) -> List[OrderTicket]:
        """
        - Description:
            向券商刷新一次當日委託狀態，並套回本行程的委託

            **盤後才呼叫**：它算在下單類額度裡，拿來輪詢會把送單額度吃光。
        - Return:
            - List[OrderTicket]
                券商端當日的委託
        """

        broker_tickets: List[OrderTicket] = self.broker.refresh_order_status()
        by_seqno: Dict[str, OrderTicket] = {
            ticket.broker_seqno: ticket
            for ticket in broker_tickets
            if ticket.broker_seqno
        }

        for ticket in self.tickets.values():
            latest: Optional[OrderTicket] = by_seqno.get(ticket.broker_seqno or "")
            if latest is None or latest.status is ticket.status:
                continue
            try:
                ticket.filled_volume = latest.filled_volume
                self.transition(ticket, latest.status, op_type="refresh")
            except OrderStateError as exc:
                logger.warning(f"刷新狀態與本地矛盾，已跳過本筆：{exc}")

        return broker_tickets

    def expire_unfinished(self, run_date: datetime.date) -> List[OrderTicket]:
        """
        - Description:
            把當日仍未終結的委託標成已撤

            ROD 單在券商端日終自動失效，**本地要跟著標**：不標的話，
            次日的恢復流程會把它們當成「還在場上」去接管，然後去撤一張
            早就不存在的單，而那個錯誤訊息看起來像真的出了事。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[OrderTicket]
                被標記的委託
        """

        expired: List[OrderTicket] = []
        for row in self.dao.get_unfinished_orders(run_date):
            client_order_id: str = str(row["client_order_id"])
            ticket: Optional[OrderTicket] = self.tickets.get(client_order_id)
            if ticket is None:
                ticket = self._rebuild_ticket(row)
                self.tickets[client_order_id] = ticket

            try:
                self.transition(
                    ticket,
                    LiveOrderStatus.CANCELLED,
                    reason="日終未成交，券商端自動失效",
                    op_type="expire",
                )
                expired.append(ticket)
            except OrderStateError as exc:
                logger.warning(f"標記日終失效時與本地狀態矛盾，已跳過：{exc}")

        if expired:
            logger.info(f"日終標記 {len(expired)} 張未成交委託為已撤")
        return expired

    # === 重啟接管 ===
    def recover(self, run_date: datetime.date) -> List[OrderTicket]:
        """
        - Description:
            重啟後接管當日未終結的委託

            比對順序是「精確 → 精確 → 模糊」，而且**模糊比對到多筆時不猜**：
            挑一個看起來最像的，會把兩張單的成交記到同一張上，
            而帳上的總量還是對的，對帳看不出來。

            完整刷新一次後仍查不到的才標為 `FAILED`，**不自動重送**。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[OrderTicket]
                接管後的委託清單
        """

        stored: List[Dict[str, Any]] = self.dao.get_unfinished_orders(run_date)
        if not stored:
            return []

        broker_tickets: List[OrderTicket] = self.broker.refresh_order_status()
        by_seqno: Dict[str, OrderTicket] = {
            ticket.broker_seqno: ticket
            for ticket in broker_tickets
            if ticket.broker_seqno
        }
        by_custom_field: Dict[str, OrderTicket] = {
            ticket.client_order_id: ticket
            for ticket in broker_tickets
            if ticket.client_order_id
        }

        recovered: List[OrderTicket] = []
        for row in stored:
            ticket: OrderTicket = self._rebuild_ticket(row)
            self.tickets[ticket.client_order_id] = ticket

            matched: Optional[OrderTicket] = None
            if row.get("broker_seqno"):
                matched = by_seqno.get(str(row["broker_seqno"]))
            if matched is None and row.get("custom_field"):
                matched = by_custom_field.get(str(row["custom_field"]))
            if matched is None:
                matched = self._fuzzy_match(row, broker_tickets)

            if matched is None:
                self.transition(
                    ticket,
                    LiveOrderStatus.FAILED,
                    reason="重啟接管時在券商端查無此單",
                    op_type="recover",
                )
            else:
                ticket.broker_seqno = matched.broker_seqno
                ticket.broker_order_id = matched.broker_order_id
                ticket.filled_volume = matched.filled_volume
                if matched.status is not ticket.status:
                    self.transition(ticket, matched.status, op_type="recover")
            recovered.append(ticket)

        return recovered

    def _fuzzy_match(
        self, row: Dict[str, Any], candidates: Sequence[OrderTicket]
    ) -> Optional[OrderTicket]:
        """
        以「標的 ＋ 買賣別 ＋ 價格 ＋ 數量」模糊比對；**多筆時回 None 並要求人工確認**

        兩支策略可能在同一秒對不同標的送出相似的單，而同一支策略也可能分批建倉——
        挑一個看起來最像的，會把兩張單的成交記到同一張上。
        """

        matches: List[OrderTicket] = [
            candidate
            for candidate in candidates
            if candidate.order is not None
            and candidate.order.symbol == row.get("symbol")
            and candidate.order.action.value == row.get("action")
            and abs(candidate.order.price - float(row.get("price") or 0)) < 1e-6
            and candidate.order.volume == int(row.get("volume") or 0)
        ]

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
            message: str = (
                f"重啟接管時模糊比對到 {len(matches)} 筆候選委託"
                f"（{row.get('client_order_id')}），不做猜測；請人工確認"
            )
            self._write_risk_event("RECOVER_AMBIGUOUS", message, None)
            self._degrade(message)
        return None

    def _rebuild_ticket(self, row: Dict[str, Any]) -> OrderTicket:
        """由 DB 紀錄重建 ticket（不含原始訂單物件；恢復只需要識別與狀態）"""

        return OrderTicket(
            client_order_id=str(row["client_order_id"]),
            strategy_name=str(row["strategy_name"]),
            status=LiveOrderStatus(str(row["status"])),
            broker_seqno=row.get("broker_seqno"),
            broker_order_id=row.get("broker_order_id"),
            filled_volume=int(row.get("filled_volume") or 0),
            avg_fill_price=float(row.get("avg_fill_price") or 0.0),
            dry_run=bool(row.get("dry_run")),
        )

    # === 內部 ===
    def _find_by_seqno(self, broker_seqno: str) -> Optional[OrderTicket]:
        """以券商委託序號找本行程的委託"""

        if not broker_seqno:
            return None
        for ticket in self.tickets.values():
            if ticket.broker_seqno == broker_seqno:
                return ticket
        return None

    def _expand(self, custom_field: str) -> str:
        """壓縮碼 → client order id（查不到時回空字串）"""

        if not custom_field:
            return ""
        row: Optional[Dict[str, Any]] = self.dao.find_order_by_custom_field(
            custom_field
        )
        return str(row["client_order_id"]) if row else ""

    def _persist(self, ticket: OrderTicket) -> None:
        """把委託寫進 `live_order`"""

        order: Any = ticket.order
        self.dao.upsert_order(
            {
                "client_order_id": ticket.client_order_id,
                "run_id": self.run_id,
                "strategy_name": ticket.strategy_name,
                "symbol": getattr(order, "symbol", "") if order else "",
                "action": order.action.value if order else "",
                "position_type": order.position_type.value if order else "",
                "price": getattr(order, "price", 0.0) if order else 0.0,
                "volume": getattr(order, "volume", 0) if order else 0,
                "price_type": self._enum_value(getattr(order, "price_type", None)),
                "order_type": self._enum_value(getattr(order, "order_type", None)),
                "order_lot": self._enum_value(getattr(order, "order_lot", None)),
                "timing": self._enum_value(getattr(order, "timing", None)),
                "status": ticket.status.value,
                "broker_order_id": ticket.broker_order_id,
                "broker_seqno": ticket.broker_seqno,
                "custom_field": self.compress(ticket.client_order_id),
                "filled_volume": ticket.filled_volume,
                "avg_fill_price": ticket.avg_fill_price,
                "reject_reason": ticket.reject_reason,
                "dry_run": int(ticket.dry_run),
                "created_at": ticket.created_at or self._now(),
                "updated_at": ticket.updated_at,
            }
        )
        self.dao.conn.commit()

    @staticmethod
    def _enum_value(member: Any) -> Optional[str]:
        """Enum → 值；None 原樣回傳"""

        return None if member is None else str(getattr(member, "value", member))

    def _append_event(
        self,
        ticket: OrderTicket,
        from_status: Optional[LiveOrderStatus],
        to_status: LiveOrderStatus,
        op_type: str,
    ) -> None:
        """追加一筆狀態轉移歷史（append-only）"""

        counter: itertools.count = self._event_sequence.setdefault(
            ticket.client_order_id, itertools.count(1)
        )
        self.dao.append_order_event(
            {
                "client_order_id": ticket.client_order_id,
                "seq": next(counter),
                "from_status": from_status.value if from_status else None,
                "to_status": to_status.value,
                "op_type": op_type,
                "message": ticket.reject_reason,
                "occurred_at": self._now(),
            }
        )
        self.dao.conn.commit()

    def _write_risk_event(
        self, category: str, message: str, ticket: Optional[OrderTicket]
    ) -> None:
        """寫一筆風控事件"""

        logger.error(message)
        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": ticket.strategy_name if ticket else None,
                "severity": "CRITICAL",
                "category": category,
                "client_order_id": ticket.client_order_id if ticket else None,
                "message": message,
                "occurred_at": self._now(),
            }
        )

    def _degrade(self, reason: str) -> None:
        """送降級事件給風控；**OMS 不自己改交易模式**"""

        if self._on_degrade is not None:
            self._on_degrade(reason)


__all__ = [
    "MAX_ORDER_INDEX",
    "MAX_RUN_INDEX",
    "OrderManager",
    "OrderStateError",
    "to_base36",
]
