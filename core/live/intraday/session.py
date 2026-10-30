import datetime
from functools import partial
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.broker.base import BaseBroker
from core.live.account_sync import AccountSynchronizer
from core.live.clock import SegmentClock
from core.live.connection import ConnectionSupervisor
from core.live.context import StrategyContext
from core.live.cross_checks import CrossStrategyChecks
from core.live.execution.base import mark_execution
from core.live.execution_engine import ExecutionEngine
from core.live.intraday.event_loop import IntradayEventLoop, LoopStats
from core.live.intraday.session_guard import SessionGuard, stops_after_cover
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.signals import SignalCollector, quote_prices_of
from core.market.segment import SegmentWindow
from core.models import BaseOrder, BaseQuote, ExecutionReport
from core.portfolio import order_rules
from core.utils import DayTradeUncoveredPolicy, ExecutionStyle, ExecutionTiming

"""
IntradaySession：盤中逐筆段落的迴圈、心跳、時窗守門與收線

行情與回報共用一個事件 queue，由迴圈逐筆分派：行情觸發逐筆策略的鉤子並走完整條
送單管線；回報更新委託與帳戶；心跳負責斷線偵測與日終強制動作（當沖回補）。
收線時把回報改回直接的 queue，再清掉迴圈裡剩下的回報。

**只餵宣告了 `is_tick_triggered` 的策略**：其餘策略的鉤子一次要一批報價，
逐筆餵給它們等於每次只看得到一檔，訊號會完全不同。
"""


