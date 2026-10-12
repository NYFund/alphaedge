import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Protocol, Set, Tuple

from loguru import logger

from core.backtest.models.fill_model import BaseFillModel
from core.datafeed.base import BaseDataFeed
from core.models import (
    BaseAccount,
    BaseOrder,
    BasePosition,
    BaseQuote,
    BaseTradeRecord,
)
from core.portfolio import order_rules
from core.position.base.position_manager import BasePositionManager
from core.strategies.base import BaseStrategy
from core.utils import BarExecutionSequence, TickUniverse

"""
回測 simulator 的共同介面與共用的開平倉執行

日 K（`BarSimulator`）與逐筆（日後的 `TickSimulator`）差在「一天怎麼切、委託什麼時候成交」，
訊號送進來之後的處理（方向驗證 → 補值 → 排序 → 成交 → 入帳）兩邊相同，所以放在基底。
"""


class SimulationEngine(Protocol):
    """
    - Description:
        simulator 向引擎取用的服務與共用狀態，實作者是 `Backtester`

        **以 Protocol 描述而不 import `Backtester`**：simulator 與可插拔 model 同層，
        引擎本體在它上面一層，往上 import 會被分層檢查擋下。
        simulator 自己不持有帳戶與委託紀錄這類狀態，全部經由引擎取用，
        `submitted_orders` 這類實盤 parity 要讀的東西才只有一份。
    """

    strategy: BaseStrategy
    account: BaseAccount
    position_manager: BasePositionManager
    fill_model: BaseFillModel
    data_feed: BaseDataFeed
    event_counts: Dict[str, int]
    submitted_orders: List[Tuple[datetime.date, str, BaseOrder]]
    cur_date: datetime.date
    max_holdings: Optional[int]
    scale: str
    adjusted_price: bool
    tick_universe: Optional[TickUniverse]

    def validate_orders(self, orders: List[BaseOrder], stage: str) -> List[BaseOrder]:
        """方向白名單驗證"""

        ...

    def enrich_orders(self, orders: List[BaseOrder]) -> List[BaseOrder]:
        """補上市場專屬的訂單欄位"""

        ...

    def sort_orders(self, orders: List[BaseOrder]) -> List[BaseOrder]:
        """同一批委託的決定性排序"""

        ...

    def get_execution_sequence(self) -> BarExecutionSequence:
        """開平倉先後"""

        ...


