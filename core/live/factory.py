import datetime
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import shioaji as sj
from loguru import logger

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
from core.live.account_sync import AccountSynchronizer
from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.live.capital_allocator import CapitalAllocator
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.live.notify.base import BaseNotifier, NullNotifier
from core.live.notify.factory import build_notifier
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.risk.risk_config import RiskConfig
from core.live.risk.risk_manager import PreTradeRiskManager
from core.live.risk.trading_mode import TradingModeState
from core.live.segment import SegmentSchedule, SegmentWindow
from core.live.trader import LiveTrader, StrategyContext
from core.managers.base.position_manager import BasePositionManager
from core.managers.futures.position_manager import FuturesPositionManager
from core.managers.stock.position_manager import StockPositionManager
from core.models import BaseOrder, FuturesAccount, StockAccount
from core.strategies.base import BaseStrategy
from core.utils import ExecutionTiming, InstrumentType, Market

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
# 目前沿用日盤 08:45~13:45 的常識值，Phase7-1 的演練要實際驗一次。
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
        {name: strategy.init_capital for name, strategy in zip(names, strategies)},
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

    account_sync: AccountSynchronizer = AccountSynchronizer(
        managers, ledger, resolved_dao, now_provider
    )
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
    _record_run(
        resolved_dao,
        resolved_run_id,
        simulation,
        dry_run,
        not isinstance(notifier, NullNotifier),
        now_provider,
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
        resume_trading=resume_trading,
        notifier=notifier,
        now_provider=now_provider,
    )


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
        account: StockAccount = StockAccount(init_capital=strategy.init_capital)
        cost_model: StockCostModel = StockCostModel(CostConfig.default())
        manager: BasePositionManager = StockPositionManager(account, cost_model)
        feed: BaseLiveDataFeed = TwStockLiveDataFeed(broker, now_provider=now_provider)
        spec: TwStockSpec = TwStockSpec()
        schedule: SegmentSchedule = TW_STOCK_SEGMENTS

    elif market == Market.TW and instrument == InstrumentType.FUTURE:
        futures_account: FuturesAccount = FuturesAccount(
            init_capital=strategy.init_capital
        )
        account = futures_account
        manager = FuturesPositionManager(
            futures_account, TwFuturesCostModel(FuturesCostConfig.default())
        )
        feed = TwFuturesLiveDataFeed(broker, now_provider=now_provider)
        spec = TwFuturesSpec()
        schedule = TW_FUTURES_SEGMENTS

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
    )
    return (context, schedule)


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
            "phase": "",
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
