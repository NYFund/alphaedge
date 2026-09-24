import copy
import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set

from core.backtest.models.fill_model import BaseFillModel
from core.models import (
    BaseAccount,
    BaseOrder,
    BasePosition,
    BaseQuote,
    StockPosition,
    StockQuote,
)
from core.utils import (
    PositionType,
)

"""BaseSettlementModel: 一根 bar 收盤後由市場規則強制執行的動作（各市場共用契約）"""


class BaseSettlementModel(ABC):
    """
    結算模型：一根 bar 收盤後，市場規則強制對部位做的事

    台股的「當沖日終強制回補」與期貨的「每日結算」是同一個掛點的兩種實作，
    因此兩個市場共用一個引擎。對應 Lean 的 SettlementModel / MarginCallModel /
    MarginInterestRateModel。
    """

    def __init__(self, fill_model: Optional[BaseFillModel] = None) -> None:
        # 引擎強制出場時同樣要走成交假設（滑價），否則同一支策略會有兩種口徑：
        # 策略自己送的平倉單吃滑價、引擎的強制出場與換月轉倉不吃
        self.fill_model: Optional[BaseFillModel] = fill_model

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

    @abstractmethod
    def on_bar_close(
        self,
        date: datetime.date,
        quotes: List[BaseQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            一根 bar 收盤後由市場規則強制執行的動作
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

    @abstractmethod
    def update_no_quote_days(
        self,
        quote_map: Dict[str, StockQuote],
        positions: List[StockPosition],
    ) -> None:
        """
        - Description:
            更新每個部位的連續無報價天數

            有報價即歸零，無報價則累加。長期停牌或已下市的標的會持續累加，
            成為強制出場的依據。
        - Parameters:
            - quote_map: Dict[str, StockQuote]
                當日報價對照表
            - positions: List[StockPosition]
                要更新的部位
        """

        pass

    def apply_force_cover_symbols(self, symbols: Set[str]) -> None:
        """
        - Description:
            更新今日觸發停券強制回補的標的（由 DataFeed 每根 bar 提供）

            與 `FillModel.apply_short_balance()` 同一種掛法：把「當日市場狀態」
            推給 model，model 不自行查資料源。沒有停券制度的市場沿用預設 no-op。
        - Parameters:
            - symbols: Set[str]
                今日觸及回補日的標的
        """

        pass

    def apply_cash_dividends(self, dividends: Dict[str, float]) -> None:
        """
        - Description:
            更新今日除息的每股現金股利（由 DataFeed 每根 bar 提供）
        - Parameters:
            - dividends: Dict[str, float]
                `{symbol: 每股現金股利}`
        """

        pass

    def apply_share_ratios(self, ratios: Dict[str, float]) -> None:
        """
        - Description:
            更新今日的股數倍率（配股、分割、減資；由 DataFeed 每根 bar 提供）
        - Parameters:
            - ratios: Dict[str, float]
                `{symbol: 新股數 / 舊股數}`
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
                `InstrumentSpec.to_units()` 換算後的計價單位數量
        - Return:
            - float
                該部位計入當日權益的金額
        """

        position_value: float

        if position.position_type == PositionType.SHORT:
            # 開倉時只扣了保證金與成本，賣出價款留作擔保品。
            # **已計提的借券費要扣掉**：它逐日累加在部位上、平倉時才結算，
            # 不扣的話持有期間的權益偏高、回補日一次掉下來，MDD 與日報酬都失真
            # （股利補償走的是即時扣款，除息當日就反映在餘額裡，不必在此重複扣）
            position.unrealized_pnl = round((position.price - mark_price) * units, 2)
            position_value = (
                position.margin
                + position.unrealized_pnl
                - getattr(position, "accrued_borrow_fee", 0.0)
            )
        else:
            position.unrealized_pnl = round((mark_price - position.price) * units, 2)
            position_value = mark_price * units

        cost_basis: float = position.price * units
        position.unrealized_roi = (
            round(position.unrealized_pnl / cost_basis * 100, 2) if cost_basis else 0.0
        )

        return position_value
