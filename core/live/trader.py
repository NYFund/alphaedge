import datetime
import time
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from loguru import logger

from core.broker.base import BaseBroker
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_monitor import AccountMonitor
from core.live.account_sync import AccountSynchronizer
from core.live.after_close import (
    AfterCloseRunner,
    TradeCostEstimator,
)
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import (
    SOURCE_LOCAL,
    PositionAttributionLedger,
)
from core.live.attribution.resync import (
    ResyncPlan,
    ResyncRefusedError,
    apply_resync,
    plan_resync,
)
from core.live.capital_allocator import CapitalAllocator
from core.live.clock import SegmentClock
from core.live.connection import ConnectionSupervisor
from core.live.context import StrategyContext
from core.live.cross_checks import CrossStrategyChecks
from core.live.datafeed.base import DataFreshnessError
from core.live.execution_engine import ExecutionEngine
from core.live.fill_backfill import FillBackfiller
from core.live.intraday.event_loop import LoopStats
from core.live.intraday.session import IntradaySession
from core.live.intraday.session_guard import SessionGuard
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.pending_actions import PendingActionsRunner
from core.live.reconciler import Reconciler
from core.live.report.live_reporter import LiveReporter
from core.live.report.parity_checker import ParityChecker
from core.live.risk import event_categories
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.margin_gate import MarginAccountQuery
from core.live.risk.risk_manager import (
    PreTradeRiskManager,
)
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.rolls import RolloverExecutor
from core.live.signals import SignalCollector
from core.live.strategy_guard import verify_strategies
from core.market.segment import SegmentSchedule, SegmentWindow, resolve_window
from core.models import (
    BaseOrder,
    BrokerAccountSnapshot,
)
from core.utils import (
    ExecutionTiming,
)

"""
LiveTrader：實盤引擎本體

對應 `Backtester`，**市場無關、沒有子類**——市場語意全部由注入的物件決定
（資料源、部位管理、風控設定、段落時窗）。程式碼內不出現任何市場或商品字樣，
`scripts/check_layer_deps.py` 會擋。

**一個 LiveTrader 編排多支策略。** 每支策略各有自己的帳戶、部位管理與風控設定；
券商閘道、委託管理、限流、資金分配、歸屬帳、帳戶層風控則是單例——
限流與委託回報都是帳戶級的，拆成多份必然互相不知情。

段落的執行順序寫在 `run()` 裡，一步都不能調換：對帳要在送單之前
（不然是帶著錯誤部位交易）、守門要在批次曝險之前（不然被擋的單還佔著額度）、
保留資金要在風控之前（不然風控算的是一個拿不到的金額）。

**每張委託送出前都經過執行層**（`StrategyContext.execution_model`）：策略只宣告
要成交或照價掛單，價格類型、委託價與 ROD／IOC 由執行層依段落換算。
送單路徑負責標註執行方式——策略鉤子沿用 `live_execution`，停損與系統產生的委託
（補平、當沖回補、換月）一律 `MARKET`；沒標註的委託在送出前擋下。
"""


