import datetime
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.segment import SegmentWindow
from core.live.trader import LiveTrader, StrategyContext
from core.managers.stock.position_manager import StockPositionManager
from core.models import BaseOrder, BaseQuote, StockAccount, StockOrder, StockQuote
from core.strategies.base import BaseStrategy
from core.utils import (
    Action,
    ExecutionTiming,
    LiveHook,
    PositionType,
    Scale,
    StockPriceType,
)

from .conftest import FakeBroker

"""
`LiveTrader` 日頻流程：以 `FakeBroker` 端到端跑一個段落

十三個步驟的**順序**是本檔的重點，一步都不能調換：
- 對帳要在送單之前（否則是帶著錯誤部位交易）。
- 守門要在批次曝險之前（否則被擋的單還佔著額度）。
- 保留資金要在風控之前（否則風控算的是一個拿不到的金額）。

另外驗兩件「一出錯就會擴散」的事：一支策略拋例外不可拖垮其他策略；
送單失敗與撤單後的資金保留一定要放掉。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 21, 13, 26)
TODAY: datetime.date = NOW.date()
CAPITAL: float = 10_000_000.0


class FakeFeed(BaseLiveDataFeed):
    """報價由測試指定；不碰資料庫"""

    def __init__(self, quotes: Optional[List[BaseQuote]] = None) -> None:
        super().__init__(broker=None, calendar_sources=[], now_provider=lambda: NOW)
        self._quotes: List[BaseQuote] = quotes or []
        self.closed: int = 0

    def setup(self, strategy: BaseStrategy) -> None:
        """測試不需要建 API"""

    def get_latest_data_date(self) -> Optional[datetime.date]:
        return TODAY - datetime.timedelta(days=1)

    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        return list(self._quotes)

    def get_quotes(
        self, date: datetime.date, scale: Any, adjusted: bool = False
    ) -> List[BaseQuote]:
        return []

    def close(self) -> None:
        self.closed += 1


class ScriptedStrategy(BaseStrategy):
    """回傳固定委託的策略；可腳本化成「鉤子拋例外」"""

    def __init__(
        self,
        name: str,
        orders: Optional[List[BaseOrder]] = None,
        raises: bool = False,
    ) -> None:
        super().__init__()
        self._name: str = name
        self._orders: List[BaseOrder] = orders or []
        self._raises: bool = raises
        self.init_capital = CAPITAL
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        self.scale = Scale.DAY

    def setup_account(self, account: Any) -> None:
        self.account = account

    def check_open_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        if self._raises:
            raise RuntimeError("策略內部爆炸")
        return list(self._orders)

    def check_close_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        return []

    def check_stop_loss_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        return []


def make_order(
    symbol: str = "2330", volume: int = 1, price: float = 100.0
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        date=TODAY,
        action=Action.BUY,
        position_type=PositionType.LONG,
        volume=volume,
        price=price,
        price_type=StockPriceType.LMT,
    )


def make_quote(symbol: str = "2330") -> StockQuote:
    return StockQuote(
        stock_id=symbol, scale=Scale.DAY, date=TODAY, cur_price=100.0, close=100.0
    )


class Harness:
    """把整組元件接起來，測試只需要指定策略與腳本"""

    def __init__(
        self,
        strategies: List[ScriptedStrategy],
        window: Optional[SegmentWindow] = None,
        dry_run: bool = False,
    ) -> None:
        self.dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
        self.dao.ensure_tables()

        self.broker: FakeBroker = FakeBroker()
        self.broker.quotes["2330"] = make_quote()
        self.broker.account.available_balance = CAPITAL
        self.broker.account.total_equity = CAPITAL

        self.mode_state: TradingModeState = TradingModeState(
            self.dao, "run1", lambda: NOW
        )
        self.ledger: PositionAttributionLedger = PositionAttributionLedger(
            self.dao, now_provider=lambda: NOW
        )
        self.risk: PreTradeRiskManager = PreTradeRiskManager(
            self.mode_state,
            RiskConfig(),
            self.dao,
            "run1",
            kill_switch_path=_MissingPath(),
            now_provider=lambda: NOW,
        )
        self.allocator: CapitalAllocator = CapitalAllocator(
            {type(s).__name__: CAPITAL for s in strategies},
            self.dao,
            "run1",
            now_provider=lambda: NOW,
        )
        self.oms: OrderManager = OrderManager(
            self.broker, self.dao, "run1", run_index=1, now_provider=lambda: NOW
        )

        managers: Dict[str, StockPositionManager] = {}
        self.contexts: List[StrategyContext] = []
        for strategy in strategies:
            account: StockAccount = StockAccount(init_capital=CAPITAL)
            manager: StockPositionManager = StockPositionManager(account)
            managers[type(strategy).__name__] = manager
            self.contexts.append(
                StrategyContext(
                    strategy=strategy,
                    account=account,
                    position_manager=manager,
                    data_feed=FakeFeed([make_quote()]),
                    risk_config=RiskConfig(),
                    symbols=["2330"],
                    calculate_notional=lambda order: order.price * order.volume * 1000,
                )
            )

        self.sync: AccountSynchronizer = AccountSynchronizer(
            managers, self.ledger, self.dao, now_provider=lambda: NOW
        )
        self.reconciler: Reconciler = Reconciler(
            self.ledger, managers, self.dao, "run1", now_provider=lambda: NOW
        )
        self.guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(
            self.ledger, self.dao, "run1", now_provider=lambda: NOW
        )

        self.clock: List[datetime.datetime] = [NOW]

        def advancing_now() -> datetime.datetime:
            return self.clock[0]

        def advancing_sleep(seconds: float) -> None:
            # 假時鐘要跟著 sleep 前進，否則等待迴圈永遠等不到時窗結束
            self.clock[0] = self.clock[0] + datetime.timedelta(seconds=seconds)

        self.trader: LiveTrader = LiveTrader(
            contexts=self.contexts,
            broker=self.broker,
            order_manager=self.oms,
            risk_manager=self.risk,
            mode_state=self.mode_state,
            ledger=self.ledger,
            allocator=self.allocator,
            account_sync=self.sync,
            reconciler=self.reconciler,
            conflict_guard=self.guard,
            dao=self.dao,
            run_id="run1",
            schedule={ExecutionTiming.AT_CLOSE: window} if window else None,
            dry_run=dry_run,
            now_provider=advancing_now,
            sleep=advancing_sleep,
        )


class _MissingPath:
    """永遠不存在的 kill switch 路徑"""

    def exists(self) -> bool:
        return False


# === 端到端 ===
def test_full_segment_places_orders_and_updates_positions() -> None:
    """一個段落跑完：委託送出、成交回報消化、部位進到歸屬帳"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1
    assert harness.ledger.get_account_positions() == {("2330", "LONG"): 1}
    assert harness.dao.conn.execute("SELECT COUNT(*) FROM live_fill").fetchone()[0] == 1


