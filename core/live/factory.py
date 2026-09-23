import datetime
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import shioaji as sj
from loguru import logger

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.models.cost_model import (
    CostConfig,
    FuturesCostConfig,
    StockCostModel,
    TwFuturesCostModel,
)
from core.backtest.models.instrument_spec import TwFuturesSpec, TwStockSpec
from core.broker.base import BaseBroker
from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.broker.tw.shioaji_session import ShioajiSession
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
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.datafeed.tw.futures_live_datafeed import (
    TwFuturesLiveDataFeed,
    split_contract_id,
)
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.notify.base import BaseNotifier, NullNotifier
from core.live.notify.factory import build_notifier
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.report.parity_checker import ParityChecker
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingModeState
from core.live.segment import SegmentSchedule, SegmentWindow
from core.live.trader import LiveTrader, StrategyContext
from core.managers.base.position_manager import BasePositionManager
from core.managers.futures.position_manager import FuturesPositionManager
from core.managers.stock.position_manager import StockPositionManager
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import (
    BaseOrder,
    BrokerAccountSnapshot,
    FuturesAccount,
    FuturesOrder,
    RealizedTradeSnapshot,
    StockAccount,
)
from core.strategies.base import BaseStrategy
from core.utils import (
    FUTURES_MULTIPLIER,
    Action,
    ExecutionTiming,
    FuturesRollRule,
    InstrumentType,
    Market,
    PositionType,
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

# 台股的段落時窗。
#
# 尾盤段 **13:25 才開始送單**：13:25 之前送出的限價單會在逐筆交易時段就成交，
# 成交價不是收盤價，與回測「以收盤價成交」的假設對不上，而且看起來完全正常。
# 13:29 停止送單讓委託進得了收盤集合競價；**13:35 才收線**，因為收盤集合競價的
# 成交回報要 13:30 之後才會進來——送完單就收線會讓當日成交明細出現一段空窗。
TW_STOCK_SEGMENTS: SegmentSchedule = {
    ExecutionTiming.AT_OPEN: SegmentWindow(
        submit_start=datetime.time(8, 30),
        submit_end=datetime.time(8, 59),
        drain_end=datetime.time(9, 5),
    ),
    ExecutionTiming.AT_CLOSE: SegmentWindow(
        submit_start=datetime.time(13, 25),
        submit_end=datetime.time(13, 29),
        drain_end=datetime.time(13, 35),
    ),
}

# 台期貨的段落時窗。
#
# ⚠️ **時點尚未以 TAIFEX 公告逐條核對**（規劃要求實作前核對收盤規則）。
# 目前沿用日盤 08:45~13:45 的常識值，要在模擬環境的連續演練中實際驗一次。
TW_FUTURES_SEGMENTS: SegmentSchedule = {
    ExecutionTiming.AT_OPEN: SegmentWindow(
        submit_start=datetime.time(8, 45),
        submit_end=datetime.time(8, 59),
        drain_end=datetime.time(9, 5),
    ),
    ExecutionTiming.AT_CLOSE: SegmentWindow(
        submit_start=datetime.time(13, 30),
        submit_end=datetime.time(13, 44),
        drain_end=datetime.time(13, 50),
    ),
}


class UnsupportedMarketError(ValueError):
    """沒有對應實作的（市場, 商品）組合"""


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


def make_account_fetcher(
    broker: BaseBroker, strategies: Sequence[BaseStrategy]
) -> Callable[[], BrokerAccountSnapshot]:
    """
    - Description:
        組出「這一批策略該看哪個帳戶」的帳務查詢

        **股票與期貨是兩個子帳戶，各有各的錢。** 原本一律查股票帳戶，於是期貨
        策略的額度被拿股票權益去檢查——那是另一筆錢的規模。2026-09-23 的演練
        實測到這個後果：股票帳戶總權益 582,608，而期貨策略宣告 3,000,000，
        額度檢查永遠過不了；把額度壓進門檻又會讓可開口數變成 0，
        等於「通過閘門但整輪零交易」。

        兩種商品同時載入時把兩個帳戶相加：都是同一個人的錢，而額度檢查問的是
        「整體撐不撐得住」。
    - Parameters:
        - broker: BaseBroker
            已建立的券商閘道
        - strategies: Sequence[BaseStrategy]
            本次載入的策略
    - Return:
        - Callable[[], BrokerAccountSnapshot]
            每次呼叫都重查一次的帳務查詢
    """

    instruments: Set[Optional[InstrumentType]] = {
        strategy.instrument_type for strategy in strategies
    }
    wants_stock: bool = InstrumentType.STOCK in instruments
    wants_futures: bool = bool(instruments - {InstrumentType.STOCK})

    if wants_stock and not wants_futures:
        return broker.get_account
    if wants_futures and not wants_stock:
        return broker.get_futures_account

    def combined() -> BrokerAccountSnapshot:
        """兩個子帳戶相加"""

        stock: BrokerAccountSnapshot = broker.get_account()
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
    run_id: Optional[str] = None,
    dao: Optional[LiveTradeDAO] = None,
    risk_config: Optional[RiskConfig] = None,
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
        - run_id: Optional[str]
            本次啟動的識別碼；None 時以時戳產生
        - dao: Optional[LiveTradeDAO]
            實盤紀錄庫；None 時自行開啟
        - risk_config: Optional[RiskConfig]
            風控設定；None 時用預設值
        - now_provider: Callable[[], datetime.datetime]
            取得目前時間
        - phase: str
            本次要跑的段落名（`run.py --phase` 的值），寫進 `live_run`；
            存活監控依它比對「該跑的段落有沒有跑」
    - Return:
        - LiveTrader
            組裝好的引擎
    - Raise:
        - UnsupportedMarketError
            某支策略的（市場, 商品）組合沒有對應實作
        - ValueError
            策略清單為空、或策略名稱重複
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

    resolved_run_id: str = run_id or now_provider().strftime("%Y%m%d%H%M%S")
    resolved_dao: LiveTradeDAO = dao if dao is not None else LiveTradeDAO()
    resolved_dao.ensure_tables()

    # === 單例元件 ===
    rate_limiter: RateLimiter = RateLimiter()
    resolved_broker: BaseBroker = broker or _build_broker(
        broker_kind, simulation, rate_limiter
    )

    mode_state: TradingModeState = TradingModeState(
        resolved_dao, resolved_run_id, now_provider
    )
    risk_manager: PreTradeRiskManager = PreTradeRiskManager(
        mode_state,
        risk_config,
        resolved_dao,
        resolved_run_id,
        now_provider=now_provider,
    )
    ledger: PositionAttributionLedger = PositionAttributionLedger(
        resolved_dao, now_provider
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
    )

    # === 逐策略元件 ===
    contexts: List[StrategyContext] = []
    managers: Dict[str, BasePositionManager] = {}
    schedules: List[SegmentSchedule] = []

    for name, strategy in zip(names, strategies):
        context, schedule = _build_context(
            name, strategy, resolved_broker, risk_config, now_provider
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
        resolved_dao, make_daily_backtest_runner(strategies)
    )
    _record_run(
        resolved_dao,
        resolved_run_id,
        simulation,
        dry_run,
        not isinstance(notifier, NullNotifier),
        now_provider,
        phase,
    )

    return LiveTrader(
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
        cost_estimator=make_trade_cost_estimator(),
        # 查哪個帳戶是商品語意，由這裡決定後注入（引擎本體不認得「股票」「期貨」）
        fetch_account=make_account_fetcher(resolved_broker, strategies),
        # 保證金查詢是期貨特性：有期貨策略時才注入，引擎本體不認得「期貨」
        margin_query=(
            getattr(resolved_broker, "get_futures_account", None)
            if any(
                context.calculate_opening_requirement is not None
                for context in contexts
            )
            else None
        ),
        resume_trading=resume_trading,
        notifier=notifier,
        now_provider=now_provider,
        parity_checker=parity_checker,
    )


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

    session: ShioajiSession = ShioajiSession(
        simulation=simulation, rate_limiter=rate_limiter
    )
    return ShioajiBroker(session, rate_limiter)


def _build_context(
    name: str,
    strategy: BaseStrategy,
    broker: BaseBroker,
    risk_config: Optional[RiskConfig],
    now_provider: Callable[[], datetime.datetime],
) -> Tuple[StrategyContext, SegmentSchedule]:
    """依 `(market, instrument_type)` 分派出這支策略需要的一整組元件"""

    market: Optional[Market] = strategy.market
    instrument: Optional[InstrumentType] = strategy.instrument_type

    if market == Market.TW and instrument == InstrumentType.STOCK:
        account: StockAccount = StockAccount(init_capital=live_capital(strategy))
        cost_model: StockCostModel = StockCostModel(CostConfig.default())
        manager: BasePositionManager = StockPositionManager(account, cost_model)
        feed: BaseLiveDataFeed = TwStockLiveDataFeed(broker, now_provider=now_provider)
        spec: TwStockSpec = TwStockSpec()
        schedule: SegmentSchedule = TW_STOCK_SEGMENTS
        build_order: FilledOrderBuilder = build_stock_order
        opening_requirement: Optional[Callable[[BaseOrder], float]] = None

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
        futures_manager: FuturesPositionManager = FuturesPositionManager(
            futures_account,
            TwFuturesCostModel(FuturesCostConfig.default()),
            margin_config=margin_config,
        )
        manager = futures_manager
        roll_config: FuturesRollConfig = to_live_roll_config(
            getattr(strategy, "roll_config", None) or FuturesRollConfig()
        )
        strategy.roll_config = roll_config
        feed = TwFuturesLiveDataFeed(
            broker,
            now_provider=now_provider,
            margin_config=margin_config,
            roll_config=roll_config,
        )
        opening_requirement = _make_opening_requirement(futures_manager, now_provider)
        spec = TwFuturesSpec()
        schedule = TW_FUTURES_SEGMENTS
        build_order = _build_futures_order

    else:
        raise UnsupportedMarketError(
            f"{name} 的組合（market={market}, instrument_type={instrument}）"
            "沒有對應的實盤實作"
        )

    context: StrategyContext = StrategyContext(
        strategy=strategy,
        account=account,
        position_manager=manager,
        data_feed=feed,
        risk_config=risk_config if risk_config is not None else RiskConfig(),
        symbols=list(getattr(strategy, "symbols", []) or []),
        calculate_notional=_make_notional_calculator(spec),
        build_filled_order=build_order,
        calculate_opening_requirement=opening_requirement,
    )
    return (context, schedule)


def make_trade_cost_estimator() -> TradeCostEstimator:
    """
    - Description:
        盤後校正成本用的估算器：依成本模型重算一筆已平倉交易的手續費與稅

        成本模型與實盤部位管理用的是同一組預設設定（`CostConfig.default()`、
        `FuturesCostConfig.default()`），校正比對的才是「實盤記帳用的那套」估得準不準。
        - 期貨：**只估平倉那一腿**（一次手續費＋平倉價的期交稅），與券商欄位的範圍一致——
          2026-09-22 模擬環境實測，台指期一口來回的已實現紀錄 `fee=50`、`tax=193`，
          正好是單邊手續費與單邊期交稅（開平倉價的稅在這個價位都是 193，分不出是哪一腿；
          平倉回報列的是平倉，故取平倉腿）。照來回估的話每筆都會被誤報 -50%。
          乘數取 `FUTURES_MULTIPLIER`，不在表內（股票期貨）時回 None。
        - 股票：手續費開平倉各一次；證交稅只課賣出那一腿——多單課在平倉價、
          空單課在開倉價；開倉日與交易日同一天時用當沖稅率。缺開倉成交時回 None。
    - Return:
        - TradeCostEstimator
            估算器
    """

    stock_cost: StockCostModel = StockCostModel(CostConfig.default())
    futures_cost: TwFuturesCostModel = TwFuturesCostModel(FuturesCostConfig.default())

    def estimate(
        trade: RealizedTradeSnapshot,
        opening: List[Dict[str, Any]],
        run_date: datetime.date,
    ) -> Optional[float]:
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
            )
        else:
            tax = stock_cost.tax(
                entry, quantity, Action.SELL, is_day_trade=is_day_trade, date=opened_on
            )
        return float(commission + tax)

    return estimate


