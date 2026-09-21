from core.backtest.backtester import Backtester, new_event_counts
from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.backtest.datafeed.tw.futures_roll import FuturesRollConfig
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.backtest.models.cost_model import (
    CostConfig,
    FuturesCostConfig,
    StockCostModel,
    TwFuturesCostModel,
)
from core.backtest.models.fill_model import (
    FillConfig,
    FuturesFillConfig,
    TwFuturesFillModel,
    TwStockFillModel,
)
from core.backtest.models.instrument_spec import TwFuturesSpec, TwStockSpec
from core.backtest.models.settlement_model import (
    TwFuturesSettlementModel,
    TwStockSettlementModel,
)
from core.backtest.report.futures_reporter import FuturesBacktestReporter
from core.backtest.report.reporter import StockBacktestReporter
from core.managers.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.managers.stock.position_manager import StockPositionManager
from core.models import FuturesAccount, StockAccount
from core.strategies.base import BaseStrategy
from core.strategies.futures import BaseFuturesStrategy
from core.strategies.stock import BaseStockStrategy
from core.utils import InstrumentType, Market, PositionType, ShortMethod

"""Backtester factory: 全專案唯一一處依（市場, 商品）組合分派的地方"""


def build_backtester(
    strategy: BaseStrategy,
    adjusted_price: bool = True,
    write_artifacts: bool = True,
) -> Backtester:
    """
    - Description:
        依策略宣告的（市場, 商品）組合組裝對應的 model 組合

        這是全專案唯一的分派點。新增一個組合只需在此加一個分支，
        `Backtester` 本身一行都不用改。

        **分派鍵是兩個欄位的組合而非單一欄位**：model 組合本來就是按組合
        實作的（`TwStockSpec`／`TwStockFillModel` ＝ TW ＋ STOCK），
        單靠 `market` 無法區分台股與美股的股票策略。
    - Parameters:
        - strategy: BaseStrategy
            要回測的策略；其 `market` ＋ `instrument_type` 兩欄位為分派鍵
        - adjusted_price: bool
            訊號是否使用還原價（後復權）。**預設 True**：未還原時除權息跳空會被
            當成真實漲跌，是資料正確性問題而非可選功能。
            `Backtester` 那一層的預設維持 False——引擎不預設任何政策，
            要用哪種價格由 factory 這個「政策層」決定
        - write_artifacts: bool
            是否寫出回測報表與 backtest log；實盤 parity 比對傳 False
    - Return:
        - Backtester
            已注入該（市場, 商品）組合對應 model 組合的引擎
    """

    if (strategy.market, strategy.instrument_type) == (
        Market.TW,
        InstrumentType.STOCK,
    ):
        return build_tw_stock_backtester(strategy, adjusted_price, write_artifacts)

    if (strategy.market, strategy.instrument_type) == (
        Market.TW,
        InstrumentType.FUTURE,
    ):
        # `adjusted_price` 不往下傳：期貨沒有除權息還原的概念，見
        # `build_tw_futures_backtester()`
        return build_tw_futures_backtester(strategy, write_artifacts)

    raise ValueError(
        f"尚未支援的（市場, 商品）組合：{strategy.market}, {strategy.instrument_type}"
    )