def test_connections_are_closed_even_on_failure() -> None:
    """
    `try/finally` 保證連線關得掉

    異常路徑上沒關的連線會佔住同帳號的連線額度，下一個段落連不進來。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.broker.fail_connect = True

    with pytest.raises(ConnectionError):
        harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.is_connected() is False
    assert harness.contexts[0].data_feed.closed == 1


def test_capital_is_released_after_the_segment() -> None:
    """
    段落結束時保留一定要放掉

    漏釋放會讓額度單向消耗到策略再也送不出單，而且不會有任何錯誤。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.allocator.reserved["Alpha"] == 0.0


# === 一支策略不可拖垮其他 ===
def test_one_failing_strategy_does_not_stop_the_others() -> None:
    """
    鉤子拋例外只降那一支

    整個段落一起死掉會讓其他策略的平倉單也送不出去。
    """

    class Broken(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Broken", raises=True)

    class Healthy(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Healthy", [make_order()])

    harness: Harness = Harness([Broken(), Healthy()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.mode_state.effective_mode("Broken") is TradingMode.HALTED
    assert harness.mode_state.effective_mode("Healthy") is TradingMode.NORMAL
    assert harness.broker.placed_count == 1


def test_failed_strategy_releases_its_capital() -> None:
    """
    降級時要釋放保留

    不回收的話，那支策略的額度會佔住**其他策略**的可用資金到重啟為止，
    症狀是別的策略莫名其妙送不出單。
    """

    class Broken(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Broken", raises=True)

    harness: Harness = Harness([Broken()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.allocator.reserved["Broken"] == 0.0


# === 交易模式 ===
def test_halted_strategy_hooks_are_not_called() -> None:
    """HALTED 的策略連鉤子都不呼叫：它的訊號不該進到任何後續步驟"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.mode_state.degrade(TradingMode.HALTED, "測試", strategy_name="Alpha")
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_reduce_only_blocks_opening_orders() -> None:
    """`REDUCE_ONLY` 下開倉鉤子不呼叫，平倉照常"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.mode_state.degrade(TradingMode.REDUCE_ONLY, "測試", strategy_name="Alpha")
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


# === 跨策略守門 ===
def test_second_strategy_is_blocked_on_the_same_symbol() -> None:
    """
    同標的只允許一支持有

    允許反向的話，台股同日同標的一買一賣會被券商判成當沖——稅率與成本跟回測不同。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    class Beta(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Beta", [make_order()])

    harness: Harness = Harness([Alpha(), Beta()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1
    assert (
        harness.dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'CROSS_STRATEGY_BLOCKED'"
        ).fetchone()[0]
        == 1
    )


# === 時窗 ===
def test_orders_are_not_sent_after_the_submit_deadline() -> None:
    """
    過了送單時限就不再送

    尾盤段只有幾分鐘；超時還送出去的單會落在收盤集合競價之外。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    window: SegmentWindow = SegmentWindow(
        submit_start=datetime.time(13, 25),
        submit_end=datetime.time(13, 25, 30),  # 已過（now 是 13:26）
        drain_end=datetime.time(13, 35),
    )
    harness: Harness = Harness([Alpha()], window=window)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_segment_window_rejects_reversed_times() -> None:
    """
    時點順序寫反會讓整段行為顛倒，建立時就擋下
    """

    with pytest.raises(ValueError, match="順序錯誤"):
        SegmentWindow(
            submit_start=datetime.time(13, 29),
            submit_end=datetime.time(13, 25),
            drain_end=datetime.time(13, 35),
        )


# === 風控 ===
def test_risk_rejection_releases_the_reservation() -> None:
    """
    被風控擋下的單要把保留放回去

    不放的話，一連串被拒的單會把額度吃光，真正該送的反而送不出去。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order(volume=999)])

    harness: Harness = Harness([Alpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert harness.allocator.reserved["Alpha"] == 0.0


def test_kill_switch_blocks_the_whole_segment() -> None:
    """kill switch 生效時一張單都不送，並把帳戶層降到 HALTED"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])

    class Present:
        def exists(self) -> bool:
            return True

    harness.risk.kill_switch_path = Present()
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert harness.mode_state.account_mode is TradingMode.HALTED


# === 分層 ===
def test_trader_contains_no_market_specific_words() -> None:
    """
    引擎本體不得出現市場或商品字樣

    出現了就代表市場語意漏進了市場無關的那一層，而那正是回測引擎當初分裂的起點。
    這條由 `check_layer_deps.py` 守著，這裡再釘一次是因為它是本檔存在的前提。
    """

    import re
    import tokenize
    from pathlib import Path

    source: Path = Path("core/live/trader.py")
    leaks: List[str] = []
    with source.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type is tokenize.NAME and re.match(
                r"^(Stock|Futures|Tw)[A-Za-z]*$", token.string
            ):
                leaks.append(token.string)

    assert leaks == []


def test_stuck_clock_does_not_hang_the_segment() -> None:
    """
    時鐘不前進時，等待回報的迴圈要有**獨立於時鐘的保險絲**

    時窗判斷靠 `now_provider()`；它若因為時鐘卡住、倒退或注入錯誤而不再前進，
    迴圈會永遠轉下去——段落不結束，下一個段落的行程拿不到連線與寫入鎖，
    而存活監控只會看到「開始了但沒有正常結束」。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    window: SegmentWindow = SegmentWindow(
        submit_start=datetime.time(13, 25),
        submit_end=datetime.time(13, 29),
        drain_end=datetime.time(13, 35),
    )
    harness: Harness = Harness([Alpha()], window=window)

    slept: List[float] = []
    harness.trader._now = lambda: NOW  # 時鐘卡死
    harness.trader._sleep = slept.append
    harness.trader.MAX_WAIT_SECONDS = 5.0

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    # 有真的睡過（代表進了等待迴圈），但總量被保險絲夾住
    assert slept
    assert (
        sum(slept) <= harness.trader.MAX_WAIT_SECONDS + LiveTrader.POLL_INTERVAL_SECONDS
    )
