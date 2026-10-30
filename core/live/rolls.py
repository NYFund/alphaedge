from typing import Any, Callable, List, Optional, Sequence, Tuple

from loguru import logger

from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.datafeed.base import RollPlan
from core.live.execution.base import mark_execution
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.market.segment import SegmentWindow
from core.models import BaseOrder, ExecutionReport, OrderTicket
from core.utils import ExecutionStyle, ExecutionTiming, LiveOrderStatus

"""
RolloverExecutor：期貨換月的兩腿執行

要不要換、換去哪由各市場的資料源決定（`plan_rolls()`），本元件只負責
「先平後開」的執行順序與失敗處理；送單走與一般委託同一條管線（注入的 `dispatch`），
不另外直接呼叫 OMS。
"""

# 送單管線：`(候選委託, 時窗) → 沒有送出的委託`，由引擎的送單元件提供
DispatchFn = Callable[
    [List[Tuple[StrategyContext, BaseOrder]], Optional[SegmentWindow]],
    List[Tuple[StrategyContext, BaseOrder]],
]
# 消化一次成交回報：回傳本次新增的成交
ApplyFillsFn = Callable[[], List[ExecutionReport]]


class RolloverExecutor:
    """期貨換月：逐策略取得今天的轉倉計畫，先平後開地執行"""

    # 換月平倉腿等成交的輪詢間隔與次數上限（秒 × 次）。**次數上限獨立於時鐘**：
    # 段落時限靠時鐘判斷，時鐘卡住時仍要能結束等待（與段落迴圈的保險絲同一種考量）
    ROLL_POLL_SECONDS: float = 0.5
    ROLL_MAX_POLLS: int = 120

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        mode_state: TradingModeState,
        order_manager: Any,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        dispatch: DispatchFn,
        apply_new_fills: ApplyFillsFn,
    ) -> None:
        """
        - Description:
            建立換月執行者；相依一律由外部注入
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - mode_state: TradingModeState
                交易模式狀態機（不允許開新倉時不換月）
            - order_manager: Any
                委託管理（查平倉腿的 ticket）
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘
            - dispatch: DispatchFn
                送單管線
            - apply_new_fills: ApplyFillsFn
                消化一次成交回報
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.mode_state: TradingModeState = mode_state
        self.order_manager: Any = order_manager
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.dispatch: DispatchFn = dispatch
        self.apply_new_fills: ApplyFillsFn = apply_new_fills

    def execute(self, window: Optional[SegmentWindow]) -> int:
        """
        - Description:
            執行各策略今天的轉倉；要不要轉、轉去哪由各市場的資料源決定

            **交易模式不允許開新倉時不換月**：開倉腿是新曝險，`REDUCE_ONLY` 下
            會被風控擋掉，只剩平倉腿等於提早平倉；不如整筆不動、寫事件讓人決定。
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗
        - Return:
            - int
                兩腿都送出的轉倉筆數
        """

        today = self.clock.today()
        completed: int = 0
        for context in self.contexts:
            if self.mode_state.effective_mode(context.name) is TradingMode.HALTED:
                continue
            try:
                plans: List[RollPlan] = context.data_feed.plan_rolls(
                    context.account.positions, today
                )
            except Exception as exc:
                logger.opt(exception=True).error(f"{context.name} 換月判定失敗：{exc}")
                self._write_roll_event(
                    context,
                    None,
                    NotifyLevel.CRITICAL,
                    "ROLL_FAILED",
                    f"換月判定失敗：{exc}",
                )
                continue

            for plan in plans:
                if not self.mode_state.allows_open(context.name):
                    self._write_roll_event(
                        context,
                        plan,
                        NotifyLevel.WARN,
                        "ROLL_SKIPPED",
                        "交易模式不允許開新倉，本次不換月",
                    )
                    continue
                if self._execute_roll(context, plan, window):
                    completed += 1
        return completed

    def _execute_roll(
        self, context: StrategyContext, plan: RollPlan, window: Optional[SegmentWindow]
    ) -> bool:
        """
        先平後開：平倉腿成交了才送開倉腿

        **平倉腿沒成交就放棄開倉腿**（次日再換）：兩腿都在場上的代價是曝險翻倍，
        換月失敗的代價只是晚一天。反過來，平倉腿成交而開倉腿送不出去時
        曝險直接消失——那要馬上有人知道，記 CRITICAL。
        """

        logger.info(f"[Roll] {context.name}：{plan.reason}")
        # 換月兩腿是系統產生的委託，一律要成交；只在尾盤段執行
        mark_execution(
            [plan.close_order, plan.open_order],
            ExecutionTiming.AT_CLOSE,
            ExecutionStyle.MARKET,
        )
        # 偏離檢查的市場基準用計畫當下的快照價：換月先於收訊號，此時本段落還沒有
        # 餵給策略的報價，送單元件會退回合約檔的參考價（前一日結算）。
        # 當日漲跌超過偏離上限時平倉腿就被擋、整筆放棄——而換月固定在最後交易日
        # 前一天，隔天已無法再換，舊月只能被交易所結算
        for leg in (plan.close_order, plan.open_order):
            if leg.price and leg.price > 0:
                context.quote_prices[leg.symbol] = float(leg.price)

        if self.dispatch([(context, plan.close_order)], window):
            self._write_roll_event(
                context,
                plan,
                NotifyLevel.WARN,
                "ROLL_ABANDONED",
                "平倉腿未送出，本次不換月",
            )
            return False

        ticket: Optional[OrderTicket] = self.order_manager.tickets.get(
            plan.close_order.client_order_id or ""
        )
        if ticket is None or not self._wait_for_fill(ticket, window):
            filled: int = ticket.filled_volume if ticket is not None else 0
            if filled <= 0:
                self._write_roll_event(
                    context,
                    plan,
                    NotifyLevel.WARN,
                    "ROLL_ABANDONED",
                    "平倉腿未成交，開倉腿放棄，次日再換",
                )
                return False
            # 部分成交（IOC 成交幾口後被取消）：已平的口數曝險已經消失，開倉腿改送
            # 已平量補回；剩下沒平的次日再換。只記 WARN 放棄的話，曝險少了幾口沒人知道
            remaining: int = int(plan.close_order.volume) - filled
            plan.open_order.volume = filled
            self._write_roll_event(
                context,
                plan,
                NotifyLevel.CRITICAL,
                "ROLL_PARTIAL",
                f"平倉腿只成交 {filled}/{plan.close_order.volume}，"
                f"開倉腿改送 {filled} 口補回已平部分的曝險，剩 {remaining} 口次日再換",
            )

        if self.dispatch([(context, plan.open_order)], window):
            self._write_roll_event(
                context,
                plan,
                NotifyLevel.CRITICAL,
                "ROLL_OPEN_FAILED",
                "舊契約已平倉、新契約未送出：這筆曝險已消失，請人工確認是否補開",
            )
            return False
        return True

    def _wait_for_fill(
        self, ticket: OrderTicket, window: Optional[SegmentWindow]
    ) -> bool:
        """持續消化回報直到這張委託全部成交；終結、到時限或次數用完時回 False"""

        for _ in range(self.ROLL_MAX_POLLS):
            self.apply_new_fills()
            if ticket.status is LiveOrderStatus.FILLED:
                return True
            if ticket.is_terminal or self.clock.past(window, "submit_end"):
                return False
            self.clock.sleep(self.ROLL_POLL_SECONDS)
        return False

    def _write_roll_event(
        self,
        context: StrategyContext,
        plan: Optional[RollPlan],
        severity: NotifyLevel,
        category: str,
        message: str,
    ) -> None:
        """換月的事件：寫紀錄並推播"""

        text: str = f"{plan.reason}：{message}" if plan is not None else message
        logger.warning(f"[Roll] {context.name}：{text}")
        self.events.write(
            category=category,
            severity=severity,
            message=text,
            strategy_name=context.name,
            symbol=plan.close_order.symbol if plan is not None else None,
        )
        notify_safely(self.notifier, severity, "換月", text)
