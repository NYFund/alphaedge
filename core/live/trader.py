import datetime
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.broker.base import BaseBroker
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution import order_preprocess
from core.live.account_sync import AccountSynchronizer
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.reconciler import Reconciler
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import ExposureItem, PreTradeRiskManager, RiskDecision
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.segment import SegmentSchedule, SegmentWindow, resolve_window
from core.live.strategy_guard import resolve_hook_timing, verify_strategies
from core.managers.base.position_manager import BasePositionManager
from core.models import BaseAccount, BaseOrder, BaseQuote, ExecutionReport
from core.strategies.base import BaseStrategy
from core.utils import BarExecutionOrder, ExecutionTiming, LiveHook

"""
LiveTrader：實盤引擎本體

對應 `Backtester`，**市場無關、沒有子類**——市場語意全部由注入的物件決定
（資料源、部位管理、風控設定、段落時窗）。檔案內不出現任何市場或商品字樣，
`scripts/check_layer_deps.py` 會擋。

**一個 LiveTrader 編排多支策略。** 每支策略各有自己的帳戶、部位管理與風控設定；
券商閘道、委託管理、限流、資金分配、歸屬帳、帳戶層風控則是單例——
限流與委託回報都是帳戶級的，拆成多份必然互相不知情。

段落的執行順序寫在 `run()` 裡，十三個步驟一步都不能調換：對帳要在送單之前
（不然是帶著錯誤部位交易）、守門要在批次曝險之前（不然被擋的單還佔著額度）、
保留資金要在風控之前（不然風控算的是一個拿不到的金額）。
"""


@dataclass
class StrategyContext:
    """
    - Description:
        一支策略跑起來需要的一整組東西

        **每支策略一份**。共用一份的話，兩支策略的部位與損益會混在一起，
        而合計仍然正確——對帳看不出來。
    """

    strategy: BaseStrategy
    account: BaseAccount
    position_manager: BasePositionManager
    data_feed: BaseLiveDataFeed
    risk_config: RiskConfig
    # 本策略要處理的標的；實盤不掃全市場，標的池由策略決定
    symbols: List[str] = field(default_factory=list)
    # 把一張委託換算成金額（風控與資金保留都要用）。
    # 計價單位是市場特性（張要乘 1000 股、口要乘保證金），故由外部注入
    calculate_notional: Optional[Callable[[BaseOrder], float]] = None

    @property
    def name(self) -> str:
        """策略名；歸屬鏈以它為鍵"""

        return type(self.strategy).__name__

    def notional(self, order: BaseOrder) -> float:
        """本張委託的金額；未注入換算器時退回「價 × 量」"""

        if self.calculate_notional is not None:
            return self.calculate_notional(order)
        return float(order.price) * float(order.volume)


