import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from core.backtest.models.event_counts import new_event_counts
from core.backtest.models.instrument_spec import (
    InstrumentSpec,
    TwFuturesSpec,
    TwStockSpec,
)
from core.models import BaseOrder, BaseQuote
from core.utils import Action, PositionType, Scale, ShortMethod, TimeUtils

"""
FillModel: 這張單成不成交、以什麼價量成交

包含四件事，全部屬「市場執行假設」而非引擎邏輯：
1. **成交價可信度**（`validate()`）：前視偏誤與不可能成交的擋板
2. **滑價**（`FillConfig.slippage_bps_*`）：拿不到理想價
3. **成交量上限**（`FillConfig.max_volume_share`）：一張單不可能吃掉當日大半成交量
4. **券源檢核**（`ShortConstraint.check_borrowable`）：借不到券就放空不了

2~4 三項的預設值皆為「關閉」，此時不改動任何訂單的價量。
"""


class VolumeCapPolicy(str, Enum):
    """超過成交量上限時的處理方式"""

    TRUNCATE = "TRUNCATE"  # 縮量到上限（預設，較貼近實務）
    REJECT = "REJECT"  # 整張拒單


@dataclass
class FillConfig:
    """
    成交假設設定

    **刻意與 `FillModel` 放在同一個檔案**：回測假設一律與其 model 同檔
    （對照 `CostConfig` 之於 `CostModel`），語意上則與「法規費率」
    （`Commission`）分離——前者是可調的模擬參數，後者是外部給定的規則。

    全部預設為關閉，此時不改動任何訂單的價量。
    """

    # 滑價基點（1 bps = 0.01%）；買進加價、賣出減價
    slippage_bps_buy: float = 0.0
    slippage_bps_sell: float = 0.0

    # 單筆訂單張數不得超過當日成交量的比例；None 為關閉
    max_volume_share: Optional[float] = None

    # 超量時縮量或拒單
    volume_cap_policy: VolumeCapPolicy = VolumeCapPolicy.TRUNCATE


@dataclass
class FuturesFillConfig(FillConfig):
    """
    期貨的成交假設：**滑價改以跳動點（tick）表達**

    為什麼不沿用基點：期貨的價差報價本來就是「幾檔」，而同一個基點數在不同價位
    換算出的檔數不同——TX 在 12,000 點時 1 bps 是 1.2 點、在 24,000 點時是 2.4 點，
    同一組設定跨年份回測會靜默變成不同的滑價假設。

    **大台與小台要分開設**：MTX 的價差與成交量都與 TX 不同，
    用同一個數字會低估小台的成本，故提供 `slippage_ticks_by_product`。

    `slippage_ticks_*` 為 0 且未逐商品指定時，退回基底的基點設定（同樣預設關閉），
    行為與未啟用任何假設時完全相同。
    """

    slippage_ticks_buy: float = 0.0  # 買進滑價（跳動點數）
    slippage_ticks_sell: float = 0.0  # 賣出滑價（跳動點數）
    # 逐商品的滑價跳動點數；未列的商品沿用上面兩個共用值
    slippage_ticks_by_product: Optional[Dict[str, float]] = None

    def get_slippage_ticks(
        self, action: Action, product: Optional[str] = None
    ) -> float:
        """取得該商品該方向的滑價跳動點數；逐商品設定優先"""

        if product and self.slippage_ticks_by_product:
            ticks: Optional[float] = self.slippage_ticks_by_product.get(product)
            if ticks is not None:
                return ticks

        return (
            self.slippage_ticks_buy
            if action == Action.BUY
            else self.slippage_ticks_sell
        )


