import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

import pandas as pd
from loguru import logger

from core.backtest.models.event_counts import new_event_counts
from core.backtest.models.fill_model import BaseFillModel
from core.backtest.models.settlement_model import BaseSettlementModel
from core.backtest.report.base import BaseBacktestReporter
from core.backtest.simulator.base import BaseSimulator
from core.backtest.simulator.tick_universe import has_live_symbols
from core.config import BACKTEST_RESULT_DIR_PATH
from core.datafeed.base import BaseDataFeed
from core.market.cost_model import BaseCostModel
from core.market.instrument_spec import InstrumentSpec
from core.models import BaseAccount, BaseOrder, BaseQuote
from core.portfolio import order_rules
from core.position.base.position_manager import BasePositionManager
from core.strategies.base import BaseStrategy
from core.utils import (
    BarExecutionSequence,
    PositionType,
    Scale,
    TickUniverse,
    TimeUtils,
)
from core.utils.log_manager import LogManager

"""Backtesting engine that simulates trading based on strategy signals"""


class InvalidTickUniverseError(ValueError):
    """TICK 回測的標的範圍不合法：DAY 回測帶了範圍，或策略沒有清單卻選了策略清單模式"""


class Backtester:
    """
    Backtest Framework：逐日迴圈與每日前後的共用處理

    **唯一引擎，市場與商品皆無關，無子類**：兩者的差異全部由注入的 model 決定
    （`InstrumentSpec` / `FillModel` / `CostModel` / `SettlementModel` / `DataFeed`）。
    新增一個市場不需要修改本檔案，只需在 factory 組出另一組 model。

    **「一天怎麼跑」交給注入的 simulator**：本類別只負責逐日迴圈、`prepare_day()`／
    `finish_day()` 與報表；當天的報價怎麼交給策略、委託怎麼成交由 `BaseSimulator` 的實作決定
    （日 K 是 `BarSimulator`）。
    """

    # === Init & Data Loading ===
    def __init__(
        self,
        strategy: BaseStrategy,
        account: BaseAccount,
        position_manager: BasePositionManager,
        instrument: InstrumentSpec,
        fill_model: BaseFillModel,
        cost_model: BaseCostModel,
        settlement: BaseSettlementModel,
        data_feed: BaseDataFeed,
        reporter_cls: Type[BaseBacktestReporter],
        build_simulator: Callable[["Backtester"], BaseSimulator],
        event_counts: Optional[Dict[str, int]] = None,
        adjusted_price: bool = False,
        write_artifacts: bool = True,
        tick_universe: Optional[TickUniverse] = None,
    ) -> None:
        self.strategy: BaseStrategy = strategy  # 要回測的策略
        self.account: BaseAccount = account  # 虛擬帳戶資訊
        self.position_manager: BasePositionManager = position_manager  # 倉位管理器

        # 可插拔的市場行為
        self.instrument: InstrumentSpec = instrument  # 商品規格
        self.fill_model: BaseFillModel = fill_model  # 成交價可信度
        self.cost_model: BaseCostModel = cost_model  # 手續費／稅／持有成本
        self.settlement: BaseSettlementModel = settlement  # 一根 bar 收盤後的強制動作
        self.data_feed: BaseDataFeed = data_feed  # 資料載入與交易日判定
        self.reporter_cls: Type[BaseBacktestReporter] = reporter_cls  # 報表產生器

        # 回測結束是否在瀏覽器開圖；`None` 代表交給 reporter 依環境變數決定。
        # 由回測入口（`apps/backtest.py`）的 `--show/--no-show` 覆寫
        self.show_figures: Optional[bool] = None

        # 回測參數
        self.scale: str = self.strategy.scale  # 回測 KBar 級別
        self.max_holdings: Optional[int] = self.strategy.max_holdings  # 最大持倉檔數
        # 本場回測「會送出的委託」，供實盤 parity 比對取用。
        # **只記錄、不參與任何判斷**：記的是通過方向白名單、檔數上限與排序之後、
        # 進入成交模擬之前的那一份——那才是實盤真正會送到券商的東西，
        # 成不成交是市場的事，不影響「這張單有沒有被送出去」
        self.submitted_orders: List[Tuple[datetime.date, str, BaseOrder]] = []

        self.start_date: datetime.date = self.strategy.start_date  # 回測起始日
        self.cur_date: datetime.date = self.strategy.start_date  # 回測當前日
        self.end_date: datetime.date = self.strategy.end_date  # 回測結束日

        # 回測結果輸出目錄
        self.strategy_result_dir: Optional[Path] = None  # 策略回測結果資料夾

        # 含未實現損益的每日權益序列（只認已實現損益會低估留倉放空的 MDD）
        self.daily_equity: List[Dict[str, Any]] = []

        # 事件統計：由 factory 傳入時與 FillModel 共用同一個 dict
        self.event_counts: Dict[str, int] = (
            event_counts if event_counts is not None else new_event_counts()
        )

        # 是否以還原價（後復權）計算訊號。
        # **預設關閉**：開啟會改變所有策略的訊號，LONG baseline 必然失效，
        # 須單獨重產回歸 baseline（`scripts/run_regression.sh` 的兩條線）
        self.adjusted_price: bool = adjusted_price

        # 是否寫出回測報表與 backtest log。**實盤 parity 比對要關掉**：它每天對同一支
        # 策略跑一天回測，報表會蓋掉研究者在 `results/<策略>/` 的多年期回測；
        # backtest logger 是沒有 filter 的全域 sink，掛上之後實盤行程的 log
        # 全都會一起寫進 `logs/backtest/<策略>.log`
        self.write_artifacts: bool = write_artifacts

        # TICK 回測每天讀哪些標的（`--tick-universe`）；None 表示沿用預設，
        # 由 `validate_tick_universe()` 決定最終值（DAY 為 None、TICK 預設策略清單）
        self.tick_universe: Optional[TickUniverse] = tick_universe

        self.validate_scale()
        self.validate_tick_universe()

        # 「一天怎麼跑」：由 factory 依級別決定怎麼建（日 K 或逐筆），simulator 經由本引擎取用帳戶與 model。
        # 排在級別與標的範圍的檢查之後：逐筆 simulator 建立時要讀已決定的設定
        self.simulator: BaseSimulator = build_simulator(self)

        self.setup()

    def validate_scale(self) -> None:
        """
        - Description:
            回測級別必須在策略宣告的支援範圍內

            `--scale` 的覆寫在組裝前就驗過；這裡守的是不經覆寫的路徑（實盤 parity、
            直接呼叫 factory），策略的 `scale` 與 `supported_scales` 矛盾時同樣當場報錯。
        - Raise:
            - UnsupportedScaleError
                級別不在支援範圍內
        """

        self.strategy.validate_scale()

    def validate_tick_universe(self) -> None:
        """
        - Description:
            決定並檢查 TICK 回測的標的範圍

            - DAY 回測帶了範圍：拒絕，避免以為選項有作用。
            - TICK 回測沒指定：預設策略清單（與實盤一致，也比較快）。
            - 選策略清單、策略卻沒覆寫 `get_live_symbols()`：拒絕並提示改用全市場——
              **不自動退回全市場**，那會在不知情下改跑一天上百萬筆的全市場。
        - Raise:
            - InvalidTickUniverseError
                範圍不合法
        """

        if self.scale != Scale.TICK:
            if self.tick_universe is not None:
                raise InvalidTickUniverseError(
                    f"--tick-universe 只在 TICK 回測有效（目前級別：{Scale(self.scale).value}）"
                )
            return

        if self.tick_universe is None:
            self.tick_universe = TickUniverse.STRATEGY

        if self.tick_universe == TickUniverse.STRATEGY and not has_live_symbols(
            self.strategy
        ):
            raise InvalidTickUniverseError(
                f"{type(self.strategy).__name__} 沒有覆寫 get_live_symbols()，"
                "無法用策略清單決定要讀哪些標的；要讀全市場請改用 --tick-universe all"
            )

    def setup(self) -> None:
        """Set Up the Config of Backtester"""

        if self.write_artifacts:
            # 確保每個 strategy 有獨立的結果資料夾
            self.strategy_result_dir = (
                Path(BACKTEST_RESULT_DIR_PATH) / self.strategy.strategy_name
            )
            self.strategy_result_dir.mkdir(parents=True, exist_ok=True)

            LogManager.setup_backtest_logger(self.strategy.strategy_name)

        self.load_datasets()

    def load_datasets(self) -> None:
        """
        - Description:
            載入回測資料，並把資料源交給策略

            **API 實例全專案只建一次**：先由 DataFeed 建立，再交給策略取用，
            策略不自行 new。
        """

        self.data_feed.setup(self.strategy)
        # 開啟名單檢核時，區間早於制度起點或名單缺日都在這裡拒絕執行，
        # 不讓「缺資料」在回測途中默默變成「不檢查」
        self.data_feed.ensure_trading_list_coverage(
            self.fill_model.check_short_sale_list,
            self.fill_model.check_day_trade_list,
        )
        self.strategy.setup_apis(self.data_feed)

    # === Direction Setting ===
    def get_allowed_directions(self) -> Set[PositionType]:
        """取得允許的訂單方向白名單：由策略的 `direction` 決定"""

        return order_rules.get_allowed_directions(self.strategy.direction)

    def get_execution_sequence(self) -> BarExecutionSequence:
        """單根 bar 的開平倉先後；推導表與理由見 `order_rules.get_execution_sequence()`"""

        return order_rules.get_execution_sequence(self.strategy.allow_day_trade)

    # === Order Validation ===
    def validate_orders(self, orders: List[BaseOrder], stage: str) -> List[BaseOrder]:
        """
        - Description:
            檢查訂單方向是否合法，不合法者剔除並記錄，禁止靜默丟棄
        - Parameters:
            - orders: List[BaseOrder]
                策略回傳的訂單
            - stage: str
                "open" 或 "close"，決定期望的動作
        - Return:
            - valid_orders: List[BaseOrder]
                通過檢查的訂單
        """

        return order_rules.validate_orders(
            orders,
            stage,
            self.get_allowed_directions(),
            self.event_counts,
        )

    def enrich_orders(self, orders: List[BaseOrder]) -> List[BaseOrder]:
        """補上市場專屬的訂單欄位；規則由 CostModel 實作"""

        return self.cost_model.enrich_orders(orders)

    @staticmethod
    def sort_orders(orders: List[BaseOrder]) -> List[BaseOrder]:
        """同一根 bar 內委託的決定性排序；為什麼要自己排見 `order_rules.sort_orders()`"""

        return order_rules.sort_orders(orders)

    def update_reference_prices(self, quotes: List[BaseQuote]) -> None:
        """收盤後記錄當日收盤價，作為次日的漲跌停參考價；狀態由 FillModel 持有"""

        self.fill_model.update_reference_prices(quotes)

    # === Main Backtest Loop ===
    def run(self) -> None:
        """Execute Backtest"""

        logger.info("========== Backtest Start ==========")
        logger.info(f"* Strategy Name: {self.strategy.strategy_name}")
        logger.info(
            f"* Backtest Period: {self.start_date.strftime('%Y/%m/%d')} ~ {self.end_date.strftime('%Y/%m/%d')}"
        )
        logger.info(f"* Initial Capital: {self.strategy.init_capital}")
        logger.info(f"* Backtest Scale: {self.scale}")

        dates: List[datetime.date] = TimeUtils.generate_date_range(
            start_date=self.start_date, end_date=self.end_date
        )

        # `try/finally`：回測常在中途因單日資料異常拋例外，不用 finally 的話
        # 那條 SQLite 連線會留著不關，反覆回測就會累積連線
        try:
            for date in dates:
                logger.info(f"--- {date.strftime('%Y/%m/%d')} ---")

                if not self.data_feed.is_market_open(date):
                    logger.info("* Market Close\n")
                    continue

                # 當天沒有報價就整天跳過：連每日準備與收盤結算都不做
                if not self.simulator.load_day(date):
                    continue

                self.prepare_day(date)
                close_quotes: List[BaseQuote] = self.simulator.run_day(date)
                self.finish_day(date, close_quotes)

            self.account.update_account_status()

            logger.info(f"""
            1. Initial Capital: {int(self.account.init_capital)}
            2. Balance: {int(self.account.balance)}
            3. Total realized pnl: {int(self.account.realized_pnl)}
            4. ROI: {round(self.account.roi, 2)}%
            """)

            if self.write_artifacts:
                self.generate_backtest_report()

        finally:
            self.data_feed.close()

    # === Daily Routine ===
    def prepare_day(self, date: datetime.date) -> None:
        """
        - Description:
            每日開盤前的準備：各 model 自己向資料源取當天的市場狀態

            日 K 與逐筆共用，simulator 開始處理當天的報價之前呼叫。要取哪些狀態（漲跌停基準、
            融券餘額、停券、交易所名單、除權息）是市場規則，由 model 的 `prepare_day()` 決定；
            引擎只告訴它們策略可不可能放空。成交模型先、結算模型後，兩者各自的取用順序在 model 內固定。
        - Parameters:
            - date: datetime.date
                當前交易日
        """

        # `submitted_orders` 以它標記每張委託的日期；不跟著前進的話，
        # 多日回測的每一筆都會記成起始日，parity 比對日期全錯且不報錯
        self.cur_date = date

        allow_short: bool = PositionType.SHORT in self.get_allowed_directions()
        self.fill_model.prepare_day(self.data_feed, date, allow_short)
        self.settlement.prepare_day(self.data_feed, date, allow_short)

    def finish_day(self, date: datetime.date, close_quotes: List[BaseQuote]) -> None:
        """
        - Description:
            收盤後的結算：市場規則強制執行的動作 → 權益快照 → 次日的漲跌停參考價（順序不可對調）
        - Parameters:
            - date: datetime.date
                當前交易日
            - close_quotes: List[BaseQuote]
                收盤報價（日 K 即當根 bar 的報價）
        """

        # 一根 bar 收盤後由市場規則強制執行的動作
        # 台股：當沖強制回補 ＋ 借券費計提 ＋ 維持率追繳
        # 期貨：每日結算 ＋ 保證金追繳 ＋ 到期換月
        self.settlement.settle_day(date, close_quotes, self.account, self.event_counts)

        self.snapshot_daily_equity(date, close_quotes)
        self.update_reference_prices(close_quotes)

    # === Daily Equity ===
    def snapshot_daily_equity(
        self, date: datetime.date, quotes: List[BaseQuote]
    ) -> float:
        """
        - Description:
            記錄含未實現損益的每日權益，並更新各部位的未實現損益

            只認已實現損益的權益曲線會把「持倉期間的逆勢」完全抹平，
            而那正是放空最大的風險來源。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[BaseQuote]
                當根 bar 的報價
        - Return:
            - equity: float
                當日權益（現金 + 部位價值）
        """

        quote_map: Dict[str, BaseQuote] = {q.symbol: q for q in quotes}
        position_value: float = 0.0

        for position in self.account.get_positions():
            price: float = self.settlement.get_mark_price(position, quote_map)
            units: int = position.volume * self.instrument.get_unit_size(
                self.instrument.resolve_product(position), date
            )

            # 「這個部位替權益貢獻多少」是**資金佔用方式**的問題，屬結算模型：
            # 股票買進是把現金換成標的（部位價值＝市值），期貨開倉只凍結保證金
            # （契約價值本身不佔用資金，部位價值＝保證金＋未結算損益）。
            # `BaseSettlementModel.mark_position()` 的預設實作即現金帳戶口徑
            position_value += self.settlement.mark_position(position, price, units)

        equity: float = round(self.account.balance + position_value, 2)
        self.daily_equity.append({"Date": date, "Equity": equity})
        return equity

    # === Report ===
    def generate_backtest_report(self) -> None:
        """Generate backtest report"""

        # `price` 共用 DataFeed 已開好的連線，避免一次回測對同一個 SQLite
        # 檔案開出兩條連線
        reporter: BaseBacktestReporter = self.reporter_cls(
            self.strategy,
            self.strategy_result_dir,
            price=getattr(self.data_feed, "price", None),
            show=self.show_figures,
        )
        reporter.trading_report = reporter.generate_trading_report()

        # 逐日權益交給 reporter，四張圖才有辦法用盯市口徑（否則 MDD 被低估）
        reporter.daily_equity = self.daily_equity

        # 多空分開統計與事件計數（放空的尾部風險不可被平均掉）
        reporter.generate_direction_summary()
        reporter.generate_event_report(self.event_counts)

        # 整體績效指標：不開前端也看得到，且 Sharpe／Sortino／MDD 只有一份計算
        reporter.generate_metrics_summary()

        if self.daily_equity:
            reporter.save_report(
                pd.DataFrame(self.daily_equity),
                f"{self.strategy.strategy_name}_daily_equity.csv",
            )

        try:
            reporter.plot_balance_curve()
            reporter.plot_balance_and_benchmark_curve()
            reporter.plot_balance_mdd()
            reporter.plot_everyday_profit()
            reporter.plot_everyday_equity_change()
        finally:
            # reporter 自己開的連線由它自己關；共用連線不歸它關
            reporter.close()
