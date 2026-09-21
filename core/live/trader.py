import datetime
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.broker.base import BaseBroker
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution import order_preprocess
from core.live.account_sync import AccountSynchronizer, FilledOrderBuilder
from core.live.after_close import AfterCloseRunner
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.intraday.event_loop import IntradayEventLoop, LoopStats
from core.live.intraday.session_guard import (
    SessionGuard,
    cover_action,
    stops_after_cover,
)
from core.live.notify.base import notify_safely
from core.live.reconciler import Reconciler
from core.live.report.live_reporter import LiveReporter
from core.live.report.parity_checker import ParityChecker
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import ExposureItem, PreTradeRiskManager, RiskDecision
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.segment import SegmentSchedule, SegmentWindow, resolve_window
from core.live.strategy_guard import resolve_hook_timing, verify_strategies
from core.managers.base.position_manager import BasePositionManager
from core.models import BaseAccount, BaseOrder, BaseQuote, ExecutionReport
from core.strategies.base import BaseStrategy
from core.utils import (
    BarExecutionOrder,
    DayTradeUncoveredPolicy,
    ExecutionTiming,
    LiveHook,
)

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
        parity_checker: Optional[ParityChecker] = None,
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

        # 盤後作業。**獨立一個類別**：它與送單段落沒有共用狀態，
        # 自己 connect／close，`run.py` 也是走完全獨立的分支
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
        )

        # 今天是不是交易日；由 `prepare()` 的啟動檢查填入。
        # **預設 True**：只有檢查實際判定為休市才會是 False，
        # 預設 False 會讓沒跑過 `prepare()` 的呼叫端誤以為今天休市
        self.is_trading_day: bool = True

        # 日終強制動作（當沖回補）
        self.session_guard: SessionGuard = SessionGuard(now_provider)
        # 每檔最後一筆報價；回補單取價用
        self._last_quotes: Dict[str, BaseQuote] = {}

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
            self.finish(window)
            self.record_finish(error)

    def prepare(self) -> None:
        """
        - Description:
            段落開始前的五件事：連線、讀回模式、接管委託、重建帳戶、對帳

            **順序固定**。接管委託要在重建帳戶之前（未終結的委託會影響部位），
            對帳要在最後（前面幾步都做完才知道本地到底是什麼狀態）。
        """

        self.broker.connect()

        # **先標記崩潰的舊紀錄再讀模式**：`get_last_account_mode()` 只讀已結束的
        # 那些，不先標記就會跳過上次崩潰的那一列，等於把它的降級狀態擦掉
        self.mark_previous_crash()

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

        crashed: List[str] = self.dao.mark_crashed_runs(self.run_id, self._now())
        if not crashed:
            return []

        message: str = (
            f"上次執行沒有正常結束（{', '.join(crashed)}），"
            "已標記為 CRASHED；本次將以券商為準重建部位並對帳"
        )
        logger.error(message)
        self._notify("CRITICAL", "偵測到上次崩潰", message)
        return crashed

    # === 盤中逐筆 ===
    def run_intraday(self, window: Optional[SegmentWindow]) -> LoopStats:
        """
        - Description:
            盤中逐筆段落：訂閱 → 驅動事件迴圈 → 收線

            **行情與回報共用一個事件 queue**，由 `broker.route_events()` 把兩條
            回呼導過來。導過去之後 `drain_execution_queue()` 會一直是空的，
            那是刻意的——回報已經改由迴圈逐筆消化，兩邊都撈會重複且順序錯亂。

            **逐筆觸發只給宣告了 `is_intraday` 的策略**：其餘策略的鉤子一次要
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
            now_provider=self._now,
            on_heartbeat=self._run_session_guard,
        )

        symbols: List[str] = sorted(
            {symbol for context in self.contexts for symbol in context.symbols}
        )
        self.broker.route_events(loop.submit_quote, loop.submit_execution)
        if symbols:
            self.broker.subscribe_quotes(symbols)

        deadline: datetime.datetime = self._resolve_intraday_deadline(window)
        logger.info(f"盤中逐筆開始，訂閱 {len(symbols)} 檔，收線 {deadline}")

        stats: LoopStats = loop.run_until(deadline)
        logger.info(
            f"盤中逐筆結束：行情 {stats.quotes} 筆、回報 {stats.executions} 筆、"
            f"心跳 {stats.heartbeats} 次、處理失敗 {stats.handler_errors} 筆"
        )
        return stats

    def _resolve_intraday_deadline(
        self, window: Optional[SegmentWindow]
    ) -> datetime.datetime:
        """
        本段落跑到什麼時候

        **沒有時窗時不可以無限跑**：常駐迴圈少了收線時間，盤後作業就永遠不會開始。
        沒給就跑到今天結束。
        """

        today: datetime.date = self._now().date()
        if window is None or window.submit_end is None:
            return datetime.datetime.combine(
                today, datetime.time.max, tzinfo=self._now().tzinfo
            )
        return datetime.datetime.combine(
            today, window.submit_end, tzinfo=self._now().tzinfo
        )

    def _run_session_guard(self) -> None:
        """
        日終強制動作：回補時點一到就把未回補的當沖空單補掉

        **掛在心跳而不是行情上**：行情停了 `on_quote` 就不會再被呼叫，
        而日終回補正是不能因為沒行情就不做的事——現股當沖先賣未回補，
        券商可能標借或直接違約交割。
        """

        if not self.session_guard.should_cover_now():
            return

        self.session_guard.mark_covered()
        stop_after: bool = False

        for context in self.contexts:
            policy: DayTradeUncoveredPolicy = getattr(
                context.strategy,
                "day_trade_uncovered_policy",
                DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE,
            )
            orders, warning = self.session_guard.build_cover_orders(
                context.account,
                policy,
                partial(self._build_cover_order_for, context),
            )

            if warning is not None:
                logger.warning(f"{context.name}：{warning}")
                self._notify("WARNING", "當沖回補政策在實盤被改寫", warning)

            if orders:
                logger.warning(
                    f"{context.name} 有 {len(orders)} 筆當沖空單未回補，送出回補單"
                )
                # **不經跨策略守門**：回補是把自己的部位平掉、不是新開倉，
                # 被守門擋下等於讓部位留倉過夜
                self.dispatch([(context, order) for order in orders], None)

            if stops_after_cover(policy):
                stop_after = True

        if stop_after:
            self.risk_manager.on_degrade_event(
                "當沖未回補且政策為 RAISE：已送出回補單，停止開新倉",
                TradingMode.REDUCE_ONLY,
            )

    def _build_cover_order_for(
        self, context: StrategyContext, position: Any
    ) -> Optional[BaseOrder]:
        """
        把一個待回補部位換成回補單

        **訂單型別走 `context.build_filled_order`**：本檔刻意不出現任何市場或
        商品字樣（`check_layer_deps.py` 會擋），而 Phase7-2 注入的那個建構器
        正是為此存在的。

        價格取**該檔最後一筆報價**；取不到就回 None，由守門記 error。
        """

        quote: Optional[BaseQuote] = self._last_quotes.get(position.symbol)
        if quote is None or context.build_filled_order is None:
            return None

        return context.build_filled_order(
            position.symbol,
            self._now(),
            cover_action(position),
            position.position_type,
            float(quote.cur_price),
            int(position.volume),
        )

    def _on_intraday_quote(self, quote: BaseQuote) -> None:
        """
        一筆行情 → 逐筆觸發策略

        **只餵宣告了 `is_intraday` 的策略**，而且只餵有訂這檔的那些。
        送單前先確認連線：斷線時繼續算訊號只會產生一批送不出去的單。
        """

        # 回補單的價格來源；**只留最後一筆**，留全部等於在記憶體裡重建一份行情庫
        self._last_quotes[quote.symbol] = quote

        if not self.ensure_connected():
            return

        for context in self.contexts:
            if not getattr(context.strategy, "is_intraday", False):
                continue
            if quote.symbol not in context.symbols:
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
            orders: List[BaseOrder] = self._invoke_hooks(
                context, ExecutionTiming.IMMEDIATE, [quote]
            )
        except Exception as exc:
            self._halt_strategy(context, f"逐筆鉤子執行失敗：{exc}")
            return

        if not orders:
            return

        survivors: List[Tuple[StrategyContext, BaseOrder]] = self.apply_cross_checks(
            [(context, order) for order in orders]
        )
        self.dispatch(survivors, None)

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
        self._notify("CRITICAL", "行情中斷", reason)

    def ensure_connected(self) -> bool:
        """
        - Description:
            確認連線還在；斷了就重連並走完恢復流程

            **這是 `ShioajiSession.reconnect()` 唯一的呼叫端**。它連同退避與
            每日登入上限都寫好了，但正式路徑上一個呼叫點都沒有——
            斷線之後程式會一路跑到收盤，每一次送單都失敗。
        - Return:
            - bool
                連線是否可用；False 時呼叫端不可再送單
        """

        if self.broker.is_connected():
            return True

        logger.error("偵測到連線中斷，嘗試重連")
        if not self.broker.reconnect():
            self._notify("CRITICAL", "重連失敗", "已停止送單，請人工確認")
            return False

        self.recover_after_reconnect()
        return True

    def recover_after_reconnect(self) -> None:
        """
        - Description:
            重連成功之後的恢復流程

            **順序固定，而且三件事都做完才可以恢復送單**：
            1. 重新訂閱行情——重連換了一個 session，舊的訂閱一併失效；
               不訂閱的話盤中迴圈收不到任何報價，然後心跳會判成「行情中斷」，
               症狀看起來像券商的問題。
            2. `order_manager.recover()`——斷線期間送出的委託可能已經成交，
               回報卻在斷掉的那條連線上。不接管就會重複下單。
            3. 對帳——前兩步都做完才知道本地到底是什麼狀態。

            **只對帳、不以券商部位重建本地帳**：`rebuild_from_broker()` 是啟動用的，
            它把 lot 逐筆 `open_position()` 回去而不先清空，運行中呼叫一次部位就多一份，
            策略會照兩倍的部位送平倉單。斷線期間漏接的成交由對帳抓出來並降級，
            不在這裡自動補——自動補會把回報漏接這類問題蓋掉。

            **失敗不吞**：恢復沒做完就繼續送單，等於拿一份不知道對不對的部位
            去交易。例外往上拋，由呼叫端決定停或再試。
        """

        logger.warning("重連成功，開始恢復：重新訂閱 → 接管委託 → 對帳")

        symbols: List[str] = sorted(
            {symbol for context in self.contexts for symbol in context.symbols}
        )
        if symbols:
            self.broker.subscribe_quotes(symbols)

        self.order_manager.recover(self._now().date())

        positions: List[Any] = self.broker.get_positions()
        self.last_reconcile = self.reconciler.check(positions)

        logger.info("恢復完成，可以繼續送單")

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
        """方向白名單 ＋ 持倉檔數上限 ＋ 決定性排序；與回測共用同一份實作"""

        allowed = order_preprocess.get_allowed_directions(
            context.strategy.allowed_directions, context.strategy.position_type
        )
        valid: List[BaseOrder] = order_preprocess.validate_orders(
            orders, stage, allowed
        )
        sorted_orders: List[BaseOrder] = order_preprocess.sort_orders(valid)

        # 只擋開倉，與回測一致（`Backtester` 也只在開倉分支呼叫）。
        # **排序之後才截斷**：先排序才知道超額時該留下哪幾張
        if stage == "open":
            return self._apply_max_holdings(context, sorted_orders)
        return sorted_orders

    def _apply_max_holdings(
        self, context: StrategyContext, orders: List[BaseOrder]
    ) -> List[BaseOrder]:
        """
        逐單套用持倉檔數上限

        **未終結的委託也要佔名額**：回測在同一根 bar 內逐單成交，持倉檔數會即時
        增加；實盤是非同步的，一批 8 張單送出時全部尚未成交，只看持倉的話整批
        都會放行——`max_holdings` 等於沒有設。

        已持有或已掛單的標的不佔新名額：那是加碼，與 `get_position_count()`
        「同一檔加碼多次只算一檔」的檔數語意一致。
        """

        if context.strategy.max_holdings is None:
            return orders

        occupied: Set[str] = self._occupied_symbols(context)
        kept: List[BaseOrder] = []
        for order in orders:
            if order.symbol in occupied:
                kept.append(order)
                continue

            if not order_preprocess.check_max_holdings(
                order, context.strategy.max_holdings, len(occupied)
            ):
                self._write_max_holdings_event(context, order)
                continue

            occupied.add(order.symbol)
            kept.append(order)
        return kept

    def _occupied_symbols(self, context: StrategyContext) -> Set[str]:
        """本策略已經佔住名額的標的：未平倉部位 ＋ 尚未終結的委託"""

        symbols: Set[str] = {
            position.symbol
            for position in context.account.positions
            if not position.is_closed
        }
        symbols.update(
            ticket.order.symbol
            for ticket in self.order_manager.tickets.values()
            if ticket.strategy_name == context.name
            and ticket.order is not None
            and not ticket.is_terminal
        )
        return symbols

    def _write_max_holdings_event(
        self, context: StrategyContext, order: BaseOrder
    ) -> None:
        """
        被檔數上限剔除的開倉單要留下紀錄

        **不沿用回測的 `rejected_max_holdings` 事件計數 key**：那是回測報表的欄位名，
        實盤沒有對應的計數器，兩邊硬共用一個名字只會讓報表欄位變得模稜兩可。
        """

        message: str = (
            f"{order.symbol} 開倉單超過持倉檔數上限 "
            f"{context.strategy.max_holdings}，已剔除"
        )
        logger.warning(f"[Max Holdings] {context.name}：{message}")
        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": context.name,
                "severity": "WARNING",
                "category": "MAX_HOLDINGS",
                "symbol": order.symbol,
                "client_order_id": order.client_order_id,
                "message": message,
                "occurred_at": self._now(),
            }
        )

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
            盤後作業；實作在 `AfterCloseRunner`

            **保留這個方法而不是讓 `run.py` 直接拿 runner**：退出碼由
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

    def resolve_end_reason(self, error: Optional[BaseException]) -> str:
        """
        - Description:
            決定本次執行寫進 `live_run` 的結束原因

            判定順序與 `run.py` 的退出碼一致（例外 → kill switch → 對帳不一致 →
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
                self._now(),
                end_reason,
                self.mode_state.account_mode.value,
            )
        except Exception as exc:
            logger.opt(exception=True).error(f"寫入段落結束紀錄失敗：{exc}")

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

    def _notify(self, level: str, title: str, body: str) -> None:
        """推播；失敗一律吞掉，監控不可拖垮被監控的東西"""

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