class LiveTrader:
    """實盤引擎：市場無關，多策略共用一組券商與帳務元件"""

    # 沒有回報時的輪詢間隔（秒）。太短會空轉吃 CPU，太長會讓撤單與時限判斷變遲鈍
    POLL_INTERVAL_SECONDS: float = 0.5

    # 等待回報的安全上限（秒）。**這條是獨立於時鐘的保險絲**：
    # 時窗判斷靠 `now_provider()`，而它若因為時鐘卡住、倒退或注入錯誤而不再前進，
    # 迴圈會永遠轉下去——段落不結束，下一個段落的行程拿不到連線與寫入鎖，
    # 而存活監控只會看到「開始了但沒有正常結束」。故另外累計實際等過的秒數，
    # 超過就強制跳出並記 error
    MAX_WAIT_SECONDS: float = 1800.0

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        broker: BaseBroker,
        order_manager: Any,
        risk_manager: PreTradeRiskManager,
        mode_state: TradingModeState,
        ledger: PositionAttributionLedger,
        allocator: CapitalAllocator,
        account_sync: AccountSynchronizer,
        reconciler: Reconciler,
        conflict_guard: CrossStrategyConflictGuard,
        dao: LiveTradeDAO,
        run_id: str,
        schedule: Optional[SegmentSchedule] = None,
        dry_run: bool = False,
        resume_trading: bool = False,
        notifier: Optional[Any] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        - Description:
            建立引擎；所有相依一律由外部注入，引擎自己不 new 任何東西
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - broker: BaseBroker
                券商閘道（單例）
            - order_manager: Any
                委託管理（單例）
            - risk_manager: PreTradeRiskManager
                事前風控（單例，`TradingMode` 的唯一持有者）
            - mode_state: TradingModeState
                交易模式狀態機
            - ledger: PositionAttributionLedger
                部位歸屬帳
            - allocator: CapitalAllocator
                資金額度分配
            - account_sync: AccountSynchronizer
                帳戶同步
            - reconciler: Reconciler
                對帳器
            - conflict_guard: CrossStrategyConflictGuard
                跨策略同標的守門
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_id: str
                本次啟動的識別碼
            - schedule: Optional[SegmentSchedule]
                段落時窗；None 時不做時限控制（測試與 dry-run 用）
            - dry_run: bool
                走完整流程但不真的送出
            - resume_trading: bool
                人工恢復交易模式（**只能由命令列旗標傳入**）
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - sleep: Callable[[float], None]
                等待函式
        """

        self.contexts: List[StrategyContext] = list(contexts)
        self.broker: BaseBroker = broker
        self.order_manager: Any = order_manager
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.mode_state: TradingModeState = mode_state
        self.ledger: PositionAttributionLedger = ledger
        self.allocator: CapitalAllocator = allocator
        self.account_sync: AccountSynchronizer = account_sync
        self.reconciler: Reconciler = reconciler
        self.conflict_guard: CrossStrategyConflictGuard = conflict_guard
        self.dao: LiveTradeDAO = dao
        self.run_id: str = run_id
        self.schedule: SegmentSchedule = schedule or {}
        self.dry_run: bool = dry_run
        self.resume_trading: bool = resume_trading
        self.notifier: Optional[Any] = notifier
        self._now: Callable[[], datetime.datetime] = now_provider
        self._sleep: Callable[[float], None] = sleep

        # 本段落已送出的委託金額，逐策略累計；段落結束時用來釋放未成交的保留
        self._reserved_by_order: Dict[str, Tuple[str, float]] = {}

        # 最後一次對帳結果。**要留給呼叫端**：對帳不一致不拋例外（它只降級），
        # 而排程需要一個退出碼才分得出「今天剛出事」與「跑完了」
        self.last_reconcile: Optional[Any] = None

    # === 主流程 ===
    def run(self, timing: ExecutionTiming) -> None:
        """
        - Description:
            跑完一個段落

            步驟順序**一步都不能調換**：對帳要在送單之前（否則是帶著錯誤部位交易）、
            守門要在批次曝險之前（否則被擋的單還佔著額度）、
            保留資金要在風控之前（否則風控算的是一個拿不到的金額）。

            `try/finally` 保證連線一定關得掉——異常路徑上沒關的連線會佔住
            同帳號的連線額度，下一個段落連不進來。
        - Parameters:
            - timing: ExecutionTiming
                本次要跑的段落
        """

        window: Optional[SegmentWindow] = resolve_window(self.schedule, timing)
        logger.info(f"=== 段落 {timing.value} 開始（run_id={self.run_id}）===")

        try:
            self.prepare()

            if not self._can_submit_any():
                logger.warning(
                    "交易模式非 NORMAL 且未帶 resume_trading，"
                    "本段落只跑對帳與快照，不進入送單路徑"
                )
                return

            self.submit_segment(timing, window)
        finally:
            self.finish(window)

    def prepare(self) -> None:
        """
        - Description:
            段落開始前的五件事：連線、讀回模式、接管委託、重建帳戶、對帳

            **順序固定**。接管委託要在重建帳戶之前（未終結的委託會影響部位），
            對帳要在最後（前面幾步都做完才知道本地到底是什麼狀態）。
        """

        self.broker.connect()

        self.mode_state.load()
        if self.resume_trading:
            # 人工恢復；**只能由命令列旗標觸發**，程式不自動呼叫
            self.mode_state.resume()
            for context in self.contexts:
                self.mode_state.resume(context.name)

        verify_strategies([context.strategy for context in self.contexts])

        today: datetime.date = self._now().date()
        self.order_manager.recover(today)

        positions: List[Any] = self.broker.get_positions()
        self.account_sync.rebuild_from_broker(positions)
        self._refresh_capital()

        self.last_reconcile = self.reconciler.check(positions)

    def submit_segment(
        self, timing: ExecutionTiming, window: Optional[SegmentWindow]
    ) -> None:
        """
        - Description:
            本段落的送單流程：收訊號 → 守門 → 曝險 → 保留 → 風控 → 送出 → 收回報
        - Parameters:
            - timing: ExecutionTiming
                執行段落
            - window: Optional[SegmentWindow]
                段落時窗；None 時不做時限控制
        """

        self._wait_until_submit_window(window)

        candidates: List[Tuple[StrategyContext, BaseOrder]] = []
        for context in self.contexts:
            candidates.extend(self.collect_orders(context, timing))

        if not candidates:
            logger.info("本段落沒有任何委託")
            return

        survivors: List[Tuple[StrategyContext, BaseOrder]] = self.apply_cross_checks(
            candidates
        )
        self.dispatch(survivors, window)

    # === 訊號 ===
    def collect_orders(
        self, context: StrategyContext, timing: ExecutionTiming
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        - Description:
            呼叫本段落該跑的鉤子，取得經過前處理的委託

            **一支策略拋例外不可拖垮其他策略**：整段包 try，失敗的那支轉入
            `HALTED` 並釋放它的資金保留，其餘照常。整個段落一起死掉會讓
            其他策略的平倉單也送不出去。
        - Parameters:
            - context: StrategyContext
                策略脈絡
            - timing: ExecutionTiming
                執行段落
        - Return:
            - List[Tuple[StrategyContext, BaseOrder]]
                `[(脈絡, 委託)]`
        """

        if self.mode_state.effective_mode(context.name) is TradingMode.HALTED:
            logger.warning(f"{context.name} 為 HALTED，本段落不呼叫任何鉤子")
            return []

        try:
            quotes: List[BaseQuote] = context.data_feed.get_live_quotes(
                timing, context.symbols
            )
            orders: List[BaseOrder] = self._invoke_hooks(context, timing, quotes)
        except Exception as exc:
            self._halt_strategy(context, f"鉤子執行失敗：{exc}")
            return []

        return [(context, order) for order in orders]

    def _invoke_hooks(
        self,
        context: StrategyContext,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
    ) -> List[BaseOrder]:
        """依 `get_execution_order()` 決定開平倉先後，與回測一致"""

        strategy: BaseStrategy = context.strategy
        execution_order: BarExecutionOrder = order_preprocess.get_execution_order(
            strategy.bar_execution_order,
            strategy.position_type,
            strategy.enable_intraday,
        )

        exit_orders: List[BaseOrder] = self._exit_orders(context, timing, quotes)
        entry_orders: List[BaseOrder] = self._entry_orders(context, timing, quotes)

        if execution_order is BarExecutionOrder.OPEN_THEN_CLOSE:
            return entry_orders + exit_orders
        return exit_orders + entry_orders

    def _entry_orders(
        self,
        context: StrategyContext,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
    ) -> List[BaseOrder]:
        """開倉：本段落沒排到就不呼叫；`REDUCE_ONLY` 以上也不呼叫"""

        if resolve_hook_timing(context.strategy, LiveHook.OPEN) is not timing:
            return []
        if not self.mode_state.allows_open(context.name):
            return []

        raw: List[BaseOrder] = context.strategy.check_open_signal(quotes)
        return self._preprocess(context, raw, stage="open")

    def _exit_orders(
        self,
        context: StrategyContext,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
    ) -> List[BaseOrder]:
        """
        出場：停損與一般平倉

        **停損排在一般平倉之前**，與回測的優先級一致——同一根 bar 內兩者都成立時，
        先停損才符合「保護部位」的語意。
        """

        if not self.mode_state.allows_close(context.name):
            return []

        orders: List[BaseOrder] = []
        if resolve_hook_timing(context.strategy, LiveHook.STOP_LOSS) is timing:
            orders.extend(
                self._preprocess(
                    context, context.strategy.check_stop_loss_signal(quotes), "close"
                )
            )
        if resolve_hook_timing(context.strategy, LiveHook.CLOSE) is timing:
            orders.extend(
                self._preprocess(
                    context, context.strategy.check_close_signal(quotes), "close"
                )
            )
        return orders

    def _preprocess(
        self, context: StrategyContext, orders: List[BaseOrder], stage: str
    ) -> List[BaseOrder]:
        """方向白名單 ＋ 決定性排序；與回測共用同一份實作"""

        allowed = order_preprocess.get_allowed_directions(
            context.strategy.allowed_directions, context.strategy.position_type
        )
        valid: List[BaseOrder] = order_preprocess.validate_orders(
            orders, stage, allowed
        )
        return order_preprocess.sort_orders(valid)

    # === 跨策略 ===
    def apply_cross_checks(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        - Description:
            跨策略守門 → 策略層批次曝險 → 帳戶層批次曝險

            守門在最前面：被擋的單不該佔用曝險額度。
        - Parameters:
            - candidates: List[Tuple[StrategyContext, BaseOrder]]
                各策略的委託（已各自前處理）
        - Return:
            - List[Tuple[StrategyContext, BaseOrder]]
                通過的委託
        """

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        guarded = self.conflict_guard.filter(
            [(context.name, order) for context, order in candidates]
        )
        survivors: List[Tuple[StrategyContext, BaseOrder]] = [
            (by_name[name], order) for name, order in guarded
        ]

        survivors = self._truncate_per_strategy(survivors)
        return self._truncate_account(survivors)

    def _truncate_per_strategy(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """逐策略試算曝險；超額的截斷後面的單"""

        kept: List[Tuple[StrategyContext, BaseOrder]] = []
        for context in self.contexts:
            items: List[ExposureItem] = [
                ExposureItem(order, context.notional(order))
                for owner, order in candidates
                if owner is context
            ]
            if not items:
                continue

            allowed: List[ExposureItem] = self.risk_manager.check_batch(
                items, context.name, context.strategy.init_capital
            )
            kept.extend((context, item.order) for item in allowed)
        return kept

    def _truncate_account(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        帳戶層合計試算

        策略層各自都過、加總仍可能超過帳戶能承受的量——那正是多策略的典型破口。
        """

        items: List[ExposureItem] = [
            ExposureItem(order, context.notional(order))
            for context, order in candidates
        ]
        allowed: List[ExposureItem] = self.risk_manager.check_batch(
            items,
            "__account__",
            self.allocator.available_balance + sum(self.allocator.used.values()),
        )
        approved: List[BaseOrder] = [item.order for item in allowed]
        return [(context, order) for context, order in candidates if order in approved]

    # === 送單 ===
    def dispatch(
        self,
        candidates: List[Tuple[StrategyContext, BaseOrder]],
        window: Optional[SegmentWindow],
    ) -> None:
        """
        - Description:
            逐單保留資金 → 風控 → 送出，期間持續消化回報
        - Parameters:
            - candidates: List[Tuple[StrategyContext, BaseOrder]]
                通過跨策略檢查的委託
            - window: Optional[SegmentWindow]
                段落時窗
        """

        for context, order in candidates:
            if self._past(window, "submit_end"):
                logger.warning("已過送單時限，其餘委託不再送出")
                break

            amount: float = context.notional(order)
            if not self.allocator.reserve(context.name, amount):
                continue

            decision: RiskDecision = self.risk_manager.check(
                order,
                context.name,
                context.strategy.init_capital,
                amount,
                reference_price=self._reference_price(order),
            )
            if not decision.passed:
                self.allocator.release(context.name, amount)
                continue

            try:
                ticket = self.order_manager.submit(order, context.name)
            except Exception as exc:
                # 送單失敗的保留一定要放掉，否則額度會單向消耗到策略再也送不出單
                self.allocator.release(context.name, amount)
                logger.opt(exception=True).error(f"{context.name} 送單失敗：{exc}")
                continue

            self._reserved_by_order[ticket.client_order_id] = (context.name, amount)
            self.drain_once()

    # === 回報 ===
    def drain_once(self) -> List[ExecutionReport]:
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

    def drain_until(self, window: Optional[SegmentWindow], attribute: str) -> None:
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
            self.drain_once()
            return

        waited: float = 0.0
        while not self._past(window, attribute):
            if self.drain_once():
                continue

            if waited >= self.MAX_WAIT_SECONDS:
                logger.error(
                    f"等待回報已達安全上限 {self.MAX_WAIT_SECONDS:.0f} 秒仍未到 "
                    f"{attribute}，強制結束等待；請確認系統時鐘是否正常"
                )
                return

            self._sleep(self.POLL_INTERVAL_SECONDS)
            waited += self.POLL_INTERVAL_SECONDS

    def _release_finished(self) -> None:
        """終結的委託釋放保留；**走遍所有已知委託**而不只是本次成交的那幾張"""

        for client_order_id, (name, amount) in list(self._reserved_by_order.items()):
            ticket = self.order_manager.tickets.get(client_order_id)
            if ticket is not None and ticket.is_terminal:
                self.allocator.release(name, amount)
                self._reserved_by_order.pop(client_order_id, None)

    # === 收尾 ===
    def finish(self, window: Optional[SegmentWindow]) -> None:
        """
        - Description:
            段落收尾：撤未成交單、續收回報、寫快照、關連線

            **撤單之後仍要繼續收回報**：撤單的回應本身也是回報，而剛好在撤單前
            成交的那張單也還沒回來。
        """

        try:
            self.order_manager.cancel_open_orders()
            self.drain_until(window, "drain_end")
            self._release_all_remaining()
            self.write_snapshots()
        except Exception as exc:
            logger.opt(exception=True).error(f"段落收尾失敗：{exc}")
        finally:
            self.broker.close()
            for context in self.contexts:
                context.data_feed.close()
            logger.info(f"=== 段落結束（run_id={self.run_id}）===")

    def _release_all_remaining(self) -> None:
        """段落結束時把還掛著的保留全部放掉：它們已經不可能再成交"""

        for name, amount in self._reserved_by_order.values():
            self.allocator.release(name, amount)
        self._reserved_by_order.clear()

    def write_snapshots(self) -> None:
        """寫入各策略與帳戶層的部位快照；一致與否都要寫"""

        today: datetime.date = self._now().date()
        for context in self.contexts:
            self.dao.upsert_account_snapshot(
                {
                    "date": today,
                    "strategy_name": context.name,
                    "source": "local",
                    "available_balance": context.account.balance,
                    "total_equity": context.account.balance,
                }
            )
        self.dao.conn.commit()

    # === 內部 ===
    def _can_submit_any(self) -> bool:
        """帳戶層模式是否允許進入送單路徑"""

        return (
            self.mode_state.account_mode is not TradingMode.HALTED
            and (
                self.mode_state.account_mode is TradingMode.NORMAL
                or self.resume_trading
            )
            or any(
                self.mode_state.allows_close(context.name) for context in self.contexts
            )
        )

    def _refresh_capital(self) -> None:
        """刷新帳戶可用餘額與各策略的持倉占用；段落內不再逐單查帳務"""

        snapshot: Any = self.broker.get_account()
        used: Dict[str, float] = {
            context.name: sum(
                position.volume * position.price
                for position in context.account.positions
                if not position.is_closed
            )
            for context in self.contexts
        }
        self.allocator.refresh(snapshot.available_balance, used)

    def _reference_price(self, order: BaseOrder) -> float:
        """
        風控用的基準價

        取委託價本身：訊號階段算出來的價格就是這張單的意圖，
        而基準價偏離檢查要擋的是「意圖與市場差太多」。真正的市場基準價由
        資料源在報價上帶過來，盤中版本由 Phase5 接上。
        """

        return float(getattr(order, "price", 0.0) or 0.0)

    def _halt_strategy(self, context: StrategyContext, reason: str) -> None:
        """
        把單一策略降到 `HALTED` 並釋放它的資金保留

        **釋放是必須的**：不回收的話，那支策略的保留會佔住**其他策略**的可用資金
        到重啟為止，症狀是別的策略莫名其妙送不出單。
        """

        logger.opt(exception=True).error(f"{context.name} 降級：{reason}")
        self.risk_manager.on_degrade_event(
            reason, TradingMode.HALTED, strategy_name=context.name
        )
        self.allocator.release_all(context.name)
        for client_order_id, (name, _) in list(self._reserved_by_order.items()):
            if name == context.name:
                self._reserved_by_order.pop(client_order_id, None)

    def _wait_until_submit_window(self, window: Optional[SegmentWindow]) -> None:
        """
        等到可以送單的時刻

        **提早送出的代價很具體**：尾盤段若在時窗之前送出限價單，它會在逐筆交易
        時段就成交，成交價不是收盤價——與回測的假設對不上，而且看起來完全正常。
        """

        if window is None:
            return

        waited: float = 0.0
        while self._now().time() < window.submit_start:
            if waited >= self.MAX_WAIT_SECONDS:
                logger.error(
                    f"等待送單時窗已達安全上限 {self.MAX_WAIT_SECONDS:.0f} 秒，"
                    "本段落放棄送單；請確認系統時鐘是否正常"
                )
                return

            self._sleep(self.POLL_INTERVAL_SECONDS)
            waited += self.POLL_INTERVAL_SECONDS

    def _past(self, window: Optional[SegmentWindow], attribute: str) -> bool:
        """目前時刻是否已過時窗上的某個時點"""

        if window is None:
            return False
        return self._now().time() >= getattr(window, attribute)
