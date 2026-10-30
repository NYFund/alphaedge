import datetime
from typing import Callable, Dict, Iterator, List, Optional, Protocol, Set, Tuple

from loguru import logger

from core.backtest.models.event_counts import TICK_EVENT_KEYS
from core.backtest.simulator.base import BaseSimulator, SimulationEngine
from core.backtest.simulator.pending_orders import (
    STAGE_CLOSE,
    STAGE_OPEN,
    STAGE_STOP_LOSS,
    PendingOrder,
    PendingOrderManager,
)
from core.backtest.simulator.tick_universe import resolve_strategy_symbols
from core.market.segment import SegmentSchedule, SegmentWindow
from core.models import BaseOrder, BaseQuote
from core.portfolio import order_rules
from core.strategies.base import resolve_hook_timing
from core.utils import (
    BarExecutionSequence,
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
    OrderType,
    TickUniverse,
)

"""
逐筆 simulator：照時間順序一筆一筆重放，委託在同一檔的下一筆才撮合

一天分三段，與實盤的段落相同（各鉤子排在哪一段由策略的 `execution_schedule` 決定）：
1. 開盤集合競價：以盤前參考價呼叫 `AT_OPEN` 的鉤子，委託在該檔當天第一筆撮合。
2. 逐筆：每一筆先撮合在途委託，再在 `IMMEDIATE` 的時間窗內把這一筆交給鉤子（長度 1 的 list）。
3. 收盤集合競價：以收盤段開始時點的快照呼叫 `AT_CLOSE` 的鉤子，委託在之後的集合競價撮合。
"""


class TickDataFeed(Protocol):
    """逐筆 simulator 需要的資料源介面（實作者：`StockTickDataFeed`）"""

    def setup_day(self, date: datetime.date, symbols: Optional[List[str]]) -> int:
        """查當天的 tick，回傳筆數"""

        ...

    def iter_day_ticks(self) -> Iterator[BaseQuote]:
        """依時間逐筆產生報價"""

        ...

    def get_reference_quotes(
        self, reference_prices: Dict[str, float]
    ) -> List[BaseQuote]:
        """盤前報價"""

        ...

    def get_snapshot_quotes(self, cutoff: datetime.time) -> List[BaseQuote]:
        """某時點之前的最後一筆"""

        ...

    def get_close_quotes(self) -> List[BaseQuote]:
        """收盤報價"""

        ...

    def get_previous_trading_day(self, date: datetime.date) -> datetime.date:
        """前一交易日"""

        ...


