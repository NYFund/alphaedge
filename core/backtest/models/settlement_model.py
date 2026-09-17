import datetime
import math
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from core.backtest.datafeed.tw.futures_roll import FuturesRollConfig, FuturesRollPlanner
from core.backtest.models.cost_model import StockCostModel
from core.backtest.models.instrument_spec import (
    InstrumentSpec,
    TwFuturesSpec,
    TwStockSpec,
)
from core.managers.futures.position_manager import (
    FuturesMarginConfig,
    FuturesPositionManager,
)
from core.managers.stock.position_manager import StockPositionManager
from core.models import (
    BaseAccount,
    BasePosition,
    BaseQuote,
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesQuote,
    FuturesTradeRecord,
    StockOrder,
    StockPosition,
    StockQuote,
    StockTradeRecord,
)
from core.utils import (
    Action,
    DayTradeUncoveredPolicy,
    MarginCallPolicy,
    PositionType,
    ShortMethod,
    TimeUtils,
)

"""SettlementModel: 一根 bar 收盤後由市場規則強制執行的動作"""


class BaseSettlementModel(ABC):
    """
    結算模型：一根 bar 收盤後，市場規則強制對部位做的事

    這是本次重構的關鍵抽象——台股的「當沖日終強制回補」與期貨的「每日結算」
    在架構上是同一個掛點的兩種實作，看出這點之後就不需要兩個引擎。
    對應 Lean 的 SettlementModel / MarginCallModel / MarginInterestRateModel。
    """

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
            成為 `check_no_quote_exit()` 的出場依據。
        - Parameters:
            - quote_map: Dict[str, StockQuote]
                當日報價對照表
            - positions: List[StockPosition]
                要更新的部位
        """

        for position in positions:
            quote: Optional[StockQuote] = quote_map.get(position.symbol)
            if quote is not None and (quote.close or quote.cur_price):
                position.no_quote_days = 0
            else:
                position.no_quote_days += 1

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

            **預設為現金帳戶口徑**（原本寫在 `Backtester.snapshot_daily_equity()`
            內的那一段）：買進即把現金換成標的，故做多部位的價值就是市值；
            放空開倉時只扣了保證金與成本、賣出價款留作擔保品，故其價值是
            保證金加未實現損益。

            **為什麼要下沉成掛點**：這一段是「資金佔用方式」而非「權益怎麼記」——
            期貨是保證金交易，契約價值本身不佔用資金，做多部位的價值同樣只有
            保證金加未結算損益，沿用現金帳戶口徑會把整個契約價值算進權益
            （TX 一口契約價值 900 萬、保證金只有 70 萬）。
            `snapshot_daily_equity()` 的其餘部分（逐日記錄、盯市價取得）
            與商品類別無關，故引擎只在此開一個掛點，見
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
            # 開倉時只扣了保證金與成本，賣出價款留作擔保品
            position.unrealized_pnl = round((position.price - mark_price) * units, 2)
            position_value = position.margin + position.unrealized_pnl
        else:
            position.unrealized_pnl = round((mark_price - position.price) * units, 2)
            position_value = mark_price * units

        cost_basis: float = position.price * units
        position.unrealized_roi = (
            round(position.unrealized_pnl / cost_basis * 100, 2) if cost_basis else 0.0
        )

        return position_value


class TwStockSettlementModel(BaseSettlementModel):
    """
    台股結算模型：當沖日終強制回補、借券費逐日計提、維持率追繳

    執行順序固定為「當沖回補 → 每日部位檢查」，不可對調：對調會讓同一次強制回補
    記到不同的事件桶。
    """

    def __init__(
        self,
        position_manager: StockPositionManager,
        cost_model: StockCostModel,
        prev_close: Dict[str, float],
        instrument: Optional[InstrumentSpec] = None,
        day_trade_uncovered_policy: DayTradeUncoveredPolicy = (
            DayTradeUncoveredPolicy.FORCE_COVER_AT_CLOSE
        ),
        margin_call_policy: MarginCallPolicy = MarginCallPolicy.FORCE_COVER,
        max_holding_days: Optional[int] = None,
        max_no_quote_days: Optional[int] = None,
    ) -> None:
        self.position_manager: StockPositionManager = position_manager
        self.cost_model: StockCostModel = cost_model
        self.instrument: InstrumentSpec = instrument or TwStockSpec()

        # 與 FillModel 共用同一個 dict：停牌盯市與漲停判定都要用前收，
        # 但「記錄前收」屬成交價模型的職責，故此處只持有參照，不自行維護
        self.prev_close: Dict[str, float] = prev_close

        # 策略宣告的處理政策，由 factory 從策略帶入
        self.day_trade_uncovered_policy: DayTradeUncoveredPolicy = (
            day_trade_uncovered_policy
        )
        self.margin_call_policy: MarginCallPolicy = margin_call_policy
        self.max_holding_days: Optional[int] = max_holding_days
        self.max_no_quote_days: Optional[int] = max_no_quote_days

        # 當日市場狀態，由引擎每根 bar 從 DataFeed 推入（本 model 不自行查資料源）
        self.force_cover_symbols: Set[str] = set()  # 今日觸及融券最後回補日的標的
        self.cash_dividends: Dict[str, float] = {}  # 今日除息的每股現金股利
        self.share_ratios: Dict[str, float] = {}  # 今日的股數倍率（配股、分割、減資）

    def apply_force_cover_symbols(self, symbols: Set[str]) -> None:
        """更新今日觸及融券最後回補日的標的"""

        self.force_cover_symbols = symbols

    def apply_cash_dividends(self, dividends: Dict[str, float]) -> None:
        """更新今日除息的每股現金股利（元／股）"""

        self.cash_dividends = dividends

    def apply_share_ratios(self, ratios: Dict[str, float]) -> None:
        """更新今日的股數倍率（`新股數 / 舊股數`）"""

        self.share_ratios = ratios

    def on_bar_close(
        self,
        date: datetime.date,
        quotes: List[StockQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """當沖日終回補 → 留倉部位的借券費計提與維持率檢查（順序不可對調）"""

        self.enforce_day_trade_cover(date, quotes, account, event_counts)
        self.execute_daily_position_check(date, quotes, account, event_counts)

    def enforce_day_trade_cover(
        self,
        date: datetime.date,
        stock_quotes: List[StockQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            當沖放空於日終仍未回補時的處理

            現行引擎不會自己發現這件事，若放著不管，回測會出現實務上不存在的
            「當沖單留倉」；因此一律依 day_trade_uncovered_policy 明確處理並計數。
        - Parameters:
            - date: datetime.date
                當前交易日
            - stock_quotes: List[StockQuote]
                當日報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        quote_map: Dict[str, StockQuote] = {sq.symbol: sq for sq in stock_quotes}
        policy: DayTradeUncoveredPolicy = self.day_trade_uncovered_policy

        for position in account.get_positions(position_type=PositionType.SHORT):
            if not position.is_day_trade:
                continue

            quote: Optional[StockQuote] = quote_map.get(position.symbol)
            if quote is None:
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 當日無報價，無法強制回補"
                )
                continue

            # 漲停鎖死無法回補：轉為融券留倉，並單獨計數（放空最致命的尾部風險）
            if self.check_limit_up_locked(quote):
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 全日鎖漲停無法回補，轉為融券留倉"
                )
                event_counts["limit_up_cover_failed"] += 1
                self.convert_to_margin_position(
                    position, account, date, quote.close, event_counts
                )
                continue

            if policy == DayTradeUncoveredPolicy.RAISE:
                raise ValueError(
                    f"[Day Trade Cover] {position.symbol} 當沖放空於 {date} 日終未回補"
                )

            if policy == DayTradeUncoveredPolicy.CONVERT_TO_MARGIN:
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 未回補，依政策轉為融券留倉"
                )
                self.convert_to_margin_position(
                    position, account, date, quote.close, event_counts
                )
                continue

            logger.warning(
                f"[Day Trade Cover] {position.symbol} 未回補，以收盤價 {quote.close} 強制回補"
            )
            event_counts["forced_cover_day_trade"] += 1
            self.force_cover_position(position, date, quote.close)

    def check_limit_up_locked(self, quote: StockQuote) -> bool:
        """
        判定是否全日鎖漲停（開高低收皆等於漲停價），此時放空無法回補

        判定式與成交價驗證共用 `InstrumentSpec.is_locked_at_limit()`——
        兩邊各寫一份必然漂移。
        """

        return self.instrument.is_locked_at_limit(
            prev_close=self.prev_close.get(quote.symbol),
            open_price=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.close,
            side=Action.BUY,
            date=TimeUtils.to_date(quote.date),
        )

    def convert_to_margin_position(
        self,
        position: StockPosition,
        account: BaseAccount,
        date: datetime.date,
        close_price: float,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            將無法當日回補的當沖空單轉為融券留倉

            補收三項：保證金、融券手續費，以及**證交稅差額**。

            稅差額不可漏收：開倉當下該筆賣出是以現股當沖的減半稅率課稅，
            一旦轉為留倉，這筆賣出在現實中就不是當沖，應適用全額稅率。
            漏收會讓「漲停鎖死轉留倉」這種放空最痛的情境成本被系統性低估——
            低估恰好發生在最不該樂觀的地方。

            **餘額不足時不可硬轉**：舊版直接從餘額扣款而不檢查，實跑「帳戶 10,000 元、
            當沖放空 500 元 × 1 張」轉留倉後餘額是 −442,113 而沒有任何拒絕或計數；
            之後的維持率追繳只看單一部位的擔保維持率、不看帳戶現金，負餘額會一路留著。
            現金不夠就是留不了倉，改依既有的追繳政策處理。
        - Parameters:
            - position: StockPosition
                要轉為留倉的當沖空單
            - account: BaseAccount
                虛擬帳戶
            - date: datetime.date
                當前交易日（餘額不足強制回補時的成交日）
            - close_price: float
                當日收盤價（餘額不足強制回補時的成交價）
            - event_counts: Dict[str, int]
                事件計數
        """

        margin: int = self.cost_model.margin_required(
            price=position.price,
            volume=position.volume,
            short_method=ShortMethod.MARGIN,
        )
        borrow_fee: int = self.cost_model.borrow_fee(
            price=position.price,
            volume=position.volume,
            short_method=ShortMethod.MARGIN,
        )
        tax_diff: int = self.get_day_trade_tax_top_up(position)

        required: int = margin + borrow_fee + tax_diff
        if account.balance < required:
            event_counts["forced_cover_insufficient_margin"] += 1
            if self.margin_call_policy == MarginCallPolicy.FORCE_COVER:
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 轉融券留倉需 {required} 元，"
                    f"帳戶只有 {account.balance} 元，改以收盤價 {close_price} 強制回補"
                )
                self.force_cover_position(position, date, close_price)
            else:
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 轉融券留倉需 {required} 元，"
                    f"帳戶只有 {account.balance} 元；依政策不強制回補，"
                    f"該部位維持當沖狀態，餘額不會被扣成負數"
                )
            return

        position.is_day_trade = False
        position.short_method = ShortMethod.MARGIN
        position.margin += margin
        position.borrow_fee += borrow_fee
        position.tax += tax_diff
        position.transaction_cost += borrow_fee + tax_diff

        account.balance -= margin + borrow_fee + tax_diff
        account.margin_used += margin

    def get_day_trade_tax_top_up(self, position: StockPosition) -> int:
        """
        - Description:
            計算當沖轉留倉時應補徵的證交稅差額

            稅率一律取自 `CostConfig`，**不寫死 0.3% 與 0.15%**——落日條款或費率
            調整時只需改設定，不必回頭找散落在各處的字面值。
        - Parameters:
            - position: StockPosition
                轉換前的當沖空單
        - Return:
            - int
                應補徵的稅額（全額稅 − 已收的當沖減半稅）
        """

        # 補徵的是「開倉那天」的差額，故兩次都用開倉日的稅制
        open_date: Optional[datetime.date] = TimeUtils.to_date(position.date)
        full_tax: int = self.cost_model.tax(
            price=position.price,
            volume=position.volume,
            action=Action.SELL,
            is_day_trade=False,
            date=open_date,
        )
        day_trade_tax: int = self.cost_model.tax(
            price=position.price,
            volume=position.volume,
            action=Action.SELL,
            is_day_trade=True,
            date=open_date,
        )
        return max(0, full_tax - day_trade_tax)

    def force_cover_position(
        self,
        position: StockPosition,
        date: datetime.date,
        price: float,
    ) -> List[StockTradeRecord]:
        """以指定價格強制回補放空部位（當沖日終、維持率追繳、超過持有天數共用）"""

        order: StockOrder = StockOrder(
            stock_id=position.symbol,
            date=date,
            action=Action.BUY,
            position_type=PositionType.SHORT,
            price=price,
            volume=position.volume,
            short_method=position.short_method,
            is_day_trade=position.is_day_trade,
        )
        return self.position_manager.close_position(order)

    def execute_daily_position_check(
        self,
        date: datetime.date,
        stock_quotes: List[StockQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            每日收盤後對所有未平倉部位的檢查

            **做多部位不可略過**：公司行動（除息、配股、分割、減資）與「長期
            無報價」對兩個方向都成立。舊版只處理空單，做多跨除息因此收不到現金
            股利、跨配股張數不調整，帳面在除權息日憑空虧一段；而下市的股票
            會永遠留在帳上，以最後一個收盤價計算權益（存活者偏差），
            還持續佔用 `max_holdings` 名額。
        - Parameters:
            - date: datetime.date
                當前交易日
            - stock_quotes: List[StockQuote]
                當日報價；停牌無報價時沿用前一交易日收盤價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        positions: List[StockPosition] = account.get_positions()
        if not positions:
            return

        quote_map: Dict[str, StockQuote] = {sq.symbol: sq for sq in stock_quotes}

        self.update_no_quote_days(quote_map, positions)
        self.accrue_holding_cost(date, quote_map, account)
        # 股利與股數調整先於強制回補／出場：除權息當日的權利義務屬該日仍在倉者，
        # 放在出場之後會讓「出場日恰為除權息日」的部位少記一筆
        self.settle_cash_dividend(date, account, event_counts)
        self.apply_corporate_actions(date, account, event_counts)
        self.check_margin_call(date, quote_map, account, event_counts)
        self.check_long_no_quote_exit(date, quote_map, account, event_counts)

    def accrue_holding_cost(
        self,
        date: datetime.date,
        quote_map: Dict[str, StockQuote],
        account: BaseAccount,
    ) -> None:
        """
        - Description:
            逐日計提持有成本並更新持有天數

            **持有天數與計提天數都是曆日，不是 bar 數**：
            週五開的空單到週一只過了 1 根 bar，卻是 3 個曆日的借券費。舊版每根
            bar `+= 1` 並以 `holding_days=1` 計提，一年只計到 252 天，
            年化費率因此低估約 31%（1 − 252/365）；而 `holding_days` 這個名字
            在 `TradeRecord` 那邊指的是曆日，同名不同義。

            只有 SBL 借券費在此逐日累加；MARGIN 的融券手續費在開倉時一次收取、
            融券利息於平倉時依日期差一次計算，在此重複計算會造成雙重計費。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quote_map: Dict[str, StockQuote]
                當日報價對照表
            - account: BaseAccount
                交易帳戶
        """

        for position in account.get_positions(position_type=PositionType.SHORT):
            open_date: Optional[datetime.date] = TimeUtils.to_date(position.date)
            if open_date is None:
                logger.warning(
                    f"[SBL] {position.symbol} 的開倉日無法解析，本日不計提借券費"
                )
                continue

            position.holding_days = max((date - open_date).days, 0)

            if position.short_method != ShortMethod.SBL:
                continue

            # 上次計提到哪一天；剛開倉時視為開倉日（開倉當日不計費）
            last_accrual: datetime.date = position.last_accrual_date or open_date
            elapsed_days: int = (date - last_accrual).days
            if elapsed_days <= 0:
                continue

            position.last_accrual_date = date

            price: float = self.get_mark_price(position, quote_map)
            position.accrued_borrow_fee += self.cost_model.borrow_fee(
                price=price,
                volume=position.volume,
                holding_days=elapsed_days,
                short_method=ShortMethod.SBL,
            )

    def settle_cash_dividend(
        self,
        date: datetime.date,
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            除息日的現金股利結算：做多者收到、放空者補償給出借方

            **只補償除息日之前就在倉的部位**：除權息交易日當天賣出者已不含權，
            當日開倉的空單不需補償（漲停鎖死轉留倉的當沖單同樣落在此例）。

            與價格還原的分工（兩者都做才不會重複計算或互相抵銷）：部位損益一律以
            `quote.close` 這條**未還原**的原始價序列盯市，除息跳空因此仍留在帳面
            損益裡；本方法扣掉的正是「那段跳空該歸誰」——放空者從跳空賺到的價差要
            原封不動付給出借方，兩者相抵後除息本身不產生損益。還原價只用於**訊號**
            （`Backtester.adjusted_price`），不參與這裡的記帳。

            現金股利為 `NaN`（上市權息並存的標的無法拆出現金股利）時
            **不猜 0**：記 warning
            並計入 `dividend_compensation_unknown`，讓報表看得見被跳過的補償筆數。
        - Parameters:
            - date: datetime.date
                當前交易日（＝除權息交易日）
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        if not self.cash_dividends:
            return

        compensate_short: bool = self.cost_model.config.compensate_cash_dividend

        for position in account.get_positions():
            if position.symbol not in self.cash_dividends:
                continue

            # 除權息交易日當天開倉者不含權
            if TimeUtils.to_date(position.date) >= date:
                continue

            is_short: bool = position.position_type == PositionType.SHORT
            if is_short and not compensate_short:
                continue
            dividend: float = self.to_dividend_per_share(
                self.cash_dividends[position.symbol]
            )
            if math.isnan(dividend):
                logger.warning(
                    f"[Dividend] {position.symbol} 於 {date} 除權息，但現金股利無法拆分"
                    f"（權息並存），本次跳過"
                    + ("股利補償——該筆放空成本會被低估" if is_short else "股利入帳")
                )
                event_counts["dividend_compensation_unknown"] += 1
                continue

            # 純除權（現金股利為 0）不產生現金流
            amount: int = int(dividend * self.instrument.to_units(position.volume))
            if amount <= 0:
                continue

            if is_short:
                logger.warning(
                    f"[Dividend] {position.symbol} 於 {date} 除息 {dividend} 元／股，"
                    f"空單補償出借方 {amount} 元"
                )
                event_counts["dividend_compensation_paid"] += 1

                # 與 accrued_borrow_fee 同一種記法：只累加在部位上，
                # 平倉時才依回補張數攤提進 carry_cost，不動 position.transaction_cost
                position.dividend_compensation += amount
                account.balance -= amount
                continue

            # 做多：股利當日入帳。**不調整成本基準**——盯市用的是未還原價，
            # 除息跳空已經反映在未實現損益裡，收到的現金正好補回那一段，
            # 兩者相抵後除息本身不產生損益
            logger.info(
                f"[Dividend] {position.symbol} 於 {date} 除息 {dividend} 元／股，"
                f"做多部位收到 {amount} 元"
            )
            event_counts["dividend_received"] += 1
            position.dividend_received += amount
            account.balance += amount

    def apply_corporate_actions(
        self,
        date: datetime.date,
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            配股、分割、減資的股數與每股成本調整（做多、放空都適用）

            價格序列在這一天會跳動（分割後砍半、減資後上跳），盯市用的又是
            **未還原價**，記帳端不跟著調整的話，張數不變、價格砍半，
            帳面就憑空虧一半——空單則反向憑空獲利。

            調整方式是「股數 × 倍率、每股成本 ÷ 倍率」，成本總額因此不變，
            權益在調整日連續。

            **不足一張的零股折現**：部位的張數是整數，倍率算出的股數多半不是
            整張（配股 0.05 → 1.05 張）。零股部分以當日每股成本折成現金入帳
            （空單則扣款），而不是四捨五入吞掉——吞掉會讓權益在每次配股時
            跳動一小段。
        - Parameters:
            - date: datetime.date
                當前交易日（＝除權交易日／恢復買賣日）
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        if not self.share_ratios:
            return

        for position in account.get_positions():
            ratio: float = self.share_ratios.get(position.symbol, 1.0)
            if ratio <= 0 or ratio == 1.0:
                continue

            # 除權交易日當天開倉者不含權
            if TimeUtils.to_date(position.date) >= date:
                continue

            old_volume: int = position.volume
            adjusted_units: float = self.instrument.to_units(old_volume) * ratio
            lot_size: int = self.instrument.to_units(1)
            new_volume: int = int(adjusted_units // lot_size)
            odd_units: float = adjusted_units - new_volume * lot_size

            if new_volume <= 0:
                logger.warning(
                    f"[Corporate Action] {position.symbol} 於 {date} 調整倍率 {ratio}，"
                    f"{old_volume} 張調整後不足一張，本次不調整"
                )
                continue

            # 成本總額不變：每股成本除以倍率
            new_price: float = position.price / ratio
            odd_cash: int = int(odd_units * new_price)

            position.volume = new_volume
            position.price = round(new_price, 4)
            if odd_cash > 0:
                if position.position_type == PositionType.SHORT:
                    account.balance -= odd_cash
                else:
                    account.balance += odd_cash

            logger.info(
                f"[Corporate Action] {position.symbol} 於 {date} 股數倍率 {ratio}："
                f"{old_volume} → {new_volume} 張，每股成本 {position.price}"
                + (f"，零股 {odd_units:.0f} 股折現 {odd_cash} 元" if odd_cash else "")
            )
            event_counts["share_adjustment_applied"] += 1

    def check_long_no_quote_exit(
        self,
        date: datetime.date,
        quote_map: Dict[str, StockQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            做多部位連續無報價達上限時強制出場

            下市或長期停牌的股票不會再出現在 `price` 表，引擎也不會把無報價的
            標的交給策略（`Backtester.execute_close_signal()` 只走有報價的部位），
            於是它永遠留在帳上、以最後一個收盤價計入權益（下市損失完全不反映，
            屬存活者偏差），還一直佔著 `max_holdings` 的名額。

            **出場價沿用空單路徑的口徑**（最後可得收盤價）。那仍然高估下市股的
            回收價——實務上多半是部分償還甚至歸零，但回測無法精確模擬，
            歸零則會系統性低估。要保守估計的人可依本事件計數自行調整。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quote_map: Dict[str, StockQuote]
                當日報價對照表
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        if self.max_no_quote_days is None:
            return

        for position in list(account.get_positions(position_type=PositionType.LONG)):
            if position.no_quote_days < self.max_no_quote_days:
                continue

            price: float = self.get_mark_price(position, quote_map)
            logger.warning(
                f"[Forced Exit] {position.symbol} 連續 {position.no_quote_days} 日"
                f"無報價（停牌／下市），以最後可得價格 {price} 強制出場"
            )
            event_counts["forced_exit_no_quote"] += 1
            self.force_exit_long_position(position, date, price)

    def force_exit_long_position(
        self,
        position: StockPosition,
        date: datetime.date,
        price: float,
    ) -> None:
        """以指定價格全量賣出做多部位（引擎強制出場用）"""

        self.position_manager.close_long_position(
            position=position,
            stock_order=StockOrder(
                stock_id=position.symbol,
                date=date,
                action=Action.SELL,
                position_type=PositionType.LONG,
                price=price,
                volume=position.volume,
            ),
            close_volume=position.volume,
        )

    @staticmethod
    def to_dividend_per_share(value: Any) -> float:
        """把資料表原樣取出的現金股利轉為 float；無法轉換者一律視為 `NaN`（未知）"""

        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    def check_force_cover(self, date: datetime.date, position: StockPosition) -> bool:
        """
        - Description:
            判定該部位今日是否觸及停券強制回補日；兩個來源任一命中即回補

            1. `ShortConstraint.force_cover_dates`：使用者明示指定的日期，
               **不分放空管道一律適用**——引擎不替使用者的政策再加條件。
            2. 除權息行事曆推導的融券最後回補日（由 DataFeed 每根 bar 推入）：
               這是**融券制度**的規則，故只對 `MARGIN` 生效。SBL 借券不受強制回補
               約束，其跨除息日的成本改由 `compensate_cash_dividend()` 反映；
               `DAY_TRADE` 當日已由 `enforce_day_trade_cover()` 處理完畢。
        - Parameters:
            - date: datetime.date
                當前交易日
            - position: StockPosition
                待判定的放空部位
        - Return:
            - bool
                True 表示今日須強制回補
        """

        constraint = self.cost_model.config.short_constraint

        if date in constraint.get_force_cover_dates(position.symbol):
            return True

        if not constraint.auto_force_cover_on_ex_dividend:
            return False

        if position.short_method != ShortMethod.MARGIN:
            return False

        return position.symbol in self.force_cover_symbols

    def check_margin_call(
        self,
        date: datetime.date,
        quote_map: Dict[str, StockQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            維持率追繳與強制回補檢查

            現行引擎沒有跨日的委託佇列，無法模擬「次一交易日開盤成交」，
            因此一律以觸發當日收盤價立即回補。
        """

        for position in list(account.get_positions(position_type=PositionType.SHORT)):
            price: float = self.get_mark_price(position, quote_map)

            # 連續無報價（停牌／下市）：強制出場
            #
            # 出場價採「最後可得價格」而非歸零：下市清算實務上多為部分償還，
            # 回測無法精確模擬，歸零會系統性高估放空獲利。保守估計者可另行
            # 以報表的本事件計數自行調整。
            if (
                self.max_no_quote_days is not None
                and position.no_quote_days >= self.max_no_quote_days
            ):
                logger.warning(
                    f"[Force Cover] {position.symbol} 連續 {position.no_quote_days} 日"
                    f"無報價（停牌／下市），以最後可得價格 {price} 強制出場"
                )
                event_counts["forced_cover_no_quote"] += 1
                self.force_cover_position(position, date, price)
                continue

            # 超過最長持有天數：強制回補
            if (
                self.max_holding_days is not None
                and position.holding_days >= self.max_holding_days
            ):
                logger.warning(
                    f"[Force Cover] {position.symbol} 持有 {position.holding_days} 天"
                    f"已達上限，以 {price} 強制回補"
                )
                event_counts["forced_cover_max_holding"] += 1
                self.force_cover_position(position, date, price)
                continue

            # 停券強制回補日（使用者指定 ＋ 除權息行事曆推導的融券最後回補日）
            if self.check_force_cover(date, position):
                logger.warning(
                    f"[Force Cover] {position.symbol} 於 {date} 停券，以 {price} 強制回補"
                )
                # 停券與持有天數到期是兩種成因，記到同一個桶會讓「策略設定的持有上限
                # 太短」與「標的停券」無法區分，兩者的因應方式完全不同
                event_counts["forced_cover_suspended"] += 1
                self.force_cover_position(position, date, price)
                continue

            # 維持率追繳
            if position.short_method != ShortMethod.MARGIN:
                continue

            if not self.cost_model.check_margin_call(
                proceeds=position.short_proceeds,
                margin=position.margin,
                cur_price=price,
                volume=position.volume,
            ):
                continue

            if self.margin_call_policy == MarginCallPolicy.WARN_ONLY:
                logger.warning(
                    f"[Margin Call] {position.symbol} 維持率已低於門檻（僅記錄不回補）"
                )
                continue

            logger.warning(
                f"[Margin Call] {position.symbol} 維持率不足，以 {price} 強制回補（斷頭）"
            )
            event_counts["forced_cover_margin_call"] += 1
            self.force_cover_position(position, date, price)

    def update_no_quote_days(
        self,
        quote_map: Dict[str, StockQuote],
        positions: List[StockPosition],
    ) -> None:
        """
        - Description:
            更新每個部位的連續無報價天數

            有報價即歸零，無報價則累加。長期停牌或已下市的標的會持續累加，
            成為 `check_no_quote_exit()` 的出場依據。
        - Parameters:
            - quote_map: Dict[str, StockQuote]
                當日報價對照表
            - positions: List[StockPosition]
                要更新的部位
        """

        for position in positions:
            quote: Optional[StockQuote] = quote_map.get(position.symbol)
            if quote is not None and (quote.close or quote.cur_price):
                position.no_quote_days = 0
            else:
                position.no_quote_days += 1

    def get_mark_price(
        self, position: StockPosition, quote_map: Dict[str, StockQuote]
    ) -> float:
        """取得盯市價格：優先用當日收盤，停牌時沿用前收，再無資料則退回開倉價"""

        quote: Optional[StockQuote] = quote_map.get(position.symbol)
        if quote is not None and (quote.close or quote.cur_price):
            return quote.close or quote.cur_price

        logger.warning(
            f"[Mark Price] {position.symbol} 當日無報價，沿用前一交易日收盤價盯市"
        )
        return self.prev_close.get(position.symbol, position.price)


class TwFuturesSettlementModel(BaseSettlementModel):
    """
    台期貨結算模型：**每日以結算價逐日盯市**

    這正是本抽象存在的理由——台股在此掛點做的是「當沖日終強制回補」，
    期貨做的是「每日結算」，兩者是同一個掛點的兩種實作。

    **逐日盯市的記帳語意**（與股票最根本的差異）：損益不等到平倉才實現，
    每個交易日以結算價結清當日損益、現金當天就進出帳戶，部位的 `price`
    隨之重設為結算價。實作在 `FuturesPositionManager.settle_daily()`，
    **FIFO 主幹一行都不用動**。

    ---

    **保證金追繳、換月轉倉與交易日曆**分別由本 model 的追繳檢查、`FuturesRollPlanner`
    與 `FuturesCalendar` 處理，細節見各自的 docstring。

    ⚠️ **到期契約的權宜出場**：已到期的契約不會再有報價，策略因此拿不到報價、
    也就下不出那張平倉單——不處理的話該部位會一路留到回測結束並持續佔用保證金
    （實測：示範策略在 2024-04 開的近月部位卡到 12 月，凍結 79 萬保證金）。
    故本 model 在契約連續 `MAX_NO_QUOTE_DAYS` 根 bar 沒有報價時，以**最近一次
    結算價**強制出場並計入 `forced_cover_no_quote`。這是**兜底不是換月**：
    換月轉倉會先把月契約部位轉走，本段接住的是轉倉接不到的部位（例如週契約）。
    """

    # 契約連續幾根 bar 沒有報價就強制出場。
    #
    # **不設成 1**：單日的資料缺漏（爬蟲漏一天、當日零成交）與「契約已到期」
    # 在報價層看起來一模一樣，設成 1 會讓前者被誤判成到期而提早平倉。
    # 到期契約晚幾天出場不影響損益——盯市價已凍結在最後一次結算價，
    # 那幾天的每日結算損益都是 0。
    MAX_NO_QUOTE_DAYS: int = 3

    def __init__(
        self,
        position_manager: FuturesPositionManager,
        instrument: Optional[InstrumentSpec] = None,
        roll_config: Optional[FuturesRollConfig] = None,
    ) -> None:
        self.position_manager: FuturesPositionManager = position_manager
        self.instrument: InstrumentSpec = instrument or TwFuturesSpec()

        # 換月設定；`calendar` 由 DataFeed 注入同一個物件（見 `FuturesRollConfig`）
        self.roll_config: FuturesRollConfig = roll_config or FuturesRollConfig()

        # {契約代號: 連續無報價的 bar 數}；同一契約的多個部位共用同一個計數
        self.no_quote_days: Dict[str, int] = {}

    @property
    def margin_config(self) -> FuturesMarginConfig:
        """保證金設定；唯一來源是部位管理層持有的那一份，不另存副本"""

        return self.position_manager.margin_config

    def on_bar_close(
        self,
        date: datetime.date,
        quotes: List[FuturesQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            一根 bar 收盤後逐日盯市：以當日結算價結清每個未平倉部位的當日損益

            保證金追繳的強制平倉計入 `forced_cover_margin_call`，
            到期兜底出場計入 `forced_cover_no_quote`。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[FuturesQuote]
                當根 bar 的報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數（本階段未使用）
        """

        quote_map: Dict[str, FuturesQuote] = {quote.symbol: quote for quote in quotes}

        for position in list(account.get_positions()):
            self.position_manager.settle_daily(
                position, self.get_mark_price(position, quote_map)
            )

        # 換月排在到期出場之前：能轉倉就轉倉，轉不了才走權宜出場。
        # 順序對調的話，所有部位都會先被當成「到期」平掉，換月永遠不會發生
        self.roll_positions(date, quotes, account, event_counts)

        # 逐日盯市完才處理到期出場：出場價即最近一次結算價，
        # 先結算才不會漏掉最後一根 bar 的損益
        self.close_expired_positions(date, quote_map, account, event_counts)

        # 追繳放最後：前面兩步都會改變權益與佔用保證金，先判斷會用到過期的數字
        self.check_margin_call(date, quote_map, account, event_counts)

    def check_margin_call(
        self,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
        account: FuturesAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            保證金追繳：帳戶**權益**低於維持保證金總額時，依政策強制平倉或僅標記

            **判斷的是權益不是可動用餘額**（權益 ＝ 可動用餘額 ＋ 佔用保證金）：
            期貨的浮動損益每日結算進帳戶，虧損會先吃掉可動用餘額，可動用餘額歸零
            並不代表已經被追繳——真正的門檻是「權益是否還撐得住維持保證金」。
            反過來說，浮動獲利會讓權益上升，因而**可以支撐加碼**，
            這正是本步驟要求「以權益決定保證金充足度」的意思。

            **強制平倉逐口處理、由保證金最大的部位開始**：先平掉佔用最多的那一口
            才可能一次把權益拉回門檻之上；每平一筆就重算，避免一次全砍
            （真實券商的斷頭也是砍到足額為止，不是清空帳戶）。

            出場價一律用當日盯市價（＝結算價）。**現行引擎沒有跨日委託佇列**，
            無法模擬「次一交易日開盤成交」，與台股的維持率追繳同一種簡化。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - account: FuturesAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        # 每一輪都重新取部位：`close_position()` 走 FIFO，被平掉的不一定是本輪挑中的
        # 那一筆（同一契約的多筆部位共用同一個 symbol），拿舊清單續跑會重複計數
        for _ in range(len(account.get_positions()) + 1):
            positions: List[FuturesPosition] = account.get_positions()
            if not positions:
                return

            requirement: float = self.get_maintenance_requirement(date, positions)
            if account.equity >= requirement * self.margin_config.margin_call_ratio:
                return

            if self.margin_config.margin_call_policy == MarginCallPolicy.WARN_ONLY:
                # **不計數**：`forced_cover_margin_call` 的語意是「強制平倉幾次」，
                # 只標記卻計數會讓報表把「撐過去了」讀成「被斷頭了」。
                # 且本狀態每根 bar 都會成立，計數會隨天數膨脹（與台股同一種處理）
                logger.warning(
                    f"[Margin Call] {date} 權益 {account.equity:.0f} 低於維持保證金 "
                    f"{requirement:.0f}（僅標記不平倉）"
                )
                return

            # 先砍佔用保證金最多的契約，才可能一次把權益拉回門檻之上；
            # 同一契約內由誰出場則由 FIFO 決定（每口佔用相同，結果等價）
            position: FuturesPosition = max(positions, key=lambda p: p.margin)
            price: float = self.get_mark_price(position, quote_map)
            logger.warning(
                f"[Margin Call] {position.symbol} 權益 {account.equity:.0f} 低於"
                f"維持保證金 {requirement:.0f}，以 {price} 強制平倉"
            )
            event_counts["forced_cover_margin_call"] += 1

            if not self.close_position_at(position, date, price):
                # 平不掉就停手，否則會在同一根 bar 內無限重試
                logger.warning(
                    f"[Margin Call] {position.symbol} 無法平倉，本根 bar 停止追繳處理"
                )
                return

    def get_maintenance_requirement(
        self, date: datetime.date, positions: List[FuturesPosition]
    ) -> float:
        """所有未平倉部位在該日的維持保證金總額（逐商品查表，見部位管理層）"""

        return sum(
            self.position_manager.calculate_maintenance_margin(position, date)
            for position in positions
        )

    def roll_positions(
        self,
        date: datetime.date,
        quotes: List[FuturesQuote],
        account: FuturesAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            換月：把未平倉部位轉到規則指定的當家契約

            **換月是市場結構強加的，不是策略訊號**：契約會到期，部位不轉倉就會
            憑空消失，因此放在結算模型而不是策略層。但「什麼時候轉」是政策，
            由 `FuturesRollConfig.rule` 決定，且**與策略挑合約用同一份規則實作**
            （`FuturesRollPlanner`）——兩處不一致會讓訊號在次月、部位在近月。

            **轉倉 ＝ 平掉舊契約 ＋ 以相同口數與方向開新契約**：舊契約以盯市價
            平倉（損益已逐日結清，這一段通常為 0），新契約以當日收盤價開倉。
            展期價差因此**真實反映在帳戶上**——這正是連續合約要調整掉的那筆錢，
            回測不該把它變不見。

            **開不進去就只記 warning 不還原**：新契約的保證金可能因調整而變高，
            此時曝險會少掉——那是真實會發生的事（真的繳不出保證金就是轉不了倉），
            靜默還原成舊部位反而是造假。

            ⚠️ **轉倉後持有天數重新計算**：新部位的開倉日是轉倉日。策略若以
            「持有滿 N 天才平倉」為出場條件，轉倉會讓那個計時重來一次。
            這與真實情況一致（那確實是一筆新的部位），但用持有天數當出場條件的
            策略要自己意識到這件事。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[FuturesQuote]
                當根 bar 的報價
            - account: FuturesAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        if not self.roll_config.enabled:
            return

        planner: Optional[FuturesRollPlanner] = self.roll_config.build_planner()
        if planner is None or not account.get_positions():
            return

        quote_map: Dict[str, FuturesQuote] = {quote.symbol: quote for quote in quotes}
        expiries: Dict[str, List[str]] = {}
        open_interest: Dict[str, Dict[str, Any]] = {}
        for quote in quotes:
            expiries.setdefault(quote.product, []).append(quote.expiry)
            open_interest.setdefault(quote.product, {})[quote.expiry] = (
                quote.open_interest
            )

        for position in list(account.get_positions()):
            # **週契約不由本規則轉倉**：規劃器只認月契約，硬轉會把週契約的部位
            # 換成月契約——那是不同的商品，不是同一條曝險的延續
            if not planner.MONTHLY_EXPIRY_PATTERN.match(position.expiry):
                continue

            active: Optional[str] = planner.resolve_active_expiry(
                date,
                expiries.get(position.product, []),
                open_interest.get(position.product),
            )
            # **不可換到比現在更近的月份**：`OPEN_INTEREST` 規則
            # 比較的是「次月未沖銷量是否超過近月」，而未沖銷量會逐日波動——
            # 換到次月之後，近月的未沖銷量可能又反超一天，於是部位被換回去。
            # 每來回一次就付兩次手續費與一次展期價差，而且是憑空產生的。
            # 換月是單向的：只往遠月走。
            if active is None or active <= position.expiry:
                continue

            new_quote: Optional[FuturesQuote] = quote_map.get(
                f"{position.product}{active}"
            )
            if new_quote is None or not new_quote.close:
                logger.warning(
                    f"[Roll] {position.symbol} 應轉倉至 {active}，但新契約當日無報價，"
                    f"本次不轉倉"
                )
                continue

            self.roll_single_position(position, new_quote, date, quote_map)
            # 引擎的 `new_event_counts()` 沒有這個 key（那是台股的清單），
            # 期貨場次自行加入；台股的事件報表因此完全不受影響
            event_counts["rolled_contract"] = event_counts.get("rolled_contract", 0) + 1

    def roll_single_position(
        self,
        position: FuturesPosition,
        new_quote: FuturesQuote,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
    ) -> None:
        """
        - Description:
            平掉舊契約並以相同口數與方向開新契約（展期價差如實入帳）

            **兩腿一定要用同一種價**：舊版舊腿走盯市價
            （＝結算價），新腿卻用 `close`。結算價與收盤價在期貨是兩個不同的
            數字，混用會讓帳上多出一筆**不存在的展期價差**——而展期價差正是
            這裡唯一該記錄的東西，摻進口徑差異就失去意義了。
        - Parameters:
            - position: FuturesPosition
                要轉倉的部位
            - new_quote: FuturesQuote
                新契約當日報價
            - date: datetime.date
                轉倉日
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
        """

        volume: int = position.volume
        position_type: PositionType = position.position_type
        exit_price: float = self.get_mark_price(position, quote_map)
        entry_price: float = self.get_quote_mark_price(new_quote)

        logger.info(
            f"* Roll {position.symbol} → {new_quote.contract_id} "
            f"({volume} lots @ {entry_price})"
        )
        self.close_position_at(position, date, exit_price)

        opened = self.position_manager.open_position(
            FuturesOrder(
                product=new_quote.product,
                expiry=new_quote.expiry,
                date=date,
                action=(
                    Action.BUY if position_type == PositionType.LONG else Action.SELL
                ),
                position_type=position_type,
                price=entry_price,
                volume=volume,
            )
        )
        if opened is None:
            logger.warning(
                f"[Roll] {new_quote.contract_id} 開倉失敗（多半是保證金不足），"
                f"原本 {volume} 口的曝險已消失"
            )

    def close_expired_positions(
        self,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            把已停止交易（連續無報價）的契約以最近一次結算價強制出場

            **為什麼非做不可**：策略是靠報價下單的，契約到期後不再有報價，
            策略永遠下不出那張平倉單，部位會留到回測結束並持續佔用保證金。

            出場價取 `position.price`——逐日盯市之後它就是最近一次結算價，
            該部位到期前的損益早已逐日結進帳戶，故這一段的價差為 0。
            與真正的最終結算價（最後交易日次一營業日的特別開盤參考價）仍有落差，
            **正常情況下換月轉倉會先把部位轉走，這段是兜底**。
        - Parameters:
            - date: datetime.date
                當前交易日（＝出場日；會比實際最後交易日晚幾根 bar）
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        self.update_no_quote_days(quote_map, account.get_positions())

        for position in list(account.get_positions()):
            if self.no_quote_days.get(position.symbol, 0) < self.MAX_NO_QUOTE_DAYS:
                continue

            logger.warning(
                f"[Expired] {position.symbol} 連續 {self.MAX_NO_QUOTE_DAYS} 根 bar "
                f"無報價（契約已到期），以最近一次結算價 {position.price} 強制出場"
            )
            event_counts["forced_cover_no_quote"] += 1
            self.close_position_at(position, date, position.price)

    def close_position_at(
        self,
        position: FuturesPosition,
        date: datetime.date,
        price: float,
    ) -> List[FuturesTradeRecord]:
        """以指定價格強制平掉部位（多單賣出、空單買進回補）"""

        order: FuturesOrder = FuturesOrder(
            product=position.product,
            expiry=position.expiry,
            date=date,
            action=(
                Action.SELL
                if position.position_type == PositionType.LONG
                else Action.BUY
            ),
            position_type=position.position_type,
            price=price,
            volume=position.volume,
        )
        return self.position_manager.close_position(order)

    def update_no_quote_days(
        self,
        quote_map: Dict[str, FuturesQuote],
        positions: List[FuturesPosition],
    ) -> None:
        """
        - Description:
            更新每個契約的連續無報價 bar 數（有報價即歸零）

            **計數掛在 model 而不是部位上**：無報價是**契約**的狀態不是部位的狀態，
            同一契約的多個部位共用同一個答案，記在部位上只會存好幾份一樣的數。
        - Parameters:
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - positions: List[FuturesPosition]
                目前的未平倉部位
        """

        for position in positions:
            quote: Optional[FuturesQuote] = quote_map.get(position.symbol)
            if quote is not None and (quote.close or quote.cur_price):
                self.no_quote_days[position.symbol] = 0
            else:
                self.no_quote_days[position.symbol] = (
                    self.no_quote_days.get(position.symbol, 0) + 1
                )

    def get_mark_price(
        self, position: FuturesPosition, quote_map: Dict[str, FuturesQuote]
    ) -> float:
        """
        - Description:
            取得盯市價：**期貨的盯市價就是當日結算價**

            結算價缺漏時退回收盤價——夜盤本來就沒有結算價（來源即為 NULL），
            日盤偶有缺漏。**不可當成 0**：那會讓部位在一天內被結算成歸零。

            當日完全無報價（契約已到期、或資料缺這一天）時沿用
            `position.price`——逐日盯市之後它就是**最近一次結算價**
            （尚未結算過則為開倉價），見 `FuturesPosition`。
        - Parameters:
            - position: FuturesPosition
                待盯市的部位
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價（以契約代號為鍵）
        - Return:
            - float
                盯市價格
        """

        quote: Optional[FuturesQuote] = quote_map.get(position.symbol)

        if quote is not None:
            price: Optional[float] = self.get_quote_mark_price(quote)
            if price:
                return float(price)

        logger.warning(
            f"[Mark Price] {position.symbol} 當日無結算價可用，"
            f"沿用最近一次結算價 {position.price} 盯市"
        )
        return position.price

    @staticmethod
    def get_quote_mark_price(quote: FuturesQuote) -> float:
        """
        - Description:
            一筆報價的盯市價：結算價優先，缺漏時退回收盤價

            抽出來是為了讓**轉倉的兩腿用同一種價**：舊腿走盯市價、
            新腿走 `close` 的話，帳上會多出一筆不存在的展期價差。
        - Parameters:
            - quote: FuturesQuote
                報價
        - Return:
            - float
                盯市價；三種價格都沒有時為 0.0
        """

        price: Optional[float] = (
            quote.settlement_price
            if quote.settlement_price is not None
            else (quote.close or quote.cur_price)
        )
        return float(price) if price else 0.0

    def mark_position(
        self, position: FuturesPosition, mark_price: float, units: int
    ) -> float:
        """
        - Description:
            期貨的部位價值 ＝ **保證金 ＋ 尚未結算的那一段損益**

            **不是契約價值**：保證金交易只凍結保證金，契約價值本身不佔用資金
            （TX 一口契約價值 900 萬、保證金只有 70 萬），沿用基底的現金帳戶口徑
            會讓權益曲線整段偏高一個數量級。

            `on_bar_close()` 的逐日盯市已把當日損益結進 `balance`，故本方法在
            多數日子算出的未實現損益是 **0——那是對的，不是沒算到**；
            只有當日無結算價（沿用舊價）或報價缺漏時才會有殘值。

            **`units` 用不到**：期貨的乘數逐契約不同，`InstrumentSpec.to_units()`
            拿不到商品（見 `TwFuturesSpec`），乘數一律取自部位自身的
            `multiplier`，損益公式直接走 `FuturesPositionManager.calculate_pnl()`。
        - Parameters:
            - position: FuturesPosition
                未平倉部位；`unrealized_pnl` 與 `unrealized_roi` 會被就地更新
            - mark_price: float
                盯市價（＝當日結算價）
            - units: int
                計價單位數量；期貨不使用，見上
        - Return:
            - float
                該部位計入當日權益的金額
        """

        unrealized_pnl: float = self.position_manager.calculate_pnl(
            position_type=position.position_type,
            entry_price=position.price,
            exit_price=mark_price,
            volume=position.volume,
            multiplier=position.multiplier,
        )
        position.unrealized_pnl = round(unrealized_pnl, 2)

        # 報酬率的分母是保證金不是契約價值（期貨投入的資金就是保證金）
        position.unrealized_roi = (
            round(position.unrealized_pnl / position.margin * 100, 2)
            if position.margin
            else 0.0
        )

        return position.margin + position.unrealized_pnl
