import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.broker.base import BaseBroker
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution import order_preprocess
from core.live.account_sync import AccountSynchronizer, FilledOrderBuilder
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.notify.base import NotifyLevel
from core.live.reconciler import Reconciler
from core.live.report.live_reporter import LiveReporter
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import ExposureItem, PreTradeRiskManager, RiskDecision
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.segment import SegmentSchedule, SegmentWindow, resolve_window
from core.live.strategy_guard import resolve_hook_timing, verify_strategies
from core.managers.base.position_manager import BasePositionManager
from core.models import BaseAccount, BaseOrder, BaseQuote, ExecutionReport
from core.strategies.base import BaseStrategy
from core.utils import BarExecutionOrder, ExecutionTiming, LiveHook, PositionType

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


def position_value(account: BaseAccount) -> float:
    """
    未平倉部位的帳面金額（開倉價 × 數量）

    **口徑是開倉成本不是即時市值**：本地帳沒有即時報價，硬要取市值就得在每個
    呼叫點各查一次行情，而帳務類限流只有 25 次／5 秒。總權益因此會落後市場，
    但它只用在額度與占用這類「分母」上——分母漏掉整批持倉（本函式修掉的那個
    問題）會讓超配變成通過，落後一段行情不會。
    """

    return float(
        sum(
            position.volume * position.price
            for position in account.positions
            if not position.is_closed
        )
    )


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
    # 把「已成交的一筆」還原成訂單物件餵給 `PositionManager`。
    # 訂單型別同樣是市場特性（期貨要 product／expiry），故一併由外部注入
    build_filled_order: Optional[FilledOrderBuilder] = None

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
        reporter: Optional[LiveReporter] = None,
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
        self.reporter: LiveReporter = (
            reporter if reporter is not None else LiveReporter(dao)
        )
        self._now: Callable[[], datetime.datetime] = now_provider
        self._sleep: Callable[[float], None] = sleep

        # 本段落已送出的委託金額，逐策略累計；段落結束時用來釋放未成交的保留
        self._reserved_by_order: Dict[str, Tuple[str, float]] = {}

        # 最後一次對帳結果。**要留給呼叫端**：對帳不一致不拋例外（它只降級），
        # 而排程需要一個退出碼才分得出「今天剛出事」與「跑完了」
        self.last_reconcile: Optional[Any] = None

        # 今天是不是交易日；由 `prepare()` 的啟動檢查填入。
        # **預設 True**：只有檢查實際判定為休市才會是 False，
        # 預設 False 會讓沒跑過 `prepare()` 的呼叫端誤以為今天休市
        self.is_trading_day: bool = True

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

            if not self.is_trading_day:
                logger.warning("今天不是交易日，本段落不進入送單路徑")
                return

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

        # 休市日不進送單路徑；判不出來一律拒絕啟動（`is_market_open()` 會拋）
        self.is_trading_day = self._verify_trading_day(today)
        if not self.is_trading_day:
            return

        # 歷史資料沒更新到前一個交易日就拒絕啟動：策略會拿舊資料算出訊號，
        # 而那條路徑不會有任何錯誤
        for context in self.contexts:
            context.data_feed.verify_data_freshness(today)

        self.order_manager.recover(today)

        positions: List[Any] = self.broker.get_positions()
        self.account_sync.rebuild_from_broker(positions)
        self._refresh_capital()

        # 額度總量要在有帳務之後才驗得動（`build_live_trader()` 當下還沒連線），
        # 且要在對帳之前——超配就不該讓這個段落繼續往下走
        self.allocator.verify_quota(self._account_equity())
        self._check_daily_loss()

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

        # **開盤段的第一件事是補平昨天沒成交的平倉單**，排在任何新訊號之前：
        # 那是預期外的隔夜部位，多留一分鐘就多一分鐘的曝險
        if timing is ExecutionTiming.AT_OPEN:
            self.apply_pending_actions()

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
            self.write_account_snapshots()
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

    def write_account_snapshots(self) -> None:
        """
        寫入各策略的**帳務**快照；一致與否都要寫

        **不是部位快照**——部位快照由 `Reconciler._write_snapshots()` 寫。
        兩者寫的是不同的表，名字混在一起會讓人以為這裡已經記了部位。
        """

        today: datetime.date = self._now().date()
        for context in self.contexts:
            self.dao.upsert_account_snapshot(
                {
                    "date": today,
                    "strategy_name": context.name,
                    "source": "local",
                    "available_balance": context.account.balance,
                    "total_equity": context.account.balance
                    + position_value(context.account),
                }
            )
        self.dao.conn.commit()

    # === 盤後 ===
    def run_after_close(self) -> Dict[str, Any]:
        """
        - Description:
            盤後作業：刷新委託、對帳、回填成本、殘量處理、輸出報表

            **和送單段落分開跑**（`--phase after_close`）：它不送任何新倉單，
            只把當天發生的事收攏成可稽核的結果，並把「明天要補的事」寫下來。
        - Return:
            - Dict[str, Any]
                本次盤後作業的摘要（報表路徑、殘量筆數、滑價統計）
        """

        today: datetime.date = self._now().date()
        logger.info(f"=== 盤後作業開始（{today}）===")

        try:
            self.broker.connect()
            self.mode_state.load()

            # 1. 刷新委託狀態：ROD 未成交單在券商端日終自動失效
            self.expire_open_orders()

            # 2. 對帳與快照
            positions: List[Any] = self.broker.get_positions()
            self.account_sync.rebuild_from_broker(positions)
            self.last_reconcile = self.reconciler.check(positions)

            # 3. 回填券商實際費用（估算值保留，差額是校正成本設定的依據）
            self.backfill_actual_costs(today)

            # 4. 未成交殘量依政策處理
            remainders: int = self.handle_unfilled_remainders(today)

            # 5. 報表與滑價
            reports: Dict[str, Path] = self.reporter.write_daily_reports(today)
            slippage: Dict[str, float] = self.reporter.summarize_slippage(today)

            return {
                "reports": reports,
                "pending_actions": remainders,
                "slippage": slippage,
            }
        finally:
            self.broker.close()
            for context in self.contexts:
                context.data_feed.close()
            logger.info("=== 盤後作業結束 ===")

    def expire_open_orders(self) -> List[Any]:
        """
        - Description:
            把當日仍未終結的委託標成已撤

            ROD 單在券商端日終自動失效，**本地要跟著標**：不標的話，
            明天的恢復流程會把它們當成「還在場上」去接管，然後撤一張不存在的單。
        - Return:
            - List[Any]
                被標記的委託
        """

        self.order_manager.refresh_from_broker()
        return self.order_manager.expire_unfinished(self._now().date())

    def backfill_actual_costs(self, run_date: datetime.date) -> int:
        """
        - Description:
            以券商的損益明細回填當日成交的實際手續費與稅

            **估算值不覆蓋**：兩者分欄保存，差額才是校正成本設定的依據；
            併成一欄之後就再也算不出「估得準不準」。

            ⚠️ **券商端的查詢方法與欄位尚未以模擬環境核對**（規劃要求實作前核對）。
            取不到時只記 warning 並略過——盤後少一次回填不影響部位，
            而在這裡拋例外會讓報表也產不出來。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                成功回填的筆數
        """

        provider: Optional[Callable[[datetime.date], List[Any]]] = getattr(
            self.broker, "get_profit_loss_details", None
        )
        if provider is None:
            logger.warning(
                "券商閘道尚未提供損益明細查詢，本次不回填實際費用；"
                "成本統計會停留在估算值"
            )
            return 0

        try:
            details: List[Any] = provider(run_date)
        except Exception as exc:
            logger.opt(exception=True).warning(f"回填實際費用失敗（略過）：{exc}")
            return 0

        filled: int = 0
        for detail in details:
            seqno: str = str(getattr(detail, "seqno", "") or "")
            trade_id: str = str(getattr(detail, "trade_id", "") or "")
            if not seqno or not trade_id:
                continue
            self.dao.backfill_fill_costs(
                seqno,
                trade_id,
                float(getattr(detail, "fee", 0.0) or 0.0),
                float(getattr(detail, "tax", 0.0) or 0.0),
            )
            filled += 1

        logger.info(f"回填實際費用 {filled} 筆")
        return filled

    def handle_unfilled_remainders(self, run_date: datetime.date) -> int:
        """
        - Description:
            未成交殘量的處理政策

            **開倉與出場的處理完全不同**：
            - **開倉未成交一律放棄**，不追價。追價等於在偏離訊號價的位置建倉，
              而回測沒有這個行為。
            - **平倉與停損未成交必須補**：那是預期外的隔夜部位，風險遠大於
              開倉沒成交。寫一筆 `PENDING` 待辦，由次日開盤段第一件事執行。

            **待辦要有狀態才冪等**：只記「明天要補」而沒有完成標記的話，
            次日開盤段重跑或崩潰重啟會重複送補平單——而重複的補平單不是多買一點，
            是直接把部位做反。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                新增的待辦筆數
        """

        created: int = 0
        for order_row in self.dao.get_orders_by_date(run_date):
            remainder: int = int(order_row.get("volume") or 0) - int(
                order_row.get("filled_volume") or 0
            )
            if remainder <= 0:
                continue

            if not self._is_exit_row(order_row):
                logger.info(
                    f"開倉單 {order_row['client_order_id']} 殘量 {remainder} 放棄，"
                    "不追價（追價等於在偏離訊號價的位置建倉）"
                )
                continue

            self._record_pending_cover(order_row, remainder, run_date)
            created += 1

        return created

    def _record_pending_cover(
        self, order_row: Dict[str, Any], remainder: int, run_date: datetime.date
    ) -> None:
        """寫一筆待辦與一則 CRITICAL 事件，並推播"""

        client_order_id: str = str(order_row["client_order_id"])
        message: str = (
            f"平倉／停損單 {client_order_id}（{order_row['symbol']}）殘量 {remainder} "
            "未成交，已成為預期外的隔夜部位；次日開盤段第一件事補平"
        )
        logger.error(message)

        self.dao.insert_pending_action(
            {
                "action_id": f"{run_date.isoformat()}-{client_order_id}",
                "strategy_name": order_row["strategy_name"],
                "symbol": order_row["symbol"],
                "action": order_row["action"],
                "position_type": order_row["position_type"],
                "volume": remainder,
                "due_date": run_date + datetime.timedelta(days=1),
                "status": self.dao.ACTION_PENDING,
                "reason": "平倉單未成交",
                "source_client_order_id": client_order_id,
                "created_at": self._now(),
            }
        )
        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": order_row["strategy_name"],
                "severity": "CRITICAL",
                "category": "UNFILLED_EXIT",
                "symbol": order_row["symbol"],
                "client_order_id": client_order_id,
                "message": message,
                "occurred_at": self._now(),
            }
        )
        self._notify("CRITICAL", "平倉單未成交", message)

    def apply_pending_actions(self) -> int:
        """
        - Description:
            執行到期的跨日待辦（次日開盤段的第一件事）

            成功送出才標 `DONE`；送不出去就**留在 `PENDING` 並把到期日滾到次日**，
            同時再推播一次——一張補不成的平倉單不會因為換了一天就變得不重要。
        - Return:
            - int
                實際送出的補平單數
        """

        today: datetime.date = self._now().date()
        pending: List[Dict[str, Any]] = self.dao.get_pending_actions(today)
        if not pending:
            return 0

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        submitted: int = 0

        for action in pending:
            context: Optional[StrategyContext] = by_name.get(
                str(action["strategy_name"])
            )
            if context is None:
                logger.warning(
                    f"待辦 {action['action_id']} 的策略未在本次啟動的清單中，本日略過"
                )
                continue

            order: Optional[BaseOrder] = self._build_cover_order(context, action)
            if order is None:
                continue

            try:
                self.order_manager.submit(order, context.name)
                self.dao.resolve_pending_action(
                    str(action["action_id"]), self.dao.ACTION_DONE, self._now()
                )
                submitted += 1
            except Exception as exc:
                logger.opt(exception=True).error(
                    f"補平單送出失敗，待辦保留：{action['action_id']}：{exc}"
                )
                self.dao.postpone_pending_action(
                    str(action["action_id"]), today + datetime.timedelta(days=1)
                )
                self._notify(
                    "CRITICAL",
                    "補平單送出失敗",
                    f"{action['symbol']} 殘量 {action['volume']} 仍未平掉",
                )

        return submitted

    def _build_cover_order(
        self, context: StrategyContext, action: Dict[str, Any]
    ) -> Optional[BaseOrder]:
        """
        由待辦組出補平單

        **交給策略自己組**：訂單型別、價格類型與商品欄位都是市場特性，
        引擎本體既不知道也不該知道。策略沒有提供組裝方法時記 warning 並略過——
        那代表這支策略還沒準備好處理跨日補平。
        """

        builder: Optional[Callable[..., BaseOrder]] = getattr(
            context.strategy, "build_cover_order", None
        )
        if builder is None:
            logger.warning(
                f"{context.name} 沒有 build_cover_order()，待辦 "
                f"{action['action_id']} 無法自動補平，需人工處理"
            )
            return None

        try:
            return builder(action)
        except Exception as exc:
            logger.opt(exception=True).error(f"組補平單失敗：{exc}")
            return None

    @staticmethod
    def _is_exit_row(order_row: Dict[str, Any]) -> bool:
        """
        這張委託是不是出場單

        以持倉方向與買賣別推導，與 `order_preprocess.resolve_close_action()` 同一套
        規則——散在多處會漂移，而漂移的後果是開倉單被當成平倉單去補，
        那會憑空建出一個新部位。
        """

        position_type: PositionType = PositionType(str(order_row["position_type"]))
        return str(order_row["action"]) == (
            order_preprocess.resolve_close_action(position_type).value
        )

    def _notify(self, level: str, title: str, body: str) -> None:
        """
        推播

        **通知失敗不可影響流程**：`BaseNotifier.send()` 自己就吞例外，
        這裡再包一層是因為 notifier 可能是任何注入進來的東西——
        監控拖垮被監控的東西是典型反例。
        """

        if self.notifier is None:
            return
        try:
            self.notifier.send(NotifyLevel(level), title, body)
        except Exception as exc:
            logger.opt(exception=True).warning(f"推播失敗（忽略）：{exc}")

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

    def _verify_trading_day(self, today: datetime.date) -> bool:
        """
        今天是不是交易日

        **各策略的資料源各判一次**：台股與期貨的交易日不必然相同。
        任何一條判定為休市就整段不送單——同一個行程裡只有一部分市場開市時，
        分開排程才是正解，硬送會被退單。

        判不出來時 `is_market_open()` 會拋 `TradingCalendarUnavailableError`，
        **刻意不接住**：官方休市日曆尚未接上，平日只剩券商合約檔一個來源，
        這時候預設為開市等於在休市日照常送單。
        """

        for context in self.contexts:
            if not context.data_feed.is_market_open(today):
                logger.warning(f"{context.name} 的資料源判定 {today} 非交易日")
                return False
        return True

    def _account_equity(self) -> float:
        """帳戶總權益：可用餘額 ＋ 持倉占用；與批次曝險檢查用的是同一個口徑"""

        return self.allocator.available_balance + sum(self.allocator.used.values())

    def _check_daily_loss(self) -> None:
        """
        段落開始前的虧損檢查：逐策略降級 ＋ 帳戶層降級

        **放開盤前不放段落結束**：開盤前就發現昨天虧太多，這個段落直接不送新倉單；
        放在結束才判等於本段落已經白送一輪。

        ⚠️ **目前恆為不觸發，缺的是輸入不是接線**：`prepare()` 走到這裡時，帳戶是剛由
        `AccountSynchronizer._restore_positions()` 以**原始開倉價**重建的，未實現損益
        因此是 0；而 `OrderManager.recover()` 只接管未終結的委託、不回放已成交的回報，
        本段落之前的已實現損益也不在這個行程的 `trade_records` 裡。

        **刻意留著這段接線而不是拿掉**：判定與降級的路徑本身是對的，缺的只是損益來源。
        日頻模式下三個段落是三個獨立行程，本地帳每次都從零開始——要真的擋得住昨天的
        虧損，得等盤中事件迴圈讓帳戶持續收到成交回報，或改由帳戶快照比對日內變動。
        """

        total_loss: float = 0.0
        for context in self.contexts:
            loss: float = self._strategy_loss(context)
            total_loss += loss
            self.risk_manager.check_daily_loss(
                context.name, loss, context.account.init_capital
            )

        self.risk_manager.check_account_daily_loss(total_loss, self._account_equity())

    @staticmethod
    def _strategy_loss(context: StrategyContext) -> float:
        """
        本策略目前的虧損金額（正數表示虧損）：已實現 ＋ 未實現

        **一律取本地帳**（與回測同一套算法），不取券商快照：對帳不一致本身
        會另外觸發降級，兩個來源在那個時候會給出不同的答案。
        """

        account: BaseAccount = context.account
        account.update_realized_pnl()
        unrealized: float = sum(
            position.unrealized_pnl
            for position in account.positions
            if not position.is_closed
        )
        return -(account.realized_pnl + unrealized)

    def _refresh_capital(self) -> None:
        """刷新帳戶可用餘額與各策略的持倉占用；段落內不再逐單查帳務"""

        snapshot: Any = self.broker.get_account()
        used: Dict[str, float] = {
            context.name: position_value(context.account) for context in self.contexts
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
