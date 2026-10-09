from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from core.live.factory import live_capital
from core.live.trader import LiveTrader
from core.models import BrokerAccountSnapshot
from strategies.stock.momentum_strategy_1 import MomentumStrategy1

"""
策略可用的資金基準

額度檢查、單日虧損與批次曝險都拿它當基準。口徑的演進：

1. 最早是「可用餘額 ＋ 各策略持倉占用」，漏了**未交割款**——只要隔日還有部位在場上，
   就必然誤判成額度超標。
2. 2026-09-23 改取券商算好的 `total_equity`。那一版把**不屬於任何策略的持倉**
   （接管來的 `__unattributed__`）也算進來，理由是「它們有價值」。
3. **2026-10-02 再改**：接管來的舊持股不是策略可以動用的資金，正式環境的總權益要扣掉它們；
   **模擬環境的帳務本來就是假的**（`account_balance()` 恆為 0、模擬下單不檢查資金），
   一律以宣告額度為虛擬資金。2026-10-01 的「Σ 額度 40 萬 ≤ 總權益 616,343 × 95%」
   其實是拿 6 檔模擬持倉當資金，沒有檢查到任何錢。
"""


def make_trader(
    total_equity: float,
    quotas: Dict[str, float],
    simulation: bool = False,
    has_snapshot: bool = True,
) -> LiveTrader:
    """只裝出 `_account_equity()` 會讀到的那幾個屬性，不建整個 trader"""

    trader: LiveTrader = LiveTrader.__new__(LiveTrader)
    trader.simulation = simulation
    trader.account_snapshot = (
        BrokerAccountSnapshot(total_equity=total_equity, available_balance=0.0)
        if has_snapshot
        else None
    )
    trader.allocator = SimpleNamespace(quotas=dict(quotas))
    return trader


# === 取券商算好的總權益 ===
def test_equity_comes_from_the_broker_snapshot() -> None:
    """總權益取快照的 `total_equity`，不是可用餘額"""

    trader: LiveTrader = make_trader(total_equity=514_890.0, quotas={"A": 400_000.0})

    assert trader._account_equity() == 514_890.0


def test_missing_snapshot_is_zero_not_a_crash() -> None:
    """還沒刷新過帳務時回 0，不是 AttributeError"""

    trader: LiveTrader = make_trader(
        total_equity=0.0, quotas={"A": 1.0}, has_snapshot=False
    )

    assert trader._account_equity() == 0.0


# === 模擬環境：一律以宣告額度為虛擬資金 ===
@pytest.mark.parametrize(
    "paper_equity", [0.0, 616_343.0], ids=["zero", "paper-positions"]
)
def test_simulation_always_uses_declared_quota(paper_equity: float) -> None:
    """
    模擬環境的帳務是假的：權益一律以 Σ 宣告額度為準

    `account_balance()` 在模擬環境恆為 0，權益只剩模擬持倉的市值——那是紙上部位，
    不是可以下單的錢。2026-10-01 的 616,343 就是 6 檔接管來的模擬持倉，
    它讓額度檢查「通過」，實際上沒有檢查到任何資金。
    """

    trader: LiveTrader = make_trader(
        total_equity=paper_equity,
        quotas={"A": 400_000.0, "B": 3_000_000.0},
        simulation=True,
    )

    assert trader.uses_virtual_capital() is True
    assert trader._account_equity() == 3_400_000.0


def test_quota_check_is_skipped_rather_than_faked() -> None:
    """
    **模擬環境略過額度檢查，不是捏一個數字讓它通過**

    以 Σ 宣告額度當基準的話，檢查會變成「Σ 額度 ≤ Σ 額度 × 安全係數」，
    因為安全係數小於 1 而**必然不成立**——2026-09-23 實測到期貨側正好差
    那 5%：3,000,000 vs 2,850,000。正式環境則照常檢查。
    """

    from core.live.capital_allocator import CapitalAllocator
    from core.live.risk.risk_config import CAPITAL_SAFETY_RATIO

    simulated: LiveTrader = make_trader(
        total_equity=582_608.0, quotas={"A": 400_000.0}, simulation=True
    )
    production: LiveTrader = make_trader(
        total_equity=582_608.0, quotas={"A": 400_000.0}, simulation=False
    )

    assert simulated.uses_virtual_capital() is True
    assert production.uses_virtual_capital() is False

    # 釘住那個必然不成立的關係：安全係數 < 1。
    # **要讀真正的預設值**——自己設一個再斷言它小於 1 是同義反覆，
    # 有人把 `CAPITAL_SAFETY_RATIO` 改成 1.0 也照樣綠，而那正是這條要防的事
    allocator: CapitalAllocator = CapitalAllocator({"Alpha": 1_000_000.0})
    assert allocator.safety_ratio == CAPITAL_SAFETY_RATIO
    assert allocator.safety_ratio < 1.0


