import dataclasses
import datetime
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.api.tw.stock_dividend_api import StockDividendAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides
from core.broker.base import BaseBroker
from core.broker.rate_limiter import RateLimiter
from core.config.live_risk import LiveRiskOverrides, load_live_risk_overrides
from core.config.paths import LIVE_RISK_CONFIG_PATH
from core.config.settings import (
    LIVE_NOTIFY_CHANNEL,
    LIVE_NOTIFY_TARGET,
    LIVE_NOTIFY_TOKEN,
    now_live,
)
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import (
    AccountSynchronizer,
    FilledOrderBuilder,
    build_stock_order,
)
from core.live.after_close import TradeCostEstimator, weighted_fill_price
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.datafeed.tw.futures_live_datafeed import (
    TwFuturesLiveDataFeed,
    split_contract_id,
)
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.execution.base import BaseExecutionModel
from core.live.execution.futures import FuturesExecutionModel
from core.live.execution.stock import StockExecutionModel
from core.live.notify.base import BaseNotifier, NullNotifier
from core.live.notify.factory import build_notifier
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.report.parity_checker import TIMING_TO_PHASE, ParityChecker
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingModeState
from core.live.trader import LiveTrader, StrategyContext
from core.market.cost_model import BaseCostModel
from core.market.instrument_spec import InstrumentSpec
from core.market.segment import SegmentSchedule, SegmentWindow
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig
from core.market.tw.instrument_spec import TwFuturesSpec, TwStockSpec
from core.market.tw.segment_schedule import TW_FUTURES_SEGMENTS, TW_STOCK_SEGMENTS
from core.models import (
    BaseOrder,
    BasePosition,
    BrokerAccountSnapshot,
    FuturesAccount,
    FuturesOrder,
    PriceLimits,
    RealizedTradeSnapshot,
    StockAccount,
    StockPositionSnapshot,
)
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.models.risk_config import (
    RISK_CONFIG_BY_INSTRUMENT,
    AccountRiskConfig,
    BaseRiskConfig,
)
from core.portfolio.order_rules import is_opening, resolve_open_action
from core.position.assembly import (
    TwFuturesModels,
    TwStockModels,
    build_tw_futures_models,
    build_tw_stock_models,
)
from core.position.base.position_manager import BasePositionManager
from core.position.futures.position_manager import FuturesPositionManager
from core.strategies.base import BaseStrategy, resolve_hook_timing
from core.utils import (
    FUTURES_MULTIPLIER,
    Action,
    Commission,
    ExecutionTiming,
    FuturesRollRule,
    InstrumentType,
    LiveHook,
    Market,
    PositionType,
    Units,
)

"""
實盤 factory：組裝與分派只寫在這裡

引擎本體市場無關，市場語意全部在本檔以 `(market, instrument_type)` 分派——
與回測 factory 用同一組分派鍵。

**單例與逐策略要分清楚**：券商 session、限流器、委託管理、資金分配、歸屬帳與
帳戶層風控各只有一份（限流與委託回報都是帳戶級的，拆成多份必然互相不知情）；
帳戶、部位管理、資料源、風控設定則每支策略一份。

**清單只有一支時，行為與單策略完全相同**——多策略不是另一條程式路徑。
"""


class UnsupportedMarketError(ValueError):
    """沒有對應實作的（市場，商品）組合"""


def live_capital(strategy: BaseStrategy) -> float:
    """
    - Description:
        這支策略在實盤的資金額度；未宣告 `live_capital` 時沿用 `init_capital`

        **實盤與回測的資金必須能分開設**：`init_capital` 同時是回測帳戶的初始
        資金，而回歸基準就是拿策略跑出來的——改它等於改掉每一筆回測結果。
        實盤帳戶的規模是另一回事（模擬帳戶、正式帳戶、不同時期的本金都不同）。

        **額度與帳戶用同一個值**：帳戶若以 `init_capital` 建、額度卻用
        `live_capital`，策略會照大的那個算張數，再被額度檢查擋下來——
        看起來像風控太嚴，其實是兩處口徑不一致。
    - Parameters:
        - strategy: BaseStrategy
            策略實例
    - Return:
        - float
            實盤額度上限
    """

    declared: Optional[float] = getattr(strategy, "live_capital", None)
    return float(declared) if declared is not None else float(strategy.init_capital)


def apply_live_max_holdings(strategy: BaseStrategy) -> None:
    """
    - Description:
        策略宣告了 `live_max_holdings` 時，把它寫進這份實盤實例的 `max_holdings`

        **寫回實例而不是另開一個讀取點**：sizer、部位建構器與引擎的持倉檢查都讀
        `strategy.max_holdings`，各處改讀別的屬性就得改三個地方，漏一處就會
        「切資金用 3 檔、擋單用 10 檔」。只寫進實盤行程的實例與 parity 的當日回測
        副本（`make_daily_backtest_runner()`），研究回測不經過這裡。
    - Parameters:
        - strategy: BaseStrategy
            實盤載入的策略實例
    """

    declared: Optional[int] = getattr(strategy, "live_max_holdings", None)
    if declared is None:
        return

    logger.info(
        f"{type(strategy).__name__} 實盤最大持倉檔數 "
        f"{strategy.max_holdings} → {declared}（live_max_holdings）"
    )
    strategy.max_holdings = declared


def unattributed_stock_value(broker: BaseBroker, dao: LiveTradeDAO) -> float:
    """
    - Description:
        不屬於任何策略（`__unattributed__`）的**股票**部位，在券商總權益裡佔的金額

        **口徑與券商總權益相同**：`ShioajiAccountQuery.get_stock_account()` 對每個部位加的是
        「張數 × 1000 × 均價 ＋ 未實現損益」，這裡就扣同一個數。只扣成本的話，
        接管部位的未實現損益會留在資金基準裡（2026-10-02 模擬帳戶實測留下 104,485），
        而那同樣不是策略能動用的錢。

        同一檔、同方向的券商部位可能只有一部分是接管來的（其餘歸屬某支策略），
        故依接管 lot 的張數佔該部位的比例扣除。歸屬帳是股票與期貨兩個行程共用的，
        只取券商回報的**股票**部位比對。空單也要扣：券商總權益把空單一併加進去了。
    - Parameters:
        - broker: BaseBroker
            已登入的券商閘道
        - dao: LiveTradeDAO
            實盤紀錄庫
    - Return:
        - float
            金額；沒有接管部位時為 0
    """

    unattributed: Dict[Tuple[str, str], float] = {}
    for lot in dao.get_open_lots(strategy_name=UNATTRIBUTED_STRATEGY):
        key: Tuple[str, str] = (str(lot["symbol"]), str(lot["direction"]))
        unattributed[key] = unattributed.get(key, 0.0) + float(lot["volume"])

    value: float = 0.0
    for position in broker.get_positions():
        if not isinstance(position, StockPositionSnapshot) or position.volume <= 0:
            continue
        direction: str = getattr(position.direction, "value", str(position.direction))
        volume: float = unattributed.get((position.symbol, direction), 0.0)
        if volume <= 0:
            continue
        ratio: float = min(volume / position.volume, 1.0)
        value += ratio * (
            position.volume * Units.LOT * position.avg_price + position.unrealized_pnl
        )
    return value


