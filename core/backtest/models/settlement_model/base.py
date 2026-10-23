import copy
import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

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
from core.position.base.position_manager import BasePositionManager
from core.utils import (
    PositionType,
    TimeUtils,
)

"""BaseSettlementModel: 一根 bar 收盤後由市場規則強制執行的動作（各市場共用契約；無報價計數與持有天數在此）"""


class BaseSettlementModel(ABC):
    """
    結算模型：一根 bar 收盤後，市場規則強制對部位做的事

    台股的「當沖日終強制回補」與期貨的「每日結算」是同一個掛點的兩種實作，
    因此兩個市場共用一個引擎。對應 Lean 的 SettlementModel / MarginCallModel /
    MarginInterestRateModel。
    """

    def __init__(
        self,
        position_manager: BasePositionManager,
        fill_model: Optional[BaseFillModel] = None,
        max_no_quote_days: Optional[int] = None,
    ) -> None:
        # 強制出場要經部位管理層入帳；各市場的子類另以精確型別再標一次
        self.position_manager: BasePositionManager = position_manager

        # 引擎強制出場時同樣要走成交假設（滑價），否則同一支策略會有兩種口徑：
        # 策略自己送的平倉單吃滑價、引擎的強制出場與換月轉倉不吃
        self.fill_model: Optional[BaseFillModel] = fill_model

        # 連續幾根 bar 沒有報價就強制出場；None 表示不做這件事。
        # 門檻是市場規則加上策略的選擇，由 factory 從策略帶入
        self.max_no_quote_days: Optional[int] = max_no_quote_days

        # `{代號: 連續無報價的 bar 數}`。**記在 model 而不是部位上**：無報價是標的
        # （停牌、下市、契約到期）的狀態，不是部位的狀態；同一標的的多筆部位共用
        # 同一個答案，記在部位上只會存好幾份一樣的數
        self.no_quote_days: Dict[str, int] = {}

    def apply_fill_price(
        self, order: BaseOrder, quote: Optional[BaseQuote]
    ) -> BaseOrder:
        """
        - Description:
            對引擎自己送出的強制出場單套用滑價，並檢查是否落在當根 bar 的區間內

            **台股與期貨共用這一份**：兩邊各寫一次必然漂移，而「強制出場要不要
            吃滑價」在兩個市場是同一個問題的同一個答案。

            **不走 `fill()` 而只取成交價**：`fill()` 會做券源檢核與成交量上限，
            那兩項會拒單或縮量，而強制出場是市場規則強加的——拒掉它等於讓部位
            違規留倉。這裡只補上「拿不到理想價」這一項。

            **超出當日區間只警告不夾回**，與策略平倉腿同一個口徑
            （計入 `close_price_out_of_range`）：強制出場的時點是市場規則決定的，
            夾回等於換一個價格假設，而且一律把成交價推向對持有者有利的一側，
            正好抵銷滑價的保守意義。

            未注入 `fill_model`（純記憶體測試）或未設定滑價時原樣回傳。
        - Parameters:
            - order: BaseOrder
                引擎產生的強制出場單
            - quote: Optional[BaseQuote]
                當根 bar 的報價；`None` 時跳過區間檢查——期貨「到期無報價」
                那條路徑本來就沒有報價，那不是遺漏
        - Return:
            - BaseOrder
                含滑價的訂單；未調整時為原物件
        """

        if self.fill_model is None:
            return order

        filled_price: float = self.fill_model.get_filled_price(order)

        filled_order: BaseOrder = order
        if filled_price != order.price:
            filled_order = copy.copy(order)
            filled_order.price = filled_price
            # 強制出場的滑價同樣要算進成本統計（理由見 `fill()`）
            filled_order.reference_price = order.price

        if quote is not None:
            self.fill_model.warn_close_price_out_of_range(filled_order, quote)

        return filled_order

    def force_close_position(
        self,
        position: BasePosition,
        date: datetime.date,
        price: float,
        quote: Optional[BaseQuote],
    ) -> List[BaseTradeRecord]:
        """
        - Description:
            引擎以指定價格強制平掉一筆部位（多單賣出、空單買進回補）

            **追繳、連續無報價、到期兜底、停券、當沖日終、轉倉的平倉腿全部共用這一條**：
            原本台股空單、台股多單、期貨各有一支，滑價口徑與入帳方式三處各走各的。
            要拆開不同口徑得先在此分流，而不是各自繞過 `apply_fill_price()`。

            **平的就是傳入的這一筆**（`close_specific_position()`），不走同標的 FIFO：
            停券回補只對 `MARGIN` 的融券生效、持有天數逐筆起算，同標的先借券（SBL）再
            融券時 FIFO 會平到不該平的 SBL 那一筆，而且不會有任何錯誤訊息。無報價、到期
            這類標的層級的判準逐筆呼叫，平掉的集合與 FIFO 相同。
        - Parameters:
            - position: BasePosition
                要平掉的部位
            - date: datetime.date
                成交日
            - price: float
                平倉參考價（滑價前）
            - quote: Optional[BaseQuote]
                當根 bar 的報價，供區間檢查；**不給預設值**是刻意的：無報價時要
                明確傳 `None`，新增的呼叫端漏傳才不會靜默跳過檢查
        - Return:
            - List[BaseTradeRecord]
                平倉產生的交易紀錄
        """

        order: BaseOrder = self.build_exit_order(position, date, price)
        return self.position_manager.close_specific_position(
            position, self.apply_fill_price(order, quote)
        )

    @abstractmethod
    def build_exit_order(
        self, position: BasePosition, date: datetime.date, price: float
    ) -> BaseOrder:
        """
        - Description:
            組出平掉該部位的訂單：方向由部位反推，其餘欄位是各市場的本質

            台股要帶 `short_method`／`is_day_trade`（稅率與費用看它們），
            期貨要帶 `product`／`expiry`。
        - Parameters:
            - position: BasePosition
                要平掉的部位
            - date: datetime.date
                成交日
            - price: float
                平倉參考價（滑價前）
        - Return:
            - BaseOrder
                全量平倉的訂單
        """

        pass

    @abstractmethod
    def settle_day(
        self,
        date: datetime.date,
        quotes: List[BaseQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            日終結算：一根 bar 收盤後由市場規則強制執行的動作
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[BaseQuote]
                當根 bar 的報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數（key 需與報表相容）
        """

        pass

    def has_quote(self, quote: BaseQuote) -> bool:
        """
        這筆報價算不算「有報價」：預設看有沒有成交價

        期貨覆寫成「有沒有盯市價」——遠月契約整天零成交是常態，但日盤仍有結算價，
        契約還掛在交易所上，不是到期。
        """

        return bool(quote.close or quote.cur_price)

    def update_no_quote_days(
        self,
        quote_map: Dict[str, BaseQuote],
        positions: List[BasePosition],
    ) -> None:
        """
        - Description:
            更新持倉標的的連續無報價 bar 數：有報價即歸零，無報價則累加

            長期停牌、已下市的股票與到期的期貨契約都不會再有報價，計數會一路累加，
            成為強制出場的依據（`has_exceeded_no_quote_limit()`）。
        - Parameters:
            - quote_map: Dict[str, BaseQuote]
                當根 bar 的報價（以代號為鍵）
            - positions: List[BasePosition]
                目前的未平倉部位
        """

        # **一個標的一天只加一次**：同一標的的多筆部位（加碼）共用同一個計數，
        # 逐部位累加會讓它以部位數的倍數衝到門檻
        for symbol in dict.fromkeys(position.symbol for position in positions):
            quote: Optional[BaseQuote] = quote_map.get(symbol)
            if quote is not None and self.has_quote(quote):
                self.no_quote_days[symbol] = 0
            else:
                self.no_quote_days[symbol] = self.no_quote_days.get(symbol, 0) + 1

    def has_exceeded_no_quote_limit(self, symbol: str) -> bool:
        """該標的連續無報價是否已達門檻；未設門檻一律 False"""

        if self.max_no_quote_days is None:
            return False

        return self.no_quote_days.get(symbol, 0) >= self.max_no_quote_days

    def update_holding_days(
        self, date: datetime.date, positions: List[BasePosition]
    ) -> None:
        """
        - Description:
            更新每個部位的已持有曆日數（＝當前日期 − 開倉日），兩個市場、多空都更新

            **是曆日不是 bar 數**：週五開的部位到週一只過了 1 根 bar，卻是 3 個曆日；
            逐根 bar `+= 1` 一年只計到 252 天。`TradeRecord.holding_days` 指的也是曆日，
            兩處口徑必須一致。
        - Parameters:
            - date: datetime.date
                當前交易日
            - positions: List[BasePosition]
                目前的未平倉部位
        """

        today: Optional[datetime.date] = TimeUtils.to_date(date)
        for position in positions:
            open_date: Optional[datetime.date] = TimeUtils.to_date(position.date)
            if open_date is None or today is None:
                logger.warning(
                    f"[Holding Days] {position.symbol} 的開倉日無法解析，本日不更新"
                )
                continue

            position.holding_days = max((today - open_date).days, 0)

    def prepare_day(
        self, feed: BaseDataFeed, date: datetime.date, allow_short: bool
    ) -> None:
        """
        - Description:
            每日開盤前，自己向資料源取當天需要的市場狀態；沒有這類制度的市場沿用預設 no-op

            **由 model 自己準備，而不是引擎逐一推入**：哪些狀態要取是市場規則（台股有停券、除權息，
            期貨沒有），引擎逐一推的話，基底就得替每個市場的狀態各掛一個空的鉤子。
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
            - date: datetime.date
                交易日
            - allow_short: bool
                策略是否可能放空；不可能時不必取放空才用得到的狀態（推導要掃整段交易日曆）
        """

        pass

    @abstractmethod
    def get_mark_price(
        self, position: BasePosition, quote_map: Dict[str, BaseQuote]
    ) -> float:
        """
        - Description:
            取得部位的盯市價格

            屬結算職責而非成交價職責：期貨的盯市價就是每日結算價。
            引擎的 snapshot_daily_equity() 也用這個價算未實現損益，故列入介面。
        - Parameters:
            - position: BasePosition
                待盯市的部位
            - quote_map: Dict[str, BaseQuote]
                當根 bar 的報價（以 symbol 為鍵）
        - Return:
            - float
                盯市價格
        """

        pass

    def mark_position(
        self, position: BasePosition, mark_price: float, units: int
    ) -> float:
        """
        - Description:
            以盯市價更新部位的未實現損益，並回傳它對當日權益的貢獻

            **預設為現金帳戶口徑**：買進即把現金換成標的，故做多部位的價值就是
            市值；放空開倉時只扣了保證金與成本、賣出價款留作擔保品，故其價值是
            保證金加未實現損益。

            **開成掛點是因為這一段是「資金佔用方式」而非「權益怎麼記」**：
            期貨是保證金交易，契約價值本身不佔用資金，做多部位的價值同樣只有
            保證金加未結算損益，沿用現金帳戶口徑會把整個契約價值算進權益
            （TX 一口契約價值 900 萬、保證金只有 70 萬），見
            `TwFuturesSettlementModel.mark_position()`。
        - Parameters:
            - position: BasePosition
                未平倉部位；`unrealized_pnl` 與 `unrealized_roi` 會被就地更新
            - mark_price: float
                盯市價（由 `get_mark_price()` 取得）
            - units: int
                計價單位數量（數量 × `InstrumentSpec.get_unit_size()`）
        - Return:
            - float
                該部位計入當日權益的金額
        """

        position_value: float

        if position.position_type == PositionType.SHORT:
            # 開倉時只扣了保證金與成本，賣出價款留作擔保品。
            # **已計提的持有成本要扣掉**：它逐日累加在部位上、平倉時才結算，
            # 不扣的話持有期間的權益偏高、回補日一次掉下來，MDD 與日報酬都失真
            # （股利補償走的是即時扣款，除息當日就反映在餘額裡，不必在此重複扣）
            position.unrealized_pnl = round((position.price - mark_price) * units, 2)
            position_value = (
                position.margin
                + position.unrealized_pnl
                - self.get_accrued_holding_cost(position)
            )
        else:
            position.unrealized_pnl = round((mark_price - position.price) * units, 2)
            position_value = mark_price * units

        cost_basis: float = position.price * units
        position.unrealized_roi = (
            round(position.unrealized_pnl / cost_basis * 100, 2) if cost_basis else 0.0
        )

        return position_value

    def get_accrued_holding_cost(self, position: BasePosition) -> float:
        """
        已逐日計提、平倉時才結算的持有成本（會從放空部位的價值扣掉）

        預設 0：期貨沒有這種成本。台股覆寫成 SBL 借券費——那是台股的制度，
        基底不該知道那個欄位叫什麼。
        """

        return 0.0