def to_live_roll_config(config: FuturesRollConfig) -> FuturesRollConfig:
    """
    - Description:
        把策略的換月設定換成實盤做得到的版本（**不改動原物件**）

        **實盤最晚要在最後交易日的前一個交易日換月**（2026-09-22 使用者裁示）：
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
        return manager.calculate_opening_requirement(order, now_provider().date())

    return calculate


def make_daily_backtest_runner(
    strategies: Sequence[BaseStrategy],
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
    - Parameters:
        - strategies: Sequence[BaseStrategy]
            本次實盤載入的策略實例；只用來取類別與 `init_capital`
    - Return:
        - Callable[[str, datetime.date], List[BaseOrder]]
            `(策略名, 交易日) → 該日回測會送出的委託清單`
    """

    blueprints: Dict[str, BaseStrategy] = {
        type(strategy).__name__: strategy for strategy in strategies
    }

    def run(strategy_name: str, run_date: datetime.date) -> List[BaseOrder]:
        blueprint: Optional[BaseStrategy] = blueprints.get(strategy_name)
        if blueprint is None:
            raise UnsupportedMarketError(f"本次執行沒有載入策略 {strategy_name}")

        replica: BaseStrategy = type(blueprint)()
        replica.init_capital = blueprint.init_capital
        replica.start_date = run_date
        replica.end_date = run_date

        # 不寫報表與 backtest log：盤後每天跑一次，寫的話會蓋掉
        # `results/<策略>/` 的研究用回測，並把實盤的 log 一併寫進 backtest log
        backtester: Backtester = build_backtester(replica, write_artifacts=False)
        backtester.run()
        return [order for _, _, order in backtester.submitted_orders]

    return run


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