class BaseSimulator(ABC):
    """
    - Description:
        回測 simulator 基底：一天的報價怎麼交給策略、委託怎麼成交

        引擎每個交易日依序呼叫 `load_day()` → `Backtester.prepare_day()` → `run_day()` →
        `Backtester.finish_day()`。`load_day()` 獨立出來，是因為「當天沒有報價就整天跳過」——
        連每日準備與收盤結算都不做——引擎必須在準備之前就知道。
    """

    def __init__(self, engine: SimulationEngine) -> None:
        """
        - Description:
            建立 simulator
        - Parameters:
            - engine: SimulationEngine
                引擎；帳戶、部位、成交模型與委託紀錄都經由它取用
        """

        self.engine: SimulationEngine = engine

    @abstractmethod
    def load_day(self, date: datetime.date) -> bool:
        """
        - Description:
            載入當天的報價
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - bool
                False 表示當天沒有任何報價，引擎整天跳過
        """

        pass

    @abstractmethod
    def run_day(self, date: datetime.date) -> List[BaseQuote]:
        """
        - Description:
            跑完一個交易日的開平倉（`load_day()` 回傳 True 之後才會被呼叫）
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - List[BaseQuote]
                收盤報價，交給 `Backtester.finish_day()` 做結算、盯市與次日的漲跌停參考價
        """

        pass

    # === Fill ===
    def validate_fill_price(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """成交價合理性檢查；規則由 FillModel 實作"""

        return self.engine.fill_model.validate(order, quote)

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

        fill_model: BaseFillModel = self.engine.fill_model
        filled_order: Optional[BaseOrder] = fill_model.fill(order, quote)
        if filled_order is None:
            return None

        if is_close:
            fill_model.warn_close_price_out_of_range(filled_order, quote)
            return filled_order

        return fill_model.clamp_filled_price(filled_order, quote)

    def check_max_holdings(self, order: BaseOrder) -> bool:
        """持倉檔數硬上限；與 sizer 為何不合併見 `order_rules.check_max_holdings()`"""

        held_symbols: Set[str] = {
            position.symbol
            for position in self.engine.account.positions
            if not position.is_closed
        }
        return order_rules.check_max_holdings(
            order, self.engine.max_holdings, held_symbols, self.engine.event_counts
        )

    # === Signal Execution ===
    def filter_tradable_quotes(self, quotes: List[BaseQuote]) -> List[BaseQuote]:
        """
        - Description:
            只把當根 bar 有成交的報價交給策略

            台股 adapter 已先丟掉無成交價的列，這裡對它是 no-op。期貨的零成交契約
            不能在 adapter 丟——日盤零成交的列仍有結算價，逐日盯市要靠它——
            只能在這裡攔：策略拿到一筆四價皆 0 的報價，會以 0 元下平倉單、
            或算出 −100% 的停損。

            攔掉之後兩個市場的行為一致：零成交標的的開倉單走「查不到報價」
            那條拒單路徑（`rejected_no_quote`），平倉則留給結算層的連續無報價出場。
            完整的報價（含零成交）仍由 `run_day()` 原樣交給 `Backtester.finish_day()`。
        - Parameters:
            - quotes: List[BaseQuote]
                當根 bar 的全部報價
        - Return:
            - List[BaseQuote]
                有成交的報價
        """

        fill_model: BaseFillModel = self.engine.fill_model
        return [quote for quote in quotes if fill_model.has_tradable_quote(quote)]

    def execute_open_signal(self, quotes: List[BaseQuote]) -> List[BasePosition]:
        """若倉位數量未達到限制且有開倉訊號，則執行開倉"""

        engine: SimulationEngine = self.engine
        quotes = self.filter_tradable_quotes(quotes)
        open_orders: List[BaseOrder] = engine.strategy.check_open_signal(quotes)

        # 方向驗證 → 補值 → 決定性排序 → 成交價驗證，最後才進倉位管理器
        open_orders = engine.sort_orders(
            engine.enrich_orders(engine.validate_orders(open_orders, "open"))
        )
        quote_map: Dict[str, BaseQuote] = {q.symbol: q for q in quotes}

        open_positions: List[BasePosition] = []
        for order in open_orders:
            if not self.check_max_holdings(order):
                continue

            engine.submitted_orders.append((engine.cur_date, "open", order))

            quote: Optional[BaseQuote] = quote_map.get(order.symbol)
            if quote is None:
                # **查不到報價的開倉單一律拒單**：放行的話成交驗證與成交模型都會
                # 被跳過（不查區間、漲跌停、成交量上限，也不吃滑價），直接以
                # 策略給的價格建倉——停牌的標的也開得進去。平倉腿不在此列：
                # 拒掉平倉會讓部位被迫留倉，那由結算層的連續無報價出場處理
                logger.warning(
                    f"[No Quote] {order.symbol} 當日查不到報價，開倉單已拒絕"
                )
                engine.event_counts["rejected_no_quote"] += 1
                continue
            if not self.validate_fill_price(order, quote):
                continue

            filled_order: Optional[BaseOrder] = self.apply_fill_model(order, quote)
            if filled_order is None:
                continue

            open_position: Optional[BasePosition] = (
                engine.position_manager.open_position(filled_order)
            )
            if open_position:
                open_positions.append(open_position)
        return open_positions

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

        engine: SimulationEngine = self.engine
        quotes = self.filter_tradable_quotes(quotes)
        positions: List[BaseQuote] = [
            q for q in quotes if engine.account.check_has_position(q.symbol)
        ]

        # 無持倉時回傳空 list 而非 None，呼叫端才能直接串接
        if not positions:
            return []

        quote_map: Dict[str, BaseQuote] = {q.symbol: q for q in quotes}

        stop_loss_orders: List[BaseOrder] = engine.strategy.check_stop_loss_signal(
            positions
        )
        stop_loss_orders = engine.sort_orders(
            engine.validate_orders(stop_loss_orders, "close")
        )
        engine.submitted_orders.extend(
            (engine.cur_date, "stop_loss", order) for order in stop_loss_orders
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
                engine.position_manager.close_position(filled_order)
            )
            close_records.extend(close_positions)

        remaining_positions: List[BaseQuote] = [
            q for q in quotes if engine.account.check_has_position(q.symbol)
        ]

        close_orders: List[BaseOrder] = engine.strategy.check_close_signal(
            remaining_positions
        )
        close_orders = engine.sort_orders(engine.validate_orders(close_orders, "close"))
        engine.submitted_orders.extend(
            (engine.cur_date, "close", order) for order in close_orders
        )

        for order in close_orders:
            filled_order: Optional[BaseOrder] = self.apply_fill_model(
                order, quote_map.get(order.symbol), is_close=True
            )
            if filled_order is None:
                continue

            close_positions: List[BaseTradeRecord] = (
                engine.position_manager.close_position(filled_order)
            )
            close_records.extend(close_positions)

        return close_records