def exclude_unattributed_stock(
    snapshot: BrokerAccountSnapshot, unattributed_value: float
) -> BrokerAccountSnapshot:
    """
    - Description:
        從股票帳戶的總權益扣掉不屬於任何策略的部位

        **那些部位不是策略可以動用的資金**：它們是接管來的舊持股，只允許平倉。
        算進總權益的話，一個沒有現金、只有舊持股的帳戶也會通過額度檢查，
        而真的送單時才被券商以資金不足退回。扣完不會是負數。
    - Parameters:
        - snapshot: BrokerAccountSnapshot
            券商的股票帳戶快照
        - unattributed_value: float
            不屬於任何策略的股票部位金額
    - Return:
        - BrokerAccountSnapshot
            扣除後的快照；扣掉的金額記在 `raw["unattributed_value"]`
    """

    return BrokerAccountSnapshot(
        ts=snapshot.ts,
        available_balance=snapshot.available_balance,
        total_equity=max(snapshot.total_equity - unattributed_value, 0.0),
        realized_pnl=snapshot.realized_pnl,
        unrealized_pnl=snapshot.unrealized_pnl,
        raw={**(snapshot.raw or {}), "unattributed_value": unattributed_value},
    )


def make_account_fetcher(
    broker: BaseBroker,
    strategies: Sequence[BaseStrategy],
    dao: Optional[LiveTradeDAO] = None,
) -> Callable[[], BrokerAccountSnapshot]:
    """
    - Description:
        組出「這一批策略該看哪個帳戶」的帳務查詢

        **股票與期貨是兩個子帳戶，各有各的錢**，故要依本批策略的商品取對應那一個。
        拿股票權益去檢查期貨策略的額度是在問另一筆錢的規模：兩邊量級差很多時，
        額度檢查永遠過不了；把額度壓進門檻又會讓可開口數變成 0，
        等於「通過閘門但整輪零交易」——兩種結果都不會報錯。

        兩種商品同時載入時把兩個帳戶相加：都是同一個人的錢，而額度檢查問的是
        「整體撐不撐得住」。
    - Parameters:
        - broker: BaseBroker
            已建立的券商閘道
        - strategies: Sequence[BaseStrategy]
            本次載入的策略
        - dao: Optional[LiveTradeDAO]
            實盤紀錄庫；提供時股票帳戶的總權益會扣掉不屬於任何策略的部位
            （`exclude_unattributed_stock()`）。**期貨帳戶不扣**：期貨權益裡的部位
            是以保證金計，接管來的期貨部位要扣多少需另定口徑，目前也沒有這種部位
    - Return:
        - Callable[[], BrokerAccountSnapshot]
            每次呼叫都重查一次的帳務查詢
    """

    def stock_account() -> BrokerAccountSnapshot:
        """股票帳戶；有紀錄庫時扣掉不屬於任何策略的部位"""

        snapshot: BrokerAccountSnapshot = broker.get_account()
        if dao is None:
            return snapshot
        return exclude_unattributed_stock(
            snapshot, unattributed_stock_value(broker, dao)
        )

    instruments: Set[Optional[InstrumentType]] = {
        strategy.instrument_type for strategy in strategies
    }
    wants_stock: bool = InstrumentType.STOCK in instruments
    wants_futures: bool = bool(instruments - {InstrumentType.STOCK})

    if wants_stock and not wants_futures:
        return stock_account
    if wants_futures and not wants_stock:
        return broker.get_futures_account

    def combined() -> BrokerAccountSnapshot:
        """兩個子帳戶相加"""

        stock: BrokerAccountSnapshot = stock_account()
        futures: BrokerAccountSnapshot = broker.get_futures_account()
        return BrokerAccountSnapshot(
            ts=stock.ts,
            available_balance=stock.available_balance + futures.available_balance,
            total_equity=stock.total_equity + futures.total_equity,
            unrealized_pnl=stock.unrealized_pnl + futures.unrealized_pnl,
            raw={"stock": stock.raw, "futures": futures.raw},
        )

    return combined