def _make_notional_calculator(spec: Any) -> Callable[[BaseOrder], float]:
    """
    以 `InstrumentSpec` 的計價單位換算委託金額

    **單位是市場特性**：台股一張是 1000 股、期貨一口要乘契約乘數。
    引擎本體算不出來也不該知道，故在這裡包成一個函式注入進去。
    """

    def calculate(order: BaseOrder) -> float:
        return float(order.price) * float(spec.to_units(order.volume))

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
        那也正是部署文件列出多行 cron 的原因。
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
) -> None:
    """
    寫入啟動紀錄，含稽核欄位

    **稽核欄位不是可有可無**：實盤出事時第一個要回答的是「那天那張單是哪一版程式
    送出的」。取不到 git commit 時記 `unknown` 並警告，**不阻擋啟動**——
    容器內沒有 `.git` 是正常的。

    `notify_enabled` 同理：**通知缺設定時退化為不推播，但那件事要落地**。
    只記一行 warning 的話，事後回頭查「那天為什麼沒收到告警」會查不到答案——
    而「以為有告警其實沒有」比「知道沒有告警」危險得多。
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
            "phase": phase,
            "simulation": int(simulation),
            "dry_run": int(dry_run),
            "notify_enabled": int(notify_enabled),
            "git_commit": _git_commit(),
            "shioaji_version": getattr(sj, "__version__", "unknown"),
        }
    )
    dao.conn.commit()


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
    except Exception:
        logger.warning("取不到 git commit，稽核欄位記為 unknown")
        return "unknown"