class BaseFillModel(ABC):
    """
    成交價模型：判斷一張訂單在該根 bar 是否可能以指定價格成交

    成交價可信度是市場規則而非引擎邏輯，故與 InstrumentSpec 一樣下沉為可插拔 model。
    對應 Lean 的 FillModel。

    **子類別必須在 `__init__` 備妥三個屬性**，基底的夾價、區間警告與逐 bar 掛點直接使用：
    - `event_counts: Dict[str, int]`：與引擎共用同一個 dict，計數才會進報表
    - `intraday_range: Dict[str, Tuple[float, float]]`：Tick 級別的當日累計高低點
    - `prev_close: Dict[str, float]`：次一根 bar 的漲跌停基準
    """

    # 數量單位：股票論張、期貨論口。只影響 log 訊息，成交量上限的政策兩邊相同
    VOLUME_UNIT: str = "張"

    @abstractmethod
    def validate(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """
        - Description:
            成交價合理性檢查
        - Parameters:
            - order: BaseOrder
                待驗證的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - bool
                False 時呼叫端應拒單
        """

        pass

    def on_bar_open(self, quotes: List[BaseQuote]) -> None:
        """
        一根 bar 開始：重置並累計盤中已發生的高低點

        **TICK 回測在這裡有前視**：引擎把整天的 tick 當成同一根 bar 一次傳進來，
        累計出來的是全日高低點，而不是「下單當下以前」的高低點——盤中較早的委託
        會通過稍後才出現的價位。目前沒有 TICK 策略；要做逐筆回測前，得先改成
        逐筆餵入並逐筆更新區間。
        """

        self.intraday_range = {}
        self.update_intraday_range(quotes)

    def update_intraday_range(self, quotes: List[BaseQuote]) -> None:
        """
        更新 Tick 級別的當日累計高低點

        只納入傳進來的報價；防不防前視取決於呼叫端怎麼餵。目前 `on_bar_open()`
        一次餵整天，得到的是全日區間（限制見上方的 `on_bar_open()`）。
        """

        for quote in quotes:
            price: float = quote.cur_price or quote.close
            if not price:
                continue

            low, high = self.intraday_range.get(quote.symbol, (price, price))
            self.intraday_range[quote.symbol] = (min(low, price), max(high, price))

    def on_bar_close(self, quotes: List[BaseQuote]) -> None:
        """
        一根 bar 收盤：記錄收盤價，作為次一根 bar 的漲跌停基準

        **記的是收盤價、不是結算價**：期貨的盯市價一律走 `SettlementModel`，
        這裡只負責次日的漲跌停基準。
        """

        for quote in quotes:
            close: float = quote.close or quote.cur_price
            if close:
                self.prev_close[quote.symbol] = close

    def get_filled_volume(self, order: BaseOrder, quote: BaseQuote) -> Optional[int]:
        """
        - Description:
            套用成交量上限：單筆訂單數量不得超過當日成交量的指定比例

            **`quote.volume` 的語意依級別不同**：DAY 為當日總量、TICK 為單筆成交量。
            TICK 級別下以單筆量當分母沒有意義，故本檢查只在 DAY 級別生效
            （TICK 的累計量檢查尚未實作）。

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

    def apply_price_limit_basis(self, basis: Dict[str, float]) -> None:
        """一根 bar 開始：以交易所公告的基準價覆寫漲跌停基準；預設不處理"""

        pass

    def apply_short_balance(self, balance: Dict[str, int]) -> None:
        """一根 bar 開始：更新當日可借券餘額；預設不處理"""

        pass

    def apply_short_suspended_symbols(self, symbols: Set[str]) -> None:
        """一根 bar 開始：更新今日處於停券期間的標的；預設不處理"""

        pass

    def fill(self, order: BaseOrder, quote: BaseQuote) -> Optional[BaseOrder]:
        """
        - Description:
            決定這張單成不成交、以什麼價量成交

            **與 `validate()` 的分工**：`validate()` 只回答「這個價格在當根 bar
            可不可能成交」，是既有的前視偏誤擋板；`fill()` 負責市場執行假設
            （滑價、成交量上限、券源），並在需要調整時回傳**訂單副本**。

            **絕不就地修改傳入的 order**：策略可能持有同一個物件，
            就地改動會讓策略下一根 bar 看到被引擎改過的價量。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - Optional[BaseOrder]
                可成交的訂單（未調整時為原物件本身）；不可成交時為 None
        """

        return order

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

    def get_price_range(
        self, quote: BaseQuote
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        取得該報價可成交的價格區間：日 K 用 OHLC，Tick 用當日累計高低點

        Tick 的區間目前是全日高低點而非下單當下以前的，有前視（見 `on_bar_open()`）。

        **放在基底而不是各市場一份**：台股與期貨的答案一模一樣，各寫一次必然漂移；
        夾價與平倉腿的區間警告都以此為唯一判準。
        """

        if quote.scale == Scale.TICK:
            return self.intraday_range.get(quote.symbol, (None, None))

        if quote.high and quote.low:
            return (quote.low, quote.high)

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
    ) -> None:
        self.instrument: InstrumentSpec = instrument or TwStockSpec()

        # 成交假設（滑價、成交量上限）；預設全關
        self.config: FillConfig = config or FillConfig()

        # 是否檢核券源；由 factory 依 ShortConstraint.check_borrowable 帶入
        self.check_borrowable: bool = check_borrowable

        # 當日可借券餘額（張）：{stock_id: 融券今日餘額}，由 DataFeed 於每根 bar 開始時提供
        self.short_balance: Dict[str, int] = {}

        # 與引擎共用同一個 dict，拒單計數才會反映到報表（傳 None 時自行持有，供單獨測試）。
        # **自備的那份要有全部 key**（`new_event_counts()`）：只塞用得到的幾個，
        # 單獨建模型時走到成交量上限、券源或停券就會 `KeyError`
        self.event_counts: Dict[str, int] = (
            event_counts if event_counts is not None else new_event_counts()
        )

        # Tick 級別的當日累計高低點（TickQuote 沒有 OHLC，成交價驗證需自行維護）
        self.intraday_range: Dict[str, Tuple[float, float]] = {}

        # 前一交易日收盤價，作為漲跌停判定基準
        self.prev_close: Dict[str, float] = {}

        # 今日停券的標的，由引擎每根 bar 從 DataFeed 推入
        self.short_suspended_symbols: Set[str] = set()

    def validate(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """成交價合理性檢查（前視偏誤與不可能成交的擋板）"""

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

        prev_close: Optional[float] = self.prev_close.get(order.symbol)
        if prev_close:
            # 帶入報價日期：2015-06-01 前的漲跌停幅度為 7%，非現行的 10%
            limit_down, limit_up = self.instrument.get_price_limits(
                prev_close, TimeUtils.to_date(quote.date)
            )
            if not (limit_down <= order.price <= limit_up):
                logger.warning(
                    f"[Validate Fill] {order.symbol} 成交價 {order.price} 超出漲跌停 "
                    f"[{limit_down}, {limit_up}]，拒單"
                )
                self.event_counts["rejected_fill_price"] += 1
                return False

        if self.is_locked_at_limit(order, quote, prev_close):
            return False

        if self.instrument.round_to_tick(order.price, "nearest") != order.price:
            logger.warning(
                f"[Validate Fill] {order.symbol} 成交價 {order.price} 未對齊檔位"
            )

        return True

    def is_locked_at_limit(
        self, order: BaseOrder, quote: BaseQuote, prev_close: Optional[float]
    ) -> bool:
        """
        - Description:
            全日鎖死漲停（買進）或跌停（賣出）時拒單，並計入事件

            **這條擋的是開倉**：`validate()` 只跑在開倉路徑上（拒掉平倉單會讓部位
            被迫留倉，那是更嚴重的失真）。開高低收都等於漲停價，代表整天沒有人
            願意在漲停以下賣出，實務上排隊也買不到——而 `MomentumStrategy1` 的
            訊號正是「當日漲幅 ≥ 9%、以收盤價買進」，這類標的大量是鎖漲停，
            照常成交會讓做多績效系統性偏樂觀，且沒有任何徵兆。
        - Parameters:
            - order: BaseOrder
                待驗證的開倉單
            - quote: BaseQuote
                當根 bar 的報價
            - prev_close: Optional[float]
                漲跌停基準價
        - Return:
            - bool
                True 表示被鎖死、應拒單
        """

        # Tick 級別沒有當日四價，無從判定鎖死
        if not all(hasattr(quote, field) for field in ("open", "high", "low", "close")):
            return False

        if not self.instrument.is_locked_at_limit(
            prev_close=prev_close,
            open_price=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.close,
            side=order.action,
            date=TimeUtils.to_date(quote.date),
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

    @staticmethod
    def has_tradable_quote(quote: BaseQuote) -> bool:
        """
        - Description:
            該報價當日是否真的有成交

            **無成交日在資料上是四價皆 0**（來源給 `--`）：`get_price_range()`
            對 0 回 `(None, None)` 而**跳過**區間檢查，不擋的話策略能在一個
            根本沒開盤或整天無量的標的上以任意價格成交。

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

    def fill(self, order: BaseOrder, quote: BaseQuote) -> Optional[BaseOrder]:
        """
        - Description:
            台股的成交假設：券源檢核 → 滑價 → 成交量上限

            三項預設皆為關閉，此時直接回傳**原物件**（不是副本），
            未啟用任何假設時不改動訂單的價量。
        - Parameters:
            - order: BaseOrder
                策略產生的訂單
            - quote: BaseQuote
                同一標的的當根 bar 報價
        - Return:
            - Optional[BaseOrder]
                可成交的訂單；不可成交時為 None
        """

        if not self.check_short_borrowable(order):
            return None

        if not self.check_short_not_suspended(order):
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

    def get_filled_price(self, order: BaseOrder) -> float:
        """依訂單方向套用滑價；係數為 0 時原價回傳"""

        bps: float = (
            self.config.slippage_bps_buy
            if order.action == Action.BUY
            else self.config.slippage_bps_sell
        )
        return self.instrument.apply_slippage(order.price, order.action, bps)

    def apply_price_limit_basis(self, basis: Dict[str, float]) -> None:
        """
        - Description:
            以交易所公告的開盤競價基準覆寫當日的漲跌停基準

            除權息日的漲跌停不是以前一交易日收盤計算，而是以除權息參考價換算的
            **開盤競價基準**。沿用前收會讓整段區間偏移——除息日前收偏高，
            上下界一起偏高，`validate()` 的第二道檢查因此失準。

            **只覆寫有公告的標的**，其餘維持 `on_bar_close()` 累積的前收盤價。
        - Parameters:
            - basis: Dict[str, float]
                `{stock_id: 開盤競價基準}`，由 DataFeed 依當日除權息公告提供
        """

        for symbol, price in basis.items():
            if price:
                self.prev_close[symbol] = price


class TwFuturesFillModel(BaseFillModel):
    """
    台期貨成交價模型

    與 `TwStockFillModel` 的三個差異：

    1. **不做漲跌停檢查**：期貨沒有可事先算出的漲跌停區間（見 `TwFuturesSpec`），
       故 `validate()` 只保留「成交價須落在當根 bar 的區間內」這道前視偏誤擋板。
    2. **不做券源檢核**：期貨賣出開倉就是放空，不需要借券也沒有融券餘額的概念。
    3. **成交量的單位是口**，不是張；`FillConfig.max_volume_share` 的語意不變。

    ⚠️ **同一契約的日盤與夜盤 `symbol` 相同**（`{product}{expiry}`）。本 model 的
    `prev_close` 與 `intraday_range` 以 symbol 為鍵，兩個時段混在同一根 bar 傳進來
    會互相覆蓋。DataFeed 一律只取策略宣告的那一個時段，見 `TwFuturesDataFeed`。
    """

    # 期貨論口，股票論張；只影響 log 訊息
    VOLUME_UNIT: str = "口"

    def __init__(
        self,
        instrument: Optional[InstrumentSpec] = None,
        event_counts: Optional[Dict[str, int]] = None,
        config: Optional[FuturesFillConfig] = None,
    ) -> None:
        self.instrument: InstrumentSpec = instrument or TwFuturesSpec()

        # 成交假設（滑價、成交量上限）；預設全關
        self.config: FillConfig = config or FuturesFillConfig()

        # 與引擎共用同一個 dict，拒單計數才會反映到報表（傳 None 時自行持有，供單獨測試）。
        # **自備的那份要有全部 key**（`new_event_counts()`）：只塞用得到的幾個，
        # 單獨建模型時走到成交量上限就會 `KeyError`
        self.event_counts: Dict[str, int] = (
            event_counts if event_counts is not None else new_event_counts()
        )

        # Tick 級別的當日累計高低點（期貨 Tick 回測尚未實作，目前不會被填入）
        self.intraday_range: Dict[str, Tuple[float, float]] = {}

        # 前一交易日收盤價；期貨沒有漲跌停檢查，此處僅供無報價時盯市與外部查詢
        self.prev_close: Dict[str, float] = {}

    def validate(self, order: BaseOrder, quote: BaseQuote) -> bool:
        """
        成交價合理性檢查：**只檢查是否落在當根 bar 的高低點之間**

        期貨沒有漲跌停可查（見 `TwFuturesSpec.get_price_limits()`）；跳動點未對齊
        僅記錄警告不拒單，與台股一致——資料本身的價格精度問題不該擋掉正常回測。
        """

        low, high = self.get_price_range(quote)
        if low is not None and high is not None and not (low <= order.price <= high):
            logger.warning(
                f"[Validate Fill] {order.symbol} 成交價 {order.price} 超出當日區間 "
                f"[{low}, {high}]，拒單"
            )
            self.event_counts["rejected_fill_price"] += 1
            return False

        product: Optional[str] = getattr(order, "product", None)
        if (
            self.instrument.round_to_tick(order.price, "nearest", product)
            != order.price
        ):
            logger.warning(
                f"[Validate Fill] {order.symbol} 成交價 {order.price} 未對齊跳動點"
            )

        return True

    def fill(self, order: BaseOrder, quote: BaseQuote) -> Optional[BaseOrder]:
        """
        期貨的成交假設：滑價 → 成交量上限

        兩項預設皆為關閉，此時直接回傳**原物件**（不是副本），
        與台股同一種寫法：未啟用任何假設時不改動訂單的價量。
        """

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

        product: Optional[str] = getattr(order, "product", None)

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
            order.action, getattr(order, "product", None)
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