def build_live_trader(
    strategies: Sequence[BaseStrategy],
    broker: Optional[BaseBroker] = None,
    broker_kind: str = "shioaji",
    simulation: bool = True,
    dry_run: bool = False,
    resume_trading: bool = False,
    resume_strategies: Optional[Sequence[str]] = None,
    run_id: Optional[str] = None,
    dao: Optional[LiveTradeDAO] = None,
    live_risk_path: Optional[Path] = None,
    now_provider: Callable[[], datetime.datetime] = now_live,
    phase: str = "",
) -> LiveTrader:
    """
    - Description:
        組裝實盤引擎

        **收的是策略清單而不是單一策略**：多策略是常態，而歸屬帳、資金分配、
        風控分層三件事一旦有部位在場上就很難改，所以架構從一開始就以清單為準。
    - Parameters:
        - strategies: Sequence[BaseStrategy]
            本次要上線的策略
        - broker: Optional[BaseBroker]
            券商閘道；提供時忽略 `broker_kind`（測試注入 `FakeBroker` 用）
        - broker_kind: str
            `shioaji` 或 `fake`
        - simulation: bool
            是否連模擬環境
        - dry_run: bool
            走完整流程但不真的送出
        - resume_trading: bool
            人工恢復交易模式
        - resume_strategies: Optional[Sequence[str]]
            只恢復這幾支策略、帳戶層不動；None 時恢復帳戶層與所有已記錄的策略
        - run_id: Optional[str]
            本次啟動的識別碼；None 時以時戳產生
        - dao: Optional[LiveTradeDAO]
            實盤紀錄庫；None 時自行開啟
        - live_risk_path: Optional[Path]
            部署端的風控設定檔（只能收緊）；None 時用 `LIVE_RISK_CONFIG_PATH`。
            檔案不存在時只用策略宣告與預設值
        - now_provider: Callable[[], datetime.datetime]
            取得目前時間
        - phase: str
            本次要跑的段落名（實盤入口 `--phase` 的值），寫進 `live_run`；
            存活監控依它比對「該跑的段落有沒有跑」
    - Return:
        - LiveTrader
            組裝好的引擎
    - Raise:
        - UnsupportedMarketError
            某支策略的（市場，商品）組合沒有對應實作
        - ValueError
            策略清單為空、策略名稱重複，或風控設定不合法（宣告的類別與市場不符、
            部署設定比策略宣告寬或寫了不存在的欄位）
    """

    if not strategies:
        raise ValueError("策略清單為空，沒有東西可以跑")

    names: List[str] = [type(strategy).__name__ for strategy in strategies]
    duplicated: List[str] = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ValueError(
            f"策略名稱重複：{duplicated}。歸屬鏈以策略名為鍵"
            "（`live_order.strategy_name`、`live_position_lot`），"
            "重名會讓兩支策略的部位與損益混在一起，而合計仍然正確——對帳看不出來"
        )

    # 風控設定在組裝任何元件之前解析完：設定不合法是啟動錯誤，不該留下半套元件
    overrides: LiveRiskOverrides = load_live_risk_overrides(
        live_risk_path if live_risk_path is not None else LIVE_RISK_CONFIG_PATH
    )
    account_risk: AccountRiskConfig = AccountRiskConfig().with_overrides(
        overrides.account
    )
    strategy_risks: Dict[str, BaseRiskConfig] = {
        name: resolve_strategy_risk_config(name, strategy, overrides)
        for name, strategy in zip(names, strategies)
    }

    resolved_run_id: str = run_id or now_provider().strftime("%Y%m%d%H%M%S")
    resolved_dao: LiveTradeDAO = dao if dao is not None else LiveTradeDAO()
    resolved_dao.ensure_tables()

    # === 單例元件 ===
    rate_limiter: RateLimiter = RateLimiter()
    resolved_broker: BaseBroker = broker or _build_broker(
        broker_kind, simulation, rate_limiter
    )

    mode_state: TradingModeState = TradingModeState(
        resolved_dao,
        resolved_run_id,
        now_provider,
        resume_trading=resume_trading,
        resume_strategies=resume_strategies,
    )
    risk_manager: PreTradeRiskManager = PreTradeRiskManager(
        mode_state,
        account_risk,
        resolved_dao,
        resolved_run_id,
        now_provider=now_provider,
    )
    ledger: PositionAttributionLedger = PositionAttributionLedger(
        resolved_dao, now_provider, run_id=resolved_run_id
    )
    allocator: CapitalAllocator = CapitalAllocator(
        {name: live_capital(strategy) for name, strategy in zip(names, strategies)},
        resolved_dao,
        resolved_run_id,
        now_provider=now_provider,
    )
    order_manager: OrderManager = OrderManager(
        resolved_broker,
        resolved_dao,
        resolved_run_id,
        run_index=_derive_run_index(resolved_run_id),
        dry_run=dry_run,
        on_degrade=risk_manager.on_degrade_event,
        now_provider=now_provider,
        strategy_names=[type(strategy).__name__ for strategy in strategies],
    )

    # === 逐策略元件 ===
    contexts: List[StrategyContext] = []
    managers: Dict[str, BasePositionManager] = {}
    schedules: List[SegmentSchedule] = []

    for name, strategy in zip(names, strategies):
        # 要在組裝任何讀 `max_holdings` 的元件之前寫回
        apply_live_max_holdings(strategy)
        context, schedule = _build_context(
            name, strategy, resolved_broker, strategy_risks[name], now_provider
        )
        contexts.append(context)
        managers[name] = context.position_manager
        schedules.append(schedule)
        strategy.setup_account(context.account)
        context.data_feed.setup(strategy)
        # 資料源可能替沒宣告標的池的策略補上預設標的（見各市場資料源的
        # `fill_default_*()`），要在 setup 之後才讀；建 context 當下讀到的是空的
        context.symbols = list(getattr(strategy, "symbols", []) or [])

    order_builders: Dict[str, FilledOrderBuilder] = {
        context.name: context.build_filled_order
        for context in contexts
        if context.build_filled_order is not None
    }
    account_sync: AccountSynchronizer = AccountSynchronizer(
        managers,
        ledger,
        resolved_dao,
        now_provider,
        order_builders=order_builders,
        run_id=resolved_run_id,
    )
    # OMS 比逐策略元件先建（它是單例），建構器要等策略都組好才齊
    order_manager.order_rebuilder = make_order_rebuilder(order_builders, now_provider)
    reconciler: Reconciler = Reconciler(
        ledger,
        managers,
        resolved_dao,
        resolved_run_id,
        on_degrade=risk_manager.on_degrade_event,
        now_provider=now_provider,
    )
    conflict_guard: CrossStrategyConflictGuard = CrossStrategyConflictGuard(
        ledger, resolved_dao, resolved_run_id, now_provider
    )

    notifier: BaseNotifier = build_notifier(
        LIVE_NOTIFY_CHANNEL, LIVE_NOTIFY_TOKEN, LIVE_NOTIFY_TARGET
    )
    parity_checker: ParityChecker = ParityChecker(
        resolved_dao,
        make_daily_backtest_runner(strategies, resolved_dao.get_lots_held_at_start),
        strategy_names=[type(strategy).__name__ for strategy in strategies],
        closing_limit=make_closing_limit_lookup(),
        order_phases=make_order_phase_lookup(strategies),
    )
    # 先組好引擎、再寫 `live_run`：組裝過程中任何一步拋錯（例如兩個市場的時窗
    # 沒有交集）都是設定錯誤，那時不該留下一筆沒有結束時間的執行紀錄——
    # 下一次啟動會把它當成崩潰，發 CRITICAL 並觸發「以券商為準重建部位」
    trader: LiveTrader = LiveTrader(
        contexts=contexts,
        broker=resolved_broker,
        order_manager=order_manager,
        risk_manager=risk_manager,
        mode_state=mode_state,
        ledger=ledger,
        allocator=allocator,
        account_sync=account_sync,
        reconciler=reconciler,
        conflict_guard=conflict_guard,
        dao=resolved_dao,
        run_id=resolved_run_id,
        schedule=_merge_schedules(schedules),
        dry_run=dry_run,
        simulation=simulation,
        cost_estimator=make_trade_cost_estimator(contexts, simulation=simulation),
        # 查哪個帳戶是商品語意，由這裡決定後注入（引擎本體不認得「股票」「期貨」）
        fetch_account=make_account_fetcher(resolved_broker, strategies, resolved_dao),
        # 保證金查詢是期貨特性：有期貨策略時才注入，引擎本體不認得「期貨」
        margin_query=(
            getattr(resolved_broker, "get_futures_account", None)
            if any(
                context.calculate_opening_requirement is not None
                for context in contexts
            )
            else None
        ),
        # 引擎用它放行「帳戶層非 NORMAL」的送單路徑：只有帳戶層真的被恢復時才成立，
        # 指名恢復策略時帳戶層不動，不可讓它放行
        resume_trading=resume_trading and resume_strategies is None,
        notifier=notifier,
        now_provider=now_provider,
        parity_checker=parity_checker,
    )
    _record_run(
        resolved_dao,
        resolved_run_id,
        simulation,
        dry_run,
        not isinstance(notifier, NullNotifier),
        now_provider,
        phase,
        [type(strategy).__name__ for strategy in strategies],
        _run_instrument_type(strategies),
        live_tags={
            type(strategy).__name__: strategy.live_tag
            for strategy in strategies
            if strategy.live_tag
        },
        risk_config={
            "account": account_risk.to_dict(),
            "strategies": {
                name: {"class": type(config).__name__, **config.to_dict()}
                for name, config in strategy_risks.items()
            },
        },
    )

    return trader


def resolve_strategy_risk_config(
    name: str, strategy: BaseStrategy, overrides: LiveRiskOverrides
) -> BaseRiskConfig:
    """
    - Description:
        決定一支策略實際套用的風控門檻：策略宣告（沒有就用該市場預設）→ 部署設定收緊

        **宣告的類別必須與策略的市場相符**：股票策略宣告了 `FuturesRiskConfig` 的話，
        它的欄位（口數、名目槓桿）在股票的送單路徑上一條都用不到，等於沒有宣告，
        而且不會有任何錯誤訊息。
    - Parameters:
        - name: str
            策略名（部署設定以它分段）
        - strategy: BaseStrategy
            策略實例
        - overrides: LiveRiskOverrides
            部署設定
    - Return:
        - BaseRiskConfig
            實際套用的門檻
    - Raise:
        - ValueError
            宣告的類別與市場不符，或部署設定不合法
    """

    expected: Optional[type] = RISK_CONFIG_BY_INSTRUMENT.get(strategy.instrument_type)
    if expected is None:
        raise ValueError(
            f"{name} 的商品類別 {strategy.instrument_type} 沒有對應的實盤風控設定"
        )

    declared: Optional[BaseRiskConfig] = getattr(strategy, "live_risk_config", None)
    if declared is not None and type(declared) is not expected:
        raise ValueError(
            f"{name} 宣告的 live_risk_config 是 {type(declared).__name__}，"
            f"但它是 {strategy.instrument_type.value} 策略，應使用 {expected.__name__}"
        )

    base: BaseRiskConfig = declared if declared is not None else expected()
    try:
        return base.with_overrides(overrides.for_strategy(name))
    except ValueError as exc:
        raise ValueError(f"{name}：{exc}") from exc


