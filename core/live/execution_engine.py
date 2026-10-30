import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.after_close import pending_cover_action_id
from core.live.capital_allocator import CapitalAllocator
from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.execution.base import BaseExecutionModel, ExecutionUnavailableError
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.risk import event_categories
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.margin_gate import MarginAccountQuery, MarginGate
from core.live.risk.risk_manager import PreTradeRiskManager, RiskDecision
from core.market.segment import SegmentWindow
from core.models import BaseOrder, ExecutionReport, PriceLimits
from core.portfolio import order_rules
from core.utils import PositionType

"""
ExecutionEngine：送單與收成交回報

每張委託送出前走同一條管線：保證金檢查 → 在途出場量截短 → 資金保留 →
執行層換算 → 事前風控 → 送出 → 消化回報。補平、換月兩腿、當沖回補、策略訊號
與逐筆路徑都走這一條，另寫一份必然漂移，而漂移的那一刻兩邊都看起來正確。

**資金保留的帳在這裡**（`_reserved_by_order`）：送出時以決策價保留，終結時釋放、
成交的部分轉入占用；段落收尾把還掛著的全部放掉。保留與釋放不對稱時額度會單向
消耗到策略再也送不出單，所以每一條失敗路徑都要放掉保留。
"""


class ExecutionEngine:
    """送單管線與成交回報：資金保留、執行層換算、事前風控、送出、記帳"""

    # 收線後撤單，再收撤單回報的時間（秒）。撤單回報通常一秒內就到；
    # 收不到也不影響正確性——日終由盤後刷新與 `expire_unfinished()` 補標
    CANCEL_REPORT_SECONDS: float = 5.0

    def __init__(
        self,
        order_manager: Any,
        risk_manager: PreTradeRiskManager,
        allocator: CapitalAllocator,
        account_sync: AccountSynchronizer,
        dao: LiveTradeDAO,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        apply_broker_degrades: Callable[[], None],
        margin_query: Optional[MarginAccountQuery] = None,
        simulation: bool = False,
    ) -> None:
        """
        - Description:
            建立送單元件；相依一律由外部注入
        - Parameters:
            - order_manager: Any
                委託管理（單例）
            - risk_manager: PreTradeRiskManager
                事前風控（單例）
            - allocator: CapitalAllocator
                資金額度分配（保留、釋放、占用）
            - account_sync: AccountSynchronizer
                帳戶同步（成交入帳）
            - dao: LiveTradeDAO
                實盤紀錄庫（出場單未送出時寫次日補平待辦）
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘
            - apply_broker_degrades: Callable[[], None]
                送單前先套用券商執行緒回報的降級事件，否則時鐘已不可信的這一批照樣送出
            - margin_query: Optional[MarginAccountQuery]
                券商的保證金帳務查詢；由組裝層依市場注入（None 代表沒有需要保證金的商品）
            - simulation: bool
                是否連模擬環境；決定保證金查不到資料時放行還是擋單
        """

        self.order_manager: Any = order_manager
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.allocator: CapitalAllocator = allocator
        self.account_sync: AccountSynchronizer = account_sync
        self.dao: LiveTradeDAO = dao
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.apply_broker_degrades: Callable[[], None] = apply_broker_degrades
        self.margin_query: Optional[MarginAccountQuery] = margin_query
        self.simulation: bool = simulation

        # 本段落已送出的委託金額，逐策略累計；段落結束時用來釋放未成交的保留
        self._reserved_by_order: Dict[str, Tuple[str, float]] = {}

    def dispatch(
        self,
        candidates: List[Tuple[StrategyContext, BaseOrder]],
        window: Optional[SegmentWindow],
        cover_unsent_exits: bool = False,
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        - Description:
            逐單保留資金 → 執行層換算 → 風控 → 送出，期間持續消化回報

            **資金保留以決策價計算，在執行層改寫委託價之前**：集合競價的 `MARKET`
            買單掛漲停，以掛單價保留的話，額度切滿的策略最後一檔一定保留不到。
            風控收到同一份漲跌停，並以決策價做偏離與漲跌停檢查（見 `PreTradeRiskManager.check()`）。
        - Parameters:
            - candidates: List[Tuple[StrategyContext, BaseOrder]]
                通過跨策略檢查的委託
            - window: Optional[SegmentWindow]
                段落時窗
            - cover_unsent_exits: bool
                出場單沒送出時是否排次日補平（見 `_record_unsent_exit()`）。只有段落收訊號
                那一條路徑開啟；補平、換月兩腿、當沖回補與逐筆路徑各有自己的失敗處理
                （順延待辦、換月事件、回補重試、下一筆 tick 再試），再排一筆會重複
        - Return:
            - List[Tuple[StrategyContext, BaseOrder]]
                沒有送出的委託（額度不足、風控擋下、送單失敗、已過時限）。
                一般段落不需要它；日終回補靠它判斷要不要重試
        """

        # 券商執行緒回報的降級要在送單前套用，否則時鐘已不可信的這一批照樣送出
        self.apply_broker_degrades()

        unsent: List[Tuple[StrategyContext, BaseOrder]] = []
        # 每批一份保證金預算：同一批的開倉單逐張累計，不各自拿同一份可用保證金比
        margin_gate: MarginGate = MarginGate(self.margin_query, self.simulation)
        for index, (context, order) in enumerate(candidates):
            # 時窗之外一律不送：過了時限不送，**還沒到也不送**——等待迴圈逾時後
            # 呼叫端若仍走到這裡（時鐘卡在時窗之前），這道檢查是最後一道
            outside: Optional[str] = self.clock.outside_window(window)
            if outside is not None:
                logger.warning(f"{outside}，其餘委託不再送出")
                unsent.extend(candidates[index:])
                for late_context, late_order in candidates[index:]:
                    if order_rules.is_closing(late_order):
                        if cover_unsent_exits:
                            self._record_unsent_exit(late_context, late_order, outside)
                    else:
                        self._write_deadline_event(late_context, late_order, outside)
                break

            blocked: Optional[str] = self._check_margin(margin_gate, context, order)
            if blocked is not None:
                self._write_margin_event(context, order, blocked)
                unsent.append((context, order))
                continue

            closing: bool = order_rules.is_closing(order)
            # 出場單先扣掉同一個部位已在場上、尚未撮合的出場量：開盤段先送的補平單
            # 09:00 才撮合，策略接著讀仍持有的帳戶再送一張平倉，兩張同時撮合就是超賣
            if closing and not self._truncate_exit_to_available(context, order):
                unsent.append((context, order))
                continue

            # **平倉與停損不保留資金**：出場是釋放資金，不是占用。可用額度是
            # 「額度 − 持倉占用」，滿倉的策略趨近 0，若出場也要先保留，之後每一張
            # 平倉／停損都會在這裡被擋下、永遠出不了場。開倉才保留；
            # 與 `_check_margin()` 對平倉單免檢查是同一條原則。金額仍算出來給風控
            amount: float = self._order_amount(context, order, closing)
            reserved: float = 0.0 if closing else amount
            if not closing and not self.allocator.reserve(
                context.name, amount, symbol=order.symbol
            ):
                unsent.append((context, order))
                continue

            limits: Optional[PriceLimits] = self._apply_execution(context, order)
            if limits is None:
                self.allocator.release(context.name, reserved)
                if cover_unsent_exits:
                    self._record_unsent_exit(context, order, "執行層換不出券商委託")
                unsent.append((context, order))
                continue

            decision: RiskDecision = self.risk_manager.check(
                order,
                context.name,
                context.capital_limit,
                amount,
                context.risk_limits,
                reference_price=self._reference_price(context, order),
                limit_up=limits.limit_up,
                limit_down=limits.limit_down,
            )
            if not decision.passed:
                self.allocator.release(context.name, reserved)
                if (
                    cover_unsent_exits
                    and decision.category not in self.DELIBERATE_EXIT_BLOCKS
                ):
                    self._record_unsent_exit(
                        context,
                        order,
                        f"風控擋下 {decision.category}：{decision.reason}",
                    )
                unsent.append((context, order))
                continue

            try:
                ticket = self.order_manager.submit(order, context.name)
            except Exception as exc:
                # 送單失敗的保留一定要放掉，否則額度會單向消耗到策略再也送不出單
                self.allocator.release(context.name, reserved)
                logger.opt(exception=True).error(f"{context.name} 送單失敗：{exc}")
                if cover_unsent_exits:
                    self._record_unsent_exit(context, order, f"送單失敗：{exc}")
                unsent.append((context, order))
                continue

            self._reserved_by_order[ticket.client_order_id] = (context.name, reserved)
            self.apply_new_fills()

        return unsent

    def _order_amount(
        self, context: StrategyContext, order: BaseOrder, closing: bool
    ) -> float:
        """
        - Description:
            本張委託的資金占用；**出場單算不出來時記 0，不中斷送單**

            期貨的資金占用要查當天的保證金表。開倉單在這之前已經過保證金檢查
            （同一張表，查不到就擋），走不到這裡；出場單不做保證金檢查，表若缺當天的資料，
            讓例外往上拋會中斷整批送單，部位反而出不了場。出場單的金額只用在當日流量統計
            （金額類上限對出場本來就不檢查），記 0 的代價是流量少算這一筆。
        - Parameters:
            - context: StrategyContext
                歸屬策略
            - order: BaseOrder
                委託
            - closing: bool
                是否為出場單
        - Return:
            - float
                資金占用
        """

        if not closing:
            return context.notional(order)
        try:
            return context.notional(order)
        except ValueError as exc:
            logger.warning(
                f"{context.name} 出場單 {order.symbol} 算不出資金占用，以 0 計：{exc}"
            )
            return 0.0

    # 刻意連出場都不送的風控類別：kill switch 與 HALTED 是人為或系統決定「現在什麼都不動」，
    # 替這兩種排次日補平等於繞過那個決定
    DELIBERATE_EXIT_BLOCKS: Set[str] = {"KILL_SWITCH", "MODE_HALTED"}

    def _write_deadline_event(
        self, context: StrategyContext, order: BaseOrder, reason: str
    ) -> None:
        """時窗之外被丟掉的開倉單：寫事件，讓 parity 歸到執行面而不是快照口徑"""

        message: str = f"{order.symbol} 開倉單未送出：{reason}"
        logger.warning(f"[Window] {context.name}：{message}")
        self.events.write(
            category=event_categories.SUBMIT_DEADLINE_PASSED,
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )

    def _in_flight_exit_volume(
        self, strategy_name: str, symbol: str, position_type: PositionType
    ) -> int:
        """
        本行程已送出、尚未終結的同部位出場單的殘量合計

        以委託管理器手上的 ticket 算，不另外記帳：補平、策略平倉、停損、當沖回補
        走的都是同一條 `dispatch()`，任一條路徑送出的出場單都算在途。
        """

        total: int = 0
        for ticket in self.order_manager.tickets.values():
            order: Optional[BaseOrder] = ticket.order
            if (
                ticket.is_terminal
                or order is None
                or ticket.strategy_name != strategy_name
                or order.symbol != symbol
                or order.position_type is not position_type
                or not order_rules.is_closing(order)
            ):
                continue
            total += ticket.remaining_volume
        return total

    def _truncate_exit_to_available(
        self, context: StrategyContext, order: BaseOrder
    ) -> bool:
        """
        - Description:
            把出場單扣掉同一個部位的在途出場量；扣到 0 就不送並寫事件

            **只扣在途量，不拿歸屬帳的持有量當上限**：策略的出場單本來就是照它帳上的部位
            開的，持有量由帳戶重建保證；而在途的那張（多半是本段落稍早的補平單）撮合後
            會平掉同一口部位，兩張都送就是超賣。截短與截到 0 都寫 `EXIT_TRUNCATED`，
            事後要查得到這張單為什麼變小或沒送。截到 0 **不排次日補平**：
            在途那張沒成交的話，盤後殘量處理會替它排。
        - Parameters:
            - context: StrategyContext
                策略脈絡
            - order: BaseOrder
                待送出的出場單；數量會被就地改小
        - Return:
            - bool
                是否仍有量可送
        """

        in_flight: int = self._in_flight_exit_volume(
            context.name, order.symbol, order.position_type
        )
        if in_flight <= 0:
            return True
        available: int = max(order.volume - in_flight, 0)

        message: str = (
            f"出場單 {order.symbol} {order.position_type.value} 要平 {order.volume}，"
            f"同部位已有在途出場 {in_flight}，"
            + ("已無可平部位，不送" if available <= 0 else f"改送 {available}")
        )
        logger.warning(f"[Exit] {context.name}：{message}")
        self.events.write(
            category=event_categories.EXIT_TRUNCATED,
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        order.volume = available
        return available > 0

    def _record_unsent_exit(
        self, context: StrategyContext, order: BaseOrder, reason: str
    ) -> None:
        """
        - Description:
            出場單從未送出：寫次日補平待辦與 CRITICAL 事件，並推播

            「出場必補」原本只由盤後掃 `live_order` 的未成交殘量承接；被風控或執行層
            擋在送出**之前**的出場單沒有 `live_order` 列，掃不到，部位會無聲留到隔夜。
            這裡補上同一條路：待辦交給次日開盤段的 `apply_pending_actions()`，
            數量到時再截到實際持有量。開倉單不補（開倉未送出一律放棄）。

            **送單途中才失敗的出場單已經有 `live_order` 列**（OMS 先寫 DB 再送單），
            盤後掃殘量會再替它排一次；兩筆待辦在次日開盤段會各送一張補平單，把部位做反。
            所以有 `client_order_id` 的用與盤後相同的編號，讓紀錄庫擋下第二筆；
            沒有列的才以時戳編號。
        - Parameters:
            - context: StrategyContext
                策略脈絡
            - order: BaseOrder
                未送出的委託
            - reason: str
                沒送出的原因
        """

        if not order_rules.is_closing(order):
            return

        now: datetime.datetime = self.clock.now()
        today: datetime.date = now.date()
        message: str = (
            f"{order.symbol} 出場單未送出（{reason}），已成為預期外的隔夜部位；"
            "次日開盤段第一件事補平"
        )

        client_order_id: Optional[str] = getattr(order, "client_order_id", None)
        action_id: str = (
            pending_cover_action_id(today, client_order_id)
            if client_order_id
            else f"{today.isoformat()}-unsent-{order.symbol}-{now.strftime('%H%M%S%f')}"
        )
        is_new: bool = self.dao.insert_pending_action(
            {
                "action_id": action_id,
                "strategy_name": context.name,
                "symbol": order.symbol,
                "action": order.action.value,
                "position_type": order.position_type.value,
                "volume": int(order.volume),
                "due_date": today + datetime.timedelta(days=1),
                "status": self.dao.ACTION_PENDING,
                "reason": f"出場單未送出：{reason}",
                "source_client_order_id": order.client_order_id,
                "created_at": now,
            }
        )
        if not is_new:
            logger.info(f"{order.symbol} 的補平待辦 {action_id} 已存在，不重複寫入")
            return

        logger.error(f"[Unsent Exit] {context.name}：{message}")
        self.events.write(
            category="UNSENT_EXIT",
            severity=NotifyLevel.CRITICAL,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        notify_safely(
            self.notifier,
            NotifyLevel.CRITICAL,
            "出場單未送出",
            f"{context.name}：{message}",
        )

    def _apply_execution(
        self, context: StrategyContext, order: BaseOrder
    ) -> Optional[PriceLimits]:
        """
        - Description:
            交給執行層換成券商委託；換不出來時寫事件並回 None（本張不送）

            **沒有執行模型、沒標註執行方式都擋下**，不退回任何預設：那代表某條送單
            路徑漏接了執行層，猜一個價格類型等於把漏洞蓋掉——上一次就是這樣，
            整批委託在券商轉換層才失敗。
        - Parameters:
            - context: StrategyContext
                策略脈絡
            - order: BaseOrder
                待送出的委託（就地改寫價格類型、委託價與委託效期）
        - Return:
            - Optional[PriceLimits]
                換算時用的漲跌停，要一併交給風控；None 表示本張不送
        """

        model: Optional[BaseExecutionModel] = context.execution_model
        if model is None:
            self._write_execution_event(
                context, order, "沒有注入執行層，無法決定怎麼送", NotifyLevel.CRITICAL
            )
            return None

        limits: PriceLimits = model.get_price_limits(order.symbol)
        try:
            model.apply(order, limits)
        except ExecutionUnavailableError as exc:
            self._write_execution_event(context, order, str(exc), NotifyLevel.WARN)
            return None
        except ValueError as exc:
            self._write_execution_event(context, order, str(exc), NotifyLevel.CRITICAL)
            return None
        return limits

    def _write_execution_event(
        self,
        context: StrategyContext,
        order: BaseOrder,
        reason: str,
        severity: NotifyLevel,
    ) -> None:
        """執行層換不出委託：寫事件並推播"""

        message: str = f"{order.symbol} 未送出：{reason}"
        logger.warning(f"[Execution] {context.name}：{message}")
        self.events.write(
            category="EXECUTION_UNAVAILABLE",
            severity=severity,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        notify_safely(self.notifier, severity, "委託無法換算", message)

    def _check_margin(
        self, gate: MarginGate, context: StrategyContext, order: BaseOrder
    ) -> Optional[str]:
        """
        需要保證金的開倉單的檢查；不需要檢查時回 None

        **平倉單不檢查**：平倉釋放保證金，擋下它只會讓部位留在場上。
        算不出需要的保證金（例如保證金表沒有這個商品）時**擋單**：
        不知道要押多少錢就送出，等於把檢查交給券商退單。
        """

        if context.calculate_opening_requirement is None:
            return None
        if order_rules.is_closing(order):
            return None

        try:
            required: float = context.calculate_opening_requirement(order)
        except Exception as exc:
            logger.opt(exception=True).warning(f"{order.symbol} 算不出保證金：{exc}")
            return f"算不出需要的保證金：{exc}"

        return gate.check(context.name, required, float(context.account.balance))

    def _write_margin_event(
        self, context: StrategyContext, order: BaseOrder, reason: str
    ) -> None:
        """保證金不足而沒有送出的開倉單：寫事件並推播"""

        message: str = f"{order.symbol} 開倉單未送出：{reason}"
        logger.warning(f"[Margin] {context.name}：{message}")
        self.events.write(
            category="MARGIN_INSUFFICIENT",
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        notify_safely(self.notifier, NotifyLevel.WARN, "保證金不足", message)

    def apply_new_fills(self) -> List[ExecutionReport]:
        """
        - Description:
            消化一次回報：更新帳戶與歸屬帳、釋放對應的資金保留
        - Return:
            - List[ExecutionReport]
                本次新增的成交
        """

        fills: List[ExecutionReport] = self.order_manager.drain_executions()
        for fill in fills:
            self.account_sync.apply_fill(fill)
        self._release_finished()
        return fills

    def apply_fills_until(
        self, window: Optional[SegmentWindow], attribute: str
    ) -> None:
        """
        - Description:
            持續消化回報直到指定時點

            **送單時限與收線時點是分開的**：尾盤段送出的委託，成交回報要等
            收盤集合競價撮合完才會進來。送完單就收線的話，當日成交明細會出現
            一段空窗，而對帳會在錯的時點判定不一致。
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗；None 時只消化一次就返回
            - attribute: str
                時窗上的時點欄位名
        """

        if window is None:
            self.apply_new_fills()
            return

        cap: float = self.clock.wait_cap_seconds(getattr(window, attribute))
        waited: float = 0.0
        while not self.clock.past(window, attribute):
            if self.apply_new_fills():
                continue

            if waited >= cap:
                logger.error(
                    f"等待回報已達安全上限 {cap:.0f} 秒仍未到 "
                    f"{attribute}，強制結束等待；請確認系統時鐘是否正常"
                )
                return

            self.clock.sleep(self.clock.POLL_INTERVAL_SECONDS)
            waited += self.clock.POLL_INTERVAL_SECONDS

    def _apply_fills_for(self, seconds: float) -> None:
        """
        消化回報一段固定時間；給收線之後才送出的撤單收回報用

        以累計等待秒數計時、不看時鐘，與 `apply_fills_until()` 的保險絲同一個理由：
        時鐘卡住時仍要能結束。
        """

        waited: float = 0.0
        while waited < seconds:
            if self.apply_new_fills():
                continue
            self.clock.sleep(self.clock.POLL_INTERVAL_SECONDS)
            waited += self.clock.POLL_INTERVAL_SECONDS

    def _release_finished(self) -> None:
        """終結的委託釋放保留；**走遍所有已知委託**而不只是本次成交的那幾張"""

        for client_order_id, (name, amount) in list(self._reserved_by_order.items()):
            ticket = self.order_manager.tickets.get(client_order_id)
            if ticket is not None and ticket.is_terminal:
                self.allocator.release(name, amount)
                # 成交的部分轉入占用：只釋放不占用的話，同一批內即時成交的單
                # 既不在保留也不在占用，後面的單會以整個額度通過
                if ticket.order is not None and ticket.order.volume > 0:
                    self.allocator.consume(
                        name,
                        amount
                        * min(ticket.filled_volume, ticket.order.volume)
                        / ticket.order.volume,
                    )
                self._reserved_by_order.pop(client_order_id, None)

    def release_all_remaining(self) -> None:
        """段落結束時把還掛著的保留全部放掉：它們已經不可能再成交"""

        for name, amount in self._reserved_by_order.values():
            self.allocator.release(name, amount)
        self._reserved_by_order.clear()

    def _reference_price(self, context: StrategyContext, order: BaseOrder) -> float:
        """
        - Description:
            風控偏離檢查用的**市場**基準價

            偏離檢查比的是「策略的決策價離市場多遠」，基準必須是市場價，不能是這張單
            自己的任何價：拿決策價當基準是自己比自己、偏離恆為 0，檢查形同不存在；
            拿委託價更不行——集合競價的 `MARKET` 買單委託價是漲停價，每一張要成交的單
            都會被擋下。

            來源依序：本段落餵給策略的報價（尾盤段是 13:20 快照的成交價、盤前段是參考價）
            → 券商合約的參考價（系統委託沒有報價時的退路）。**都取不到就放行並記 WARN**：
            資料源缺一檔報價不該讓那一檔的出場送不出去，但要留下「這張單沒檢查到」的紀錄。
        - Parameters:
            - context: StrategyContext
                策略脈絡（持有本段落的報價）
            - order: BaseOrder
                待檢查的委託
        - Return:
            - float
                市場基準價；取不到為 0（`check_price_deviation()` 對 0 放行）
        """

        price: Optional[float] = context.quote_prices.get(order.symbol)
        if price is None or price <= 0:
            price = context.data_feed.get_reference_price(order.symbol)
        if price is not None and price > 0:
            return float(price)

        message: str = (
            f"{order.symbol} 取不到市場基準價（本段落報價與合約參考價皆無），"
            "本張決策價偏離檢查放行"
        )
        logger.warning(f"[Risk] {context.name}：{message}")
        self.events.write(
            category="REFERENCE_PRICE_MISSING",
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        return 0.0

    def settle(self, window: Optional[SegmentWindow], aborted: bool = False) -> None:
        """
        - Description:
            段落收尾的結算：收回報到收線 → 撤未成交單 → 收撤單回報 → 放掉剩餘保留

            **正常結束時先等到收線才撤單**：集合競價段落送出的 ROD 委託要留在場上
            等撮合（尾盤段 13:30、開盤段 09:00），收線時點排在撮合之後。送完單就撤的話，
            委託在撮合前就離場，集合競價的單一張都不可能成交，而段落仍會「正常結束」。

            **中止時立刻撤單**：段落是因例外或終止訊號結束的，引擎已經不在可控狀態，
            留在場上的單沒有人會處理它的回報。撤完仍續收回報到收線。

            **撤單之後仍要繼續收回報**：撤單的回應本身也是回報，而剛好在撤單前
            成交的那張單也還沒回來。
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗
            - aborted: bool
                段落是否因例外或終止訊號結束
        """

        if aborted:
            self.order_manager.cancel_open_orders()
            self.apply_fills_until(window, "drain_end")
        else:
            self.apply_fills_until(window, "drain_end")
            self.order_manager.cancel_open_orders()
            self._apply_fills_for(self.CANCEL_REPORT_SECONDS)
        self.release_all_remaining()

    def forget_reservations(self, strategy_name: str) -> None:
        """
        把一支策略的保留紀錄全部忘掉（策略被降到 `HALTED` 時用）

        **只忘掉紀錄、不呼叫 `allocator.release()`**：呼叫端已以 `release_all()`
        一次放掉該策略的全部保留，這裡再逐筆釋放會放掉兩次。
        """

        for client_order_id, (name, _) in list(self._reserved_by_order.items()):
            if name == strategy_name:
                self._reserved_by_order.pop(client_order_id, None)