class LiveTrader:
    """實盤引擎：市場無關，多策略共用一組券商與帳務元件"""

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
        parity_checker: Optional[ParityChecker] = None,
        simulation: bool = False,
        margin_query: Optional[MarginAccountQuery] = None,
        cost_estimator: Optional[TradeCostEstimator] = None,
        fetch_account: Optional[Callable[[], BrokerAccountSnapshot]] = None,
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
            - simulation: bool
                是否連模擬環境；決定保證金查不到資料時放行還是擋單
            - margin_query: Optional[MarginAccountQuery]
                券商的保證金帳務查詢；由組裝層依市場注入
            - cost_estimator: Optional[TradeCostEstimator]
                盤後校正成本用的估算器（依市場重算一筆已平倉交易的費用與稅）
            - resume_trading: bool
                帳戶層已人工恢復（**只能由命令列旗標傳入**）；指名只恢復部分策略時為 False，
                帳戶層的降級仍然有效
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - parity_checker: Optional[ParityChecker]
                訊號 parity 比對器；只在盤後用得到，故直接轉給 `AfterCloseRunner`
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
        self.dao: LiveTradeDAO = dao
        self.run_id: str = run_id
        self.schedule: SegmentSchedule = schedule or {}
        self.dry_run: bool = dry_run
        self.resume_trading: bool = resume_trading
        self.notifier: Optional[Any] = notifier
        self.reporter: LiveReporter = (
            reporter if reporter is not None else LiveReporter(dao)
        )
        # 段落時窗的時間判斷與等待；引擎本體與各元件共用同一個
        self.clock: SegmentClock = SegmentClock(now_provider, sleep)
        # 風控事件的唯一寫入口。**自建而不是注入**：本類已經持有
        # `(dao, run_id, now_provider)` 三件組，注入只是把同一組東西再傳一次，
        # 卻要改動建構子簽名與每一個建這個類別的地方
        self.events: RiskEventLogger = RiskEventLogger(
            self.dao, self.run_id, now_provider=self.clock.now
        )
        self.backfiller: FillBackfiller = FillBackfiller(
            order_manager, account_sync, dao, run_id, now_provider, notifier
        )

        # 最後一次對帳結果。**要留給呼叫端**：對帳不一致不拋例外（它只降級），
        # 而排程需要一個退出碼才分得出「今天剛出事」與「跑完了」
        self.last_reconcile: Optional[Any] = None

        # 盤後作業。**獨立一個類別**：它與送單段落沒有共用狀態，
        # 自己 connect／close，實盤入口（`apps/live.py`）也是走完全獨立的分支
        self.after_close: AfterCloseRunner = AfterCloseRunner(
            data_feeds=[context.data_feed for context in contexts],
            broker=broker,
            order_manager=order_manager,
            account_sync=account_sync,
            reconciler=reconciler,
            reporter=self.reporter,
            mode_state=mode_state,
            dao=dao,
            run_id=run_id,
            notifier=notifier,
            now_provider=now_provider,
            parity_checker=parity_checker,
            cost_estimator=cost_estimator,
        )

        # 券商連線的降級、重連與恢復；重連後的對帳結果回填到 `last_reconcile`
        self.connection: ConnectionSupervisor = ConnectionSupervisor(
            contexts=self.contexts,
            broker=broker,
            order_manager=order_manager,
            risk_manager=risk_manager,
            reconciler=reconciler,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            on_reconciled=self._store_reconcile,
        )

        # 送單管線與成交回報；資金保留的帳在它身上
        self.execution: ExecutionEngine = ExecutionEngine(
            order_manager=order_manager,
            risk_manager=risk_manager,
            allocator=allocator,
            account_sync=account_sync,
            dao=dao,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            apply_broker_degrades=self.connection.apply_broker_degrades,
            margin_query=margin_query,
            simulation=simulation,
        )

        # 帳戶層的錢：總權益口徑、額度刷新、單日虧損檢查
        self.account_monitor: AccountMonitor = AccountMonitor(
            contexts=self.contexts,
            broker=broker,
            order_manager=order_manager,
            allocator=allocator,
            risk_manager=risk_manager,
            clock=self.clock,
            fetch_account=fetch_account,
            simulation=simulation,
        )

        # 跨策略守門與兩層批次曝險截斷
        self.cross_checks: CrossStrategyChecks = CrossStrategyChecks(
            contexts=self.contexts,
            conflict_guard=conflict_guard,
            risk_manager=risk_manager,
            account_equity=self.account_monitor.account_equity,
        )

        # 策略訊號 → 經過前處理的委託；鉤子失敗時把該策略降到 HALTED
        self.signals: SignalCollector = SignalCollector(
            mode_state=mode_state,
            order_manager=order_manager,
            risk_manager=risk_manager,
            allocator=allocator,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            forget_reservations=self.execution.forget_reservations,
        )

        # 盤中逐筆段落：迴圈、心跳、日終回補與收線
        self.intraday: IntradaySession = IntradaySession(
            contexts=self.contexts,
            broker=broker,
            order_manager=order_manager,
            account_sync=account_sync,
            risk_manager=risk_manager,
            mode_state=mode_state,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            session_guard=SessionGuard(now_provider),
            signals=self.signals,
            cross_checks=self.cross_checks,
            execution=self.execution,
            connection=self.connection,
        )

        # 開盤段補平昨日的跨日待辦
        self.pending_actions: PendingActionsRunner = PendingActionsRunner(
            contexts=self.contexts,
            dao=dao,
            ledger=ledger,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            dispatch=self.execution.dispatch,
        )

        # 期貨換月：送單與收回報走引擎的同一條管線
        self.rolls: RolloverExecutor = RolloverExecutor(
            contexts=self.contexts,
            mode_state=mode_state,
            order_manager=order_manager,
            events=self.events,
            notifier=notifier,
            clock=self.clock,
            dispatch=self.execution.dispatch,
            apply_new_fills=self.execution.apply_new_fills,
        )

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

        error: Optional[BaseException] = None
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

            # 逐筆段落走事件迴圈，其餘走「收訊號 → 送單」一次性流程。
            # **分派寫在這裡而不是讓兩條流程互相呼叫**：它們的生命週期不同，
            # 一個跑一次就結束、一個要跑到收線
            if timing is ExecutionTiming.IMMEDIATE:
                self.run_intraday(window)
            else:
                self.submit_segment(timing, window)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.finish(window, aborted=error is not None)
            self.record_finish(error)

    def prepare(self) -> None:
        """
        - Description:
            段落開始前的五件事：連線、讀回模式、接管委託、重建帳戶、對帳

            **順序固定**。接管委託要在重建帳戶之前（未終結的委託會影響部位），
            對帳要在最後（前面幾步都做完才知道本地到底是什麼狀態）。
        """

        self.broker.connect()
        self.fill_default_symbols()

        # **先標記崩潰的舊紀錄再讀模式**：`get_last_account_mode()` 只讀已結束的
        # 那些，不先標記就會跳過上次崩潰的那一列，等於把它的降級狀態擦掉
        self.mark_previous_crash()

        # 人工恢復由 `load()` 一併套用（**只能由命令列旗標觸發**，程式不自動呼叫）：
        # 讀回與恢復的先後有相依，綁在一起才不會有呼叫端漏掉其中一半
        self.mode_state.load()

        verify_strategies([context.strategy for context in self.contexts])

        today: datetime.date = self.clock.now().date()

        # 休市日不進送單路徑；判不出來一律拒絕啟動（`is_market_open()` 會拋）
        self.is_trading_day = self._verify_trading_day(today)
        if not self.is_trading_day:
            return

        # 歷史資料沒更新到前一個交易日就拒絕啟動：策略會拿舊資料算出訊號，
        # 而那條路徑不會有任何錯誤
        for context in self.contexts:
            context.data_feed.verify_data_freshness(today)

        self.order_manager.recover(today)
        self.account_monitor.preload_daily_amounts(today)
        # 接管回來的委託可能已有成交寫在 `live_fill`（另一個行程先收到回報、或本行程
        # 送單後死掉沒套進）。**先依成交事實補進委託與歸屬帳，再以歸屬帳重建部位**：
        # 順序反了，這些成交會被重建收進 `__unattributed__`——對帳一致，策略卻失去那口部位
        self.backfiller.backfill(today)

        positions: List[Any] = self.broker.get_positions()
        self.account_sync.rebuild_from_broker(positions)
        self.account_monitor.reset_local_balances()
        self.account_monitor.refresh_capital()

        # 額度總量要在有帳務之後才驗得動（`build_live_trader()` 當下還沒連線），
        # 且要在對帳之前——超配就不該讓這個段落繼續往下走
        if self.account_monitor.uses_virtual_capital():
            # **略過而不是捏一個數字讓它通過**：這道檢查問的是「帳戶撐不撐得住
            # 這些額度」，而模擬環境的帳務本來就是假的（見 `uses_virtual_capital()`）。
            # 以 Σ 宣告額度當基準的話，檢查會變成「Σ 額度 ≤ Σ 額度 × 安全係數」，
            # 反而必然不成立
            logger.warning(
                "模擬環境的帳務不代表真實資金，以宣告額度為虛擬資金，**略過額度總量檢查**。"
                "正式環境不走這條路：資金不足會拒絕啟動"
            )
        else:
            self.allocator.verify_quota(self.account_monitor.account_equity())
        self.account_monitor.check_daily_loss()

        self.last_reconcile = self.reconciler.check(positions)

    def resync_from_broker(self, confirm: bool) -> ResyncPlan:
        """
        - Description:
            以券商部位重建歸屬帳（人工觸發的獨立作業，不跑任何段落）

            **預設只列計畫**：`confirm` 為 False 時算完就結束，歸屬帳一筆都不動；
            人看過計畫、確認要以券商為準，才帶 `confirm` 再跑一次。
            寫入後立刻重建帳戶並對帳，結果留在 `last_reconcile`，
            不一致時照常降級——重建沒有對齊就不該讓下一個段落開新倉。

            **不恢復交易模式**：先前對帳不一致留下的降級，仍要人確認重建結果後
            以 `--resume-trading` 解除。重建與恢復分成兩步，才有機會看一眼結果。

            **有未終結的委託就拒絕**：那些委託隨時可能成交，券商部位還在變，
            這時算出來的差額下一秒就不對了。先跑完盤後作業再重建。
        - Parameters:
            - confirm: bool
                是否寫入
        - Return:
            - ResyncPlan
                重建計畫
        - Raise:
            - ResyncRefusedError
                歸屬帳已損壞（同一標的有兩個持有者），或仍有未終結的委託
        """

        error: Optional[BaseException] = None
        try:
            self.broker.connect()

            # 與 `prepare()` 同一個順序：先標崩潰再讀模式。**模式一定要讀回來**——
            # 本次結束時會把帳戶層模式寫進 `live_run`，沒讀的話寫進去的是預設的
            # NORMAL，等於用一次重建把先前的降級擦掉
            self.mark_previous_crash()
            self.mode_state.load()

            today: datetime.date = self.clock.now().date()
            unfinished: List[Dict[str, Any]] = self.dao.get_unfinished_orders(today)
            if unfinished:
                raise ResyncRefusedError(
                    f"今天還有 {len(unfinished)} 張未終結的委託，券商部位仍可能變動；"
                    "請先跑完盤後作業（--phase after_close）再重建"
                )

            positions: List[Any] = self.broker.get_positions()
            plan: ResyncPlan = plan_resync(positions, self.dao.get_open_lots())
            for line in plan.describe():
                logger.warning(f"[Resync] {line}")

            if plan.is_refused:
                raise ResyncRefusedError("；".join(plan.describe()))
            if not confirm or not plan.actions:
                return plan

            apply_resync(self.ledger, plan, self.run_id, self.clock.now)
            self.account_sync.rebuild_from_broker(positions)
            self.last_reconcile = self.reconciler.check(positions)
            return plan
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.broker.close()
            for context in self.contexts:
                context.data_feed.close()
            self.record_finish(error)

    def fill_default_symbols(self) -> None:
        """
        - Description:
            券商登入後替沒有標的池的策略補上預設標的，並更新 `context.symbols`

            組裝時資料源的 `setup()` 就補過一次，但那時券商還沒登入：要靠券商查的
            標的池（期貨的掛牌月份）在那裡一定補不到，`context.symbols` 抄到的是空清單，
            之後每個段落都以空清單取報價、鉤子照常被呼叫卻永遠沒有訊號，**不會有任何錯誤**。

            補完仍是空的就寫 CRITICAL 事件並推播：沒有標的的策略等於沒有在跑。
        """

        for context in self.contexts:
            if context.symbols:
                continue

            context.data_feed.fill_default_symbols_after_connect(context.strategy)
            context.symbols = list(getattr(context.strategy, "symbols", []) or [])
            if context.symbols:
                continue

            message: str = (
                f"{context.name} 在券商登入後仍組不出標的池："
                "本段落取不到任何報價，不會產生任何訊號"
            )
            logger.error(message)
            self.events.write(
                category="UNIVERSE_EMPTY",
                severity=NotifyLevel.CRITICAL,
                message=message,
                strategy_name=context.name,
            )
            self._notify(NotifyLevel.CRITICAL, "標的池為空", message)

    def mark_previous_crash(self) -> List[str]:
        """
        - Description:
            把上次沒有正常結束的紀錄標記起來並告警

            `ended_at IS NULL` 只有兩種可能：正在跑的這一次，或是上次崩潰了。
            **崩潰要當成事件而不是沉默的常態**——沒有人知道上次是怎麼停的，
            部位與委託就都處在未確認的狀態。
        - Return:
            - List[str]
                被標記的 `run_id`
        """

        crashed: List[str] = self.dao.mark_crashed_runs(self.run_id, self.clock.now())
        if not crashed:
            return []

        message: str = (
            f"上次執行沒有正常結束（{', '.join(crashed)}），"
            "已標記為 CRASHED；本次將以券商為準重建部位並對帳"
        )
        logger.error(message)
        self._notify(NotifyLevel.CRITICAL, "偵測到上次崩潰", message)
        return crashed

    def _store_reconcile(self, result: Any) -> None:
        """重連後的對帳結果回填到 `last_reconcile`：退出碼由最後一次對帳結果決定"""

        self.last_reconcile = result

    # === 盤中逐筆 ===
    def run_intraday(self, window: Optional[SegmentWindow]) -> LoopStats:
        """
        - Description:
            盤中逐筆段落：等到時窗再進入迴圈；迴圈本身在 `IntradaySession`
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗；None 時不設收線時間
        - Return:
            - LoopStats
                本段的事件統計
        """

        # 開盤段與尾盤段是另外的行程、共用同一個帳號：時窗開始前不連上場，
        # 否則兩個行程同時在場上，各自都會收到對方的委託回報
        self._wait_until_submit_window(window)
        return self.intraday.run(window)

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

        # 等不到時窗就整段不送：補平、換月與策略訊號都在時窗之外送出會與回測分岔
        if not self._wait_until_submit_window(window):
            return

        # **開盤段的第一件事是補平昨天沒成交的平倉單**，排在任何新訊號之前：
        # 那是預期外的隔夜部位，多留一分鐘就多一分鐘的曝險
        if timing is ExecutionTiming.AT_OPEN:
            self.pending_actions.apply()

        # **尾盤段先換月、再收新訊號**：策略挑合約與轉倉用同一份換月規則，
        # 今天該換的部位先換過去，策略看到的持倉才與它要交易的契約一致
        if timing is ExecutionTiming.AT_CLOSE:
            self.rolls.execute(window)

        candidates: List[Tuple[StrategyContext, BaseOrder]] = []
        for context in self.contexts:
            candidates.extend(self.signals.collect_orders(context, timing))

        if not candidates:
            logger.info("本段落沒有任何委託")
            return

        survivors: List[Tuple[StrategyContext, BaseOrder]] = self.cross_checks.apply(
            candidates
        )
        # 策略自己產生的出場單沒送出時要排次日補平；其他送單路徑各有自己的失敗處理
        self.execution.dispatch(survivors, window, cover_unsent_exits=True)

    # === 收尾 ===
    def finish(self, window: Optional[SegmentWindow], aborted: bool = False) -> None:
        """
        - Description:
            段落收尾：收回報到收線 → 撤未成交單 → 收撤單回報 → 寫快照、關連線

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

        # 段落中途發生、還沒被送單或心跳取走的降級事件，在寫結束紀錄之前落地
        self.connection.apply_broker_degrades()

        try:
            self.execution.settle(window, aborted)
            self.write_account_snapshots()
        except Exception as exc:
            logger.opt(exception=True).error(f"段落收尾失敗：{exc}")
        finally:
            self.broker.close()
            for context in self.contexts:
                context.data_feed.close()
            logger.info(f"=== 段落結束（run_id={self.run_id}）===")

    def write_account_snapshots(self) -> None:
        """
        寫入各策略的**帳務**快照；一致與否都要寫

        **不是部位快照**——部位快照由 `Reconciler._write_snapshots()` 寫。
        兩者寫的是不同的表，名字混在一起會讓人以為這裡已經記了部位。
        """

        today: datetime.date = self.clock.now().date()
        for context in self.contexts:
            self.dao.upsert_account_snapshot(
                {
                    "date": today,
                    "strategy_name": context.name,
                    "source": SOURCE_LOCAL,
                    "available_balance": context.account.balance,
                    "total_equity": context.account.balance + context.position_value(),
                }
            )
        self.dao.commit()

    # === 盤後 ===
    def run_after_close(self) -> Dict[str, Any]:
        """
        - Description:
            盤後作業；實作在 `AfterCloseRunner`

            **保留這個方法而不是讓實盤入口（`apps/live.py`）直接拿 runner**：退出碼由
            `trader.last_reconcile` 決定，盤後的對帳結果要回填回來。
        - Return:
            - Dict[str, Any]
                本次盤後作業的摘要（報表路徑、殘量筆數、滑價統計）
        """

        try:
            summary: Dict[str, Any] = self.after_close.run()
        except BaseException as exc:
            self.record_finish(exc)
            raise
        self.last_reconcile = self.after_close.last_reconcile
        self.record_finish(None)
        return summary

    def run_parity(self, run_date: Optional[datetime.date] = None) -> int:
        """
        - Description:
            補比某一交易日的訊號 parity；預設為歷史資料最新的那一天（前一交易日）

            **在次日資料更新之後跑**：盤後當下當天的日 K 還沒入庫，當日回測產生不出
            任何委託，比了也只會把實盤的開倉單全歸成快照口徑差異。

            **不連券商**：比對只讀紀錄庫與歷史資料。交易日判定少了券商合約檔這個佐證，
            新鮮度檢查只擋得住官方日曆確定的缺漏——寧可漏報，也不要為了比對去登入。

            先做與段落相同的新鮮度檢查：資料沒更新到前一交易日時拋出，
            不比一份會把開倉單全判成差異的結果。
        - Parameters:
            - run_date: Optional[datetime.date]
                要比對的交易日；None 時取歷史資料最新日。指定時不可晚於資料最新日
        - Return:
            - int
                未解釋差異的筆數
        - Raise:
            - DataFreshnessError
                資料沒有更新到前一交易日，或指定的日期還沒有資料
        """

        error: Optional[BaseException] = None
        try:
            target: datetime.date = self._resolve_parity_date(run_date)
            logger.info(f"=== 訊號 parity 補比 {target}（run_id={self.run_id}）===")
            return self.after_close.check_signal_parity(target)
        except BaseException as exc:
            error = exc
            raise
        finally:
            for context in self.contexts:
                context.data_feed.close()
            self.record_finish(error)

    def _resolve_parity_date(self, run_date: Optional[datetime.date]) -> datetime.date:
        """
        補比的交易日：確認那一天的日 K 已經入庫

        **未指定日期時先過新鮮度檢查**：排程補比的對象是前一交易日，資料沒更新到那裡
        就不比。**指定日期時只要那一天有資料**：回頭補比過去某天時，資料庫只會更新到
        更早的日子（例如半夜補比前天，昨天的資料清晨才入庫），要求「更新到前一交易日」
        只會把合法的補比擋掉。

        各策略的資料源取**最早**的最新日：同一個行程的策略共用一個市場，
        但只要有一個還沒更新，那一天就不能拿來比。
        """

        today: datetime.date = self.clock.now().date()
        latest_dates: List[datetime.date] = []
        for context in self.contexts:
            if run_date is None:
                context.data_feed.verify_data_freshness(today)
            latest: Optional[datetime.date] = context.data_feed.get_latest_data_date()
            if latest is not None:
                latest_dates.append(latest)

        available: Optional[datetime.date] = min(latest_dates, default=None)
        target: Optional[datetime.date] = run_date or available
        if target is None or available is None or target > available:
            raise DataFreshnessError(
                f"歷史資料最新日為 {available}，沒有 {target} 的日 K，無法比對"
            )
        return target

    def resolve_end_reason(self, error: Optional[BaseException]) -> str:
        """
        - Description:
            決定本次執行寫進 `live_run` 的結束原因

            判定順序與實盤入口（`apps/live.py`）的退出碼一致（例外 → kill switch → 對帳不一致 →
            帳戶層非 NORMAL）：存活監控只看這一欄決定要不要推播，
            退出碼非 0 的執行在這裡卻寫成正常結束，推播就會漏掉。
            非交易日與只跑對帳的段落算正常結束——那是預期中的行為。
        - Parameters:
            - error: Optional[BaseException]
                中止本次執行的例外；正常跑完為 None
        - Return:
            - str
                結束原因
        """

        if error is not None:
            return f"例外中止：{type(error).__name__}: {error}"
        if self.risk_manager.is_kill_switch_on():
            return "kill switch"
        if self.last_reconcile is not None and not self.last_reconcile.is_consistent:
            return "對帳不一致"
        mode: TradingMode = self.mode_state.account_mode
        if mode is not TradingMode.NORMAL:
            return f"帳戶層交易模式 {mode.value}"
        return LiveTradeDAO.END_REASON_NORMAL

    def record_finish(self, error: Optional[BaseException]) -> None:
        """
        - Description:
            把本次執行的結束時間、原因與帳戶層模式寫進 `live_run`

            沒有這一筆，`ended_at` 會一直是 NULL，要等下一次啟動才被標成崩潰——
            正常結束與崩潰分不出來，存活監控會對每個跑完的段落報「沒有結束紀錄」。
            行程被直接殺掉時這裡跑不到，那種情況仍由下一次啟動的
            `mark_crashed_runs()` 補標。

            **本身失敗只記 log 不往外拋**：它在 `finally` 裡執行，
            拋出去會蓋掉真正讓段落中止的那個例外。
        - Parameters:
            - error: Optional[BaseException]
                中止本次執行的例外；正常跑完為 None
        """

        try:
            end_reason: str = self.resolve_end_reason(error)
            self.dao.finish_run(
                self.run_id,
                self.clock.now(),
                end_reason,
                self.mode_state.account_mode.value,
            )
        except Exception as exc:
            logger.opt(exception=True).error(f"寫入段落結束紀錄失敗：{exc}")

    def _notify(self, level: NotifyLevel, title: str, body: str) -> None:
        """
        推播；失敗一律吞掉，監控不可拖垮被監控的東西

        **收 `NotifyLevel` 而不是字串**：等級的唯一來源是
        `live_risk_event.severity`，而字串可以寫成 `WARNING` 這種不在值域內的
        拼法，`notify_safely()` 會把它拋出的例外一併吞掉——
        事件有紀錄、通知卻送不出去。
        """

        notify_safely(self.notifier, level, title, body)

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
        **刻意不接住**：官方開休市日曆的年度未入庫、或與券商合約檔衝突時就會拋，
        這時候預設為開市等於在休市日照常送單。
        """

        for context in self.contexts:
            if not context.data_feed.is_market_open(today):
                logger.warning(f"{context.name} 的資料源判定 {today} 非交易日")
                return False
        return True

    def _wait_until_submit_window(self, window: Optional[SegmentWindow]) -> bool:
        """
        - Description:
            等到可以送單的時刻；等不到就寫事件、推播，並回 False

            **提早送出的代價很具體**：尾盤段若在時窗之前送出限價單，它會在逐筆交易
            時段就成交，成交價不是收盤價——與回測的假設對不上，而且看起來完全正常。
            所以逾時不是「記一行就繼續」：呼叫端要照回傳值整段放棄送單。
        - Parameters:
            - window: Optional[SegmentWindow]
                段落時窗；None 時不做時限控制、視為已到
        - Return:
            - bool
                是否到達送單時窗
        """

        if window is None:
            return True

        cap: float = self.clock.wait_cap_seconds(window.submit_start)
        waited: float = 0.0
        while self.clock.now().time() < window.submit_start:
            if waited >= cap:
                message: str = (
                    f"等待送單時窗（{window.submit_start}）已達安全上限 {cap:.0f} 秒，"
                    f"時鐘仍停在 {self.clock.now().time()}；本段落放棄送單，請確認系統時鐘是否正常"
                )
                logger.error(message)
                self.events.write(
                    category=event_categories.SUBMIT_WINDOW_MISSED,
                    severity=NotifyLevel.CRITICAL,
                    message=message,
                )
                self._notify(NotifyLevel.CRITICAL, "等不到送單時窗", message)
                return False

            self.clock.sleep(self.clock.POLL_INTERVAL_SECONDS)
            waited += self.clock.POLL_INTERVAL_SECONDS
        return True