def _run_instrument_type(strategies: Sequence[BaseStrategy]) -> Optional[str]:
    """
    本次行程載入的商品類別；只有一種才寫，混合或沒宣告時為 None

    股票與期貨各是一個行程、段落名相同，存活監控靠這一欄分辨是哪一條線沒跑。
    混合市場的行程組裝時就會被拒絕，這裡回 None 只是不替未定義的情況編一個值。
    """

    instruments: Set[Optional[InstrumentType]] = {
        strategy.instrument_type for strategy in strategies
    }
    if len(instruments) != 1:
        return None
    (instrument,) = instruments
    return instrument.value if instrument is not None else None


def make_order_rebuilder(
    order_builders: Dict[str, FilledOrderBuilder],
    now_provider: Callable[[], datetime.datetime] = now_live,
) -> Callable[[Dict[str, Any]], Optional[BaseOrder]]:
    """
    - Description:
        由 `live_order` 的一列還原原始訂單，給 OMS 重建委託用

        **建構器與帳戶同步共用同一份**（依策略注入，股票還原成 `StockOrder`、
        期貨還原成 `FuturesOrder`）：各寫一份的話，換月時兩邊會拆出不同的契約。
        標的或數量是空的列（舊版被清空過的紀錄）還原不出來，回 None。
    - Parameters:
        - order_builders: Dict[str, FilledOrderBuilder]
            `{策略名: 還原訂單的建構器}`；未提供的策略退回股票訂單
        - now_provider: Callable[[], datetime.datetime]
            紀錄缺送單時間時的替代值
    - Return:
        - Callable[[Dict[str, Any]], Optional[BaseOrder]]
            還原函式
    """

    def rebuild(row: Dict[str, Any]) -> Optional[BaseOrder]:
        """依該列的策略取建構器還原訂單；標的或數量是空的列回 None"""

        if not row.get("symbol") or not int(row.get("volume") or 0):
            return None

        builder: FilledOrderBuilder = order_builders.get(
            str(row["strategy_name"]), build_stock_order
        )
        created_at: Any = row.get("created_at")
        return builder(
            str(row["symbol"]),
            datetime.datetime.fromisoformat(str(created_at))
            if created_at
            else now_provider(),
            Action(str(row["action"])),
            PositionType(str(row["position_type"])),
            float(row.get("price") or 0.0),
            int(row["volume"]),
        )

    return rebuild


def _build_broker(
    broker_kind: str, simulation: bool, rate_limiter: RateLimiter
) -> BaseBroker:
    """
    建立券商閘道

    `fake` 只給測試用，**正式環境一律拒絕**——一個打錯的參數不該讓真單變成假單，
    反過來更不行。
    """

    if broker_kind == "fake":
        if not simulation:
            raise ValueError("正式環境不可使用 fake 券商")
        raise ValueError("fake 券商請由測試直接注入 `broker=`，不經由 factory 建立")

    if broker_kind != "shioaji":
        raise UnsupportedMarketError(f"不支援的券商：{broker_kind}（目前只有 shioaji）")

    # **延後 import**：只有真的要連券商時才需要 SDK。寫在模組層級的話，
    # 任何一個 `from core.live.factory import ...` 都會把整包 SDK 拉進來，
    # 沒裝的環境當場 ImportError——而回測不該為了它付 import 成本，
    # 也不該因為它壞掉而跑不動
    from core.broker.tw.shioaji_broker import ShioajiBroker
    from core.broker.tw.shioaji_session import ShioajiSession

    session: ShioajiSession = ShioajiSession(
        simulation=simulation, rate_limiter=rate_limiter
    )
    return ShioajiBroker(session, rate_limiter)


def _build_context(
    name: str,
    strategy: BaseStrategy,
    broker: BaseBroker,
    risk_config: BaseRiskConfig,
    now_provider: Callable[[], datetime.datetime],
) -> Tuple[StrategyContext, SegmentSchedule]:
    """依 `(market, instrument_type)` 分派出這支策略需要的一整組元件"""

    market: Optional[Market] = strategy.market
    instrument: Optional[InstrumentType] = strategy.instrument_type

    if market == Market.TW and instrument == InstrumentType.STOCK:
        account: StockAccount = StockAccount(init_capital=live_capital(strategy))
        # 與回測同一組法：成本設定依策略宣告推導（當沖策略的空單走現股當沖沖賣），
        # 不再一律用預設費率——否則實盤本地帳與回測記出不同的帳，且不會有任何錯誤訊息
        stock_models: TwStockModels = build_tw_stock_models(strategy, account)
        cost_model: BaseCostModel = stock_models.cost_model
        manager: BasePositionManager = stock_models.position_manager
        feed: BaseLiveDataFeed = TwStockLiveDataFeed(broker, now_provider=now_provider)
        spec: TwStockSpec = TwStockSpec()
        schedule: SegmentSchedule = TW_STOCK_SEGMENTS
        build_order: FilledOrderBuilder = build_stock_order
        opening_requirement: Optional[Callable[[BaseOrder], float]] = None
        # 股票的資金占用與曝險是同一個數（價 × 股數）
        notional: Callable[[BaseOrder], float] = _make_contract_value_calculator(
            spec, now_provider
        )
        exposure: Callable[[BaseOrder], float] = notional
        execution_model: BaseExecutionModel = StockExecutionModel(
            price_limits=feed.get_price_limits
        )

    elif market == Market.TW and instrument == InstrumentType.FUTURE:
        futures_account: FuturesAccount = FuturesAccount(
            init_capital=live_capital(strategy)
        )
        account = futures_account
        # 保證金設定與回測同一套：策略沒宣告就預設查表，並回寫給策略，
        # 讓策略層與部位管理層算的每口保證金是同一份（表由資料源注入）
        margin_config: FuturesMarginConfig = (
            getattr(strategy, "margin_config", None) or FuturesMarginConfig.default()
        )
        strategy.margin_config = margin_config
        roll_config: FuturesRollConfig = to_live_roll_config(
            getattr(strategy, "roll_config", None) or FuturesRollConfig()
        )
        strategy.roll_config = roll_config
        futures_feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
            broker,
            now_provider=now_provider,
            margin_config=margin_config,
            roll_config=roll_config,
        )
        feed = futures_feed
        # 與回測同一組法：成本設定讀策略宣告、保證金模型與部位管理層共用同一個、
        # 乘數由資料源解析（股期的契約單位不在常數表，退回常數表會 KeyError）。
        # 股期標的股價在實盤沒有來源，比例表那條路暫不接
        futures_models: TwFuturesModels = build_tw_futures_models(
            strategy,
            futures_account,
            multiplier_resolver=futures_feed.resolve_multiplier,
        )
        cost_model = futures_models.cost_model
        futures_manager: FuturesPositionManager = futures_models.position_manager
        manager = futures_manager
        opening_requirement = _make_opening_requirement(futures_manager, now_provider)
        # 資金占用是保證金、曝險是契約價值：一口台指期押約 70 萬、承擔約 900 萬的風險
        # 乘數解析器與部位管理層同一個：spec 與記帳算出的乘數不會分岔
        futures_spec: TwFuturesSpec = TwFuturesSpec(
            multiplier_resolver=futures_feed.resolve_multiplier
        )
        notional = _make_futures_margin_calculator(
            futures_manager, futures_spec, now_provider
        )
        exposure = _make_contract_value_calculator(futures_spec, now_provider)
        schedule = TW_FUTURES_SEGMENTS
        build_order = _build_futures_order
        execution_model = FuturesExecutionModel(price_limits=feed.get_price_limits)

    else:
        raise UnsupportedMarketError(
            f"{name} 的組合（market={market}, instrument_type={instrument}）"
            "沒有對應的實盤實作"
        )

    context: StrategyContext = StrategyContext(
        strategy=strategy,
        account=account,
        position_manager=manager,
        cost_model=cost_model,
        data_feed=feed,
        symbols=list(getattr(strategy, "symbols", []) or []),
        calculate_notional=notional,
        calculate_exposure=exposure,
        risk_config=risk_config,
        build_filled_order=build_order,
        calculate_opening_requirement=opening_requirement,
        execution_model=execution_model,
    )
    return (context, schedule)