def test_production_with_zero_equity_stays_zero() -> None:
    """
    **正式環境不走虛擬資金**

    那裡的 0 是真的沒有錢。放寬的話，一個空帳戶會通過額度檢查、
    一路跑到盤中被券商退單——而那時已經有部位在場上。
    """

    trader: LiveTrader = make_trader(
        total_equity=0.0,
        quotas={"A": 400_000.0},
        simulation=False,
    )

    assert trader._account_equity() == 0.0


def test_simulation_allocator_gets_virtual_cash() -> None:
    """
    模擬環境的額度分配以「Σ 額度 − Σ 已用」當可用現金

    照實拿券商的 0 的話，`allocate_capital()` 的帳戶上限恆為 0，
    每一張買單都會被自己的程式擋下、永遠送不到模擬環境。
    """

    from core.live.capital_allocator import CapitalAllocator

    trader: LiveTrader = make_trader(
        total_equity=616_343.0, quotas={"A": 400_000.0}, simulation=True
    )
    trader.allocator = CapitalAllocator({"A": 400_000.0})
    trader.contexts = []
    trader.fetch_account = lambda: BrokerAccountSnapshot(available_balance=0.0)

    trader._refresh_capital()

    assert trader.allocator.available("A") == 400_000.0


# === 部位金額與本地餘額 ===
def make_context(positions: List[Any]) -> Any:
    """帶台股換算器（張 → ×1000 股）的策略 context"""

    from core.live.trader import StrategyContext

    account: SimpleNamespace = SimpleNamespace(positions=positions, balance=400_000.0)
    return StrategyContext(
        strategy=SimpleNamespace(),
        account=account,
        position_manager=None,
        data_feed=None,
        calculate_notional=lambda order: order.price * order.volume * 1000,
    )


def test_position_value_uses_the_market_unit() -> None:
    """
    部位金額要乘計價單位：台股一張是 1000 股

    以前是「價 × 張」，已用額度小了一千倍，持倉幾乎不佔額度。
    已平倉的部位不算。
    """

    context = make_context(
        [
            SimpleNamespace(price=45.15, volume=2, is_closed=False),
            SimpleNamespace(price=100.0, volume=1, is_closed=True),
        ]
    )

    assert context.position_value() == pytest.approx(90_300.0)


def test_local_balance_is_quota_minus_held_positions() -> None:
    """
    啟動重建後，本地餘額 ＝ 額度 − 還持有部位的金額

    重建時餘額被設回建帳時的完整額度，持有部位的成本沒扣掉——
    部位管理層會照偏大的餘額算張數，再被額度分配整筆拒絕。
    """

    trader: LiveTrader = make_trader(
        total_equity=0.0, quotas={"SimpleNamespace": 400_000.0}, simulation=True
    )
    context = make_context([SimpleNamespace(price=45.15, volume=2, is_closed=False)])
    trader.contexts = [context]

    trader._reset_local_balances()

    assert context.account.balance == pytest.approx(400_000.0 - 90_300.0)


# === 正式環境：不屬於任何策略的部位不算資金 ===
class _PositionBroker:
    """回一個股票帳戶快照與部位清單的假閘道"""

    def __init__(self, total_equity: float, positions: List[Any]) -> None:
        self.snapshot: BrokerAccountSnapshot = BrokerAccountSnapshot(
            available_balance=0.0, total_equity=total_equity
        )
        self.positions: List[Any] = positions

    def get_account(self) -> BrokerAccountSnapshot:
        return self.snapshot

    def get_positions(self) -> List[Any]:
        return self.positions


