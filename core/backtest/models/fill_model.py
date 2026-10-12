import copy
import datetime
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from core.backtest.models.event_counts import (
    EVENT_KEYS,
    FUTURES_EVENT_KEYS,
    new_event_counts,
)
from core.datafeed.base import BaseDataFeed
from core.market.instrument_spec import InstrumentSpec
from core.market.tw.instrument_spec import TwFuturesSpec, TwStockSpec
from core.models import (
    BaseOrder,
    BaseQuote,
    DayTradeListSnapshot,
    PriceLimits,
    ShortSaleListSnapshot,
)
from core.models.fill_config import FillConfig, FuturesFillConfig, VolumeCapPolicy
from core.utils import (
    Action,
    ExecutionStyle,
    PositionType,
    Scale,
    ShortMethod,
    TimeUtils,
)

"""
FillModel: 這張單成不成交、以什麼價量成交

包含五件事，全部屬「市場執行假設」而非引擎邏輯：
1. **成交價可信度**（`validate()`）：前視偏誤與不可能成交的擋板
2. **滑價**（`FillConfig.slippage_bps_*`）：拿不到理想價
3. **成交量上限**（`FillConfig.max_volume_share`）：一張單不可能吃掉當日大半成交量
4. **券源檢核**（`ShortConstraint.check_borrowable`）：借不到券就放空不了
5. **交易所名單檢核**（`ShortConstraint.check_short_sale_list`／`check_day_trade_list`）：
   不在名單上、被註記暫停，或平盤下不得放空的標的放空不了

2~5 四項的預設值皆為「關閉」，此時不改動任何訂單的價量。
"""