def build_tw_stock_backtester(
    strategy: BaseStockStrategy,
    adjusted_price: bool = True,
    write_artifacts: bool = True,
) -> Backtester:
    """組裝台股的 model 組合"""

    account: StockAccount = StockAccount(strategy.init_capital)
    strategy.setup_account(account)

    cost_model: StockCostModel = StockCostModel(build_cost_config(strategy))

    # 事件計數由 factory 建立，引擎、FillModel 與部位管理層共用同一個 dict
    event_counts: dict = new_event_counts()
    position_manager: StockPositionManager = StockPositionManager(
        account, cost_model, event_counts=event_counts
    )

    instrument: TwStockSpec = TwStockSpec()
    fill_model: TwStockFillModel = TwStockFillModel(
        instrument=instrument,
        event_counts=event_counts,
        config=strategy.fill_config or FillConfig(),
        check_borrowable=cost_model.config.short_constraint.check_borrowable,
    )

    settlement: TwStockSettlementModel = TwStockSettlementModel(
        position_manager=position_manager,
        cost_model=cost_model,
        prev_close=fill_model.prev_close,
        instrument=instrument,
        day_trade_uncovered_policy=strategy.day_trade_uncovered_policy,
        margin_call_policy=strategy.margin_call_policy,
        max_holding_days=strategy.max_holding_days,
        max_no_quote_days=strategy.max_no_quote_days,
        fill_model=fill_model,
    )

    return Backtester(
        strategy=strategy,
        account=account,
        position_manager=position_manager,
        instrument=instrument,
        fill_model=fill_model,
        cost_model=cost_model,
        settlement=settlement,
        data_feed=TwStockDataFeed(),
        reporter_cls=StockBacktestReporter,
        event_counts=event_counts,
        adjusted_price=adjusted_price,
        write_artifacts=write_artifacts,
    )


def build_tw_futures_backtester(
    strategy: BaseFuturesStrategy, write_artifacts: bool = True
) -> Backtester:
    """
    - Description:
        組裝台期貨的 model 組

        **與台股那一組的四個差異**（都是期貨的記帳語意造成的，不是實作偏好）：

        1. **`adjusted_price` 一律為 False**：期貨沒有除權息，不存在還原價。
        2. **成本設定由 `FuturesCostConfig` 提供**，且**同一個物件**同時交給
           `FuturesPositionManager` 與 `TwFuturesCostModel`——費率兩處各填一份
           必然漂移。預設帶市場常見費率，驗證口徑可用 `FuturesCostConfig.free()`。
        3. **保證金設定預設查表**（`FuturesMarginConfig.default()`），API 由
           DataFeed 注入同一個設定物件，策略層與部位管理層因此共用同一個來源。
           要改用比率近似必須明確宣告 `FuturesMarginConfig.ratio()`——
           近似的誤差跨年份實測為 +143% ~ −38%（TX，而且會變號）。
        4. **`SettlementModel` 不需要 `cost_model`**：期貨在收盤後只做逐日盯市，
           不像台股要在此計提借券費與稅差。
        5. **`DataFeed` 建在 `PositionManager` 之前**：後者的契約乘數要由前者解析
           （股期的契約單位逐日不同），台股沒有這個相依。

        另有一道 `fill_config` 的型別檢查，理由見函式內的註解。
    - Parameters:
        - strategy: BaseFuturesStrategy
            要回測的台期貨策略
        - write_artifacts: bool
            是否寫出回測報表與 backtest log
    - Return:
        - Backtester
            已注入台期貨 model 組的引擎
    """

    account: FuturesAccount = FuturesAccount(strategy.init_capital)
    strategy.setup_account(account)

    # 成本與保證金設定：與 PositionManager 共用同一個物件
    cost_config: FuturesCostConfig = strategy.cost_config or FuturesCostConfig.default()
    margin_config: FuturesMarginConfig = (
        strategy.margin_config or FuturesMarginConfig.default()
    )

    # **回寫給策略**：策略層算可開口數、部位管理層算應繳保證金，
    # 兩者必須是同一個設定物件——否則策略算得出口數、部位管理層卻開不進去，
    # 而且不會有任何錯誤訊息。API 稍後由 DataFeed 注入這同一個物件
    strategy.margin_config = margin_config

    # 換月設定同理：策略挑合約與結算模型轉倉必須是同一份規則，
    # 否則會出現「訊號在次月、部位還在近月」這種不會報錯的錯配
    roll_config: FuturesRollConfig = strategy.roll_config or FuturesRollConfig()
    strategy.roll_config = roll_config

    cost_model: TwFuturesCostModel = TwFuturesCostModel(cost_config)

    # **DataFeed 先建**：部位管理層的乘數要由它解析（見下），故建構順序與台股相反。
    # DataFeed 此時只是建好物件、還沒連線，連線在 `setup()` 才開
    data_feed: TwFuturesDataFeed = TwFuturesDataFeed(
        margin_config=margin_config, roll_config=roll_config
    )

    # **乘數與標的股價都由 DataFeed 解析**：指數期貨的乘數查常數表、股票期貨逐日查
    # 標的池的契約單位（會隨除權息被交易所調整，寫死必錯）；股期的保證金走比例表，
    # 算式需要的標的股價在 `tw_stock.db`，跨庫取值同樣屬 DataFeed 的職責
    # （連線一律由它持有）。部位管理層自己查常數表的話，股期第一筆開倉就 KeyError
    position_manager: FuturesPositionManager = FuturesPositionManager(
        account,
        cost_model=cost_model,
        margin_config=margin_config,
        multiplier_resolver=data_feed.resolve_multiplier,
        underlying_price_resolver=data_feed.resolve_underlying_price,
    )

    instrument: TwFuturesSpec = TwFuturesSpec()

    # **型別標註擋不住這件事，這道檢查才擋得住**：期貨的滑價以跳動點表達，
    # 策略若寫成基底的 `FillConfig(slippage_bps_buy=...)` 照樣跑得完，只是
    # `get_slippage_ticks()` 回 0、整組假設默默換成基點。不自動轉型是刻意的——
    # 包成 `FuturesFillConfig` 會讓那組 bps 繼續生效，等於接受一個本就不該用的單位
    if strategy.fill_config is not None and not isinstance(
        strategy.fill_config, FuturesFillConfig
    ):
        raise TypeError(
            f"{type(strategy).__name__}.fill_config 必須是 FuturesFillConfig，"
            f"收到 {type(strategy.fill_config).__name__}。"
            f"期貨的滑價以跳動點（slippage_ticks_*）表達，"
            f"基底 FillConfig 只有基點（slippage_bps_*），兩者不可互換"
        )

    # 事件計數由 factory 建立，引擎與 FillModel 共用同一個 dict
    event_counts: dict = new_event_counts()
    fill_model: TwFuturesFillModel = TwFuturesFillModel(
        instrument=instrument,
        event_counts=event_counts,
        config=strategy.fill_config or FuturesFillConfig(),
    )

    # **注入與引擎同一個 fill_model**（台股那邊同一個寫法）：強制平倉、到期兜底
    # 與換月轉倉的兩腿因此與策略自己送的單吃同一組滑價
    settlement: TwFuturesSettlementModel = TwFuturesSettlementModel(
        position_manager=position_manager,
        instrument=instrument,
        roll_config=roll_config,
        fill_model=fill_model,
    )

    return Backtester(
        strategy=strategy,
        account=account,
        position_manager=position_manager,
        instrument=instrument,
        fill_model=fill_model,
        cost_model=cost_model,
        settlement=settlement,
        data_feed=data_feed,
        reporter_cls=FuturesBacktestReporter,
        event_counts=event_counts,
        adjusted_price=False,
        write_artifacts=write_artifacts,
    )


