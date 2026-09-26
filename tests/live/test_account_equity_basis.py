from types import SimpleNamespace
from typing import Dict, List

from core.live.factory import live_capital
from core.live.trader import LiveTrader
from core.models import BrokerAccountSnapshot
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1

"""
帳戶總權益的口徑

額度檢查、單日虧損與批次曝險都拿它當基準，而它曾經被算成
「可用餘額 ＋ 各策略持倉占用」——那個算式少了兩樣東西：

1. **未交割款**（T+2 還沒入帳的錢）。
2. **不屬於任何策略的持倉**：接管來的 `__unattributed__` 不在任何策略的
   `Account` 裡，於是它的價值整個消失。

`ShioajiAccountQuery.get_stock_account()` 早就把 `total_equity` 算好了，
而且它的說明直接寫著「拿可用餘額當分母的話，只要隔日還有部位在場上就必然
誤判成額度超標」。2026-09-23 的演練正是如此：帳上 6 檔接管部位、現金為 0，
總權益被算成 0，三個段落全部在 `prepare()` 就拒絕啟動。
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


def test_positions_without_an_owner_still_count() -> None:
    """
    接管來的部位有價值，要算進總權益

    這是舊算式漏掉的那一半：`Σ used` 只加總「各策略的持倉占用」，
    而 `__unattributed__` 不是策略，它的部位在那個算式裡等於不存在。
    快照的 `total_equity` 是從券商部位清單直接加總的，不經過策略歸屬。
    """

    # 券商端：沒有現金，全部價值都在接管來的部位上
    trader: LiveTrader = make_trader(total_equity=514_890.0, quotas={"A": 400_000.0})

    assert trader._account_equity() > 0, "帳上有部位，總權益不該是 0"


def test_missing_snapshot_is_zero_not_a_crash() -> None:
    """還沒刷新過帳務時回 0，不是 AttributeError"""

    trader: LiveTrader = make_trader(
        total_equity=0.0, quotas={"A": 1.0}, has_snapshot=False
    )

    assert trader._account_equity() == 0.0


# === 模擬環境的退路 ===
def test_simulation_falls_back_to_declared_quota_when_equity_is_zero() -> None:
    """
    模擬環境帳務欄位全為 0 時，曝險與虧損檢查改以 Σ 宣告額度為基準

    那兩道檢查需要一個尺度，而「策略自己說要動用多少」是此刻唯一已知的尺度。
    **額度總量檢查不走這條**，見下一條測試。
    """

    trader: LiveTrader = make_trader(
        total_equity=0.0,
        quotas={"A": 400_000.0, "B": 3_000_000.0},
        simulation=True,
    )

    assert trader.equity_unavailable() is True
    assert trader._account_equity() == 3_400_000.0


def test_quota_check_is_skipped_rather_than_faked() -> None:
    """
    **查不到帳務時要略過額度檢查，不是捏一個數字讓它通過**

    以 Σ 宣告額度當基準的話，檢查會變成「Σ 額度 ≤ Σ 額度 × 安全係數」，
    因為安全係數小於 1 而**必然不成立**——2026-09-23 實測到期貨側正好差
    那 5%：3,000,000 vs 2,850,000。捏數字不只不誠實，還剛好行不通。
    """

    from core.live.capital_allocator import CapitalAllocator
    from core.live.risk.risk_config import CAPITAL_SAFETY_RATIO

    # **驗行為不驗方法名**：原本是比對 `inspect.getsource(prepare)` 含不含
    # `"equity_unavailable()"` 與 `"verify_quota"` 這兩個字面值——改名或內聯就紅，
    # 而行為根本沒變。改成直接問那道閘門本身。
    blind: LiveTrader = make_trader(
        total_equity=0.0, quotas={"A": 1_000_000.0}, simulation=True
    )
    visible: LiveTrader = make_trader(
        total_equity=582_608.0, quotas={"A": 400_000.0}, simulation=True
    )

    assert blind.equity_unavailable() is True, "模擬環境查不到帳務時要能自己說出來"
    assert visible.equity_unavailable() is False, "查得到帳務就不該略過檢查"

    # 釘住那個必然不成立的關係：安全係數 < 1。
    # **要讀真正的預設值**——自己設一個再斷言它小於 1 是同義反覆，
    # 有人把 `CAPITAL_SAFETY_RATIO` 改成 1.0 也照樣綠，而那正是這條要防的事
    allocator: CapitalAllocator = CapitalAllocator({"Alpha": 1_000_000.0})
    assert allocator.safety_ratio == CAPITAL_SAFETY_RATIO
    assert allocator.safety_ratio < 1.0


def test_production_with_zero_equity_stays_zero() -> None:
    """
    **正式環境不走退路**

    那裡的 0 是真的沒有錢。放寬的話，一個空帳戶會通過額度檢查、
    一路跑到盤中被券商退單——而那時已經有部位在場上。
    """

    trader: LiveTrader = make_trader(
        total_equity=0.0,
        quotas={"A": 400_000.0},
        simulation=False,
    )

    assert trader._account_equity() == 0.0


def test_simulation_with_real_equity_does_not_fall_back() -> None:
    """模擬環境查得到權益時照常用它，退路只在查不到時啟動"""

    trader: LiveTrader = make_trader(
        total_equity=514_890.0,
        quotas={"A": 400_000.0},
        simulation=True,
    )

    assert trader._account_equity() == 514_890.0


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

    from core.strategies.futures.momentum_futures_strategy import (
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