class BaseFillModel(ABC):
    """
    成交價模型：判斷一張訂單在該根 bar 是否可能以指定價格成交

    成交價可信度是市場規則而非引擎邏輯，故與 InstrumentSpec 一樣下沉為可插拔 model。
    對應 Lean 的 FillModel。

    **`validate()` 與 `fill()` 都是樣板方法**，兩個市場共用同一條流程，只在鉤子處不同：
    - `validate()`：無成交擋板 → 當根 bar 區間 → `validate_market_rules()`（台股做漲跌停與
      鎖死，期貨沒有）→ 檔位警告
    - `fill()`：`check_order_eligibility()`（台股依序檢查交易所名單、券源、停券，期貨一律通過）
      → 滑價 → 成交量上限 → 訂單副本

    建構子備妥共用的四個屬性：`instrument`、`config`、`event_counts`（與引擎共用同一個 dict）、
    `intraday_range`、`reference_prices`。市場專屬的狀態由子類在 `super().__init__()` 之後補。
    """

    # 自備計數器時要用的 key 清單：台股用台股清單，期貨子類改成期貨清單。
    # **自備的那份要有全部 key**：只塞用得到的幾個，單獨建模型時走到成交量上限、
    # 券源或停券就會 `KeyError`
    EVENT_KEYS: Tuple[str, ...] = EVENT_KEYS

    # 未指定成交假設時的預設設定類別（滑價、成交量上限全關）
    CONFIG_CLS: type = FillConfig

    # 數量單位：股票論張、期貨論口。只影響 log 訊息，成交量上限的政策兩邊相同
    VOLUME_UNIT: str = "張"

    # 交易所名單檢核是否開啟；引擎據此要求資料源確認名單涵蓋整段區間。
    # 沒有名單制度的市場維持 False
    check_short_sale_list: bool = False
    check_day_trade_list: bool = False

    def __init__(
        self,
        instrument: InstrumentSpec,
        event_counts: Optional[Dict[str, int]] = None,
        config: Optional[FillConfig] = None,
    ) -> None:
        self.instrument: InstrumentSpec = instrument

        # 成交假設（滑價、成交量上限）；預設全關
        self.config: FillConfig = config or self.CONFIG_CLS()

        # 與引擎共用同一個 dict，拒單計數才會反映到報表（傳 None 時自行持有，供單獨測試）
        self.event_counts: Dict[str, int] = (
            event_counts
            if event_counts is not None
            else new_event_counts(self.EVENT_KEYS)
        )

        # Tick 級別的當日累計高低點（TickQuote 沒有 OHLC，成交價驗證需自行維護）
        self.intraday_range: Dict[str, Tuple[float, float]] = {}

        # 漲跌停參考價：前一交易日收盤，台股除權息日由 `apply_price_limit_basis()`
        # 覆寫為公告基準。期貨沒有漲跌停檢查，這份只供外部查詢——
        # 盯市一律走 `SettlementModel.get_mark_price()`，不看這裡
        self.reference_prices: Dict[str, float] = {}

    def validate(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """
        - Description:
            成交價合理性檢查（前視偏誤與不可能成交的擋板）：
            無成交 → 當根 bar 區間 → 市場規則 → 檔位警告

            檔位未對齊僅記錄警告不拒單——資料本身的價格精度問題不該擋掉正常回測。
        - Parameters:
            - order: BaseOrder
                待驗證的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        if not self.has_tradable_quote(quote):
            logger.warning(
                f"[Validate Fill] {order.symbol} 當日無成交（volume={quote.volume}、"
                f"close={quote.close}），拒單"
            )
            self.event_counts["rejected_fill_price"] += 1
            return False

        low, high = self.get_price_range(quote)
        if low is not None and high is not None and not (low <= order.price <= high):
            logger.warning(
                f"[Validate Fill] {order.symbol} 成交價 {order.price} 超出當日區間 "
                f"[{low}, {high}]，拒單"
            )
            self.event_counts["rejected_fill_price"] += 1
            return False

        if not self.validate_market_rules(order, quote):
            return False

        product: Optional[str] = self.instrument.resolve_product(order)
        if (
            self.instrument.round_to_tick(order.price, "nearest", product)
            != order.price
        ):
            logger.warning(
                f"[Validate Fill] {order.symbol} 成交價 {order.price} 未對齊檔位"
            )

        return True

    def validate_market_rules(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """
        - Description:
            市場專屬的成交擋板（台股：漲跌停與全日鎖死）；沒有這類制度的市場一律通過

            拒單時由子類自行 warning 並計數——事件 key 依規則不同。
        - Parameters:
            - order: BaseOrder
                已通過區間檢查的訂單
            - quote: BaseQuote
                當根 bar 的報價
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        return True

    def update_intraday_range(self, quotes: List[BaseQuote]) -> None:
        """
        更新 Tick 級別的當日累計高低點

        只納入傳進來的報價；逐筆 simulator 每一筆呼叫一次（每天開始時清空），
        累計出來的是「到這一筆為止」的區間，沒有前視。
        """

        for quote in quotes:
            price: float = quote.cur_price or quote.close
            if not price:
                continue

            low, high = self.intraday_range.get(quote.symbol, (price, price))
            self.intraday_range[quote.symbol] = (min(low, price), max(high, price))

    def update_reference_prices(self, quotes: List[BaseQuote]) -> None:
        """
        一根 bar 收盤後：記錄收盤價，作為次一根 bar 的漲跌停參考價

        除權息日的參考價不是前收，由 `apply_price_limit_basis()` 在當天開盤前覆寫。

        **記的是收盤價、不是結算價**：期貨的盯市價一律走 `SettlementModel`，
        這裡只負責次日的漲跌停基準。
        """

        for quote in quotes:
            close: float = quote.close or quote.cur_price
            if close:
                self.reference_prices[quote.symbol] = close

    def get_filled_volume(self, order: BaseOrder, quote: BaseQuote) -> Optional[int]:
        """
        - Description:
            套用成交量上限：單筆訂單數量不得超過當日成交量的指定比例

            **只在 DAY 級別生效**：TICK 的報價 `volume` 是當日累計量，逐筆成交的上限另以
            `TwStockFillModel.get_tick_filled_volume()` 計算（分母是這一筆的對手方委託量）。

            股票與期貨是同一套政策（縮量或拒單），只有單位不同——
            單位字由子類的 `VOLUME_UNIT` 提供，不為了一個字各寫一份。
        - Parameters:
            - order: BaseOrder
                待檢查的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - Optional[int]
                可成交數量；整筆拒單時為 None
        """

        share: Optional[float] = self.config.max_volume_share

        if not share or quote.scale != Scale.DAY or not quote.volume:
            return order.volume

        cap: int = int(quote.volume * share)

        if order.volume <= cap:
            return order.volume

        unit: str = self.VOLUME_UNIT

        if self.config.volume_cap_policy == VolumeCapPolicy.REJECT:
            logger.warning(
                f"[Fill] {order.symbol} 委託 {order.volume} {unit} > 當日成交量上限 "
                f"{cap} {unit}（{share:.1%} × {quote.volume}），拒單"
            )
            self.event_counts["rejected_volume_cap"] += 1
            return None

        if cap <= 0:
            logger.warning(
                f"[Fill] {order.symbol} 當日成交量上限不足一{unit}（{share:.1%} × "
                f"{quote.volume}），拒單"
            )
            self.event_counts["rejected_volume_cap"] += 1
            return None

        logger.warning(
            f"[Fill] {order.symbol} 委託 {order.volume} {unit}縮量至 {cap} {unit}"
            f"（當日成交量 {quote.volume} {unit}的 {share:.1%}）"
        )
        self.event_counts["truncated_by_volume"] += 1
        return cap

    def prepare_day(
        self, feed: BaseDataFeed, date: datetime.date, allow_short: bool
    ) -> None:
        """
        - Description:
            每日開盤前，自己向資料源取當天需要的市場狀態；沒有這類制度的市場沿用預設 no-op

            理由與 `BaseSettlementModel.prepare_day()` 相同：要取哪些狀態是市場規則，不是引擎的事。
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
            - date: datetime.date
                交易日
            - allow_short: bool
                策略是否可能放空；不可能時不必取放空才用得到的狀態
        """

        pass

    def fill(self, order: BaseOrder, quote: BaseQuote) -> Optional[BaseOrder]:
        """
        - Description:
            決定這張單成不成交、以什麼價量成交：
            `check_order_eligibility()` → 滑價 → 成交量上限 → 訂單副本

            **與 `validate()` 的分工**：`validate()` 只回答「這個價格在當根 bar
            可不可能成交」，是既有的前視偏誤擋板；`fill()` 負責市場執行假設
            （名單、券源、滑價、成交量上限），並在需要調整時回傳**訂單副本**。

            **絕不就地修改傳入的 order**：策略可能持有同一個物件，
            就地改動會讓策略下一根 bar 看到被引擎改過的價量。
            未啟用任何假設時直接回傳**原物件**，下游就不可能拿到被改過的價量。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - Optional[BaseOrder]
                可成交的訂單（未調整時為原物件本身）；不可成交時為 None
        """

        if not self.check_order_eligibility(order):
            return None

        price: float = self.get_filled_price(order)
        volume: Optional[int] = self.get_filled_volume(order, quote)

        if volume is None:
            return None

        if price == order.price and volume == order.volume:
            return order

        filled_order: BaseOrder = copy.copy(order)
        filled_order.price = price
        filled_order.volume = volume
        # **滑價前的委託價留在副本上**：滑價成本＝（成交價 − 委託價）× 數量 ×
        # 計價單位，而計價單位只有部位管理層知道（期貨的乘數逐契約不同），
        # 它又拿不到原單——兩者只能在副本上碰頭
        filled_order.reference_price = order.price
        return filled_order

    def check_order_eligibility(self, order: BaseOrder) -> bool:
        """
        - Description:
            這張單在制度上能不能成交（台股：交易所名單、券源、停券）；沒有這類制度的市場一律通過

            拒單時由子類自行 warning 並計數——事件 key 依規則不同。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單（逐筆路徑傳入的是已帶成交價的副本）
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        return True

    @staticmethod
    def get_tick_fill_price(
        order: BaseOrder, style: ExecutionStyle, quote: BaseQuote, auction: bool
    ) -> Optional[float]:
        """
        - Description:
            在途委託遇到同一檔的一筆 tick 時，能不能成交、成交價多少

            - **對手方掛單為 0 不成交**：買單要有委賣（`ask_price` > 0）、賣單要有委買；
              為 0 代表鎖漲停沒有人賣、鎖跌停沒有人買，集合競價也一樣。
            - **集合競價的那一筆**（開盤的第一筆、收盤集合競價）：以該筆成交價成交；
              限價單要成交價優於或等於限價才成交。
            - **連續競價、市價**：買吃這一筆的 `ask_price`、賣吃 `bid_price`。
            - **連續競價、限價**：成交價**穿越**限價才成交（買要成交價低於限價、賣要高於），
              成交價取限價。只碰到限價不算：同價位前面還有人排隊，碰到就成交會偏樂觀。

            放在基底：這是「逐筆撮合」的規則，與市場無關；市場差異（名單、券源、成交量）在 `fill_tick()`。
        - Parameters:
            - order: BaseOrder
                在途委託（`price` 是策略的委託價：限價單的限價、市價單的決策價）
            - style: ExecutionStyle
                市價或限價
            - quote: BaseQuote
                同一檔的這一筆 tick（`tick_quote` 帶 bid/ask）
            - auction: bool
                這一筆是否為集合競價的成交
        - Return:
            - Optional[float]
                成交價；這一筆不能成交時為 None
        """

        tick: Any = quote.tick_quote
        is_buy: bool = order.action == Action.BUY

        if is_buy and not tick.ask_price:
            return None
        if not is_buy and not tick.bid_price:
            return None

        if auction:
            price: float = tick.close
            if style == ExecutionStyle.LIMIT and (
                (is_buy and price > order.price) or (not is_buy and price < order.price)
            ):
                return None
            return price

        if style == ExecutionStyle.MARKET:
            return tick.ask_price if is_buy else tick.bid_price

        if (is_buy and tick.close < order.price) or (
            not is_buy and tick.close > order.price
        ):
            return order.price
        return None

    def fill_tick(
        self, order: BaseOrder, price: float, quote: BaseQuote, auction: bool
    ) -> Optional[BaseOrder]:
        """
        - Description:
            逐筆成交：以 `get_tick_fill_price()` 決定的價格成交，套用市場的成交假設

            預設不支援：沒有逐筆資料的市場（期貨）走不到這裡，走到就是組裝錯了。
        - Parameters:
            - order: BaseOrder
                在途委託
            - price: float
                這一筆決定的成交價（滑價前）
            - quote: BaseQuote
                同一檔的這一筆 tick
            - auction: bool
                這一筆是否為集合競價的成交
        - Return:
            - Optional[BaseOrder]
                成交的訂單副本；不可成交時為 None
        """

        raise NotImplementedError(f"{type(self).__name__} 不支援逐筆成交")

    @abstractmethod
    def get_filled_price(self, order: BaseOrder) -> float:
        """
        - Description:
            依訂單方向套用滑價，回傳含滑價的成交價

            **列為抽象方法**：策略委託（`fill()`）與引擎強制出場
            （`BaseSettlementModel.apply_fill_price()`）都以本方法為唯一的滑價入口。
            少一個市場沒實作，那個市場的強制出場要到執行期才會炸。
        - Parameters:
            - order: BaseOrder
                待套用滑價的訂單
        - Return:
            - float
                含滑價的成交價；未設定滑價時即原委託價
        """

        pass

    @staticmethod
    def has_tradable_quote(quote: BaseQuote) -> bool:
        """
        - Description:
            該報價當日是否真的有成交（台股與期貨共用同一個判準）

            **無成交的 bar 在報價上是成交量 0、四價皆 0**：台股的來源給 `--`，
            舊資料存成 0；期貨的零成交列存成 NULL，由 adapter 收斂成 0。
            `get_price_range()` 對 0 回 `(None, None)` 而**跳過**區間檢查，
            不擋的話策略能在一個根本沒開盤或整天無量的標的上以任意價格成交。

            台股 adapter 會先丟掉這種列，策略根本看不到；期貨不能丟（零成交的
            日盤列仍有結算價，盯市要靠它），所以 simulator 把策略看得到的報價
            也用這一條過濾，開倉的 `validate()` 再擋一次是最後一道。

            只擋 DAY 級別：TICK 的每一筆本來就是成交，`volume` 為 0 的 tick
            （試撮）由 `intraday_range` 自行處理。
        - Parameters:
            - quote: BaseQuote
                當根 bar 的報價
        - Return:
            - bool
                有成交為 True
        """

        if quote.scale == Scale.TICK:
            return True

        return bool(quote.volume) and bool(quote.close or quote.cur_price)

    def get_price_range(
        self, quote: BaseQuote
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        取得該報價可成交的價格區間：日 K 用 OHLC，Tick 用「到目前這一筆為止」的當日累計高低點

        **放在基底而不是各市場一份**：台股與期貨的答案一模一樣，各寫一次必然漂移；
        夾價與平倉腿的區間警告都以此為唯一判準。
        """

        if quote.scale == Scale.TICK:
            return self.intraday_range.get(quote.symbol, (None, None))

        if quote.high and quote.low:
            return (quote.low, quote.high)

        # 只成交一筆的 bar 來源不給高低價（期交所記成 `-`），但那一筆的價格就是
        # 收盤價，區間就是 `[close, close]`——沒有這一條，委託價等於實際成交價
        # 也會被當成「沒有區間可比」而放行或誤拒
        if quote.close:
            return (quote.close, quote.close)

        return (None, None)

    def clamp_filled_price(self, order: BaseOrder, quote: BaseQuote) -> BaseOrder:
        """
        - Description:
            把成交價夾回當根 bar 的 `[low, high]`

            **`validate()` 跑在 `fill()` 之前**，於是滑價把價格推出區間之後
            沒有任何檢查：一筆本來在區間內的單，加了 0.5% 滑價
            就可能成交在當日根本沒出現過的價位，而回測不會有任何跡象。

            夾回而不是拒單：滑價是使用者刻意開啟的假設，拒單會讓「加了滑價之後
            訊號數反而變少」，那比價格偏一點更難解釋。

            **改動的是副本，不是傳入的 order**（與 `fill()` 同一種寫法）：
            雖然目前夾價只會發生在 `fill()` 產生的副本上，那是呼叫順序的巧合
            而非保證；就地改動會讓策略下一根 bar 看到被引擎改過的價格。
        - Parameters:
            - order: BaseOrder
                已套用滑價的訂單
            - quote: BaseQuote
                當根 bar 的報價
        - Return:
            - BaseOrder
                價格落在區間內的訂單（未超出時為原物件）
        """

        low, high = self.get_price_range(quote)
        if low is None or high is None:
            return order

        clamped: float = min(max(order.price, low), high)
        if clamped == order.price:
            return order

        logger.warning(
            f"[Fill] {order.symbol} 滑價後成交價 {order.price} 超出當日區間 "
            f"[{low}, {high}]，夾回 {clamped}"
        )
        self.event_counts["fill_price_clamped"] = (
            self.event_counts.get("fill_price_clamped", 0) + 1
        )

        clamped_order: BaseOrder = copy.copy(order)
        clamped_order.price = clamped
        return clamped_order

    def warn_close_price_out_of_range(self, order: BaseOrder, quote: BaseQuote) -> None:
        """
        - Description:
            平倉腿的成交價超出當日區間時只警告並計數，不拒單

            **平倉不能被拒**：拒掉一張平倉單會讓部位被迫留倉，那是比價格偏一點
            嚴重得多的失真。但超出區間仍是值得看見的事，故留一個事件計數。
        - Parameters:
            - order: BaseOrder
                平倉訂單
            - quote: BaseQuote
                當根 bar 的報價
        """

        low, high = self.get_price_range(quote)
        if low is None or high is None:
            return

        if low <= order.price <= high:
            return

        logger.warning(
            f"[Fill] {order.symbol} 平倉成交價 {order.price} 超出當日區間 "
            f"[{low}, {high}]（平倉不拒單，僅計數）"
        )
        self.event_counts["close_price_out_of_range"] = (
            self.event_counts.get("close_price_out_of_range", 0) + 1
        )


class TwStockFillModel(BaseFillModel):
    """
    台股成交價模型

    - DAY 級別以當日 high/low 為界；TICK 級別以當日「已發生」的累計高低點為界
    - 漲跌停以前一交易日收盤為基準；尚未取得前收時跳過該項檢查
    - 檔位未對齊僅記錄警告，不拒單（避免既有資料的價格精度問題擋掉正常回測）
    """

    def __init__(
        self,
        instrument: Optional[InstrumentSpec] = None,
        event_counts: Optional[Dict[str, int]] = None,
        config: Optional[FillConfig] = None,
        check_borrowable: bool = False,
        check_short_sale_list: bool = False,
        check_day_trade_list: bool = False,
    ) -> None:
        super().__init__(instrument or TwStockSpec(), event_counts, config)

        # 是否檢核券源；由 factory 依 ShortConstraint.check_borrowable 帶入
        self.check_borrowable: bool = check_borrowable

        # 是否依交易所名單檢核放空開倉；由 factory 依 ShortConstraint 帶入
        self.check_short_sale_list: bool = check_short_sale_list
        self.check_day_trade_list: bool = check_day_trade_list
        # 當日名單，`prepare_day()` 每天從 DataFeed 取
        self.short_sale_list: Optional[ShortSaleListSnapshot] = None
        self.day_trade_list: Optional[DayTradeListSnapshot] = None

        # 當日可借券餘額（張）：{stock_id: 融券今日餘額}，由 DataFeed 於每根 bar 開始時提供
        self.short_balance: Dict[str, int] = {}

        # 今日停券的標的，`prepare_day()` 每天從 DataFeed 取
        self.short_suspended_symbols: Set[str] = set()

    def validate_market_rules(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """台股的市場擋板：漲跌停區間（前收 ±幅度，除權息日為公告基準）→ 全日鎖漲跌停"""

        reference_price: Optional[float] = self.reference_prices.get(order.symbol)
        if reference_price:
            # 帶入報價日期：2015-06-01 前的漲跌停幅度為 7%，非現行的 10%
            limits: PriceLimits = self.instrument.get_price_limits(
                reference_price, TimeUtils.to_date(quote.date), order.symbol
            )
            if not (limits.limit_down <= order.price <= limits.limit_up):
                logger.warning(
                    f"[Validate Fill] {order.symbol} 成交價 {order.price} 超出漲跌停 "
                    f"[{limits.limit_down}, {limits.limit_up}]，拒單"
                )
                self.event_counts["rejected_fill_price"] += 1
                return False

        return not self.is_locked_at_limit(order, quote, reference_price)

    def is_locked_at_limit(
        self, order: BaseOrder, quote: BaseQuote, reference_price: Optional[float]
    ) -> bool:
        """
        - Description:
            全日鎖死漲停（買進）或跌停（賣出）時拒單，並計入事件

            **這條擋的是開倉**：`validate()` 只跑在開倉路徑上（拒掉平倉單會讓部位
            被迫留倉，那是更嚴重的失真）。開高低收都等於漲停價，代表整天沒有人
            願意在漲停以下賣出，實務上排隊也買不到——而 `VolumeBreakoutMomentumStrategy` 的
            訊號正是「當日漲幅 ≥ 9%、以收盤價買進」，這類標的大量是鎖漲停，
            照常成交會讓做多績效系統性偏樂觀，且沒有任何徵兆。
        - Parameters:
            - order: BaseOrder
                待驗證的開倉單
            - quote: BaseQuote
                當根 bar 的報價
            - reference_price: Optional[float]
                漲跌停參考價
        - Return:
            - bool
                True 表示被鎖死、應拒單
        """

        # Tick 級別沒有當日四價，無從判定鎖死
        if not all(hasattr(quote, field) for field in ("open", "high", "low", "close")):
            return False

        if not self.instrument.is_locked_at_limit(
            prev_close=reference_price,
            open_price=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.close,
            side=order.action,
            date=TimeUtils.to_date(quote.date),
            product=order.symbol,
        ):
            return False

        locked_side: str = "漲停" if order.action == Action.BUY else "跌停"
        event_key: str = (
            "rejected_limit_up_locked"
            if order.action == Action.BUY
            else "rejected_limit_down_locked"
        )
        logger.warning(
            f"[Validate Fill] {order.symbol} 當日全日鎖{locked_side}"
            f"（開高低收皆為 {quote.close}），開倉單無法成交，拒單"
        )
        self.event_counts[event_key] += 1
        return True

    def prepare_day(
        self, feed: BaseDataFeed, date: datetime.date, allow_short: bool
    ) -> None:
        """
        - Description:
            每日開盤前取當天的漲跌停基準、融券餘額，以及（可能放空時）停券標的與交易所名單

            - **除權息日的漲跌停基準要在下任何單之前覆寫**：否則整段漲跌停區間會沿用偏高的前收而失準。
            - **停券與名單只在可能放空時取**：推導停券日要掃整段交易日曆，純做多策略不可能有空單，連查都不查。
            - **交易所名單只在開啟檢核時取**：每天兩次查詢，不用的回測不該付這個成本。
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
            - date: datetime.date
                交易日
            - allow_short: bool
                策略是否可能放空
        """

        self.apply_price_limit_basis(feed.get_price_limit_basis(date))
        self.apply_short_balance(feed.get_short_balance(date))

        if not allow_short:
            return

        # 停券期間不得新增融券賣出；回補日當天的強制回補由 settlement 處理，
        # 這裡擋的是回補日之後到除權息交易日之間的新開倉
        self.apply_short_suspended_symbols(feed.get_short_suspended_symbols(date))
        if self.check_short_sale_list or self.check_day_trade_list:
            self.apply_trading_lists(
                feed.get_short_sale_list(date) if self.check_short_sale_list else None,
                feed.get_day_trade_list(date) if self.check_day_trade_list else None,
            )

    def apply_short_balance(self, balance: Dict[str, int]) -> None:
        """
        - Description:
            更新當日可借券餘額（融券今日餘額，單位：張）

            **空 dict 代表「今日查無融券資料」而非「所有標的都借不到券」**——
            `fill()` 在查不到餘額時一律放行並記錄，不可預設拒單。
        - Parameters:
            - balance: Dict[str, int]
                `{stock_id: 融券今日餘額}`，由 DataFeed 依當日信用交易資料提供
        """

        self.short_balance = balance

    def apply_short_suspended_symbols(self, symbols: Set[str]) -> None:
        """
        - Description:
            更新今日處於停券期間（融券最後回補日 ~ 除權息交易日）的標的

            **回補日當天的強制回補擋不住新開倉**：少了這份清單，留倉放空策略
            可以在回補日之後到除權息交易日之間開新的融券空單並持有跨過除權息。
        - Parameters:
            - symbols: Set[str]
                今日停券的標的，由 DataFeed 依除權息行事曆推導
        """

        self.short_suspended_symbols = symbols

    def apply_trading_lists(
        self,
        short_sale_list: Optional[ShortSaleListSnapshot],
        day_trade_list: Optional[DayTradeListSnapshot],
    ) -> None:
        """
        - Description:
            更新當日的平盤下融（借）券名單與現股當沖名單
        - Parameters:
            - short_sale_list: Optional[ShortSaleListSnapshot]
                當日平盤下得融（借）券賣出名單
            - day_trade_list: Optional[DayTradeListSnapshot]
                當日現股當沖標的名單
        """

        self.short_sale_list = short_sale_list
        self.day_trade_list = day_trade_list

    def check_order_eligibility(self, order: BaseOrder) -> bool:
        """台股的制度擋板：交易所名單 → 券源 → 停券（三項預設皆關閉，此時一律通過）"""

        if not self.check_trading_lists(order):
            return False

        if not self.check_short_borrowable(order):
            return False

        return self.check_short_not_suspended(order)

    def check_trading_lists(self, order: BaseOrder) -> bool:
        """
        - Description:
            依交易所每日名單檢核**放空開倉**能不能成交

            依放空管道分流：
            - **融券**：須在平盤下名單內（名單＝可融資融券的證券）且未暫停融券
            - **借券**：未暫停借券；不要求在名單內
            - 以上兩者若委託價**低於平盤**（前一交易日收盤，除權息日為開盤競價基準），
              還須在名單內且未被註記「前一交易日跌停、本日禁止平盤下」
            - **現股當沖（先賣後買）**：須在當沖名單內且未暫停先賣後買；它是現股賣出，
              **不受平盤下限制**

            比較的是委託價（滑價前）：平盤下限制管的是委託價格，平盤本身可以賣。
            沒有平盤基準（回測第一根 bar）時無從判斷平盤下，只檢查名單與暫停註記。

            **開啟檢核卻沒有當日名單時拋錯**：資料源在回測開始前已確認區間每天都有名單，
            走到這裡沒有名單代表那道檢查被繞過了，照常放行會讓結果看似有檢查。
        - Parameters:
            - order: BaseOrder
                待檢核的訂單
        - Return:
            - bool
                False 時呼叫端應拒單
        - Raise:
            - RuntimeError
                開啟檢核但當日沒有名單
        """

        is_short_open: bool = (
            order.action == Action.SELL and order.position_type == PositionType.SHORT
        )
        if not is_short_open:
            return True

        short_method: Optional[ShortMethod] = getattr(order, "short_method", None)

        if short_method == ShortMethod.DAY_TRADE:
            if not self.check_day_trade_list:
                return True
            day_trade_list: DayTradeListSnapshot = self.require_list(
                self.day_trade_list, "現股當沖標的名單"
            )
            if day_trade_list.allows_sell_first(order.symbol):
                return True
            logger.warning(
                f"[Fill] {order.symbol} 當日不在現股當沖名單或暫停先賣後買，拒單"
            )
            self.event_counts["rejected_not_day_tradable"] += 1
            return False

        if not self.check_short_sale_list:
            return True
        short_sale_list: ShortSaleListSnapshot = self.require_list(
            self.short_sale_list, "平盤下得融（借）券賣出名單"
        )

        if short_method == ShortMethod.MARGIN:
            allowed: bool = short_sale_list.allows_margin_short(order.symbol)
        else:
            allowed = short_sale_list.allows_sbl_short(order.symbol)
        if not allowed:
            logger.warning(
                f"[Fill] {order.symbol} 當日不得{self.short_method_label(short_method)}"
                f"（名單外或被註記暫停），拒單"
            )
            self.event_counts["rejected_short_halted"] += 1
            return False

        reference: Optional[float] = self.reference_prices.get(order.symbol)
        if (
            reference
            and order.price < reference
            and not short_sale_list.allows_below_reference(order.symbol)
        ):
            logger.warning(
                f"[Fill] {order.symbol} 委託價 {order.price} 低於平盤 {reference}，"
                f"當日不得平盤下{self.short_method_label(short_method)}，拒單"
            )
            self.event_counts["rejected_below_reference"] += 1
            return False

        return True

    @staticmethod
    def require_list(snapshot: Optional[Any], label: str) -> Any:
        """開啟檢核時取當日名單；沒有就拋錯（理由見 `check_trading_lists()`）"""

        if snapshot is None:
            raise RuntimeError(
                f"已開啟{label}檢核，但當日沒有名單；資料源的涵蓋檢查應該在回測開始前就擋下"
            )
        return snapshot

    @staticmethod
    def short_method_label(short_method: Optional[ShortMethod]) -> str:
        """log 用的放空管道名稱"""

        return "融券賣出" if short_method == ShortMethod.MARGIN else "借券賣出"

    def check_short_not_suspended(self, order: BaseOrder) -> bool:
        """
        - Description:
            停券期間拒絕**融券**放空開倉

            停券期間是「融券最後回補日 ~ 除權息交易日」這一段，制度上不得新增
            融券賣出。**SBL 借券不受停券限制**，其跨除息的成本由股利補償反映，
            故不擋；現股當沖沖賣不經過券源，同樣不擋。
        - Parameters:
            - order: BaseOrder
                待檢核的訂單
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        if not self.short_suspended_symbols:
            return True

        is_short_open: bool = (
            order.action == Action.SELL and order.position_type == PositionType.SHORT
        )
        if not is_short_open:
            return True

        if getattr(order, "short_method", None) != ShortMethod.MARGIN:
            return True

        if order.symbol not in self.short_suspended_symbols:
            return True

        logger.warning(
            f"[Fill] {order.symbol} 今日處於停券期間（融券最後回補日至除權息交易日），"
            f"不得新增融券賣出，拒單"
        )
        self.event_counts["rejected_short_suspended"] += 1
        return False

    def check_short_borrowable(self, order: BaseOrder) -> bool:
        """
        - Description:
            券源檢核：融券餘額不足時拒絕放空開倉

            只檢查**放空開倉**（賣出且方向為 SHORT）。放空回補是買進、
            做多賣出是 `PositionType.LONG`，兩者都不需要券源。

            **現股當沖沖賣（`ShortMethod.DAY_TRADE`）一律放行**：先賣後買、
            當日沖銷，根本不經過券源。判準必須看 `short_method` 而非 `is_day_trade`
            ——融券當沖（融券賣出後當日買回）的 `is_day_trade` 同樣是 True，
            但它確實借了券，仍須檢核餘額。

            **查無資料時放行**：`margin` 表可能尚未回補歷史，
            此時「查不到」不等於「借不到」。但若使用者明確開啟了檢核卻整場都查無資料，
            等於開關沒有實際作用，故以 warning 提示。
        - Parameters:
            - order: BaseOrder
                待檢核的訂單
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        if not self.check_borrowable:
            return True

        is_short_open: bool = (
            order.action == Action.SELL and order.position_type == PositionType.SHORT
        )
        if not is_short_open:
            return True

        # 放空管道由 enrich_orders() 在 fill 之前補值，此處讀得到
        short_method: Optional[ShortMethod] = getattr(order, "short_method", None)
        if short_method == ShortMethod.DAY_TRADE:
            return True

        balance: Optional[int] = self.short_balance.get(order.symbol)

        if balance is None:
            logger.warning(
                f"[Fill] {order.symbol} 查無融券餘額資料，本次跳過券源檢核。"
                f"若整場回測皆如此，代表 margin 資料未涵蓋該區間，check_borrowable 形同未啟用"
            )
            return True

        if balance < order.volume:
            logger.warning(
                f"[Fill] {order.symbol} 融券餘額 {balance} 張 < 委託 {order.volume} 張，"
                f"券源不足，拒單"
            )
            self.event_counts["rejected_no_borrow"] += 1
            return False

        return True

    def fill_tick(
        self, order: BaseOrder, price: float, quote: BaseQuote, auction: bool
    ) -> Optional[BaseOrder]:
        """
        - Description:
            台股的逐筆成交：交易所名單 → 券源 → 停券 → 滑價 → 成交量上限，與 `fill()` 同一套檢查

            - **成交價是這一筆決定的價格**（對手價、限價或集合競價價），不是策略的委託價；
              策略的委託價留在副本的 `reference_price`，滑價統計因此量得到「決策價到成交價」的差。
            - **不再額外加滑價**：bid/ask 已經含價差；策略明確設定 `slippage_bps_*` 時才疊加。
            - **不做 `validate()` 的區間檢查與夾價**：成交價取自實際的報價，市價買在委賣價本來就可能
              高於目前為止的最高成交價，用成交區間去擋會把正常的成交拒掉或夾成沒人賣的價格。
            - **成交量上限的分母**：連續競價是這一筆的對手方委託量（買看 `ask_volume`、賣看 `bid_volume`），
              集合競價是這一筆的成交量（撮合後對手方掛單常是 0，不代表沒成交）。截斷的部分直接放棄，不留在途。
        - Parameters:
            - order: BaseOrder
                在途委託
            - price: float
                這一筆決定的成交價（滑價前）
            - quote: BaseQuote
                同一檔的這一筆 tick
            - auction: bool
                這一筆是否為集合競價的成交
        - Return:
            - Optional[BaseOrder]
                成交的訂單副本；不可成交時為 None
        """

        priced: BaseOrder = copy.copy(order)
        priced.price = price

        # 平盤下的限制看**實際成交價**：市價放空以委買價成交，決策價在平盤上、成交價卻可能在平盤下
        if not self.check_order_eligibility(priced):
            return None

        priced.price = self.get_filled_price(priced)
        priced.reference_price = order.price

        volume: Optional[int] = self.get_tick_filled_volume(order, quote, auction)
        if volume is None:
            return None
        priced.volume = volume
        return priced

    def get_tick_filled_volume(
        self, order: BaseOrder, quote: BaseQuote, auction: bool
    ) -> Optional[int]:
        """
        逐筆成交量上限：分母見 `fill_tick()`；未設定 `max_volume_share` 時不限制。
        截斷與拒單的政策和日 K 相同（`VolumeCapPolicy`）
        """

        share: Optional[float] = self.config.max_volume_share
        if not share:
            return order.volume

        tick: Any = quote.tick_quote
        available: int
        if auction:
            available = tick.volume
        elif order.action == Action.BUY:
            available = tick.ask_volume
        else:
            available = tick.bid_volume

        cap: int = int(available * share)
        if order.volume <= cap:
            return order.volume

        if self.config.volume_cap_policy == VolumeCapPolicy.REJECT or cap <= 0:
            logger.warning(
                f"[Fill] {order.symbol} 委託 {order.volume} 張 > 這一筆可成交量上限 "
                f"{cap} 張（{share:.1%} × {available}），拒單"
            )
            self.event_counts["rejected_volume_cap"] += 1
            return None

        logger.warning(
            f"[Fill] {order.symbol} 委託 {order.volume} 張縮量至 {cap} 張"
            f"（這一筆可成交量 {available} 張的 {share:.1%}）"
        )
        self.event_counts["truncated_by_volume"] += 1
        return cap

    def get_filled_price(self, order: BaseOrder) -> float:
        """依訂單方向套用滑價；係數為 0 時原價回傳"""

        bps: float = (
            self.config.slippage_bps_buy
            if order.action == Action.BUY
            else self.config.slippage_bps_sell
        )
        return self.instrument.apply_slippage(
            order.price, order.action, bps, order.symbol
        )

    def apply_price_limit_basis(self, basis: Dict[str, float]) -> None:
        """
        - Description:
            以交易所公告的開盤競價基準覆寫當日的漲跌停基準

            除權息日的漲跌停不是以前一交易日收盤計算，而是以除權息參考價換算的
            **開盤競價基準**。沿用前收會讓整段區間偏移——除息日前收偏高，
            上下界一起偏高，`validate()` 的第二道檢查因此失準。

            **只覆寫有公告的標的**，其餘維持 `update_reference_prices()` 記錄的前收盤價。
        - Parameters:
            - basis: Dict[str, float]
                `{stock_id: 開盤競價基準}`，由 DataFeed 依當日除權息公告提供
        """

        for symbol, price in basis.items():
            if price:
                self.reference_prices[symbol] = price


class TwFuturesFillModel(BaseFillModel):
    """
    台期貨成交價模型

    與 `TwStockFillModel` 的三個差異（流程本身在基底，這裡只是鉤子沒有內容）：

    1. **不做漲跌停檢查**：期貨沒有可事先算出的漲跌停區間（見 `TwFuturesSpec`），
       `validate_market_rules()` 沿用基底的一律通過，`validate()` 只剩無成交擋板與
       「成交價須落在當根 bar 的區間內」這道前視偏誤擋板。
    2. **不做名單與券源檢核**：期貨賣出開倉就是放空，不需要借券也沒有融券餘額的概念，
       `check_order_eligibility()` 同樣沿用基底。
    3. **成交量的單位是口**，不是張；`FillConfig.max_volume_share` 的語意不變。
       滑價以跳動點表達（`FuturesFillConfig`），`get_filled_price()` 是本類唯一的實作。

    ⚠️ **同一契約的日盤與夜盤 `symbol` 相同**（`{product}{expiry}`）。本 model 的
    `reference_prices` 與 `intraday_range` 以 symbol 為鍵，兩個時段混在同一根 bar 傳進來
    會互相覆蓋。DataFeed 一律只取策略宣告的那一個時段，見 `TwFuturesDataFeed`。
    """

    # 期貨論口，股票論張；只影響 log 訊息
    VOLUME_UNIT: str = "口"

    # 自備計數器時用期貨清單，事件報表才不會列出二十個永遠是 0 的台股列
    EVENT_KEYS: Tuple[str, ...] = FUTURES_EVENT_KEYS

    # 期貨的滑價以跳動點表達
    CONFIG_CLS: type = FuturesFillConfig

    def __init__(
        self,
        instrument: Optional[InstrumentSpec] = None,
        event_counts: Optional[Dict[str, int]] = None,
        config: Optional[FuturesFillConfig] = None,
    ) -> None:
        super().__init__(instrument or TwFuturesSpec(), event_counts, config)

    def get_filled_price(self, order: BaseOrder) -> float:
        """
        - Description:
            依訂單方向套用滑價，**跳動點優先於基點**

            兩者都設時以跳動點為準：期貨的價差本來就以檔數報價，
            基點只是為了與基底介面相容而保留（見 `FuturesFillConfig`）。
            方向一律往對下單者不利的一側，這點與台股相同。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單
        - Return:
            - float
                含滑價的成交價
        """

        product: Optional[str] = self.instrument.resolve_product(order)

        ticks: float = self.get_slippage_ticks(order)
        if ticks:
            return self.apply_tick_slippage(order.price, order.action, ticks, product)

        bps: float = (
            self.config.slippage_bps_buy
            if order.action == Action.BUY
            else self.config.slippage_bps_sell
        )
        return self.instrument.apply_slippage(order.price, order.action, bps, product)

    def get_slippage_ticks(self, order: BaseOrder) -> float:
        """取得該筆訂單的滑價跳動點數；設定不是 `FuturesFillConfig` 時為 0"""

        if not isinstance(self.config, FuturesFillConfig):
            return 0.0

        return self.config.get_slippage_ticks(
            order.action, self.instrument.resolve_product(order)
        )

    def apply_tick_slippage(
        self,
        price: float,
        action: Action,
        ticks: float,
        product: Optional[str] = None,
    ) -> float:
        """
        - Description:
            以跳動點數套用滑價：買進往上加、賣出往下減

            **方向寫死不由呼叫端決定符號**，理由同 `InstrumentSpec.apply_slippage()`
            ——滑價的意義是「你拿不到理想價」，允許負值等於允許「滑價讓績效變好」。

            **跳動點逐商品查表**（`TwFuturesSpec.get_tick_size()`）：同一個
            `slippage_ticks=1` 在 TX 是 1 點、在 TE 是 0.05 點。算出的價差與
            事後的檔位對齊必須用同一個跳動點，否則滑價算對了、對齊又把它推回去。
        - Parameters:
            - price: float
                參考價
            - action: Action
                訂單動作
            - ticks: float
                滑價跳動點數
            - product: Optional[str]
                商品代碼；`None` 時退回 `TwFuturesSpec.DEFAULT_TICK_SIZE`
        - Return:
            - float
                含滑價且已對齊跳動點的成交價
        """

        tick_size: float = self.get_tick_size(product)
        offset: float = ticks * tick_size

        if action == Action.BUY:
            return self.instrument.round_to_tick(price + offset, "up", product)
        return self.instrument.round_to_tick(price - offset, "down", product)

    def get_tick_size(self, product: Optional[str] = None) -> float:
        """取得該商品的跳動點；spec 沒有查表能力時退回預設值（非期貨 spec 的保險）"""

        getter = getattr(self.instrument, "get_tick_size", None)
        if getter is None:
            return TwFuturesSpec.DEFAULT_TICK_SIZE

        return getter(product)
