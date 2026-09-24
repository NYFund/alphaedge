import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type

import pandas as pd
from loguru import logger

from core.backtest.models.cost_model import BaseCostModel
from core.backtest.models.event_counts import new_event_counts
from core.backtest.models.fill_model import BaseFillModel
from core.backtest.models.instrument_spec import InstrumentSpec
from core.backtest.models.settlement_model import BaseSettlementModel
from core.backtest.report.base import BaseBacktestReporter
from core.config import BACKTEST_RESULT_DIR_PATH
from core.datafeed.base import BaseDataFeed
from core.execution import order_preprocess
from core.managers.base.position_manager import BasePositionManager
from core.models import (
    BaseAccount,
    BaseOrder,
    BasePosition,
    BaseQuote,
    BaseTradeRecord,
)
from core.strategies.base import BaseStrategy
from core.utils import (
    BarExecutionOrder,
    PositionType,
    Scale,
    TimeUtils,
)
from core.utils.log_manager import LogManager

"""Backtesting engine that simulates trading based on strategy signals"""


class IntradayScaleMismatchError(RuntimeError):
    """逐筆觸發的策略與整天一次給的 TICK 回測，報價語意不同"""


class Backtester:
    """
    Backtest Framework: Tick and Daily price intervals

    **唯一引擎，市場與商品皆無關，無子類**：兩者的差異全部由注入的 model 決定
    （`InstrumentSpec` / `FillModel` / `CostModel` / `SettlementModel` / `DataFeed`）。
    新增一個市場不需要修改本檔案，只需在 factory 組出另一組 model。
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
        event_counts: Optional[Dict[str, int]] = None,
        adjusted_price: bool = False,
        write_artifacts: bool = True,
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
        # 由 `run.py --show/--no-show` 覆寫
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

        self._reject_intraday_tick_backtest()

        self.setup()

    def _reject_intraday_tick_backtest(self) -> None:
        """
        - Description:
            逐筆觸發的策略不得跑 `Scale.TICK` 回測

            同一個 `check_open_signal(stock_quotes)`，實盤逐筆拿到**長度 1** 的 list，
            TICK 回測卻**一次拿到整天**的 tick——兩邊的 list 語意根本不同。
            需要橫斷面的邏輯（挑當下最強的前 N 檔）在回測裡看起來完全正常，
            上了實盤每次只看得到一檔，訊號完全不同，**而且兩邊都跑得完、都不報錯**。

            **讓錯誤現形而不是留一個看起來正常的績效**：這是 `PreOpenQuote`
            「讀 OHLC 就拋」在盤中這一側的對應物。要用回測估量級的人，
            得自己明確把 `is_intraday` 關掉，不會在不知情的狀況下拿到一份
            訊號語意不同的報表。
        - Raise:
            - IntradayScaleMismatchError
                策略宣告 `is_intraday=True` 且 `scale` 為 `Scale.TICK`
        """

        if not getattr(self.strategy, "is_intraday", False):
            return

        if self.scale != Scale.TICK:
            return

        raise IntradayScaleMismatchError(
            f"{type(self.strategy).__name__} 宣告 is_intraday=True（實盤逐筆觸發，"
            "每次鉤子只拿到一檔的一筆報價），但現行 Scale.TICK 回測是"
            "**整天的 tick 一次給**，兩者的報價 list 語意不同，訊號不可比。"
            "要用回測估量級請明確改為 Scale.DAY，或把 is_intraday 關掉。"
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
        self.strategy.setup_apis(self.data_feed)

    # === Direction Setting ===
    def get_allowed_directions(self) -> Set[PositionType]:
        """取得允許的訂單方向白名單；策略未指定時等同其宣告方向"""

        return order_preprocess.get_allowed_directions(
            self.strategy.allowed_directions, self.strategy.position_type
        )

    def get_execution_order(self) -> BarExecutionOrder:
        """單根 bar 的開平倉先後；推導表與理由見 `order_preprocess.get_execution_order()`"""

        return order_preprocess.get_execution_order(
            self.strategy.bar_execution_order,
            self.strategy.position_type,
            self.strategy.enable_intraday,
        )

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

        return order_preprocess.validate_orders(
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
        """同一根 bar 內委託的決定性排序；為什麼要自己排見 `order_preprocess.sort_orders()`"""

        return order_preprocess.sort_orders(orders)

    def validate_fill_price(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """成交價合理性檢查；規則由 FillModel 實作"""

        return self.fill_model.validate(order, quote)

    def apply_fill_model(
        self, order: BaseOrder, quote: Optional[BaseQuote], is_close: bool = False
    ) -> Optional[BaseOrder]:
        """
        - Description:
            套用市場執行假設（券源、滑價、成交量上限），回傳實際可成交的訂單

            未啟用任何假設時回傳原物件本身，行為與導入前逐筆相同。
            查無報價時直接放行——那是資料缺口，不是成交假設該處理的事。

            **成交後還要再驗一次價格區間**：`validate_fill_price()`
            跑在滑價之前，滑價把價格推出 `[low, high]` 之後沒有任何檢查。
            開倉腿夾回區間、平倉腿只警告——拒掉平倉單會讓部位被迫留倉，
            那是比價格偏一點嚴重得多的失真。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單
            - quote: Optional[BaseQuote]
                同一標的的當根 bar 報價
            - is_close: bool
                是否為平倉腿（決定超出區間時夾回還是只警告）
        - Return:
            - Optional[BaseOrder]
                可成交的訂單；不可成交時為 None
        """

        if quote is None:
            return order

        filled_order: Optional[BaseOrder] = self.fill_model.fill(order, quote)
        if filled_order is None:
            return None

        if is_close:
            self.fill_model.warn_close_price_out_of_range(filled_order, quote)
            return filled_order

        return self.fill_model.clamp_filled_price(filled_order, quote)

    def update_prev_close(self, quotes: List[BaseQuote]) -> None:
        """收盤後記錄當日收盤價；狀態由 FillModel 持有"""

        self.fill_model.on_bar_close(quotes)

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

                if self.scale == Scale.TICK:
                    self.run_tick_backtest(date)

                elif self.scale == Scale.DAY:
                    self.run_day_backtest(date)

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

    def run_tick_backtest(self, date: datetime.date) -> None:
        """
        Tick 級別的回測架構

        **已知前視**：整天的 tick 一次交給 `fill_model.on_bar_open()`，成交驗證用的是
        全日高低點，盤中較早的委託會通過稍後才出現的價位。TICK 回測的結果只能
        當量級參考，要精確得先改成逐筆餵入。
        """

        quotes: List[BaseQuote] = self.data_feed.get_quotes(date, Scale.TICK)

        if not quotes:
            return

        self.fill_model.on_bar_open(quotes)
        self.execute_bar(date, quotes)

    def run_day_backtest(self, date: datetime.date) -> None:
        """日 K 級別的回測架構"""

        quotes: List[BaseQuote] = self.data_feed.get_quotes(
            date, Scale.DAY, adjusted=self.adjusted_price
        )

        if not quotes:
            return

        self.execute_bar(date, quotes)

    def execute_bar(self, date: datetime.date, quotes: List[BaseQuote]) -> None:
        """
        - Description:
            單一時間切片的完整流程：依設定的執行順序開平倉，再做收盤後的部位檢查

            **同一標的同時出現在開倉與平倉訊號時，兩腿分別成交，不做 net 合併。**
            理由是成本與損益歸屬：證交稅只課賣出腿、當沖稅率減半也只認當沖的那一腿，
            合併成一張淨額委託會讓兩腿的費用與稅無法各自計算；且平倉腿必須實際成交
            才會產生 `TradeRecord`，net 掉等於整筆交易在報表上消失。
            兩腿的先後完全由 `BarExecutionOrder` 決定——這正是它存在的理由：

            - `OPEN_THEN_CLOSE`：先開後平，同一根 bar 內可完成當沖來回。
            - `CLOSE_THEN_OPEN`：先平後開，同一標的是「先出清舊倉再重新建倉」。

            同一階段內同一標的的多筆委託則依到達順序逐筆處理
            （`sort_orders()` 為穩定排序），支援分批建倉與部分平倉。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[BaseQuote]
                當根 bar 的報價
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

        # **除權息資料兩個方向都要**：做多跨除息要收現金股利、跨配股要調整股數，
        # 不餵的話做多績效會系統性偏低（除權息日的跳空變成憑空虧損）
        self.settlement.apply_cash_dividends(self.data_feed.get_cash_dividend_map(date))
        self.settlement.apply_share_ratios(self.data_feed.get_share_ratio_map(date))

        if self.get_execution_order() == BarExecutionOrder.OPEN_THEN_CLOSE:
            self.execute_open_signal(quotes)
            self.execute_close_signal(quotes)
        else:
            self.execute_close_signal(quotes)
            self.execute_open_signal(quotes)

        # 一根 bar 收盤後由市場規則強制執行的動作
        # 台股：當沖強制回補 ＋ 借券費計提 ＋ 維持率追繳
        # 期貨：每日結算 ＋ 保證金追繳 ＋ 到期換月
        self.settlement.on_bar_close(date, quotes, self.account, self.event_counts)

        self.snapshot_daily_equity(date, quotes)
        self.update_prev_close(quotes)

    # === Signal Execution ===
    def execute_open_signal(self, quotes: List[BaseQuote]) -> List[BasePosition]:
        """若倉位數量未達到限制且有開倉訊號，則執行開倉"""

        open_orders: List[BaseOrder] = self.strategy.check_open_signal(quotes)

        # 方向驗證 → 補值 → 決定性排序 → 成交價驗證，最後才進倉位管理器
        open_orders = self.sort_orders(
            self.enrich_orders(self.validate_orders(open_orders, "open"))
        )
        quote_map: Dict[str, BaseQuote] = {q.symbol: q for q in quotes}

        open_positions: List[BasePosition] = []
        for order in open_orders:
            if not self.check_max_holdings(order):
                continue

            self.submitted_orders.append((self.cur_date, "open", order))

            quote: Optional[BaseQuote] = quote_map.get(order.symbol)
            if quote is None:
                # **查不到報價的開倉單一律拒單**：放行的話成交驗證與成交模型都會
                # 被跳過（不查區間、漲跌停、成交量上限，也不吃滑價），直接以
                # 策略給的價格建倉——停牌的標的也開得進去。平倉腿不在此列：
                # 拒掉平倉會讓部位被迫留倉，那由結算層的連續無報價出場處理
                logger.warning(
                    f"[No Quote] {order.symbol} 當日查不到報價，開倉單已拒絕"
                )
                self.event_counts["rejected_no_quote"] += 1
                continue
            if not self.validate_fill_price(order, quote):
                continue

            filled_order: Optional[BaseOrder] = self.apply_fill_model(order, quote)
            if filled_order is None:
                continue

            open_position: Optional[BasePosition] = self.position_manager.open_position(
                filled_order
            )
            if open_position:
                open_positions.append(open_position)
        return open_positions

    def check_max_holdings(self, order: BaseOrder) -> bool:
        """持倉檔數硬上限；與 sizer 為何不合併見 `order_preprocess.check_max_holdings()`"""

        held_symbols: Set[str] = {
            position.symbol
            for position in self.account.positions
            if not position.is_closed
        }
        return order_preprocess.check_max_holdings(
            order, self.max_holdings, held_symbols, self.event_counts
        )

    def execute_close_signal(self, quotes: List[BaseQuote]) -> List[BaseTradeRecord]:
        """
        - Description:
            執行平倉邏輯：先判斷停損訊號，後判斷一般平倉

            **平倉的優先級固定為「停損 → 一般平倉」**，這是本階段唯一的優先級軸，
            不由排序鍵表達：停損是風控，若被一般平倉先吃掉部位，停損就等於沒發生。
            兩個優先級各自再依 `sort_orders()` 的穩定排序鍵處理，
            故整個平倉階段的順序完全可重現。

            停損執行完會**重新掃描剩餘部位**再產生一般平倉訊號，
            已被停損掉的標的不會再進入一般平倉的候選。
        - Parameters:
            - quotes: List[BaseQuote]
                當根 bar 的報價
        - Return:
            - close_records: List[BaseTradeRecord]
                本階段產生的平倉紀錄
        """

        positions: List[BaseQuote] = [
            q for q in quotes if self.account.check_has_position(q.symbol)
        ]

        # 無持倉時回傳空 list 而非 None，呼叫端才能直接串接
        if not positions:
            return []

        quote_map: Dict[str, BaseQuote] = {q.symbol: q for q in quotes}

        stop_loss_orders: List[BaseOrder] = self.strategy.check_stop_loss_signal(
            positions
        )
        stop_loss_orders = self.sort_orders(
            self.validate_orders(stop_loss_orders, "close")
        )
        self.submitted_orders.extend(
            (self.cur_date, "stop_loss", order) for order in stop_loss_orders
        )

        close_records: List[BaseTradeRecord] = []

        for order in stop_loss_orders:
            # 平倉同樣套用市場執行假設（滑價、成交量上限），但**刻意不做**
            # 價格合理性檢查——那是開倉專屬擋板，拒掉平倉單會讓部位被迫留倉，
            # 失真比成交價偏離區間嚴重得多。超出當日區間時只警告並計入
            # `close_price_out_of_range`
            filled_order: Optional[BaseOrder] = self.apply_fill_model(
                order, quote_map.get(order.symbol), is_close=True
            )
            if filled_order is None:
                continue

            close_positions: List[BaseTradeRecord] = (
                self.position_manager.close_position(filled_order)
            )
            close_records.extend(close_positions)

        remaining_positions: List[BaseQuote] = [
            q for q in quotes if self.account.check_has_position(q.symbol)
        ]

        close_orders: List[BaseOrder] = self.strategy.check_close_signal(
            remaining_positions
        )
        close_orders = self.sort_orders(self.validate_orders(close_orders, "close"))
        self.submitted_orders.extend(
            (self.cur_date, "close", order) for order in close_orders
        )

        for order in close_orders:
            filled_order: Optional[BaseOrder] = self.apply_fill_model(
                order, quote_map.get(order.symbol), is_close=True
            )
            if filled_order is None:
                continue

            close_positions: List[BaseTradeRecord] = (
                self.position_manager.close_position(filled_order)
            )
            close_records.extend(close_positions)

        return close_records

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
