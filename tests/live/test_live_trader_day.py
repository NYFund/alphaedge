import datetime
import sqlite3
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer, build_stock_order
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed, DataFreshnessError
from core.live.datafeed.calendar import TradingCalendarUnavailableError
from core.live.execution.stock import StockExecutionModel
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.risk.risk_config import CAPITAL_SAFETY_RATIO, RiskConfig
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.live.segment import SegmentWindow
from core.live.trader import LiveTrader, StrategyContext
from core.models import (
    BaseOrder,
    BaseQuote,
    OrderTicket,
    PendingAction,
    StockAccount,
    StockOrder,
    StockPosition,
    StockQuote,
)
from core.position.stock.position_manager import StockPositionManager
from core.strategies.base import BaseStrategy
from core.utils import (
    Action,
    ExecutionStyle,
    ExecutionTiming,
    LiveHook,
    LiveOrderStatus,
    PositionType,
    Scale,
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

    def __init__(
        self,
        quotes: Optional[List[BaseQuote]] = None,
        is_open: bool = True,
        latest_data_date: Optional[datetime.date] = None,
    ) -> None:
        super().__init__(broker=None, calendar_sources=[], now_provider=lambda: NOW)
        self._quotes: List[BaseQuote] = quotes or []
        self.closed: int = 0
        self._is_open: bool = is_open
        self._latest: datetime.date = latest_data_date or (
            TODAY - datetime.timedelta(days=1)
        )

    def setup(self, strategy: BaseStrategy) -> None:
        """測試不需要建 API"""

    def is_market_open(self, date: datetime.date) -> bool:
        """由測試指定；不建日曆來源（那是 `test_live_datafeed.py` 的範疇）"""

        return self._is_open

    def get_latest_data_date(self) -> Optional[datetime.date]:
        return self._latest

    def _probe_contract(self, resolver: Any) -> Optional[Any]:
        """開市與否由測試直接指定，不經過券商合約檔"""

        return None

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
        close_orders: Optional[List[BaseOrder]] = None,
    ) -> None:
        super().__init__()
        self._name: str = name
        self._orders: List[BaseOrder] = orders or []
        self._close_orders: List[BaseOrder] = close_orders or []
        self._raises: bool = raises
        self.init_capital = CAPITAL
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        # 照價掛單：委託價維持策略給的價，既有斷言不必跟著漲跌停變；
        # 「要成交」的換算由執行層的專屬測試涵蓋
        self.live_execution = ExecutionStyle.LIMIT
        self.scale = Scale.DAY

    def setup_account(self, account: Any) -> None:
        self.account = account

    def check_open_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        if self._raises:
            raise RuntimeError("策略內部爆炸")
        return list(self._orders)

    def check_close_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        return list(self._close_orders)

    def check_stop_loss_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        return []

    def build_cover_order(self, action: PendingAction) -> StockOrder:
        """
        由跨日待辦組出補平單；訂單型別是市場特性，交給策略組

        **收的是 `PendingAction` 不是資料列**：這個參數是策略層的公開契約，
        傳 dict 等於把紀錄庫的 schema 變成契約，改個欄位名就會無聲地壞掉。
        """

        return StockOrder(
            stock_id=action.symbol,
            date=TODAY,
            action=Action.SELL,
            position_type=PositionType.LONG,
            volume=action.volume,
            price=100.0,
        )