class TickSimulator(BaseSimulator):
    """
    - Description:
        逐筆重放：沒有前視（委託只會用下單之後的 tick 成交），與實盤同一套段落時點

        - **先撮合、再呼叫策略**：同一筆 tick 上，前一筆送出的委託先成交，策略才看到這一筆；
          策略看到的帳戶因此不含「未來」的成交。
        - **13:25 之前的 tick 跑完才進收盤段**：收盤段的鉤子在 13:25 看快照，之後的集合競價成交
          才撮合；整天跑完再呼叫的話，鉤子會看到 13:30 才發生的成交。
        - **重複委託**：已有在途出場委託的標的，不再拿去問停損與平倉；已有在途開倉委託的標的，
          新的開倉單略過並計數。實盤的市價單幾毫秒內就成交，不會重複；回測要等下一筆，不擋會疊單。
        - 段落時點由外部注入（台股見 `core/market/tw/segment_schedule.py`），simulator 不持有任何時刻常數。
    """

    def __init__(self, engine: SimulationEngine, schedule: SegmentSchedule) -> None:
        """
        - Description:
            建立逐筆 simulator
        - Parameters:
            - engine: SimulationEngine
                引擎
            - schedule: SegmentSchedule
                市場的段落時窗表；`IMMEDIATE` 的送單區間決定逐筆鉤子的時間窗，
                `AT_CLOSE` 的開始送單時刻是收盤段快照的時點
        - Raise:
            - ValueError
                段落表缺 `IMMEDIATE` 或 `AT_CLOSE`，或策略沒宣告 `live_execution`
        """

        super().__init__(engine)

        immediate: Optional[SegmentWindow] = schedule.get(ExecutionTiming.IMMEDIATE)
        closing: Optional[SegmentWindow] = schedule.get(ExecutionTiming.AT_CLOSE)
        if immediate is None or closing is None:
            raise ValueError("逐筆回測需要 IMMEDIATE 與 AT_CLOSE 兩個段落的時窗")

        # 開倉與一般平倉的執行方式；停損一律市價（與實盤相同）。
        # 沒宣告就拒絕：給預設值等於替策略作者決定它要不要成交
        if engine.strategy.live_execution is None:
            raise ValueError(
                f"{type(engine.strategy).__name__} 沒有宣告 live_execution："
                "逐筆回測要知道委託是市價（MARKET）還是限價（LIMIT）"
            )

        # 逐筆才有的事件計數加進共用的計數器，事件報表才看得到（日 K 的報表不受影響）
        for key in TICK_EVENT_KEYS:
            engine.event_counts.setdefault(key, 0)

        self.immediate_window: SegmentWindow = immediate
        self.closing_cutoff: datetime.time = closing.submit_start
        self.pending: PendingOrderManager = PendingOrderManager()

    @property
    def feed(self) -> TickDataFeed:
        """逐筆資料源（每次取引擎上的，測試替換資料源時才跟得上）"""

        return self.engine.data_feed

    # === 一天 ===
    def load_day(self, date: datetime.date) -> bool:
        """
        - Description:
            依標的範圍查當天的 tick

            策略清單模式下，清單與持倉都是空的日子照樣要跑（權益照記）；
            有標的卻一筆 tick 都沒有、或全市場模式沒有資料，才整天跳過（與日 K 沒報價的日子相同）。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - bool
                False 表示整天跳過
        """

        symbols: Optional[List[str]] = None
        if self.engine.tick_universe == TickUniverse.STRATEGY:
            symbols = resolve_strategy_symbols(
                self.engine.strategy,
                self.engine.account,
                self.feed.get_previous_trading_day(date),
            )

        count: int = self.feed.setup_day(date, symbols)
        return count > 0 or symbols == []

    def run_day(self, date: datetime.date) -> List[BaseQuote]:
        """
        - Description:
            開盤集合競價 → 逐筆 → 收盤集合競價；收盤時未成交的委託全部作廢
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - List[BaseQuote]
                每檔一筆的收盤報價
        """

        self.engine.fill_model.intraday_range = {}
        self.pending.cancel_all()

        self.process_opening_auction()

        cutoff_at: datetime.datetime = datetime.datetime.combine(
            date, self.closing_cutoff
        )
        seen: Set[str] = set()
        closing_done: bool = False

        for quote in self.feed.iter_day_ticks():
            tick_time: datetime.datetime = quote.tick_quote.time
            if not closing_done and tick_time >= cutoff_at:
                self.process_closing_auction()
                closing_done = True

            first_tick: bool = quote.symbol not in seen
            seen.add(quote.symbol)
            self.process_tick(quote, first_tick, tick_time >= cutoff_at)

        if not closing_done:
            self.process_closing_auction()

        self.expire_pending()
        return self.feed.get_close_quotes()

    # === 三段 ===
    def process_opening_auction(self) -> None:
        """開盤集合競價：以盤前參考價呼叫 `AT_OPEN` 的鉤子，委託在各檔第一筆撮合"""

        quotes: List[BaseQuote] = self.feed.get_reference_quotes(
            self.engine.fill_model.reference_prices
        )
        self.submit_segment(ExecutionTiming.AT_OPEN, quotes, opening_auction=True)

    def process_tick(
        self, quote: BaseQuote, first_tick: bool, closing_auction: bool
    ) -> None:
        """
        - Description:
            一筆 tick：更新當日區間 → 撮合同一檔的在途委託 → 時間窗內交給 `IMMEDIATE` 的鉤子
        - Parameters:
            - quote: BaseQuote
                這一筆
            - first_tick: bool
                是否為該檔當天第一筆（開盤集合競價的成交）
            - closing_auction: bool
                是否為收盤集合競價的成交（收盤段開始時點之後）
        """

        self.engine.fill_model.update_intraday_range([quote])
        self.match_pending(quote, first_tick or closing_auction)

        tick_clock: datetime.time = quote.tick_quote.time.time()
        window: SegmentWindow = self.immediate_window
        if window.submit_start <= tick_clock <= window.submit_end:
            self.submit_segment(ExecutionTiming.IMMEDIATE, [quote])

    def process_closing_auction(self) -> None:
        """收盤集合競價：以收盤段開始時點的快照呼叫 `AT_CLOSE` 的鉤子，委託在之後的集合競價撮合"""

        quotes: List[BaseQuote] = self.feed.get_snapshot_quotes(self.closing_cutoff)
        self.submit_segment(ExecutionTiming.AT_CLOSE, quotes)

    # === 送出委託 ===
    def submit_segment(
        self,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
        opening_auction: bool = False,
    ) -> None:
        """
        - Description:
            呼叫排在本段的鉤子，把委託放進在途委託簿

            開倉與出場的先後依 `allow_day_trade`（與實盤 `SignalCollector.invoke_hooks()` 相同），
            它決定同一筆 tick 上兩邊委託的撮合順序。
        - Parameters:
            - timing: ExecutionTiming
                段落
            - quotes: List[BaseQuote]
                本段交給鉤子的報價
            - opening_auction: bool
                是否為開盤段（委託只在各檔第一筆撮合）
        """

        if not quotes:
            return

        submitted_at: datetime.datetime = self._submitted_at(quotes)
        sequence: BarExecutionSequence = self.engine.get_execution_sequence()
        if sequence == BarExecutionSequence.OPEN_THEN_CLOSE:
            self.submit_entries(timing, quotes, submitted_at, opening_auction)
            self.submit_exits(timing, quotes, submitted_at, opening_auction)
        else:
            self.submit_exits(timing, quotes, submitted_at, opening_auction)
            self.submit_entries(timing, quotes, submitted_at, opening_auction)

    def submit_entries(
        self,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
        submitted_at: datetime.datetime,
        opening_auction: bool,
    ) -> None:
        """開倉：本段沒排到就不呼叫；方向驗證 → 補值 → 排序 → 重複與檔數上限"""

        engine: SimulationEngine = self.engine
        if resolve_hook_timing(engine.strategy, LiveHook.OPEN) is not timing:
            return

        orders: List[BaseOrder] = engine.sort_orders(
            engine.enrich_orders(
                engine.validate_orders(
                    engine.strategy.check_open_signal(quotes), "open"
                )
            )
        )
        for order in orders:
            if self.pending.has_open(order.symbol):
                engine.event_counts["rejected_duplicate_pending"] += 1
                continue
            if not self.check_max_holdings(order):
                continue

            engine.submitted_orders.append((engine.cur_date, STAGE_OPEN, order))
            self.add_pending(
                order,
                STAGE_OPEN,
                engine.strategy.live_execution,
                submitted_at,
                opening_auction,
            )

    def submit_exits(
        self,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
        submitted_at: datetime.datetime,
        opening_auction: bool,
    ) -> None:
        """
        出場：先停損（一律市價）再一般平倉；只拿持有、且沒有在途出場委託的標的去問策略。
        停損送出的標的不再進一般平倉的候選（與日 K 的「停損 → 重新掃描剩餘部位」相同）
        """

        engine: SimulationEngine = self.engine
        stages: List[Tuple[LiveHook, str, ExecutionStyle]] = [
            (LiveHook.STOP_LOSS, STAGE_STOP_LOSS, ExecutionStyle.MARKET),
            (LiveHook.CLOSE, STAGE_CLOSE, engine.strategy.live_execution),
        ]
        for hook, stage, style in stages:
            if resolve_hook_timing(engine.strategy, hook) is not timing:
                continue

            held: List[BaseQuote] = [
                q
                for q in quotes
                if engine.account.check_has_position(q.symbol)
                and not self.pending.has_exit(q.symbol)
            ]
            if not held:
                continue

            signal: Callable[[List[BaseQuote]], List[BaseOrder]] = (
                engine.strategy.check_stop_loss_signal
                if hook is LiveHook.STOP_LOSS
                else engine.strategy.check_close_signal
            )
            orders: List[BaseOrder] = engine.sort_orders(
                engine.validate_orders(signal(held), "close")
            )
            for order in orders:
                engine.submitted_orders.append((engine.cur_date, stage, order))
                self.add_pending(order, stage, style, submitted_at, opening_auction)

    def check_max_holdings(self, order: BaseOrder) -> bool:
        """持倉檔數上限：未平倉部位之外，**在途的開倉委託也佔名額**（與實盤相同）"""

        occupied: Set[str] = {
            position.symbol
            for position in self.engine.account.positions
            if not position.is_closed
        } | set(self.pending.open_symbols())
        return order_rules.check_max_holdings(
            order, self.engine.max_holdings, occupied, self.engine.event_counts
        )

    def add_pending(
        self,
        order: BaseOrder,
        stage: str,
        style: ExecutionStyle,
        submitted_at: datetime.datetime,
        opening_auction: bool,
    ) -> None:
        """放進在途委託簿：市價為 IOC、限價為 ROD"""

        self.pending.add(
            PendingOrder(
                order=order,
                stage=stage,
                style=style,
                time_in_force=(
                    OrderType.IOC if style == ExecutionStyle.MARKET else OrderType.ROD
                ),
                submitted_at=submitted_at,
                opening_auction=opening_auction,
            )
        )

    # === 撮合 ===
    def match_pending(self, quote: BaseQuote, auction: bool) -> None:
        """
        - Description:
            以這一筆撮合同一檔的在途委託（送出順序）

            - 開盤段的委託只在該檔第一筆撮合；沒成交時市價作廢、限價轉成盤中的 ROD。
            - 市價（IOC）只試這一筆，沒成交就作廢，不追價。
            - 限價（ROD）留到收盤。
        - Parameters:
            - quote: BaseQuote
                這一筆
            - auction: bool
                這一筆是否為集合競價的成交（開盤段的委託在該檔第一筆就處理掉，不會留到之後）
        """

        for pending in self.pending.get_orders(quote.symbol):
            price: Optional[float] = self.engine.fill_model.get_tick_fill_price(
                pending.order, pending.style, quote, auction
            )
            if price is not None:
                self.pending.remove(pending)
                self.execute_fill(pending, price, quote, auction)
                continue

            if pending.opening_auction and pending.style == ExecutionStyle.LIMIT:
                pending.opening_auction = False
                continue

            if pending.time_in_force == OrderType.IOC:
                self.pending.remove(pending)
                self.engine.event_counts["expired_ioc"] += 1

    def execute_fill(
        self, pending: PendingOrder, price: float, quote: BaseQuote, auction: bool
    ) -> None:
        """套用市場的成交假設後入帳：開倉進部位、出場平倉（盤中即時入帳）"""

        filled: Optional[BaseOrder] = self.engine.fill_model.fill_tick(
            pending.order, price, quote, auction
        )
        if filled is None:
            return

        if pending.is_exit:
            self.engine.position_manager.close_position(filled)
        else:
            self.engine.position_manager.open_position(filled)

    def expire_pending(self) -> None:
        """收盤：未成交的委託全部作廢並計數"""

        for pending in self.pending.cancel_all():
            key: str = (
                "expired_ioc"
                if pending.time_in_force == OrderType.IOC
                else "expired_at_close"
            )
            self.engine.event_counts[key] += 1
            logger.debug(
                f"[Tick] {pending.order.symbol} {pending.stage} 委託收盤未成交，作廢"
            )

    @staticmethod
    def _submitted_at(quotes: List[BaseQuote]) -> datetime.datetime:
        """委託的送出時間：觸發那一筆的時間；盤前報價沒有時間，記當天 00:00"""

        tick: Optional[object] = getattr(quotes[0], "tick_quote", None)
        if tick is not None and getattr(tick, "time", None) is not None:
            return tick.time
        return datetime.datetime.combine(quotes[0].date, datetime.time())