def make_trade_cost_estimator(
    contexts: Optional[Sequence[StrategyContext]] = None,
    simulation: bool = False,
) -> TradeCostEstimator:
    """
    - Description:
        盤後校正成本用的估算器：依成本模型重算一筆已平倉交易的手續費與稅

        **成本模型取實盤記帳用的那一個**（各策略脈絡裡的 `cost_model`），校正比對的
        才是「實盤記帳用的那套」估得準不準。費率是帳戶層級的設定，同一個帳戶跑多支
        策略時取第一支該市場策略的成本模型；沒有該市場的策略才退回預設費率。
        - 期貨：**只估平倉那一腿**（一次手續費＋平倉價的期交稅），與券商欄位的範圍一致——
          2026-09-22 模擬環境實測，台指期一口來回的已實現紀錄 `fee=50`、`tax=193`，
          正好是單邊手續費與單邊期交稅（開平倉價的稅在這個價位都是 193，分不出是哪一腿；
          平倉回報列的是平倉，故取平倉腿）。照來回估的話每筆都會被誤報 -50%。
          乘數取 `FUTURES_MULTIPLIER`，不在表內（股票期貨）時回 None。
        - 股票：手續費開平倉各一次；證交稅只課賣出那一腿——多單課在平倉價、
          空單課在開倉價；開倉日與交易日同一天時用當沖稅率；ETF 課 ETF 稅率。缺開倉成交時回 None。
        - **模擬環境的股票手續費改用模擬券商的折扣**（`Commission.SimulationDiscount`）：
          模擬環境以 5 折計、記帳用的是正式帳戶的折扣，不換的話每一筆個股都會被誤報偏高。
          只換校正用的這一份，實盤記帳的費率不動。
    - Parameters:
        - contexts: Optional[Sequence[StrategyContext]]
            已組好的策略脈絡；None 或沒有該市場的策略時用預設費率
        - simulation: bool
            是否為模擬環境
    - Return:
        - TradeCostEstimator
            估算器
    """

    stock_cost: TwStockCostModel = next(
        (
            context.cost_model
            for context in contexts or []
            if isinstance(context.cost_model, TwStockCostModel)
        ),
        TwStockCostModel(CostConfig.default()),
    )
    if simulation:
        stock_cost = TwStockCostModel(
            dataclasses.replace(
                stock_cost.config, comm_discount=float(Commission.SimulationDiscount)
            )
        )
    futures_cost: TwFuturesCostModel = next(
        (
            context.cost_model
            for context in contexts or []
            if isinstance(context.cost_model, TwFuturesCostModel)
        ),
        TwFuturesCostModel(FuturesCostConfig.default()),
    )

    def estimate(
        trade: RealizedTradeSnapshot,
        opening: List[Dict[str, Any]],
        run_date: datetime.date,
    ) -> Optional[float]:
        """依市場重算一筆已平倉交易的手續費與稅；算不出來時回 None"""

        quantity: int = trade.quantity
        if trade.is_futures:
            product, _ = split_contract_id(trade.symbol)
            multiplier: Optional[int] = FUTURES_MULTIPLIER.get(product)
            if multiplier is None or trade.entry_price is None:
                return None
            return float(
                futures_cost.commission(volume=quantity, product=product)
                + futures_cost.tax(trade.cover_price, quantity, multiplier)
            )

        if not opening:
            return None
        entry: float = weighted_fill_price(opening)
        opened_on: datetime.date = datetime.date.fromisoformat(
            str(opening[0]["filled_at"])[:10]
        )
        is_day_trade: bool = opened_on == run_date
        commission: int = stock_cost.commission(
            entry, quantity
        ) + stock_cost.commission(trade.cover_price, quantity)
        if str(opening[0]["action"]) == Action.BUY.value:
            tax: int = stock_cost.tax(
                trade.cover_price,
                quantity,
                Action.SELL,
                is_day_trade=is_day_trade,
                date=run_date,
                symbol=trade.symbol,
            )
        else:
            tax = stock_cost.tax(
                entry,
                quantity,
                Action.SELL,
                is_day_trade=is_day_trade,
                date=opened_on,
                symbol=trade.symbol,
            )
        return float(commission + tax)

    return estimate


def to_live_roll_config(config: FuturesRollConfig) -> FuturesRollConfig:
    """
    - Description:
        把策略的換月設定換成實盤做得到的版本（**不改動原物件**）

        **實盤最晚要在最後交易日的前一個交易日換月。**
        回測的 `LAST_TRADING_DAY` 是撐過最後交易日、隔天才以結算價平掉舊月；
        實盤做不到——台指期最後交易日 13:30 就收盤（期貨尾盤段 13:30 起），
        過了那天交易所已現金結算，券商端部位消失、本地歸屬帳卻還在。
        故 `LAST_TRADING_DAY` 與「提前 0 日」都轉成「提前 1 個交易日」。
        與回測差一天（最後一天的曝險在次月），parity 在換月日會有可解釋的差異。

        策略挑合約（`select_near_month()`）與轉倉共用這份轉換後的設定，
        兩者才不會出現「訊號在近月、部位已換到次月」。
    - Parameters:
        - config: FuturesRollConfig
            策略宣告的換月設定
    - Return:
        - FuturesRollConfig
            實盤用的換月設定
    - Raise:
        - ValueError
            `OPEN_INTEREST` 規則：它要當日的未沖銷量，實盤盤中取不到
    """

    if config.rule is FuturesRollRule.OPEN_INTEREST:
        raise ValueError(
            "實盤不支援 OPEN_INTEREST 換月規則：它比較的是當日未沖銷量，"
            "盤中取不到；請改用 DAYS_BEFORE_EXPIRY"
        )

    days: int = (
        1
        if config.rule is FuturesRollRule.LAST_TRADING_DAY
        else max(config.days_before_expiry, 1)
    )
    if config.rule is FuturesRollRule.LAST_TRADING_DAY or days != (
        config.days_before_expiry
    ):
        logger.warning(
            f"換月規則 {config.rule.value}（提前 {config.days_before_expiry} 日）"
            "在實盤改為最後交易日前 1 個交易日換月：最後交易日當天的尾盤段"
            "舊月已收盤，撐到那天就只能被交易所結算"
        )

    return FuturesRollConfig(
        rule=FuturesRollRule.DAYS_BEFORE_EXPIRY,
        days_before_expiry=days,
        enabled=config.enabled,
        calendar=config.calendar,
    )