def make_order(
    symbol: str = "2330",
    volume: int = 1,
    price: float = 100.0,
    action: Action = Action.BUY,
) -> StockOrder:
    return StockOrder(
        stock_id=symbol,
        date=TODAY,
        action=action,
        position_type=PositionType.LONG,
        volume=volume,
        price=price,
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
        is_open: bool = True,
        latest_data_date: Optional[datetime.date] = None,
        quota: Optional[float] = None,
        price_limits: Tuple[Optional[float], Optional[float]] = (110.0, 90.0),
        live_capital: Optional[float] = None,
        risk_config: Optional[RiskConfig] = None,
    ) -> None:
        # 實盤額度：與組裝層一致，帳戶與額度分配用同一個值（未指定時等於研究本金）
        capital: float = live_capital if live_capital is not None else CAPITAL

        self.dao: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
        self.dao.ensure_tables()

        self.broker: FakeBroker = FakeBroker()
        self.broker.quotes["2330"] = make_quote()
        # 帳戶權益要撐得住 Σ 各策略額度 ÷ 安全係數，否則 `prepare()` 的
        # `verify_quota()` 會當場拒絕啟動——那正是它要擋的事
        equity: float = CAPITAL * len(strategies) / CAPITAL_SAFETY_RATIO
        self.broker.account.available_balance = equity
        self.broker.account.total_equity = equity

        self.mode_state: TradingModeState = TradingModeState(
            self.dao, "run1", lambda: NOW
        )
        self.ledger: PositionAttributionLedger = PositionAttributionLedger(
            self.dao, now_provider=lambda: NOW
        )
        self.risk: PreTradeRiskManager = PreTradeRiskManager(
            self.mode_state,
            risk_config or RiskConfig(),
            self.dao,
            "run1",
            kill_switch_path=_MissingPath(),
            now_provider=lambda: NOW,
        )
        self.allocator: CapitalAllocator = CapitalAllocator(
            {
                type(s).__name__: quota if quota is not None else capital
                for s in strategies
            },
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
            account: StockAccount = StockAccount(init_capital=capital)
            manager: StockPositionManager = StockPositionManager(account)
            managers[type(strategy).__name__] = manager
            self.contexts.append(
                StrategyContext(
                    strategy=strategy,
                    account=account,
                    position_manager=manager,
                    data_feed=FakeFeed(
                        [make_quote()],
                        is_open=is_open,
                        latest_data_date=latest_data_date,
                    ),
                    symbols=["2330"],
                    calculate_notional=lambda order: order.price * order.volume * 1000,
                    # 與 `factory._build_context()` 一致；少了它，
                    # 任何需要還原訂單的路徑（帳戶同步、當沖回補）都會靜靜做不了事
                    build_filled_order=build_stock_order,
                    # 與真實組裝一致地經過執行層；漲跌停由測試指定，不碰券商合約
                    execution_model=StockExecutionModel(
                        price_limits=lambda symbol: price_limits
                    ),
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


def seed_holding(harness: Harness, strategy_name: str, volume: int) -> None:
    """
    在歸屬帳與券商端放一筆 2330 多單

    兩邊都要放：只放歸屬帳的話，`prepare()` 對帳會判成不一致而降級；
    只放券商端的話，部位會被收進 `__unattributed__` 而不屬於這支策略。
    """

    from core.models import BrokerPositionSnapshot

    harness.dao.open_lot(
        {
            "lot_id": f"seed-{strategy_name}",
            "strategy_name": strategy_name,
            "symbol": "2330",
            "direction": "LONG",
            "volume": volume,
            "open_date": TODAY - datetime.timedelta(days=1),
            "open_price": 1000.0,
            "client_order_id": None,
        }
    )
    harness.broker.positions = [
        BrokerPositionSnapshot(
            symbol="2330", direction=PositionType.LONG, volume=volume, avg_price=1000.0
        )
    ]


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


@pytest.mark.parametrize(
    "risk_config",
    [
        # 只有批次曝險（單一標的 25%）擋得住，逐筆單筆上限 40% 放行
        RiskConfig(single_order_amount_ratio=0.40, single_symbol_exposure_ratio=0.25),
        # 只有逐筆單筆上限（25%）擋得住，批次的單一標的上限 40% 放行
        RiskConfig(single_order_amount_ratio=0.25, single_symbol_exposure_ratio=0.40),
    ],
    ids=["batch_exposure", "single_order"],
)
@pytest.mark.parametrize(
    ("live_capital", "placed"),
    [
        # 30 萬的單：以實盤額度 100 萬計是 30%，兩組設定都恰好只有一道擋得住
        (1_000_000.0, 0),
        # 對照組：實盤額度等於研究本金 1,000 萬時只佔 3%，照常送出
        (None, 1),
    ],
)
def test_risk_limits_use_live_capital(
    risk_config: RiskConfig, live_capital: Optional[float], placed: int
) -> None:
    """
    批次曝險與逐筆檢查都以實盤額度為基準，不是研究回測的 `init_capital`

    策略宣告較小的 `live_capital` 時，若仍拿 `init_capital` 當基準，
    每道上限都會照較大的本金放寬（曾經寬了 2.5 倍）。兩組設定各讓一道上限
    單獨負責擋單，任一處退回舊基準都會讓那一組的單送出去。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order(volume=3)])

    harness: Harness = Harness(
        [Alpha()], live_capital=live_capital, risk_config=risk_config
    )
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == placed


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
    harness.trader.WAIT_GRACE_SECONDS = 5.0

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    # 有真的睡過（代表進了等待迴圈），但總量被保險絲夾住：上限是開始等待時
    # 離收線還有多久（13:26 → 13:35）加寬限；收線後收撤單回報的固定秒數另計，
    # 兩段都不看時鐘
    remaining: float = (
        datetime.datetime.combine(TODAY, window.drain_end) - NOW
    ).total_seconds()
    assert slept
    assert sum(slept) <= (
        remaining
        + harness.trader.WAIT_GRACE_SECONDS
        + LiveTrader.CANCEL_REPORT_SECONDS
        + 2 * LiveTrader.POLL_INTERVAL_SECONDS
    )


def test_long_window_does_not_hit_the_wait_cap() -> None:
    """
    時鐘正常前進時，比固定秒數長的時窗也要等到收線

    股票開盤段 08:30～09:05 共 35 分鐘；保險絲曾寫死 1800 秒，
    每天在 09:03 左右撞到上限、提早收線並記「請確認系統時鐘」的假警報。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    window: SegmentWindow = SegmentWindow(
        submit_start=datetime.time(13, 26),
        submit_end=datetime.time(13, 27),
        drain_end=datetime.time(14, 1),
    )
    harness: Harness = Harness([Alpha()], window=window)

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.clock[0].time() >= window.drain_end


def test_auction_orders_stay_open_until_the_drain_end() -> None:
    """
    正常結束時，未成交的委託要留到收線才撤

    集合競價的 ROD 委託要在場上等撮合（尾盤段 13:30）。2026-10-07 演練中三張買單
    在送出後一秒就被收尾流程撤掉，收盤集合競價時已不在場上，而段落照樣「正常結束」。
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
    harness.broker.fill_ratio = 0.0
    cancelled_at: List[datetime.datetime] = []
    original_cancel: Any = harness.broker.cancel_order

    def recording_cancel(ticket: OrderTicket) -> OrderTicket:
        cancelled_at.append(harness.clock[0])
        return original_cancel(ticket)

    harness.broker.cancel_order = recording_cancel  # type: ignore[method-assign]

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert len(cancelled_at) == 1
    assert cancelled_at[0].time() >= window.drain_end
    # 撤單回報有收回來：本地轉成已撤，不會留到盤後才補標
    (ticket,) = harness.oms.tickets.values()
    assert ticket.status is LiveOrderStatus.CANCELLED


# === 盤後作業 ===
def test_after_close_writes_reports(tmp_path: Path) -> None:
    """盤後跑完要留下三份 CSV"""

    from core.live.report.live_reporter import LiveReporter

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)

    summary: Dict[str, Any] = harness.trader.run_after_close()

    assert summary["reports"]["Alpha/orders"].exists()
    assert summary["reports"]["Alpha/fills"].exists()
    assert summary["reports"]["Alpha/positions"].exists()


def test_after_close_does_not_compare_parity(tmp_path: Path) -> None:
    """
    盤後不比 parity：當天的日 K 次日才入庫，此時的當日回測一張委託都產生不出來

    2026-10-07 演練：盤後比對把三張實盤開倉單全歸成快照口徑差異，parity 照樣「通過」。
    """

    from core.live.report.live_reporter import LiveReporter

    called: List[datetime.date] = []
    harness: Harness = Harness([ScriptedStrategy("Alpha", [make_order()])])
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)
    harness.trader.after_close.check_signal_parity = (  # type: ignore[method-assign]
        lambda run_date: called.append(run_date) or 0
    )

    harness.trader.run_after_close()

    assert called == []


def make_parity_harness(
    tmp_path: Path, latest_data_date: Optional[datetime.date] = None
) -> Tuple[Harness, List[datetime.date]]:
    """接上一個會記錄回測日期的 parity 比對器"""

    from core.live.report.parity_checker import ParityChecker

    harness: Harness = Harness(
        [ScriptedStrategy("Alpha", [make_order()])], latest_data_date=latest_data_date
    )
    backtest_dates: List[datetime.date] = []

    def run_backtest(name: str, run_date: datetime.date) -> List[BaseOrder]:
        backtest_dates.append(run_date)
        return []

    harness.trader.after_close.parity_checker = ParityChecker(
        harness.dao, run_backtest, output_root=tmp_path, strategy_names=["Alpha"]
    )
    return (harness, backtest_dates)


def test_parity_is_compared_for_the_latest_data_date(tmp_path: Path) -> None:
    """
    次日補比：預設比歷史資料最新的那一天（前一交易日）

    紀錄庫裡要有那天的委託，比對範圍才會含這支策略。
    """

    yesterday: datetime.date = TODAY - datetime.timedelta(days=1)
    harness, backtest_dates = make_parity_harness(tmp_path, latest_data_date=yesterday)
    harness.dao.upsert_order(
        {
            "client_order_id": "run0-0001",
            "run_id": "run0",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 100.0,
            "volume": 1,
            "status": "CANCELLED",
            "created_at": datetime.datetime.combine(yesterday, datetime.time(13, 25)),
        }
    )

    harness.trader.run_parity()

    assert backtest_dates == [yesterday]
    # 比的是前一交易日那一天，差異寫在那一天名下
    rows: List[Any] = harness.dao.conn.execute(
        "SELECT date, symbol FROM live_parity_diff WHERE strategy_name = 'Alpha'"
    ).fetchall()
    assert rows == [(yesterday.isoformat(), "2330")]
    assert harness.contexts[0].data_feed.closed == 1


def test_parity_refuses_stale_data(tmp_path: Path) -> None:
    """資料沒更新到前一交易日就不比：比了只會把實盤的單全判成差異"""

    harness, backtest_dates = make_parity_harness(
        tmp_path, latest_data_date=TODAY - datetime.timedelta(days=30)
    )

    with pytest.raises(DataFreshnessError):
        harness.trader.run_parity()

    assert backtest_dates == []


def test_explicit_date_backfill_does_not_need_yesterday(tmp_path: Path) -> None:
    """
    指定日期補比時，只要那一天有資料，不要求資料更新到前一交易日

    2026-10-08 00:30 補比 10/6：資料只到 10/6（10/7 清晨才入庫），
    新鮮度檢查把這個合法的補比擋了下來。
    """

    two_days_ago: datetime.date = TODAY - datetime.timedelta(days=2)
    harness, backtest_dates = make_parity_harness(
        tmp_path, latest_data_date=two_days_ago
    )
    harness.contexts[0].data_feed.calendar_sources = [_AlwaysTradingDay()]

    harness.trader.run_parity(two_days_ago)

    # 沒被新鮮度檢查擋下；那天沒有委託仍照樣比（本行程載入的策略一律比）
    assert backtest_dates == [two_days_ago]


class _AlwaysTradingDay:
    """每一天都確定是交易日；讓新鮮度檢查一定判得出缺漏"""

    name: str = "always"

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        return True


def test_parity_refuses_a_date_without_data(tmp_path: Path) -> None:
    """指定的日期還沒有日 K 時拒絕，不比一份空的回測"""

    harness, backtest_dates = make_parity_harness(
        tmp_path, latest_data_date=TODAY - datetime.timedelta(days=1)
    )

    with pytest.raises(DataFreshnessError, match="沒有"):
        harness.trader.run_parity(TODAY)

    assert backtest_dates == []


def test_unfilled_entry_order_is_abandoned(tmp_path: Path) -> None:
    """
    開倉未成交一律放棄，不追價

    追價等於在偏離訊號價的位置建倉，而回測沒有這個行為。
    """

    from core.live.report.live_reporter import LiveReporter

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.broker.fill_ratio = 0.0
    harness.trader.run(ExecutionTiming.AT_CLOSE)
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)

    summary: Dict[str, Any] = harness.trader.run_after_close()

    assert summary["pending_actions"] == 0


def test_unfilled_exit_order_creates_a_pending_action(tmp_path: Path) -> None:
    """
    平倉未成交必須補：寫 `PENDING` 待辦 ＋ CRITICAL 事件

    那是預期外的隔夜部位，風險遠大於開倉沒成交。
    """

    from core.live.report.live_reporter import LiveReporter

    exit_order: StockOrder = make_order()
    exit_order.action = Action.SELL  # LONG 的賣出＝平倉

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            # **平倉單要從平倉鉤子出來**：從開倉鉤子回傳的話，委託前處理會依
            # 「開倉階段的動作應為 BUY」把它剔除——那是對的，但驗不到殘量政策
            super().__init__("Alpha", close_orders=[exit_order])

    harness: Harness = Harness([Alpha()])
    harness.broker.fill_ratio = 0.0
    harness.trader.run(ExecutionTiming.AT_CLOSE)
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)

    summary: Dict[str, Any] = harness.trader.run_after_close()

    assert summary["pending_actions"] == 1
    assert (
        harness.dao.conn.execute(
            "SELECT COUNT(*) FROM live_risk_event WHERE category = 'UNFILLED_EXIT'"
        ).fetchone()[0]
        == 1
    )


def test_pending_action_is_covered_next_morning(tmp_path: Path) -> None:
    """
    次日開盤段的第一件事就是補平

    那是預期外的隔夜部位，多留一分鐘就多一分鐘的曝險。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [])
            self.live_schedule = {
                LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
                LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
            }

    harness: Harness = Harness([Alpha()])
    seed_holding(harness, "Alpha", 1)
    harness.dao.insert_pending_action(
        {
            "action_id": "P1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": 1,
            "due_date": TODAY,
            "status": harness.dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )

    harness.trader.run(ExecutionTiming.AT_OPEN)

    assert harness.broker.placed_count == 1
    assert harness.dao.get_pending_actions(TODAY) == []


def test_pending_action_without_a_reference_price_is_left_for_humans(
    tmp_path: Path,
) -> None:
    """
    策略沒組、引擎也取不到參考價時留給人工，**不猜一個價格**

    沒有決策價就沒有風控可比的基準；待辦留在 `PENDING`，由推播通知人工。
    （測試的資料源沒有合約，參考價一律取不到）
    """

    class NoBuilder(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("NoBuilder", [])
            self.live_schedule = {
                LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
                LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
            }
            self.build_cover_order = None  # type: ignore[assignment]

    harness: Harness = Harness([NoBuilder()])
    seed_holding(harness, "NoBuilder", 1)
    harness.dao.insert_pending_action(
        {
            "action_id": "P1",
            "strategy_name": "NoBuilder",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": 1,
            "due_date": TODAY,
            "status": harness.dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )

    harness.trader.run(ExecutionTiming.AT_OPEN)

    assert harness.broker.placed_count == 0
    assert len(harness.dao.get_pending_actions(TODAY)) == 1  # 待辦留著


def make_opening_strategy() -> ScriptedStrategy:
    """開盤段才有鉤子、自己不產生訊號的策略；只用來跑跨日待辦"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [])
            self.live_schedule = {
                LiveHook.OPEN.value: ExecutionTiming.AT_OPEN,
                LiveHook.CLOSE.value: ExecutionTiming.AT_OPEN,
            }

    return Alpha()


def insert_cover_action(harness: Harness, volume: int) -> None:
    harness.dao.insert_pending_action(
        {
            "action_id": "P1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Sell",
            "position_type": "LONG",
            "volume": volume,
            "due_date": TODAY,
            "status": harness.dao.ACTION_PENDING,
            "created_at": NOW,
        }
    )


def placed_volumes(harness: Harness) -> List[int]:
    return [ticket.order.volume for ticket in harness.broker.tickets.values()]


def test_pending_action_is_truncated_to_the_current_holding() -> None:
    """
    待辦 2 張、歸屬帳只剩 1 張 → 只送 1 張

    待辦寫下之後部位可能已經變了（遲到的成交、人工在券商端平倉）。
    照原數量送出的話，多出來的那張不是多平一點，是把部位做反。
    """

    harness: Harness = Harness([make_opening_strategy()])
    seed_holding(harness, "Alpha", 1)
    insert_cover_action(harness, 2)

    harness.trader.run(ExecutionTiming.AT_OPEN)

    assert placed_volumes(harness) == [1]
    assert harness.dao.get_pending_actions(TODAY) == []
    categories: List[str] = [
        row["category"] for row in harness.dao.get_risk_events_by_date(TODAY)
    ]
    assert "PENDING_ACTION_TRUNCATED" in categories


def test_pending_action_without_holding_is_closed_without_an_order() -> None:
    """部位已經不在就不送單，待辦標成已處理並留下事件"""

    harness: Harness = Harness([make_opening_strategy()])
    insert_cover_action(harness, 2)

    harness.trader.run(ExecutionTiming.AT_OPEN)

    assert harness.broker.placed_count == 0
    assert harness.dao.get_pending_actions(TODAY) == []
    status: str = harness.dao.conn.execute(
        "SELECT status FROM live_pending_action WHERE action_id = 'P1'"
    ).fetchone()[0]
    assert status == harness.dao.ACTION_DONE


def test_pending_action_goes_through_pre_trade_risk() -> None:
    """
    補平單要經過事前風控；被擋下時待辦留著、滾到次日並推播

    以前直接呼叫 OMS 送單，任何一條風控都管不到它。
    """

    from core.live.risk.risk_manager import RiskDecision

    harness: Harness = Harness([make_opening_strategy()])
    seed_holding(harness, "Alpha", 1)
    insert_cover_action(harness, 1)
    checked: List[str] = []

    def reject(order: BaseOrder, *args: Any, **kwargs: Any) -> RiskDecision:
        checked.append(order.symbol)
        return RiskDecision(passed=False, reason="測試：風控擋下")

    harness.risk.check = reject  # type: ignore[method-assign]

    harness.trader.run(ExecutionTiming.AT_OPEN)

    assert checked == ["2330"]
    assert harness.broker.placed_count == 0
    tomorrow: datetime.date = TODAY + datetime.timedelta(days=1)
    assert harness.dao.get_pending_actions(TODAY) == []
    assert len(harness.dao.get_pending_actions(tomorrow)) == 1


def test_open_orders_are_expired_at_day_end(tmp_path: Path) -> None:
    """
    ROD 單在券商端日終自動失效，**本地要跟著標**

    不標的話，次日的恢復流程會把它們當成「還在場上」去接管，
    然後去撤一張早就不存在的單，而那個錯誤訊息看起來像真的出了事。
    """

    from core.live.report.live_reporter import LiveReporter
    from core.utils import LiveOrderStatus

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])

    # 模擬「段落行程已經結束、盤後是另一個行程」：DB 裡留著一張未終結的委託，
    # 而盤後行程的記憶體裡什麼都沒有——那正是日終標記真正會遇到的狀態
    harness.dao.upsert_order(
        {
            "client_order_id": "run1-0001",
            "run_id": "run1",
            "strategy_name": "Alpha",
            "symbol": "2330",
            "action": "Buy",
            "position_type": "LONG",
            "price": 100.0,
            "volume": 1,
            "status": LiveOrderStatus.SUBMITTED.value,
            "custom_field": "010001",
            "created_at": NOW,
        }
    )
    harness.dao.conn.commit()
    assert len(harness.dao.get_unfinished_orders(TODAY)) == 1

    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)
    harness.trader.run_after_close()

    assert harness.dao.get_unfinished_orders(TODAY) == []


# === 啟動檢查：寫好了就要有人呼叫 ===
def test_stale_data_refuses_to_start() -> None:
    """
    歷史資料沒更新到前一個交易日就拒絕啟動

    接上之前，`DataFreshnessError` 全庫只在 `verify_data_freshness()` 內拋出而
    沒有任何呼叫端——實盤入口（`apps/live.py`）的結束碼 3 因此永遠不會發生，ETL 掛掉三天也照跑。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness(
        [Alpha()], latest_data_date=TODAY - datetime.timedelta(days=30)
    )

    with pytest.raises(DataFreshnessError):
        harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_market_closed_does_not_enter_the_submit_path() -> None:
    """休市日只連線不送單；**不是拋例外**——沒開市不是錯誤"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()], is_open=False)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.trader.is_trading_day is False
    assert harness.broker.placed_count == 0


def test_undecidable_trading_day_refuses_to_start() -> None:
    """判不出今天是不是交易日 → 拒絕啟動，**不預設為開市**"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    # 還原成「沒有任何日曆來源」的真實行為
    for context in harness.contexts:
        feed: BaseLiveDataFeed = context.data_feed
        feed.is_market_open = partial(BaseLiveDataFeed.is_market_open, feed)

    with pytest.raises(TradingCalendarUnavailableError):
        harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_over_allocated_quota_refuses_to_start() -> None:
    """Σ 各策略額度超過帳戶總權益 × 安全係數 → 拒絕啟動，不等盤中被退單"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()], quota=CAPITAL * 10)

    with pytest.raises(ValueError, match="超過帳戶總權益"):
        harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_daily_loss_degrades_the_strategy_before_submitting() -> None:
    """
    段落開始前就發現虧損超標 → 降級到 `REDUCE_ONLY` 並寫 `live_risk_event`

    **判定在送單之前**：放到段落結束才判，等於本段落已經白送一輪。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    # 讓本地帳帶著超過 `daily_loss_ratio`（3%）的已實現虧損
    harness.contexts[0].account.realized_pnl = -CAPITAL * 0.05
    harness.contexts[0].account.update_realized_pnl = lambda: None

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.mode_state.effective_mode("Alpha") is TradingMode.REDUCE_ONLY
    rows = harness.dao.conn.execute(
        "SELECT COUNT(*) FROM live_risk_event WHERE category = 'DAILY_LOSS'"
    ).fetchone()
    assert rows[0] == 1


# === 持倉檔數上限：回測與實盤共用同一份判定 ===
def test_max_holdings_blocks_the_order_beyond_the_cap() -> None:
    """
    `max_holdings` 在實盤也要擋得住

    `check_max_holdings()` 放在共用層 `core/portfolio/order_rules.py`，
    但接上之前只有 `Backtester` 呼叫——**回測會擋掉的開倉單，實盤會送出去**。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__(
                "Alpha",
                [make_order("2330"), make_order("2317"), make_order("2454")],
            )
            self.max_holdings = 2

    harness: Harness = Harness([Alpha()])
    harness.broker.quotes["2317"] = make_quote("2317")
    harness.broker.quotes["2454"] = make_quote("2454")
    harness.contexts[0].symbols = ["2330", "2317", "2454"]

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 2
    rejected = harness.dao.conn.execute(
        "SELECT symbol FROM live_risk_event WHERE category = 'MAX_HOLDINGS'"
    ).fetchall()
    assert [row[0] for row in rejected] == ["2454"]


def test_unfilled_orders_still_occupy_a_holding_slot() -> None:
    """
    未終結的委託也要佔名額

    回測在同一根 bar 內逐單成交、持倉檔數即時增加；實盤是非同步的，
    只看持倉的話一批單會全部放行，`max_holdings` 等於沒有設。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order("2454")])
            self.max_holdings = 2

    harness: Harness = Harness([Alpha()])
    harness.broker.quotes["2454"] = make_quote("2454")
    harness.contexts[0].symbols = ["2454"]

    # 兩張還沒成交的委託先佔住兩個名額
    for symbol in ("2330", "2317"):
        ticket: OrderTicket = OrderTicket(
            client_order_id=f"run1-{symbol}",
            strategy_name="Alpha",
            order=make_order(symbol),
            status=LiveOrderStatus.SUBMITTED,
        )
        harness.oms.tickets[ticket.client_order_id] = ticket

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0


def test_adding_to_an_existing_holding_does_not_take_a_new_slot() -> None:
    """加碼不佔新名額：與 `get_position_count()` 的「同一檔只算一檔」語意一致"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order("2330")])
            self.max_holdings = 1

    harness: Harness = Harness([Alpha()])
    ticket: OrderTicket = OrderTicket(
        client_order_id="run1-2330",
        strategy_name="Alpha",
        order=make_order("2330"),
        status=LiveOrderStatus.SUBMITTED,
    )
    harness.oms.tickets[ticket.client_order_id] = ticket

    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1


def test_close_orders_are_not_limited_by_max_holdings() -> None:
    """平倉不受檔數上限影響——擋掉平倉單等於把部位鎖在場上"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__(
                "Alpha", [], close_orders=[make_order("2330", action=Action.SELL)]
            )
            self.max_holdings = 0

    harness: Harness = Harness([Alpha()])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1


# === live_run 生命週期 ===
def start_run(harness: Harness, phase: str = "close") -> None:
    """寫入本次的啟動紀錄（正式流程由 factory 寫，Harness 直接組引擎所以要自己補）"""

    harness.dao.insert_run(
        {"run_id": "run1", "started_at": NOW, "phase": phase, "simulation": 1}
    )


def run_row(harness: Harness) -> Any:
    """`(phase, started_at, ended_at, end_reason, account_mode)`"""

    return harness.dao.conn.execute(
        "SELECT phase, started_at, ended_at, end_reason, account_mode "
        "FROM live_run WHERE run_id = 'run1'"
    ).fetchone()


def test_finished_segment_is_recorded_as_normal() -> None:
    """
    正常跑完的段落要寫結束紀錄，存活監控據此判定健康

    沒寫的話 `ended_at` 一直是 NULL：監控對每個跑完的段落報「沒有結束紀錄」，
    下一次啟動還會把它標成崩潰——正常結束與崩潰分不出來。
    """

    from scripts.live_watchdog import check_phases

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    start_run(harness)
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    row: Any = run_row(harness)
    assert row[2] is not None
    assert row[3] == LiveTradeDAO.END_REASON_NORMAL
    assert row[4] == TradingMode.NORMAL.value

    statuses = check_phases(
        [row[:4]],
        {"close": datetime.time(13, 20)},
        NOW + datetime.timedelta(hours=1),
        grace_minutes=15,
    )
    assert statuses[0].is_healthy is True


def test_aborted_segment_records_the_exception() -> None:
    """例外中止時結束原因要寫出例外，且例外照樣往外拋給實盤入口"""

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    start_run(harness)
    harness.broker.fail_connect = True

    with pytest.raises(ConnectionError):
        harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert run_row(harness)[3].startswith("例外中止：ConnectionError")


def test_kill_switch_end_is_not_recorded_as_normal() -> None:
    """
    kill switch 停下的段落不可寫成正常結束

    實盤入口以結束碼 5 退出，監控卻只看這一欄；寫成正常結束的話推播就漏了。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    start_run(harness)

    class Present:
        def exists(self) -> bool:
            return True

    harness.risk.kill_switch_path = Present()
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    row: Any = run_row(harness)
    assert row[3] == "kill switch"
    assert row[4] == TradingMode.HALTED.value


def test_after_close_is_recorded(tmp_path: Path) -> None:
    """盤後作業同樣要寫結束紀錄：它也是存活監控檢查的段落"""

    from core.live.report.live_reporter import LiveReporter

    harness: Harness = Harness([ScriptedStrategy("Alpha", [])])
    start_run(harness, phase="after_close")
    harness.trader.reporter = LiveReporter(harness.dao, output_root=tmp_path)

    harness.trader.run_after_close()

    row: Any = run_row(harness)
    assert row[2] is not None
    assert row[3] == LiveTradeDAO.END_REASON_NORMAL


def test_failed_finish_record_does_not_mask_the_real_error() -> None:
    """
    寫結束紀錄本身失敗時，拋出去的仍是讓段落中止的那個例外

    它在 `finally` 裡執行；它一拋，真正的原因就被蓋掉，事後只看得到「寫 DB 失敗」。
    """

    class Alpha(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha", [make_order()])

    harness: Harness = Harness([Alpha()])
    harness.broker.fail_connect = True

    def broken_finish_run(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    harness.dao.finish_run = broken_finish_run  # type: ignore[method-assign]

    with pytest.raises(ConnectionError):
        harness.trader.run(ExecutionTiming.AT_CLOSE)


def test_held_symbols_outside_the_universe_still_get_quotes() -> None:
    """
    帳上持有、卻不在標的池的標的照樣拿報價

    盤中策略每天重新篩選標的池（受逐筆訂閱上限），昨天買的那一檔今天掉出池外的話，
    平倉鉤子永遠看不到它，部位就一直留在帳上而沒有任何錯誤。
    """

    harness: Harness = Harness([ScriptedStrategy("Alpha")])
    context: StrategyContext = harness.contexts[0]
    context.account.positions.append(
        StockPosition(
            id=1,
            stock_id="2317",
            position_type=PositionType.LONG,
            date=NOW.date(),
            price=100.0,
            volume=1,
        )
    )
    requested: List[List[str]] = []

    def recording(timing: ExecutionTiming, symbols: Sequence[str]) -> List[BaseQuote]:
        requested.append(list(symbols))
        return []

    context.data_feed.get_live_quotes = recording  # type: ignore

    harness.trader.collect_orders(context, ExecutionTiming.AT_CLOSE)

    assert requested == [["2330", "2317"]]


def test_intraday_quotes_for_held_symbols_reach_the_strategy() -> None:
    """
    盤中逐筆：池外持有標的的報價也要餵給策略

    只訂閱、不派送的話，報價到了卻在派送那一層被「不在標的池」擋掉，
    停損鉤子一樣看不到它。
    """

    received: List[str] = []

    class Watcher(ScriptedStrategy):
        def __init__(self) -> None:
            super().__init__("Alpha")
            self.is_tick_triggered = True
            self.live_schedule = {
                LiveHook.OPEN.value: ExecutionTiming.IMMEDIATE,
                LiveHook.CLOSE.value: ExecutionTiming.IMMEDIATE,
            }

        def check_close_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
            received.extend(quote.symbol for quote in quotes)
            return []

    harness: Harness = Harness([Watcher()])
    harness.trader.prepare()
    harness.contexts[0].account.positions.append(
        StockPosition(
            id=1,
            stock_id="2317",
            position_type=PositionType.LONG,
            date=NOW.date(),
            price=100.0,
            volume=1,
        )
    )

    harness.trader._on_intraday_quote(
        StockQuote(
            stock_id="2317",
            scale=Scale.TICK,
            date=NOW,
            cur_price=100.0,
            volume=1,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
        )
    )

    assert received == ["2317"]