class _LotDAO:
    """只回 `__unattributed__` lot 的假紀錄庫"""

    def __init__(self, lots: List[Dict[str, Any]]) -> None:
        self.lots: List[Dict[str, Any]] = lots

    def get_open_lots(self, strategy_name: str) -> List[Dict[str, Any]]:
        assert strategy_name == "__unattributed__"
        return self.lots


def test_unattributed_stock_positions_are_not_capital() -> None:
    """
    接管來的舊持股不是策略可以動用的資金：股票帳戶的總權益要扣掉它們

    扣的口徑與券商總權益相同（張數 × 1000 × 均價 ＋ 未實現損益）——只扣成本的話，
    2026-10-02 的模擬帳戶會留下 104,485 的未實現損益在資金基準裡。
    空單也要扣；期貨的接管 lot 不在股票帳戶裡，不扣。
    """

    from core.live.factory import make_account_fetcher
    from core.models import FuturesPositionSnapshot, StockPositionSnapshot
    from core.utils import InstrumentType, PositionType

    broker: _PositionBroker = _PositionBroker(
        total_equity=200_000.0,
        positions=[
            StockPositionSnapshot(
                symbol="2362", volume=1, avg_price=50.0, unrealized_pnl=5_000.0
            ),
            StockPositionSnapshot(
                symbol="6134",
                direction=PositionType.SHORT,
                volume=3,
                avg_price=20.0,
                unrealized_pnl=-1_000.0,
            ),
            FuturesPositionSnapshot(symbol="TXFJ6", volume=1, avg_price=42000.0),
        ],
    )
    dao: _LotDAO = _LotDAO(
        [
            {"symbol": "2362", "direction": "LONG", "volume": 1, "open_price": 50.0},
            {"symbol": "6134", "direction": "SHORT", "volume": 3, "open_price": 20.0},
            {
                "symbol": "TXFJ6",
                "direction": "LONG",
                "volume": 1,
                "open_price": 42000.0,
            },
        ]
    )

    fetch = make_account_fetcher(broker, [_strategy(InstrumentType.STOCK)], dao)
    snapshot: BrokerAccountSnapshot = fetch()

    # 2362：50,000 ＋ 5,000；6134：60,000 − 1,000
    assert snapshot.raw["unattributed_value"] == pytest.approx(55_000.0 + 59_000.0)
    assert snapshot.total_equity == pytest.approx(200_000.0 - 114_000.0)


def test_partly_attributed_position_is_prorated() -> None:
    """同一檔券商部位 5 張、只有 2 張是接管的：只扣 2／5"""

    from core.live.factory import unattributed_stock_value
    from core.models import StockPositionSnapshot

    broker: _PositionBroker = _PositionBroker(
        total_equity=0.0,
        positions=[
            StockPositionSnapshot(
                symbol="2330", volume=5, avg_price=100.0, unrealized_pnl=10_000.0
            )
        ],
    )
    dao: _LotDAO = _LotDAO(
        [{"symbol": "2330", "direction": "LONG", "volume": 2, "open_price": 100.0}]
    )

    assert unattributed_stock_value(broker, dao) == pytest.approx(
        0.4 * (500_000.0 + 10_000.0)
    )


def test_equity_never_goes_negative_after_exclusion() -> None:
    """扣完不可以是負數：負的資金基準傳到下游會被當成一個數字繼續算"""

    from core.live.factory import exclude_unattributed_stock

    snapshot: BrokerAccountSnapshot = exclude_unattributed_stock(
        BrokerAccountSnapshot(total_equity=100_000.0), 250_000.0
    )

    assert snapshot.total_equity == 0.0