class IntradaySession:
    """盤中逐筆段落：訂閱 → 驅動事件迴圈 → 收線；心跳上掛斷線偵測與日終回補"""

    # 盤中迴圈沒有事件時多久跳一次心跳（秒）。斷線偵測與日終回補都掛在心跳上
    INTRADAY_HEARTBEAT_SECONDS: float = 1.0

    # 日終回補失敗時最多再試幾次心跳。有上限是因為取不到報價、被風控擋下這類原因
    # 重試也不會好；每次失敗都推 CRITICAL，用完就交給人工
    MAX_COVER_ATTEMPTS: int = 3

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        broker: BaseBroker,
        order_manager: Any,
        account_sync: AccountSynchronizer,
        risk_manager: PreTradeRiskManager,
        mode_state: TradingModeState,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        session_guard: SessionGuard,
        signals: SignalCollector,
        cross_checks: CrossStrategyChecks,
        execution: ExecutionEngine,
        connection: ConnectionSupervisor,
    ) -> None:
        """
        - Description:
            建立盤中段落；相依一律由外部注入，與日頻段落共用同一組送單元件
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - broker: BaseBroker
                券商閘道（訂閱行情、導回報）
            - order_manager: Any
                委託管理（回報套進委託）
            - account_sync: AccountSynchronizer
                帳戶同步（成交入帳）
            - risk_manager: PreTradeRiskManager
                事前風控（行情中斷與回補政策的降級）
            - mode_state: TradingModeState
                交易模式狀態機
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘
            - session_guard: SessionGuard
                日終強制動作（當沖回補）的時點判定
            - signals: SignalCollector
                逐筆鉤子呼叫與策略降級
            - cross_checks: CrossStrategyChecks
                跨策略守門與批次曝險
            - execution: ExecutionEngine
                送單管線
            - connection: ConnectionSupervisor
                斷線偵測、重連與降級套用
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.broker: BaseBroker = broker
        self.order_manager: Any = order_manager
        self.account_sync: AccountSynchronizer = account_sync
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.mode_state: TradingModeState = mode_state
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.signals: SignalCollector = signals
        self.cross_checks: CrossStrategyChecks = cross_checks
        self.execution: ExecutionEngine = execution
        self.connection: ConnectionSupervisor = connection

        # 日終強制動作（當沖回補）
        self.session_guard: SessionGuard = session_guard
        # 當天已送出回補單的 `(交易日, 策略, 標的)`。重試時要跳過它們：
        # 已送出但還沒成交的回補單，部位仍然算「未回補」，再送一次就是超買
        self._cover_sent: Set[Tuple[datetime.date, str, str]] = set()
        # 各交易日回補失敗的次數
        self._cover_attempts: Dict[datetime.date, int] = {}
        # 每檔最後一筆報價；回補單取價用
        self._last_quotes: Dict[str, BaseQuote] = {}
        # 盤中段落各策略的標的池集合（`run()` 開始時建立）
        self._declared_symbols: Dict[str, FrozenSet[str]] = {}

    def run(self, window: Optional[SegmentWindow]) -> LoopStats:
        """
        - Description:
            盤中逐筆段落：訂閱 → 驅動事件迴圈 → 收線

            **行情與回報共用一個事件 queue**，由 `broker.route_events()` 把兩條
            回呼導過來。導過去之後 `drain_execution_queue()` 會一直是空的，
            那是刻意的——回報已經改由迴圈逐筆消化，兩邊都撈會重複且順序錯亂。

            **逐筆觸發只給宣告了 `is_tick_triggered` 的策略**：其餘策略的鉤子一次要
            一批報價，逐筆餵給它們等於每次只看得到一檔，訊號會完全不同。
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗；None 時不設收線時間
        - Return:
            - LoopStats
                本段的事件統計
        """

        loop: IntradayEventLoop = IntradayEventLoop(
            on_quote=self._on_intraday_quote,
            on_execution=self._on_intraday_execution,
            on_market_data_lost=self._on_market_data_lost,
            now_provider=self.clock.now,
            heartbeat_seconds=self.INTRADAY_HEARTBEAT_SECONDS,
            on_heartbeat=self._on_heartbeat,
        )

        symbols: List[str] = sorted(
            {
                symbol
                for context in self.contexts
                for symbol in self.signals.quote_symbols(context)
            }
        )
        # 逐筆派送每一筆都要判斷「這檔是不是這支策略的」；標的池在段落內不變，先建成集合
        self._declared_symbols = {
            context.name: frozenset(context.symbols) for context in self.contexts
        }
        self.broker.route_events(loop.submit_quote, loop.submit_execution)
        if symbols:
            self.broker.subscribe_quotes(symbols)

        deadline: datetime.datetime = self._resolve_intraday_deadline(window)
        logger.info(f"盤中逐筆開始，訂閱 {len(symbols)} 檔，收線 {deadline}")

        stats: LoopStats = loop.run_until(deadline)
        self._close_intraday_loop(loop, stats)
        logger.info(
            f"盤中逐筆結束：行情 {stats.quotes} 筆、回報 {stats.executions} 筆、"
            f"心跳 {stats.heartbeats} 次、處理失敗 {stats.handler_errors} 筆"
        )
        return stats

    def _close_intraday_loop(self, loop: IntradayEventLoop, stats: LoopStats) -> None:
        """
        - Description:
            盤中收線：回報改回直接的 queue → 清掉迴圈裡剩下的回報

            **收線前一刻送出的單可能在收線後才成交**（台股盤中段落送單迄 13:18、收回報到 13:19）。
            迴圈導向期間 `drain_execution_queue()` 恆為空，不改回的話，這段時間的成交與撤單回報
            會留在迴圈那個已經沒人讀的 queue 裡，本行程的委託與歸屬帳都看不到。改回之後，
            `finish()` 照日頻段落的同一套流程在直接的 queue 上收回報到 `drain_end`、撤單、收撤單回報。

            **先改回 queue、再清迴圈裡的回報**：改道前進來的回報比之後進來的早，先處理完，順序才不會亂。

            **不退訂行情**：改回之後行情進到一個沒人讀的 queue，只到段落收尾登出為止；
            為這一兩分鐘另外退訂，失敗時還得決定要不要中止段落，不值得。
        - Parameters:
            - loop: IntradayEventLoop
                本段的事件迴圈
            - stats: LoopStats
                本段統計；收線後清出的回報與失敗一併累計進去
        """

        self.broker.restore_event_routing()
        flushed: LoopStats = loop.flush_executions()

        stats.executions += flushed.executions
        stats.handler_errors += flushed.handler_errors
        if flushed.executions:
            logger.info(
                f"盤中收線時 queue 裡還有 {flushed.executions} 筆回報，已套進委託與歸屬帳"
            )

    def _resolve_intraday_deadline(
        self, window: Optional[SegmentWindow]
    ) -> datetime.datetime:
        """
        本段落跑到什麼時候

        **沒有時窗時不可以無限跑**：常駐迴圈少了收線時間，盤後作業就永遠不會開始。
        沒給就跑到今天結束。
        """

        today: datetime.date = self.clock.now().date()
        if window is None or window.submit_end is None:
            return datetime.datetime.combine(
                today, datetime.time.max, tzinfo=self.clock.now().tzinfo
            )
        return datetime.datetime.combine(
            today, window.submit_end, tzinfo=self.clock.now().tzinfo
        )

    def _on_heartbeat(self) -> None:
        """
        - Description:
            心跳工作：先確認連線，再跑日終強制動作

            **斷線偵測一定要掛在心跳上**：session 斷了行情也跟著停，
            只掛在行情回呼上的話 `ensure_connected()` 再也不會被呼叫——
            迴圈只會在靜默 30 秒後降為 REDUCE_ONLY，然後空轉到收線，期間的回報全部遺失。

            重連的退避是阻塞式的（最長 300 秒），這段時間迴圈停在這裡。
            可以接受：連線斷著時本來就什麼都送不出去，而恢復流程要在
            下一個事件被處理前做完。代價是收線時刻可能被延後一個退避週期。
            連不回來時不跑日終動作，留給下一次心跳再試。
        """

        self.connection.apply_broker_degrades()
        if not self.connection.ensure_connected():
            return
        self._run_session_guard()

    def _run_session_guard(self) -> None:
        """
        - Description:
            日終強制動作：回補時點一到就把未回補的當沖空單補掉

            **掛在心跳而不是行情上**：行情停了 `on_quote` 就不會再被呼叫，
            而日終回補正是不能因為沒行情就不做的事——現股當沖先賣未回補，
            券商可能標借或直接違約交割。

            **全部回補單都送出才標記「今天已回補」**：先標記再送的話，
            這次心跳只要送單失敗或取不到報價，當天就不會再試。
            失敗時寫 CRITICAL 風控事件並推播，下一次心跳重試，
            最多 `MAX_COVER_ATTEMPTS` 次。
        """

        if not self.session_guard.should_cover_now():
            return

        today: datetime.date = self.clock.now().date()
        failures: List[str] = []
        stop_after: bool = False

        for context in self.contexts:
            policy: DayTradeUncoveredPolicy = getattr(
                context.strategy,
                "day_trade_uncovered_policy",
                DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE,
            )
            missing_price: List[str] = []
            orders, warning = self.session_guard.build_cover_orders(
                context.account,
                policy,
                partial(self._build_cover_order_tracked, context, today, missing_price),
            )
            # 回補是系統產生的出場單，一律要成交；回補時點在逐筆交易時段
            orders = mark_execution(
                [
                    order
                    for order in orders
                    if (today, context.name, order.symbol) not in self._cover_sent
                ],
                ExecutionTiming.IMMEDIATE,
                ExecutionStyle.MARKET,
            )
            failures.extend(
                f"{context.name} {symbol} 取不到可成交價，無法產生回補單"
                for symbol in missing_price
            )

            if warning is not None:
                logger.warning(f"{context.name}：{warning}")
                notify_safely(
                    self.notifier, NotifyLevel.WARN, "當沖回補政策在實盤被改寫", warning
                )

            if orders:
                logger.warning(
                    f"{context.name} 有 {len(orders)} 筆當沖空單未回補，送出回補單"
                )
                # **不經跨策略守門**：回補是把自己的部位平掉、不是新開倉，
                # 被守門擋下等於讓部位留倉過夜
                unsent: List[Tuple[StrategyContext, BaseOrder]] = (
                    self.execution.dispatch(
                        [(context, order) for order in orders], None
                    )
                )
                unsent_ids: Set[int] = {id(order) for _, order in unsent}
                for order in orders:
                    if id(order) in unsent_ids:
                        failures.append(f"{context.name} {order.symbol} 回補單未送出")
                    else:
                        self._cover_sent.add((today, context.name, order.symbol))

            if stops_after_cover(policy):
                stop_after = True

        if failures:
            self._record_cover_failure(today, failures)
        else:
            self.session_guard.mark_covered()

        if stop_after:
            self.risk_manager.on_degrade_event(
                "當沖未回補且政策為 RAISE：已送出回補單，停止開新倉",
                TradingMode.REDUCE_ONLY,
            )

    def _record_cover_failure(self, today: datetime.date, failures: List[str]) -> None:
        """
        回補失敗：寫 CRITICAL 事件並推播；次數用完才標記今天已處理

        **每一次失敗都要落地與推播**：部位留倉過夜是現股當沖最不能發生的事，
        晚一分鐘有人知道就少一分鐘可以人工處理。
        """

        attempts: int = self._cover_attempts.get(today, 0) + 1
        self._cover_attempts[today] = attempts
        exhausted: bool = attempts >= self.MAX_COVER_ATTEMPTS

        message: str = (
            f"當沖回補第 {attempts}／{self.MAX_COVER_ATTEMPTS} 次失敗："
            + "；".join(failures)
            + ("。已停止重試，請立即人工處理" if exhausted else "。下一次心跳重試")
        )
        logger.error(message)
        self.events.write(
            category="DAY_TRADE_COVER_FAILED",
            severity=NotifyLevel.CRITICAL,
            message=message,
        )
        notify_safely(self.notifier, NotifyLevel.CRITICAL, "當沖回補失敗", message)

        if exhausted:
            self.session_guard.mark_covered()

    def _build_cover_order_tracked(
        self,
        context: StrategyContext,
        today: datetime.date,
        missing_price: List[str],
        position: Any,
    ) -> Optional[BaseOrder]:
        """
        產生回補單，並把「取不到價格」的標的記進 `missing_price`

        今天已送出過回補單的標的不算缺價：它可能只是報價暫時沒更新，
        而那筆部位已經有一張單在路上了。
        """

        order: Optional[BaseOrder] = self._build_cover_order_for(context, position)
        sent: bool = (today, context.name, position.symbol) in self._cover_sent
        if order is None and not sent:
            missing_price.append(position.symbol)
        return order

    def _build_cover_order_for(
        self, context: StrategyContext, position: Any
    ) -> Optional[BaseOrder]:
        """
        把一個待回補部位換成回補單

        **訂單型別走 `context.build_filled_order`**：本檔刻意不出現任何市場或
        商品字樣（`check_layer_deps.py` 會擋），而 factory 依市場注入的那個建構器
        正是為此存在的。

        價格取**該檔最後一筆報價**；取不到就回 None，由守門記 error。
        """

        quote: Optional[BaseQuote] = self._last_quotes.get(position.symbol)
        if quote is None or context.build_filled_order is None:
            return None

        return context.build_filled_order(
            position.symbol,
            self.clock.now(),
            order_rules.resolve_close_action(position.position_type),
            position.position_type,
            float(quote.cur_price),
            int(position.volume),
        )

    def _on_intraday_quote(self, quote: BaseQuote) -> None:
        """
        一筆行情 → 逐筆觸發策略

        **只餵宣告了 `is_tick_triggered` 的策略**，而且只餵有訂這檔的那些。
        送單前先確認連線：斷線時繼續算訊號只會產生一批送不出去的單。
        """

        # 回補單的價格來源；**只留最後一筆**，留全部等於在記憶體裡重建一份行情庫
        self._last_quotes[quote.symbol] = quote

        if not self.connection.ensure_connected():
            return

        for context in self.contexts:
            if not getattr(context.strategy, "is_tick_triggered", False):
                continue
            # 標的池查集合；池外的只認帳上持有的（與訂閱範圍 `quote_symbols()` 相同）
            declared: FrozenSet[str] = self._declared_symbols.get(
                context.name, frozenset(context.symbols)
            )
            if quote.symbol not in declared and not context.account.check_has_position(
                quote.symbol
            ):
                continue
            self._submit_one_quote(context, quote)

    def _submit_one_quote(self, context: StrategyContext, quote: BaseQuote) -> None:
        """
        把一筆報價餵給一支策略，並走完整條送單流程

        **重用日頻那一條，不另寫一份**：跨策略守門、曝險截斷、資金保留與風控
        在逐筆之下一樣要做。另寫一份必然漂移，而漂移的那一刻兩邊都看起來正確。
        """

        if self.mode_state.effective_mode(context.name) is TradingMode.HALTED:
            return

        try:
            # 逐筆只更新這一檔的市場價，其餘標的沿用本段落先前的報價
            context.quote_prices.update(quote_prices_of([quote]))
            orders: List[BaseOrder] = self.signals.invoke_hooks(
                context, ExecutionTiming.IMMEDIATE, [quote]
            )
        except Exception as exc:
            self.signals.halt_strategy(context, f"逐筆鉤子執行失敗：{exc}")
            return

        if not orders:
            return

        survivors: List[Tuple[StrategyContext, BaseOrder]] = self.cross_checks.apply(
            [(context, order) for order in orders]
        )
        self.execution.dispatch(survivors, None)

    def _on_intraday_execution(self, event: Any) -> None:
        """一筆回報 → 更新委託狀態與帳戶；與日頻走同一份判定"""

        fill: Optional[ExecutionReport] = self.order_manager.apply_event(event)
        if fill is not None:
            self.account_sync.apply_fill(fill)

    def _on_market_data_lost(self, reason: str) -> None:
        """
        行情中斷 → 帳戶層降到 `REDUCE_ONLY`

        **不降 `HALTED`**：部位還在場上，`HALTED` 連平倉都不送，
        等於在看不到行情的時候把停損也關掉。
        """

        self.risk_manager.on_degrade_event(reason, TradingMode.REDUCE_ONLY)
        notify_safely(self.notifier, NotifyLevel.CRITICAL, "行情中斷", reason)