def _make_opening_requirement(
    manager: FuturesPositionManager, now_provider: Callable[[], datetime.datetime]
) -> Callable[[BaseOrder], float]:
    """
    期貨開倉需要的資金，**以今天查保證金表**

    策略產生的訂單日期是訊號所依據的那根 bar（前一交易日），保證金卻要用
    送單當天生效的那一檔——期交所調整保證金的生效日正是用來決定這件事的。
    """

    def calculate(order: BaseOrder) -> float:
        """開倉需要的原始保證金與開倉成本（保證金表以今天為準）"""

        return manager.calculate_opening_requirement(order, now_provider().date())

    return calculate


def make_daily_backtest_runner(
    strategies: Sequence[BaseStrategy],
    held_lots: Optional[Callable[[str, datetime.date], List[Dict[str, Any]]]] = None,
) -> Callable[[str, datetime.date], List[BaseOrder]]:
    """
    - Description:
        產生 parity 比對用的「跑一天回測，回傳會送出的委託」函式

        **組裝寫在這裡而不是比對器裡**：跑回測要 `core.backtest.factory`（組裝層），
        而 `core/live/report/` 在元件層——比對器自己 import 它就是反向相依。
        注入之後，比對邏輯的測試也不必真的跑一場回測。

        **每次呼叫都重建一份策略實例**：實盤那份已經帶著當天的帳戶與持倉，
        拿它跑回測會讓回測從「今天的部位」開始，而比對基準應該是
        「這支策略單獨從零跑這一天會送什麼單」。

        **資金與檔數照實盤的口徑**（`live_capital`、`live_max_holdings`）：parity 量的是
        訊號有沒有漂移，不是兩邊設定不同。用研究回測的 `init_capital`／`max_holdings`
        的話，宣告了實盤額度的策略每天都會因為張數與檔數不同而報未解釋差異——
        `VolumeBreakoutMomentumStrategy` 實盤 40 萬切 3 檔、研究回測 100 萬切 10 檔，
        2026-10-06 試跑的三筆未解釋差異全是這個原因。

        **起始部位＝實盤當天開盤前的持倉**（`held_lots`）：從零部位跑的話，回測帳上
        沒有前一天留下的部位，實盤當天的平倉單在回測一定沒有對應，平倉日必定報
        未解釋差異——開倉→次日平倉這組驗收永遠過不了。順帶的效果是：開倉沒成交
        而被放棄的單，實盤沒有部位、回測也從沒有部位出發，次日不會多出平倉單。
    - Parameters:
        - strategies: Sequence[BaseStrategy]
            本次實盤載入的策略實例；只用來取類別與 `init_capital`
        - held_lots: Optional[Callable[[str, datetime.date], List[Dict[str, Any]]]]
            `(策略名, 交易日) → 開盤前持有的 lot`（`LiveTradeDAO.get_lots_held_at_start()`）；
            None 時從零部位跑
    - Return:
        - Callable[[str, datetime.date], List[BaseOrder]]
            `(策略名, 交易日) → 該日回測會送出的委託清單`
    """

    blueprints: Dict[str, BaseStrategy] = {
        type(strategy).__name__: strategy for strategy in strategies
    }

    def run(strategy_name: str, run_date: datetime.date) -> List[BaseOrder]:
        """以新建的策略實例跑一天回測，回傳它會送出的委託"""

        blueprint: Optional[BaseStrategy] = blueprints.get(strategy_name)
        if blueprint is None:
            raise UnsupportedMarketError(f"本次執行沒有載入策略 {strategy_name}")

        replica: BaseStrategy = type(blueprint)()
        apply_live_max_holdings(replica)

        # 不寫報表與 backtest log：盤後每天跑一次，寫的話會蓋掉
        # `results/<策略>/` 的研究用回測，並把實盤的 log 一併寫進 backtest log
        backtester: Backtester = build_backtester(
            replica,
            write_artifacts=False,
            overrides=BacktestOverrides(
                start=run_date, end=run_date, capital=live_capital(replica)
            ),
        )
        if held_lots is not None:
            _seed_positions(backtester, replica, held_lots(strategy_name, run_date))
        backtester.run()
        return [order for _, _, order in backtester.submitted_orders]

    return run


def make_order_phase_lookup(
    strategies: Sequence[BaseStrategy],
) -> Callable[[str, BaseOrder], Set[str]]:
    """
    - Description:
        產生 parity 用的「回測這張單該在哪些段落送出」查詢

        開倉單看 `open` 鉤子的段落；平倉單同時看 `close` 與 `stop_loss`——回測的委託分不出
        是一般平倉還是停損，兩個段落都要中止，parity 才能把它歸給段落中止。
        段落沿用實盤引擎同一套解析（`resolve_hook_timing()`，沒宣告的鉤子會退回 `close`），
        兩邊各算一份的話，宣告改了只有一邊跟上。
    - Parameters:
        - strategies: Sequence[BaseStrategy]
            本次行程載入的策略
    - Return:
        - Callable[[str, BaseOrder], Set[str]]
            `(策略名, 回測委託) → 段落名集合`；認不得的策略或沒有段落時為空集合
    """

    by_name: Dict[str, BaseStrategy] = {
        type(strategy).__name__: strategy for strategy in strategies
    }

    def lookup(strategy_name: str, order: BaseOrder) -> Set[str]:
        """這張回測委託該送出的段落名；任一鉤子解析不出段落就回空集合"""

        strategy: Optional[BaseStrategy] = by_name.get(strategy_name)
        if strategy is None:
            return set()
        opening: bool = is_opening(order)
        hooks: List[LiveHook] = (
            [LiveHook.OPEN] if opening else [LiveHook.CLOSE, LiveHook.STOP_LOSS]
        )
        phases: Set[str] = set()
        for hook in hooks:
            timing: Optional[ExecutionTiming] = resolve_hook_timing(strategy, hook)
            if timing is None or timing not in TIMING_TO_PHASE:
                return set()
            phases.add(TIMING_TO_PHASE[timing])
        return phases

    return lookup