def build_cost_config(strategy: BaseStockStrategy) -> CostConfig:
    """
    - Description:
        依策略宣告推導成本設定：放空且允許當沖時一律走現股當沖沖賣

        原本是 `Backtester.build_cost_config()`，但「依策略宣告組裝 model」
        本來就是 factory 的職責；留在引擎會讓引擎知道台股的信用交易語意。
    - Parameters:
        - strategy: BaseStockStrategy
            要回測的台股策略
    - Return:
        - config: CostConfig
            本次回測使用的成本參數
    """

    is_day_trade: bool = (
        strategy.position_type == PositionType.SHORT and strategy.enable_intraday
    )
    short_method: ShortMethod = (
        ShortMethod.DAY_TRADE if is_day_trade else strategy.short_method
    )

    config: CostConfig = strategy.cost_config or CostConfig.default(
        short_method, is_day_trade
    )

    if strategy.short_constraint is not None:
        config.short_constraint = strategy.short_constraint

    # 回測區間交給 CostModel 做稅制邊界的警示（當沖證交稅減半有實施日與落日日期）。
    # 由 factory 注入是因為策略才知道自己要跑哪一段，而 CostModel 不該反向去問策略
    config.backtest_start_date = strategy.start_date
    config.backtest_end_date = strategy.end_date

    return config
