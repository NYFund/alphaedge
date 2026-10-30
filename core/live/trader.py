import datetime
import time
from dataclasses import replace
from functools import partial
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from loguru import logger

from core.broker.base import BaseBroker
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
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
from core.live.execution.base import (
    mark_execution,
)
from core.live.execution_engine import ExecutionEngine
from core.live.fill_backfill import FillBackfiller
from core.live.intraday.event_loop import IntradayEventLoop, LoopStats
from core.live.intraday.session_guard import SessionGuard, stops_after_cover
from core.live.notify.base import NotifyLevel, notify_safely
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
from core.live.signals import SignalCollector, quote_prices_of
from core.live.strategy_guard import verify_strategies
from core.market.segment import SegmentSchedule, SegmentWindow, resolve_window
from core.models import (
    BaseAccount,
    BaseOrder,
    BaseQuote,
    BrokerAccountSnapshot,
    ExecutionReport,
    PendingAction,
)
from core.portfolio import order_rules
from core.utils import (
    DayTradeUncoveredPolicy,
    ExecutionStyle,
    ExecutionTiming,
    LiveOrderStatus,
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
        # 是否連模擬環境。**預設 False（正式環境的嚴格行為）**：券商查不到
        # 期貨保證金時，模擬環境略過帳戶層檢查、正式環境擋單
        self.simulation: bool = simulation
        # 取帳務快照的方式；`None` 時退回 `broker.get_account()`。
        # **由組裝層注入**：股票與期貨是兩個子帳戶，「這一批策略該看哪一筆」
        # 是商品語意，而本檔不出現任何商品字樣（見模組說明）
        self.fetch_account: Optional[Callable[[], BrokerAccountSnapshot]] = (
            fetch_account
        )
        # 最近一次刷新的券商帳務快照；`_account_equity()` 由它取總權益。
        # **不存進 `CapitalAllocator`**：總權益是券商事實，而 allocator 管的是
        # 額度與保留，兩件事放一起會讓「這個數字是誰說的」失去單一出處
        self.account_snapshot: Optional[BrokerAccountSnapshot] = None
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

        # 跨策略守門與兩層批次曝險截斷
        self.cross_checks: CrossStrategyChecks = CrossStrategyChecks(
            contexts=self.contexts,
            conflict_guard=conflict_guard,
            risk_manager=risk_manager,
            account_equity=self._account_equity,
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

        # 日終強制動作（當沖回補）
        self.session_guard: SessionGuard = SessionGuard(now_provider)
        # 當天已送出回補單的 `(交易日, 策略, 標的)`。重試時要跳過它們：
        # 已送出但還沒成交的回補單，部位仍然算「未回補」，再送一次就是超買
        self._cover_sent: Set[Tuple[datetime.date, str, str]] = set()
        # 各交易日回補失敗的次數
        self._cover_attempts: Dict[datetime.date, int] = {}
        # 每檔最後一筆報價；回補單取價用
        self._last_quotes: Dict[str, BaseQuote] = {}
        # 盤中段落各策略的標的池集合（`run_intraday()` 開始時建立）
        self._declared_symbols: Dict[str, FrozenSet[str]] = {}

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
        self._preload_daily_amounts(today)
        # 接管回來的委託可能已有成交寫在 `live_fill`（另一個行程先收到回報、或本行程
        # 送單後死掉沒套進）。**先依成交事實補進委託與歸屬帳，再以歸屬帳重建部位**：
        # 順序反了，這些成交會被重建收進 `__unattributed__`——對帳一致，策略卻失去那口部位
        self.backfiller.backfill(today)

        positions: List[Any] = self.broker.get_positions()
        self.account_sync.rebuild_from_broker(positions)
        self._reset_local_balances()
        self._refresh_capital()

        # 額度總量要在有帳務之後才驗得動（`build_live_trader()` 當下還沒連線），
        # 且要在對帳之前——超配就不該讓這個段落繼續往下走
        if self.uses_virtual_capital():
            # **略過而不是捏一個數字讓它通過**：這道檢查問的是「帳戶撐不撐得住
            # 這些額度」，而模擬環境的帳務本來就是假的（見 `uses_virtual_capital()`）。
            # 以 Σ 宣告額度當基準的話，檢查會變成「Σ 額度 ≤ Σ 額度 × 安全係數」，
            # 反而必然不成立
            logger.warning(
                "模擬環境的帳務不代表真實資金，以宣告額度為虛擬資金，**略過額度總量檢查**。"
                "正式環境不走這條路：資金不足會拒絕啟動"
            )
        else:
            self.allocator.verify_quota(self._account_equity())
        self._check_daily_loss()

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

        # 開盤段與尾盤段是另外的行程、共用同一個帳號：時窗開始前不連上場，
        # 否則兩個行程同時在場上，各自都會收到對方的委託回報
        self._wait_until_submit_window(window)

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
                self._notify(NotifyLevel.WARN, "當沖回補政策在實盤被改寫", warning)

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
        self._notify(NotifyLevel.CRITICAL, "當沖回補失敗", message)

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
        self._notify(NotifyLevel.CRITICAL, "行情中斷", reason)

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
            self.apply_pending_actions()

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

    def apply_pending_actions(self) -> int:
        """
        - Description:
            執行到期的跨日待辦（次日開盤段的第一件事）

            **數量截到目前還持有的量**：待辦寫下之後部位可能已經變了——遲到的成交、
            人工在券商端平倉、重建後部位已不在。照原數量送出的話，
            多出來的部分不是「多平一點」，是直接把部位做反。持有量為 0 就標 `DONE`
            並記一筆事件，不送單。

            **同一個部位的多筆待辦要扣掉前面已送出的量**：出場單連兩天沒成交時，
            盤後會對「補平單殘量」與「策略出場單殘量」各寫一筆待辦。開盤段 08:30 送單、
            09:00 才撮合，逐筆只看歸屬帳的話兩筆都會截到「持有 N」而送出 2N——
            本段落已送出、尚未撮合的補平量也是在途的出場，第二筆要先扣掉它。

            **送單走 `dispatch()`**：與一般委託同一條管線（資金保留、事前風控、
            送出後消化回報），不另外直接呼叫 OMS。

            成功送出才標 `DONE`；送不出去就**留在 `PENDING` 並把到期日滾到次日**，
            同時再推播一次——一張補不成的平倉單不會因為換了一天就變得不重要。
        - Return:
            - int
                實際送出的補平單數
        """

        today: datetime.date = self.clock.now().date()
        pending: List[PendingAction] = self._load_pending_actions(today)
        if not pending:
            return 0

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        submitted: int = 0
        # 本段落已送出的補平量，以（策略, 標的, 方向）累計
        in_flight: Dict[Tuple[str, str, str], int] = {}

        for action in pending:
            context: Optional[StrategyContext] = by_name.get(action.strategy_name)
            if context is None:
                logger.warning(
                    f"待辦 {action.action_id} 的策略未在本次啟動的清單中，本日略過"
                )
                continue

            key: Tuple[str, str, str] = (
                context.name,
                action.symbol,
                action.position_type or "",
            )
            volume: Optional[int] = self._pending_volume_to_cover(
                context, action, in_flight.get(key, 0)
            )
            if volume is None:
                continue

            order: Optional[BaseOrder] = self._build_cover_order(
                context, replace(action, volume=volume)
            )
            if order is None:
                continue
            # 補平是系統產生的出場單，一律要成交；只在開盤段執行
            mark_execution([order], ExecutionTiming.AT_OPEN, ExecutionStyle.MARKET)

            if self.execution.dispatch([(context, order)], None):
                logger.error(f"補平單未送出，待辦保留：{action.action_id}")
                self.dao.postpone_pending_action(
                    action.action_id, today + datetime.timedelta(days=1)
                )
                self._notify(
                    NotifyLevel.CRITICAL,
                    "補平單送出失敗",
                    f"{action.symbol} 殘量 {volume} 仍未平掉",
                )
                continue

            self.dao.resolve_pending_action(
                action.action_id, self.dao.ACTION_DONE, self.clock.now()
            )
            in_flight[key] = in_flight.get(key, 0) + volume
            submitted += 1

        return submitted

    def _load_pending_actions(self, today: datetime.date) -> List[PendingAction]:
        """
        讀出到期待辦並轉成領域物件

        **一列轉不出來只丟那一列**：這是開盤段的第一件事，而每一筆待辦都是
        一個未平掉的隔夜部位。讓例外往上拋的話，一列壞資料會讓**所有**待辦
        都補不成，而那正是最需要它們動起來的時候。
        """

        actions: List[PendingAction] = []
        for row in self.dao.get_pending_actions(today):
            try:
                actions.append(PendingAction.from_row(row))
            except (KeyError, TypeError, ValueError) as exc:
                logger.error(
                    f"待辦 {row.get('action_id', '?')} 的欄位無法解讀（{exc}），"
                    "本筆略過，需人工處理"
                )
        return actions

    def _pending_volume_to_cover(
        self, context: StrategyContext, action: PendingAction, in_flight: int = 0
    ) -> Optional[int]:
        """
        - Description:
            待辦實際要補平的數量：待辦數量與「歸屬帳持有量 − 本段落已送出的補平量」取小

            持有量為 0 時把待辦標成 `DONE` 並寫事件；數量被截短時也寫事件——
            兩者都代表「寫待辦之後部位變了」或「前一筆待辦已經在平它」，
            事後要查得到是怎麼截的。待辦沒有部位方向時無從比對，留給人工。
        - Parameters:
            - context: StrategyContext
                待辦所屬的策略
            - action: PendingAction
                待辦內容
            - in_flight: int
                本段落已對同一個（標的, 方向）送出、尚未撮合的補平量
        - Return:
            - Optional[int]
                要送出的數量；None 表示本筆不送
        """

        action_id: str = action.action_id
        symbol: str = action.symbol
        wanted: int = action.volume
        direction: str = action.position_type or ""
        if not direction:
            logger.warning(f"待辦 {action_id} 沒有部位方向，無法比對持有量，需人工處理")
            return None

        held: int = self.ledger.get_strategy_positions(context.name).get(
            (symbol, direction), 0
        )
        available: int = max(held - in_flight, 0)
        volume: int = min(wanted, available)
        if volume == wanted:
            return volume

        message: str = (
            f"待辦 {action_id} 要補平 {symbol} {direction} {wanted}，"
            f"歸屬帳目前持有 {held}"
            + (f"、本段落已送出補平 {in_flight}" if in_flight else "")
            + "，"
            + ("已無可平部位，不送單" if volume <= 0 else f"改送 {volume}")
        )
        logger.warning(message)
        self.events.write(
            category="PENDING_ACTION_TRUNCATED",
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=symbol,
        )
        if volume <= 0:
            self.dao.resolve_pending_action(
                action_id, self.dao.ACTION_DONE, self.clock.now()
            )
            return None
        return volume

    def _build_cover_order(
        self, context: StrategyContext, action: PendingAction
    ) -> Optional[BaseOrder]:
        """
        - Description:
            由待辦組出補平單：策略有實作 `build_cover_order()` 就用策略的，否則由引擎組

            **引擎預設組**：補平是系統產生的出場單，價格類型由執行層依 `MARKET` 換算，
            訂單型別走依市場注入的 `context.build_filled_order`（與當沖回補同一個），
            引擎本體因此不必知道任何市場特性。只靠策略組的話，沒實作的策略
            出場殘量會一路留到人工處理——「出場必補」等於沒有執行端。

            決策價取**合約參考價**：開盤段還沒有成交，參考價是唯一的公告價格。
            取不到就不送，待辦留在 `PENDING`，並推播讓人知道。

            **傳給策略的是 `PendingAction` 而不是資料列**：這個參數會落到策略作者手上，
            傳 dict 等於把紀錄庫的 schema 變成策略層的公開契約——
            改一個欄位名就會無聲地弄壞每一支策略。
        - Parameters:
            - context: StrategyContext
                待辦所屬的策略
            - action: PendingAction
                待辦（數量已截到目前持有量，`position_type` 已確認有值）
        - Return:
            - Optional[BaseOrder]
                補平單；組不出來時為 None
        """

        builder: Optional[Callable[..., BaseOrder]] = getattr(
            context.strategy, "build_cover_order", None
        )
        if builder is not None:
            try:
                return builder(action)
            except Exception as exc:
                logger.opt(exception=True).error(f"組補平單失敗：{exc}")
                return None

        reason: Optional[str] = None
        reference: Optional[float] = None
        if context.build_filled_order is None or action.position_type is None:
            reason = "沒有可用的訂單建構器或部位方向"
        else:
            reference = context.data_feed.get_reference_price(action.symbol)
            if not reference:
                reason = "取不到合約參考價"

        if reason is not None or reference is None:
            message: str = (
                f"待辦 {action.action_id}（{action.symbol}）無法組出補平單：{reason}，"
                "待辦保留，需人工確認"
            )
            logger.error(message)
            self._notify(NotifyLevel.CRITICAL, "補平單無法組出", message)
            return None

        return context.build_filled_order(
            action.symbol,
            self.clock.now(),
            action.action,
            action.position_type,
            float(reference),
            int(action.volume),
        )

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

    def _account_equity(self) -> float:
        """
        - Description:
            策略可用的資金基準；額度檢查、單日虧損與批次曝險共用這一個口徑

            **正式環境：取券商快照的 `total_equity`**，而組裝層注入的帳務查詢已把
            **不屬於任何策略的部位（`__unattributed__`）扣掉**（見 `factory.make_account_fetcher()`）。
            那些部位是接管來的舊持股，策略不能拿它們當額度——算進分母的話，
            一個沒有現金、只有舊持股的帳戶也會通過額度檢查。未交割款仍算在內：
            它是 T+2 後就會入帳的錢，拿掉會讓隔日還有部位時必然誤判成額度超標。

            **模擬環境：一律以 Σ 宣告額度為虛擬資金**（見 `uses_virtual_capital()`）。

            放寬只記 `logger.warning`，不落地成風控事件：這條路每一次模擬都會走，
            每輪都寫一筆 CRITICAL 等於沒有 CRITICAL。
        - Return:
            - float
                資金基準
        """

        if self.uses_virtual_capital():
            return sum(self.allocator.quotas.values())

        return (
            self.account_snapshot.total_equity
            if self.account_snapshot is not None
            else 0.0
        )

    def uses_virtual_capital(self) -> bool:
        """
        - Description:
            是否以宣告額度當作虛擬資金（模擬環境一律是）

            **模擬環境的帳務是假的**（Shioaji 文件〈模擬模式注意事項〉）：`account_balance()`
            一律回 0、交割款與額度查詢回空值，只有持倉與損益走模擬伺服器；
            模擬下單也**不檢查資金**（模擬環境的拒單訊息沒有任何一條是資金不足）。
            照實拿券商的 0 當可用資金的話，額度分配會把每一張買單都擋下來，
            演練永遠驗不到下單路徑；拿模擬持倉的市值當權益，又會把紙上部位當成真錢。
            **正式環境永遠回 False**：那裡的數字就是真的。
        - Return:
            - bool
                模擬環境為 True
        """

        return self.simulation

    def _preload_daily_amounts(self, today: datetime.date) -> None:
        """
        以紀錄庫裡當日已送出的開倉單金額起算風控的單日累計

        三個段落是三個行程、重啟也歸零，單日累計上限要防的正是「重啟反覆送單」。
        被拒與送單失敗的不算（沒有送出去）；出場單不算（上限只管開倉）。
        還原不出訂單的列算不出金額，記 warning 略過——寧可少算也不猜。
        """

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        totals: Dict[str, float] = {name: 0.0 for name in by_name}
        for row in self.order_manager.orders_of_day(today):
            context: Optional[StrategyContext] = by_name.get(
                str(row.get("strategy_name"))
            )
            if context is None or str(row.get("status")) in (
                LiveOrderStatus.REJECTED.value,
                LiveOrderStatus.FAILED.value,
            ):
                continue
            order: Optional[BaseOrder] = self.order_manager.rebuild_order(row)
            if order is None:
                logger.warning(
                    f"委託 {row.get('client_order_id')} 還原不出訂單，當日累計金額少算這一筆"
                )
                continue
            if order_rules.is_closing(order):
                continue
            totals[context.name] += context.notional(order)

        for name, amount in totals.items():
            self.risk_manager.preload_daily_amount(name, amount)
            if amount > 0:
                logger.info(
                    f"{name} 本日啟動前已送出開倉金額 {amount:,.0f}，累計上限由此起算"
                )

    def _reset_local_balances(self) -> None:
        """
        - Description:
            啟動重建後，把各策略本地帳的餘額設為「額度 − 還持有部位的金額」

            重建時為了把部位還原進帳戶，會暫時停用餘額檢查、結束後把餘額設回原值，
            而原值是建帳時的完整額度——**持有部位的成本因此沒有被扣掉**。
            部位管理層會照這個偏大的餘額算張數，再被額度分配整筆拒絕。
            額度本身固定不滾入已實現損益，與額度分配的 `額度 − 已用` 同一個口徑。
        """

        for context in self.contexts:
            quota: float = self.allocator.quotas.get(
                context.name, context.account.balance
            )
            context.account.balance = max(quota - context.position_value(), 0.0)

    def _check_daily_loss(self) -> None:
        """
        段落開始前的虧損檢查：逐策略降級 ＋ 帳戶層降級

        **放開盤前不放段落結束**：開盤前就發現昨天虧太多，這個段落直接不送新倉單；
        放在結束才判等於本段落已經白送一輪。

        **兩層的損益來源不同，狀態也不同**：

        - **帳戶層：取券商端**（已實現 ＋ 未實現）。資料不足時**略過並記 warning**，
          不當成「沒有虧損」。
        - **策略層：目前恆為不觸發，缺的是輸入不是接線。** `prepare()` 走到這裡時，
          帳戶是剛由 `AccountSynchronizer._restore_positions()` 以**原始開倉價**重建的，
          未實現損益因此是 0；而 `OrderManager.recover()` 只接管未終結的委託、
          不回放已成交的回報，本段落之前的已實現損益也不在這個行程的 `trade_records` 裡。

        **策略層刻意留著接線而不是拿掉**：判定與降級的路徑本身是對的，缺的只是損益來源。
        券商只給得出逐標的的合併損益，拆不回策略——要逐策略判定得以歸屬帳的逐策略
        均價自行推算，那是另一步。
        """

        for context in self.contexts:
            self.risk_manager.check_daily_loss(
                context.name,
                self._strategy_loss(context),
                context.capital_limit,
                context.risk_limits,
            )

        account_loss: Optional[float] = self._account_loss()
        if account_loss is None:
            logger.warning(
                "取不到券商端的當日損益，本段落略過帳戶層虧損檢查"
                "（**不是判定為沒有虧損**）"
            )
            return

        self.risk_manager.check_account_daily_loss(account_loss, self._account_equity())

    def _account_loss(self) -> Optional[float]:
        """
        - Description:
            帳戶當日虧損金額（正數表示虧損）；取不到時回 `None`

            **取券商端而不是各策略本地帳的加總**：日頻模式下三個段落是三個獨立
            行程，本地帳每次都由原始開倉價重建，未實現恆為 0、當日已實現也不在
            這個行程裡——加總出來永遠是 0，那道守門等於不存在。

            **取不到一律回 `None`，不可回 0**：0 的語意是「沒有虧損」，
            回 0 會讓這道檢查看起來有在跑、實際永遠不觸發。
            回 `None` 讓呼叫端明確略過並留下紀錄。

            **已實現與未實現分兩個來源**：未實現在帳務快照上（由持倉逐檔加總），
            當日已實現要另外查券商的已平倉交易——快照的 `realized_pnl`
            目前沒有任何券商實作會填，讀它只會拿到 0。
        - Return:
            - Optional[float]
                虧損金額（正數為虧損）；資料不足時為 None
        """

        if self.account_snapshot is None:
            return None

        realized: Optional[float] = self._today_realized_pnl()
        if realized is None:
            return None

        return -(realized + self.account_snapshot.unrealized_pnl)

    def _today_realized_pnl(self) -> Optional[float]:
        """
        當日已實現損益；閘道不提供查詢時回 `None`

        **不把「查不到」當成 0**：那會讓帳戶層的虧損被低估成只剩未實現的部分，
        而當日沖銷完的虧損正好一毛都不算。
        """

        provider: Optional[Callable[..., Any]] = getattr(
            self.broker, "get_realized_trades", None
        )
        if provider is None:
            return None

        try:
            trades: List[Any] = list(provider(self.clock.now().date()))
        except Exception as exc:
            # 帳務查詢失敗不該讓段落起不來；但也不可當成「今天沒有已實現損益」
            logger.opt(exception=True).warning(f"取當日已實現損益失敗：{exc}")
            return None

        return float(sum(trade.pnl for trade in trades))

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

        snapshot: BrokerAccountSnapshot = self._query_account_snapshot()
        used: Dict[str, float] = {
            context.name: context.position_value() for context in self.contexts
        }
        self.account_snapshot = snapshot
        available: float = (
            # 模擬環境的可用現金恆為 0（見 `uses_virtual_capital()`），改以額度扣掉已用
            max(sum(self.allocator.quotas.values()) - sum(used.values()), 0.0)
            if self.uses_virtual_capital()
            else snapshot.available_balance
        )
        self.allocator.refresh(available, used)

    def _query_account_snapshot(self) -> BrokerAccountSnapshot:
        """
        - Description:
            取得本次要當作基準的帳務快照

            **查哪個帳戶由組裝層決定、以 `fetch_account` 注入**：股票與期貨是
            兩個子帳戶，各有各的錢，而「這一批策略該看哪一筆」是商品語意——
            本檔刻意不出現任何商品字樣（見模組說明）。

            未注入時退回券商的預設帳務查詢，維持既有呼叫端的行為。
        - Return:
            - BrokerAccountSnapshot
                帳務快照
        """

        if self.fetch_account is not None:
            return self.fetch_account()
        return self.broker.get_account()

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