def make_closing_limit_lookup(
    api_factory: Callable[[], StockPriceAPI] = StockPriceAPI,
    dividend_api_factory: Callable[[], StockDividendAPI] = StockDividendAPI,
) -> Callable[[str, str, datetime.date], Optional[Tuple[float, float]]]:
    """
    - Description:
        產生 parity 用的「當日收盤價與漲跌停價」查詢

        集合競價的 `MARKET` 委託掛在漲跌停卻沒成交時，parity 要確認收盤是不是真的鎖住，
        才能歸「鎖漲跌停」；收盤沒鎖住卻沒成交是另一回事（例如模擬環境不撮合集合競價）。

        **漲跌停基準與回測同一口徑**：前一交易日收盤，除權息日改用交易所公告的
        開盤競價基準（`StockDividendAPI.get_opening_reference_price_map()`，與
        `TwStockFillModel.apply_price_limit_basis()` 同一來源），再以
        `TwStockSpec.get_price_limits()` 推算。兩邊口徑不同時，除權息日鎖住的單會被
        歸到 `UNFILLED`，沒鎖住的也可能被誤判成鎖住。

        **同一天只查一次資料庫**：一天的差異可能有好幾檔，逐檔連線查詢沒有意義。
    - Parameters:
        - api_factory: Callable[[], StockPriceAPI]
            建立價格 API 的函式；測試以此注入假的資料來源
        - dividend_api_factory: Callable[[], StockDividendAPI]
            建立除權息 API 的函式（開盤競價基準）；測試以此注入假的資料來源
    - Return:
        - Callable[[str, str, datetime.date], Optional[Tuple[float, float]]]
            `(標的, 買賣別, 交易日) → (收盤價, 該方向的漲跌停價)`；當日或前一交易日
            沒有該檔收盤時回 None
    """

    spec: TwStockSpec = TwStockSpec()
    cache: Dict[datetime.date, Tuple[Dict[str, Any], Dict[str, Any]]] = {}

    def load(run_date: datetime.date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """取當日收盤價與漲跌停基準（前收，除權息日改用開盤競價基準）；同一天只查一次"""

        if run_date not in cache:
            with api_factory() as api:
                # 往前 30 個曆日足以跨過任何連假
                days: List[datetime.date] = api.get_trading_days(
                    run_date - datetime.timedelta(days=30),
                    run_date - datetime.timedelta(days=1),
                )
                closes: Dict[str, Any] = api.get_close_map(run_date)
                prev_closes: Dict[str, Any] = (
                    api.get_close_map(days[-1]) if days else {}
                )
            # 除權息日的開盤競價基準覆寫前收：與回測 `apply_price_limit_basis()` 同一條規則
            with dividend_api_factory() as dividend:
                basis: Dict[str, float] = dividend.get_opening_reference_price_map(
                    run_date
                )
            cache[run_date] = (closes, {**prev_closes, **basis})
        return cache[run_date]

    def lookup(
        symbol: str, side: str, run_date: datetime.date
    ) -> Optional[Tuple[float, float]]:
        """當日收盤價與該方向的漲跌停價；任一項取不到時回 None"""

        closes, references = load(run_date)
        close: Optional[float] = _positive_price(closes.get(symbol))
        reference: Optional[float] = _positive_price(references.get(symbol))
        if close is None or reference is None:
            return None

        limits: PriceLimits = spec.get_price_limits(reference, run_date, symbol)
        limit: Optional[float] = (
            limits.limit_up if side == Action.BUY.value else limits.limit_down
        )
        if limit is None:
            return None
        return (close, float(limit))

    return lookup


def _positive_price(value: Any) -> Optional[float]:
    """價格欄位轉成正數 float；缺值、`NaN`、0 或無法轉換時回 None（無成交日收盤可能是 `NaN`）"""

    try:
        price: float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(price) or price <= 0:
        return None
    return price


def _seed_positions(
    backtester: Backtester, strategy: BaseStrategy, lots: Sequence[Dict[str, Any]]
) -> None:
    """
    把實盤開盤前的持倉放進回測帳戶

    **走開倉的正常路徑**（補值放空管道 → `open_position()`）：直接塞部位物件會略過
    成本與保證金的扣帳，回測帳上的可用資金就比實盤多，開倉張數跟著不同。
    訂單型別依市場決定，與帳戶同步共用同一組建構器。
    """

    builder: FilledOrderBuilder = (
        _build_futures_order
        if strategy.instrument_type == InstrumentType.FUTURE
        else build_stock_order
    )
    for lot in lots:
        position_type: PositionType = PositionType(str(lot["direction"]))
        order: BaseOrder = builder(
            str(lot["symbol"]),
            datetime.date.fromisoformat(str(lot["open_date"])[:10]),
            resolve_open_action(position_type),
            position_type,
            float(lot["open_price"]),
            int(lot["volume"]),
        )
        for enriched in backtester.enrich_orders([order]):
            backtester.position_manager.open_position(enriched)


def _build_futures_order(
    symbol: str,
    date: Any,
    action: Action,
    position_type: PositionType,
    price: float,
    volume: int,
) -> BaseOrder:
    """
    期貨版的還原建構器

    `FuturesPositionManager` 會讀 `order.product` 與 `order.contract_id`，
    故一定要把契約代號拆回 product／expiry，不能只塞 symbol。
    """

    product, expiry = split_contract_id(symbol)
    return FuturesOrder(
        product=product,
        expiry=expiry,
        date=date,
        action=action,
        position_type=position_type,
        price=price,
        volume=volume,
    )


def _make_contract_value_calculator(
    spec: InstrumentSpec, now_provider: Callable[[], datetime.datetime]
) -> Callable[[BaseOrder], float]:
    """
    - Description:
        委託（或部位）的契約價值：價格 × 數量 × 計價單位（`InstrumentSpec.get_unit_size()`）

        **兩個市場同一個公式，單位由 spec 決定**：台股一張 1000 股、期貨一口乘契約乘數
        （由注入 spec 的解析器查，與部位管理層同一個來源，股期也查得到）。
        股票的資金占用與曝險都是它；期貨只有曝險（名目槓桿）用它，資金占用另算保證金，
        見 `_make_futures_margin_calculator()`。
    - Parameters:
        - spec: InstrumentSpec
            商品規格
        - now_provider: Callable[[], datetime.datetime]
            取得目前時間；計價單位以今天為準（股期的契約單位會隨除權息調整）
    - Return:
        - Callable[[BaseOrder], float]
            委託或部位 → 契約價值
    """

    def calculate(order: BaseOrder) -> float:
        """價格 × 數量 × 計價單位"""

        unit: int = spec.get_unit_size(spec.resolve_product(order), now_provider())
        return float(order.price) * float(order.volume) * float(unit)

    return calculate


def _make_futures_margin_calculator(
    manager: FuturesPositionManager,
    spec: InstrumentSpec,
    now_provider: Callable[[], datetime.datetime],
) -> Callable[[BaseOrder], float]:
    """
    - Description:
        期貨的資金占用：原始保證金（**以今天查保證金表**）

        風控的單筆與當日上限、資金保留、已用額度都用它。以契約價值計的話，
        一口台指期就是千萬級，任何合理的額度連一口都送不出去；只算點數 × 口數
        （不乘乘數）則一口只算幾萬元，金額類風控等於沒有作用。

        **未平倉部位用它記帳時繳的保證金，不重新查表**：已用額度與權益在段落一開始就要算，
        當天的保證金表若查不到，重新查表會讓整個段落起不來——連平倉都送不出去。
        委託才查表；查不到時拋 `ValueError`，開倉單在那之前已被保證金檢查擋下。
    - Parameters:
        - manager: FuturesPositionManager
            部位管理層（保證金表與記帳同一份）
        - spec: InstrumentSpec
            商品規格；契約乘數由它提供，與契約價值同一個來源
        - now_provider: Callable[[], datetime.datetime]
            取得目前時間；保證金與乘數都以今天為準
    - Return:
        - Callable[[BaseOrder], float]
            委託或部位 → 原始保證金
    """

    def calculate(order: BaseOrder) -> float:
        """部位取記帳時繳的保證金；委託（或沒有紀錄的部位）以今天查保證金表"""

        recorded: float = float(getattr(order, "margin", 0.0) or 0.0)
        if isinstance(order, BasePosition) and recorded > 0:
            return recorded

        today: datetime.date = now_provider().date()
        product: Optional[str] = spec.resolve_product(order)
        multiplier: int = spec.get_unit_size(product, today)
        return float(
            manager.calculate_margin(
                order.price, order.volume, multiplier, product=product, date=today
            )
        )

    return calculate


def _merge_schedules(schedules: Sequence[SegmentSchedule]) -> SegmentSchedule:
    """
    - Description:
        合併各策略的段落時窗；同一段落取**交集**（最晚開始、最早結束、最晚收線）

        取交集而不是聯集：在某個市場還沒開盤的時候送單，那些單會被退。

        **時窗不重疊時拒絕合併，而不是湊一個出來。** 台股尾盤是 13:25~13:29、
        期貨是 13:30~13:44——兩者**根本沒有交集**。硬湊會得到一個
        「開始晚於結束」的窗，那等於整段都不送單，而且不會有任何錯誤訊息。
        真正的解法是**分開排程**：`--phase close` 對台股與對期貨各跑一次，
        排程也要各排一行。
    - Parameters:
        - schedules: Sequence[SegmentSchedule]
            各策略的段落時窗
    - Return:
        - SegmentSchedule
            合併後的時窗
    - Raise:
        - ValueError
            同一段落的時窗沒有交集
    """

    merged: Dict[ExecutionTiming, SegmentWindow] = {}
    for schedule in schedules:
        for timing, window in schedule.items():
            current: Optional[SegmentWindow] = merged.get(timing)
            if current is None:
                merged[timing] = window
                continue

            start: datetime.time = max(current.submit_start, window.submit_start)
            end: datetime.time = min(current.submit_end, window.submit_end)
            if start > end:
                raise ValueError(
                    f"段落 {timing.value} 的時窗沒有交集："
                    f"{current.submit_start}~{current.submit_end} 與 "
                    f"{window.submit_start}~{window.submit_end}。"
                    "不同市場的送單時點不同時**要分開排程**，"
                    "硬湊出來的時窗等於整段都不送單，而且不會有任何錯誤訊息"
                )

            merged[timing] = SegmentWindow(
                submit_start=start,
                submit_end=end,
                drain_end=max(current.drain_end, window.drain_end),
            )
    return merged


def _derive_run_index(run_id: str) -> int:
    """
    由 run_id 推導壓縮碼用的 run 序號

    只取數字尾段再取模：同一個交易日內不同時間啟動會得到不同的序號，
    而那正是壓縮碼要能區分的東西。
    """

    digits: str = "".join(char for char in run_id if char.isdigit())
    return int(digits[-6:] or 0) % 1296


def _record_run(
    dao: LiveTradeDAO,
    run_id: str,
    simulation: bool,
    dry_run: bool,
    notify_enabled: bool,
    now_provider: Callable[[], datetime.datetime],
    phase: str,
    strategy_names: Sequence[str] = (),
    instrument_type: Optional[str] = None,
    risk_config: Optional[Dict[str, Any]] = None,
    live_tags: Optional[Dict[str, str]] = None,
) -> None:
    """
    寫入啟動紀錄，含稽核欄位

    **稽核欄位不是可有可無**：實盤出事時第一個要回答的是「那天那張單是哪一版程式
    送出的」。取不到 git commit 時記 `unknown` 並警告，**不阻擋啟動**——
    容器內沒有 `.git` 是正常的。

    `notify_enabled` 同理：**通知缺設定時退化為不推播，但那件事要落地**。
    只記一行 warning 的話，事後回頭查「那天為什麼沒收到告警」會查不到答案——
    而「以為有告警其實沒有」比「知道沒有告警」危險得多。

    **本次載入的策略寫進 `strategy_params_json`**（`{"strategies": [...]}`）：盤後 parity
    要知道某支策略當天的段落有沒有正常跑完，才分得出「回測多出的開倉單」是快照口徑
    還是實盤根本沒跑到。這一欄先前從未寫入，2026-10-08 以前的紀錄為 NULL。
    有設定 `live_tag` 的策略另記在同一欄的 `live_tags`（`{策略類別名: 代號}`）：策略代號
    宣稱「只寫本地紀錄」，這裡就是它落地的地方；沒設定的策略不列，免得每筆紀錄都帶一串空字串。

    **商品類別寫進 `instrument_type`**：股票與期貨是兩個行程、段落名相同，存活監控
    要靠它分辨「期貨尾盤沒跑」而不是被股票尾盤的紀錄遮住。更早的紀錄為 NULL。

    **實際生效的風控門檻寫進 `risk_config_json`**（帳戶層一份、各策略各一份，已套用部署設定）：
    風控值會因策略宣告與部署設定而不同，拒單數突然變了時要能回答「那天的上限是多少」。
    這一欄先前從未寫入。
    """

    if not notify_enabled:
        logger.warning(
            "本次啟動沒有可用的通知管道：異常只會寫進 log 與 live_risk_event，"
            "不會主動通知任何人"
        )

    dao.insert_run(
        {
            "run_id": run_id,
            "started_at": now_provider(),
            # 判斷「另一個段落是不是還活著」的依據：排程刻意重疊，
            # 少了它就會把還在送單的同伴標成崩潰
            "pid": os.getpid(),
            "phase": phase,
            "simulation": int(simulation),
            "dry_run": int(dry_run),
            "notify_enabled": int(notify_enabled),
            "git_commit": _git_commit(),
            "shioaji_version": _shioaji_version(),
            "strategy_params_json": json.dumps(
                {
                    "strategies": list(strategy_names),
                    "live_tags": dict(live_tags or {}),
                },
                ensure_ascii=False,
            ),
            "instrument_type": instrument_type,
            "risk_config_json": (
                json.dumps(risk_config, ensure_ascii=False)
                if risk_config is not None
                else None
            ),
        }
    )
    dao.commit()


def _git_commit() -> str:
    """目前的 commit；取不到時回 `unknown` 並警告"""

    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except (subprocess.SubprocessError, OSError):
        # 只擋得住「查不到」：非零結束碼、git 不存在、工作目錄不是 repo。
        # 其餘例外代表這支函式自己寫錯了，不該被稽核欄位的降級路徑吃掉
        logger.warning("取不到 git commit，稽核欄位記為 unknown")
        return "unknown"


def _shioaji_version() -> str:
    """
    券商 SDK 版本；讀不到時回 `unknown`

    **import 寫在函式內**：整個模組只為了這一個版本字串而相依券商 SDK，
    寫在模組層級的話，任何一個 `from core.live.factory import ...`
    都得把整包 SDK 拉進來，沒裝的環境則當場 ImportError。
    回測不該為了它付 import 成本，也不該因為它壞掉而跑不動——
    這條邊界沒有東西釘住就會漂回去，故另有測試掃描模組層級的 import。
    """

    try:
        import shioaji
    except ImportError:
        logger.warning("讀不到 shioaji 版本，稽核欄位記為 unknown")
        return "unknown"

    return str(getattr(shioaji, "__version__", "unknown"))
