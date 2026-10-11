import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type

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


class IntradayScaleMismatchError(RuntimeError):
    """逐筆觸發的策略與整天一次給的 TICK 回測，報價語意不同"""


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
        simulator_cls: Type[BaseSimulator],
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

        # 「一天怎麼跑」：由 factory 依級別選定類別，simulator 經由本引擎取用帳戶與 model
        self.simulator: BaseSimulator = simulator_cls(self)

        # 逐筆觸發策略的拒絕排在標的範圍之前：它說的是「這種回測根本不該跑」，比範圍不合法更根本
        self.validate_scale()
        self._reject_intraday_tick_backtest()
        self.validate_tick_universe()

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

    def _reject_intraday_tick_backtest(self) -> None:
        """
        - Description:
            逐筆觸發的策略不得跑 `Scale.TICK` 回測

            同一個 `check_open_signal(stock_quotes)`，實盤逐筆拿到**長度 1** 的 list，
            TICK 回測卻**一次拿到整天**的 tick——兩邊的 list 語意根本不同。
            需要橫斷面的邏輯（挑當下最強的前 N 檔）在回測裡看起來完全正常，
            上了實盤每次只看得到一檔，訊號完全不同，**而且兩邊都跑得完、都不報錯**。

            **讓錯誤現形而不是留一個看起來正常的績效**：這是 `PreOpenQuoteMixin`
            「讀 OHLC 就拋」在盤中這一側的對應物。要用回測估量級的人，
            得自己明確把 `is_tick_triggered` 關掉，不會在不知情的狀況下拿到一份
            訊號語意不同的報表。
        - Raise:
            - IntradayScaleMismatchError
                策略宣告 `is_tick_triggered=True` 且 `scale` 為 `Scale.TICK`
        """

        if not getattr(self.strategy, "is_tick_triggered", False):
            return

        if self.scale != Scale.TICK:
            return

        raise IntradayScaleMismatchError(
            f"{type(self.strategy).__name__} 宣告 is_tick_triggered=True（實盤逐筆觸發，"
            "每次鉤子只拿到一檔的一筆報價），但現行 Scale.TICK 回測是"
            "**整天的 tick 一次給**，兩者的報價 list 語意不同，訊號不可比。"
            "要用回測估量級請明確改為 Scale.DAY，或把 is_tick_triggered 關掉。"
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
            每日開盤前的準備：把當天的市場狀態推給成交與結算模型

            順序固定為：漲跌停參考價 → 融券餘額 → 停券與交易所名單（只在允許放空時）→ 除權息。
            日 K 與逐筆共用，simulator 開始處理當天的報價之前呼叫。
        - Parameters:
            - date: datetime.date
                當前交易日
        """

        # `submitted_orders` 以它標記每張委託的日期；不跟著前進的話，
        # 多日回測的每一筆都會記成起始日，parity 比對日期全錯且不報錯
        self.cur_date = date

        # 除權息日的漲跌停基準由交易所另行公告，須在下任何單之前覆寫，
        # 否則整段漲跌停區間會沿用偏高的前一交易日收盤而失準
        self.fill_model.apply_price_limit_basis(
            self.data_feed.get_price_limit_basis(date)
        )
        self.fill_model.apply_short_balance(self.data_feed.get_short_balance(date))

        # 停券日只有放空路徑會用到，且推導它需掃整段交易日曆；
        # 純做多策略不可能有空單，故連查都不查，避免替 LONG 回測加上無謂的成本
        if PositionType.SHORT in self.get_allowed_directions():
            self.settlement.apply_force_cover_symbols(
                self.data_feed.get_force_cover_symbols(date)
            )
            # 停券期間不得新增融券賣出；回補日當天的強制回補由 settlement 處理，
            # 這裡擋的是回補日之後到除權息交易日之間的新開倉
            self.fill_model.apply_short_suspended_symbols(
                self.data_feed.get_short_suspended_symbols(date)
            )
            # 交易所名單只有開啟檢核時才查：每天兩次查詢，不用的回測不該付這個成本
            if (
                self.fill_model.check_short_sale_list
                or self.fill_model.check_day_trade_list
            ):
                self.fill_model.apply_trading_lists(
                    self.data_feed.get_short_sale_list(date)
                    if self.fill_model.check_short_sale_list
                    else None,
                    self.data_feed.get_day_trade_list(date)
                    if self.fill_model.check_day_trade_list
                    else None,
                )

        # **除權息資料兩個方向都要**：做多跨除息要收現金股利、跨配股要調整股數，
        # 不餵的話做多績效會系統性偏低（除權息日的跳空變成憑空虧損）
        self.settlement.apply_cash_dividends(self.data_feed.get_cash_dividend_map(date))
        self.settlement.apply_share_ratios(self.data_feed.get_share_ratio_map(date))

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
            units: int = self.instrument.to_units(position.volume)

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