# === 三處共用同一個口徑 ===
def test_every_caller_uses_the_same_basis() -> None:
    """
    額度檢查、單日虧損與批次曝險都要呼叫 `_account_equity()`

    批次曝險那處原本**內嵌抄了一份**「可用餘額 ＋ 持倉占用」的公式，
    於是改了一邊另一邊不會跟著改——而兩邊算出不同的權益不會有任何錯誤訊息。
    """

    import inspect

    source: str = inspect.getsource(LiveTrader)

    assert "self.allocator.available_balance + sum(" not in source, (
        "又有人把總權益的公式內嵌抄了一份"
    )
    # **只留負向斷言**。原本還有一條 `count("self._account_equity()") >= 3`，
    # 釘的是呼叫點數量——把三處抽成一個 helper 行為完全沒變，測試卻會紅。
    # 要防的是「公式被抄第二份」，上面那條就夠了


# === 實盤額度與回測本金脫鉤 ===
def test_live_capital_overrides_init_capital() -> None:
    """宣告 `live_capital` 時以它為準"""

    strategy: SimpleNamespace = SimpleNamespace(
        init_capital=1_000_000.0, live_capital=400_000.0
    )

    assert live_capital(strategy) == 400_000.0


def test_live_capital_falls_back_to_init_capital() -> None:
    """沒宣告時沿用 `init_capital`，既有策略行為不變"""

    strategy: SimpleNamespace = SimpleNamespace(
        init_capital=1_000_000.0, live_capital=None
    )

    assert live_capital(strategy) == 1_000_000.0


def test_changing_live_capital_leaves_backtest_capital_alone() -> None:
    """
    **這是 `live_capital` 存在的唯一理由**

    `init_capital` 同時是回測帳戶的初始資金，而 LONG 回歸基準就是拿
    `MomentumStrategy1` 跑出來的。改它等於改掉每一筆回測結果、破壞回歸雙線。
    """

    strategy: MomentumStrategy1 = MomentumStrategy1()

    assert strategy.init_capital == 1_000_000.0, "回測本金不可被實盤需求改動"
    assert strategy.live_capital == 400_000.0
    assert live_capital(strategy) == 400_000.0


def test_base_strategies_default_to_no_override() -> None:
    """預設 `None`：沒有人宣告時，實盤與回測用同一個數字"""

    from strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()

    assert strategy.live_capital is None
    assert live_capital(strategy) == strategy.init_capital


def test_backtest_never_reads_live_capital() -> None:
    """回測那條路徑一個字都不碰 `live_capital`"""

    import pathlib

    root: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent.parent
    offenders: List[str] = [
        str(path.relative_to(root))
        for path in (root / "core" / "backtest").rglob("*.py")
        if "live_capital" in path.read_text()
    ]

    assert offenders == [], f"回測讀到了實盤專用的額度：{offenders}"


# === 實盤持倉檔數與回測脫鉤 ===
def test_live_max_holdings_is_written_to_the_live_instance() -> None:
    """宣告 `live_max_holdings` 時，實盤實例的 `max_holdings` 改用它"""

    from core.live.factory import apply_live_max_holdings

    strategy: MomentumStrategy1 = MomentumStrategy1()

    apply_live_max_holdings(strategy)

    assert strategy.max_holdings == strategy.live_max_holdings == 3


def test_live_max_holdings_leaves_backtest_defaults_alone() -> None:
    """
    新建的實例仍是回測的檔數

    parity 比對跑回測時重建策略實例，靠的就是這一點：寫回只發生在實盤那一份。
    """

    from core.live.factory import apply_live_max_holdings

    apply_live_max_holdings(MomentumStrategy1())

    assert MomentumStrategy1().max_holdings == MomentumStrategy1.DEFAULT_MAX_HOLDINGS


def test_undeclared_live_max_holdings_keeps_max_holdings() -> None:
    """沒宣告時不動，既有策略行為不變"""

    from core.live.factory import apply_live_max_holdings
    from strategies.stock.investment_trust_momentum_swing_strategy import (
        InvestmentTrustMomentumSwingStrategy,
    )

    strategy: InvestmentTrustMomentumSwingStrategy = (
        InvestmentTrustMomentumSwingStrategy()
    )
    before: object = strategy.max_holdings

    apply_live_max_holdings(strategy)

    assert strategy.live_max_holdings is None
    assert strategy.max_holdings == before


