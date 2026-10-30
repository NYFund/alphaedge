import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from loguru import logger

from core.live.capital_allocator import CapitalAllocator
from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.execution.base import mark_execution
from core.live.notify.base import NotifyLevel, notify_safely
from core.live.risk import event_categories
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.models import BaseOrder, BaseQuote
from core.portfolio import order_rules
from core.strategies.base import BaseStrategy, resolve_hook_timing
from core.utils import (
    BarExecutionSequence,
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
)

"""
SignalCollector：呼叫策略鉤子、收進出場單並做委託前處理

一支策略在一個段落裡要拿哪些報價、該呼叫哪些鉤子（開倉、停損、平倉）、
鉤子吐出的委託怎麼驗證、補值、排序與套用持倉檔數上限，都在這裡；
與回測走同一份 `order_rules`，兩邊的順序一致。

**一支策略拋例外不可拖垮其他策略**：鉤子失敗的那支轉入 `HALTED` 並釋放它的
資金保留（`halt_strategy()`），其餘照常。
"""


class SignalCollector:
    """策略訊號 → 經過前處理的委託；鉤子失敗時把該策略降到 `HALTED`"""

    def __init__(
        self,
        mode_state: TradingModeState,
        order_manager: Any,
        risk_manager: PreTradeRiskManager,
        allocator: CapitalAllocator,
        events: RiskEventLogger,
        notifier: Optional[Any],
        clock: SegmentClock,
        forget_reservations: Callable[[str], None],
    ) -> None:
        """
        - Description:
            建立訊號收集者；相依一律由外部注入
        - Parameters:
            - mode_state: TradingModeState
                交易模式狀態機（`HALTED` 不呼叫鉤子；`REDUCE_ONLY` 不開倉）
            - order_manager: Any
                委託管理（未終結的委託也佔持倉檔數的名額）
            - risk_manager: PreTradeRiskManager
                事前風控（鉤子失敗時把策略降到 `HALTED`）
            - allocator: CapitalAllocator
                資金額度分配（降級時釋放該策略的保留）
            - events: RiskEventLogger
                風控事件寫入口
            - notifier: Optional[Any]
                事件推播；None 時不推播
            - clock: SegmentClock
                段落時鐘（報價日期要對到今天）
            - forget_reservations: Callable[[str], None]
                降級時把該策略的保留紀錄從送單元件忘掉；保留帳在送單元件身上，本元件不持有
        """

        self.mode_state: TradingModeState = mode_state
        self.order_manager: Any = order_manager
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.allocator: CapitalAllocator = allocator
        self.events: RiskEventLogger = events
        self.notifier: Optional[Any] = notifier
        self.clock: SegmentClock = clock
        self.forget_reservations: Callable[[str], None] = forget_reservations

    @staticmethod
    def quote_symbols(context: StrategyContext) -> List[str]:
        """
        - Description:
            這支策略要拿報價的標的：宣告的標的池，加上帳上持有、卻不在池裡的標的

            **持有的標的一定要拿得到報價**：標的池可能每天重新篩選（盤中策略受訂閱上限
            所限，只看前 N 檔），昨天買的那一檔今天掉出池外的話，平倉與停損鉤子
            永遠看不到它，部位就一直留在帳上，而且不會有任何錯誤。
        - Parameters:
            - context: StrategyContext
                策略脈絡
        - Return:
            - List[str]
                標的池在前、池外的持有標的依代號排序附在後面
        """

        declared: List[str] = list(context.symbols)
        held: Set[str] = {
            position.symbol for position in context.account.get_positions()
        } - set(declared)
        return declared + sorted(held)

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
                timing, self.quote_symbols(context)
            )
            fresh: Optional[List[BaseQuote]] = self._fresh_quotes(context, quotes)
            if fresh is None:
                return []
            quotes = fresh
            # 先記下市場價再呼叫鉤子：鉤子產生的每一張委託都要拿同一份報價當偏離基準
            context.quote_prices = quote_prices_of(quotes)
            orders: List[BaseOrder] = self.invoke_hooks(context, timing, quotes)
        except Exception as exc:
            self.halt_strategy(context, f"鉤子執行失敗：{exc}")
            return []

        return [(context, order) for order in orders]

    def _fresh_quotes(
        self, context: StrategyContext, quotes: List[BaseQuote]
    ) -> Optional[List[BaseQuote]]:
        """
        - Description:
            剔除日期不是今天的報價；**全部都不是今天時回 None**，本段落不呼叫這支策略的鉤子

            實盤報價的日期取自券商快照的時戳。行情源停在前一日時（模擬環境實際發生過：
            收盤段拿到的快照全是前一日 14:30），策略會把報價日當成前一日決策——
            「持有滿一天才平倉」的出場不成立而不賣、選股照前一日的漲幅重選，
            整段照常結束、沒有任何錯誤。開段的資料新鮮度檢查只看歷史資料，擋不到這種情況。

            只剔除過期的那幾檔、其餘照常：個別標的的快照停住（例如當日暫停交易）
            不該拖住整支策略。有剔除或整段跳過時寫 CRITICAL 事件並推播一次。
            **事件的寫法讓 parity 分得出兩種情況**：整段跳過寫一筆不帶標的的事件
            （以 `run_id` 對到段落）；部分剔除則每檔一筆、帶標的——只有被剔除的那幾檔
            的差異可以歸給它，其餘標的的差異照常判定。
            出場鉤子跟著不呼叫——部位留到下一個段落再由策略判斷，不排次日補平：
            報價不可信時連「該不該出場」都不知道。
        - Parameters:
            - context: StrategyContext
                策略脈絡
            - quotes: List[BaseQuote]
                資料源給的段落報價
        - Return:
            - Optional[List[BaseQuote]]
                今天的報價；原本有報價、但全部都不是今天時為 None
        """

        today: datetime.date = self.clock.now().date()
        fresh: List[BaseQuote] = [
            quote for quote in quotes if _quote_day(quote) == today
        ]
        if len(fresh) == len(quotes):
            return quotes

        stale: List[BaseQuote] = [
            quote for quote in quotes if _quote_day(quote) != today
        ]
        days: str = "、".join(sorted({str(_quote_day(quote)) for quote in stale}))
        sample: str = "、".join(quote.symbol for quote in stale[:5])
        outcome: str = (
            "本段落不呼叫鉤子（開倉、平倉、停損都不做）" if not fresh else "已剔除"
        )
        message: str = (
            f"{context.name} 有 {len(stale)}／{len(quotes)} 檔報價的日期不是今天 {today}"
            f"（{days}），{outcome}：{sample}"
        )
        logger.error(message)
        if not fresh:
            self.events.write(
                category=event_categories.QUOTE_STALE,
                severity=NotifyLevel.CRITICAL,
                message=message,
                strategy_name=context.name,
            )
        else:
            for quote in stale:
                self.events.write(
                    category=event_categories.QUOTE_STALE,
                    severity=NotifyLevel.CRITICAL,
                    message=(
                        f"{quote.symbol} 報價日期 {_quote_day(quote)} 不是今天 {today}，"
                        "已剔除"
                    ),
                    strategy_name=context.name,
                    symbol=quote.symbol,
                )
        notify_safely(self.notifier, NotifyLevel.CRITICAL, "報價日期不是今天", message)
        return fresh or None

    def invoke_hooks(
        self,
        context: StrategyContext,
        timing: ExecutionTiming,
        quotes: List[BaseQuote],
    ) -> List[BaseOrder]:
        """依 `get_execution_sequence()` 決定開平倉先後，與回測一致"""

        strategy: BaseStrategy = context.strategy
        execution_sequence: BarExecutionSequence = order_rules.get_execution_sequence(
            strategy.allow_day_trade
        )

        exit_orders: List[BaseOrder] = self._exit_orders(context, timing, quotes)
        entry_orders: List[BaseOrder] = self._entry_orders(context, timing, quotes)

        if execution_sequence is BarExecutionSequence.OPEN_THEN_CLOSE:
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
        return mark_execution(
            self._preprocess(context, raw, stage="open"),
            timing,
            context.strategy.live_execution,
        )

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

        **停損一律 `MARKET`**，不看策略的 `live_execution`：出場不該因為價格掛不到
        而失敗。一般平倉沿用策略宣告的執行方式。
        """

        if not self.mode_state.allows_close(context.name):
            return []

        orders: List[BaseOrder] = []
        if resolve_hook_timing(context.strategy, LiveHook.STOP_LOSS) is timing:
            stop_loss: List[BaseOrder] = self._preprocess(
                context, context.strategy.check_stop_loss_signal(quotes), "close"
            )
            orders.extend(mark_execution(stop_loss, timing, ExecutionStyle.MARKET))
        if resolve_hook_timing(context.strategy, LiveHook.CLOSE) is timing:
            close: List[BaseOrder] = self._preprocess(
                context, context.strategy.check_close_signal(quotes), "close"
            )
            orders.extend(
                mark_execution(close, timing, context.strategy.live_execution)
            )
        return orders

    def _preprocess(
        self, context: StrategyContext, orders: List[BaseOrder], stage: str
    ) -> List[BaseOrder]:
        """方向白名單 → 市場欄位補值 → 決定性排序 → 持倉檔數上限；與回測同一個順序、同一份實作"""

        allowed = order_rules.get_allowed_directions(context.strategy.direction)
        valid: List[BaseOrder] = order_rules.validate_orders(orders, stage, allowed)
        # 台股空單的放空管道與當沖旗標由成本模型依策略設定補上，與回測同一步。
        # 少了這一步，SHORT 委託到券商轉換層才因 `short_method` 為 None 被拒，
        # 當沖空單的部位也不會被日終回補認出來
        if context.cost_model is not None:
            valid = context.cost_model.enrich_orders(valid)
        sorted_orders: List[BaseOrder] = order_rules.sort_orders(valid)

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

        已持有或已掛單的標的不佔新名額（加碼），豁免寫在共用的
        `order_rules.check_max_holdings()`，與回測同一份。
        """

        if context.strategy.max_holdings is None:
            return orders

        occupied: Set[str] = self._occupied_symbols(context)
        kept: List[BaseOrder] = []
        for order in orders:
            if not order_rules.check_max_holdings(
                order, context.strategy.max_holdings, occupied
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
        self.events.write(
            category="MAX_HOLDINGS",
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=context.name,
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )

    def halt_strategy(self, context: StrategyContext, reason: str) -> None:
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
        self.forget_reservations(context.name)


def quote_prices_of(quotes: Sequence[BaseQuote]) -> Dict[str, float]:
    """
    報價 → `symbol → 市場價`；沒有正的價格的報價略過

    先取 `cur_price`（最新成交價），沒有才退回 `close`：尾盤段的快照兩者相同，
    盤前段的參考價報價只填其中一邊。0 或負值不算價格，留著會讓偏離檢查除以 0
    或把「沒有報價」當成真實價格。
    """

    prices: Dict[str, float] = {}
    for quote in quotes:
        price: float = float(getattr(quote, "cur_price", 0.0) or 0.0)
        if price <= 0:
            price = float(getattr(quote, "close", 0.0) or 0.0)
        if price > 0 and quote.symbol:
            prices[quote.symbol] = price
    return prices


def _quote_day(quote: BaseQuote) -> Optional[datetime.date]:
    """報價的日期部分；逐筆報價帶的是 `datetime`，日頻報價是 `date`"""

    value: Optional[Union[datetime.date, datetime.datetime]] = quote.date
    if isinstance(value, datetime.datetime):
        return value.date()
    return value