def test_backtest_never_reads_live_max_holdings() -> None:
    """回測那條路徑一個字都不碰 `live_max_holdings`"""

    import pathlib

    root: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent.parent
    offenders: List[str] = [
        str(path.relative_to(root))
        for path in (root / "core" / "backtest").rglob("*.py")
        if "live_max_holdings" in path.read_text()
    ]

    assert offenders == [], f"回測讀到了實盤專用的持倉檔數：{offenders}"


# === 依商品分派帳戶 ===
class _StubBroker:
    """只回兩個帳務快照的假閘道；記下被問了哪幾次"""

    def __init__(self, stock_equity: float, futures_equity: float) -> None:
        self.calls: List[str] = []
        self._stock: BrokerAccountSnapshot = BrokerAccountSnapshot(
            available_balance=stock_equity, total_equity=stock_equity
        )
        self._futures: BrokerAccountSnapshot = BrokerAccountSnapshot(
            available_balance=futures_equity, total_equity=futures_equity
        )

    def get_account(self) -> BrokerAccountSnapshot:
        self.calls.append("stock")
        return self._stock

    def get_futures_account(self) -> BrokerAccountSnapshot:
        self.calls.append("futures")
        return self._futures


def _strategy(instrument: object) -> SimpleNamespace:
    """只帶 `instrument_type` 的假策略"""

    return SimpleNamespace(instrument_type=instrument)


def test_stock_only_reads_the_stock_account() -> None:
    """只有股票策略時查股票帳戶"""

    from core.live.factory import make_account_fetcher
    from core.utils import InstrumentType

    broker: _StubBroker = _StubBroker(stock_equity=582_608.0, futures_equity=0.0)
    fetch = make_account_fetcher(broker, [_strategy(InstrumentType.STOCK)])

    assert fetch().total_equity == 582_608.0
    assert broker.calls == ["stock"]


def test_futures_only_reads_the_futures_account() -> None:
    """
    只有期貨策略時查期貨保證金帳戶，**不是股票帳戶**

    股票與期貨是兩個子帳戶，各有各的錢。拿股票權益去檢查期貨額度，
    等於用另一筆錢的規模在管這一筆——2026-09-23 的演練就因此永遠過不了額度檢查。
    """

    from core.live.factory import make_account_fetcher
    from core.utils import InstrumentType

    broker: _StubBroker = _StubBroker(stock_equity=582_608.0, futures_equity=0.0)
    fetch = make_account_fetcher(broker, [_strategy(InstrumentType.FUTURE)])

    assert fetch().total_equity == 0.0, "查到股票帳戶了"
    assert broker.calls == ["futures"]


def test_mixed_instruments_sum_both_accounts() -> None:
    """兩種商品同時載入時兩個帳戶相加：都是同一個人的錢"""

    from core.live.factory import make_account_fetcher
    from core.utils import InstrumentType

    broker: _StubBroker = _StubBroker(stock_equity=500_000.0, futures_equity=300_000.0)
    fetch = make_account_fetcher(
        broker,
        [_strategy(InstrumentType.STOCK), _strategy(InstrumentType.FUTURE)],
    )
    snapshot: BrokerAccountSnapshot = fetch()

    assert snapshot.total_equity == 800_000.0
    assert snapshot.available_balance == 800_000.0
    assert sorted(broker.calls) == ["futures", "stock"]


def test_fetcher_requeries_every_time() -> None:
    """每次呼叫都重查：段落之間帳務會變，快取住等於用開盤時的數字管收盤"""

    from core.live.factory import make_account_fetcher
    from core.utils import InstrumentType

    broker: _StubBroker = _StubBroker(stock_equity=1.0, futures_equity=0.0)
    fetch = make_account_fetcher(broker, [_strategy(InstrumentType.STOCK)])

    fetch()
    fetch()

    assert broker.calls == ["stock", "stock"]


def test_trader_without_a_fetcher_falls_back_to_get_account() -> None:
    """未注入時退回券商的預設帳務查詢，既有呼叫端行為不變"""

    broker: _StubBroker = _StubBroker(stock_equity=123.0, futures_equity=0.0)
    trader: LiveTrader = LiveTrader.__new__(LiveTrader)
    trader.broker = broker
    trader.fetch_account = None

    assert trader._query_account_snapshot().total_equity == 123.0
