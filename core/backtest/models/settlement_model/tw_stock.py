import datetime
import math
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from core.backtest.models.cost_model import StockCostModel
from core.backtest.models.fill_model import BaseFillModel
from core.backtest.models.instrument_spec import (
    InstrumentSpec,
    TwStockSpec,
)
from core.backtest.models.settlement_model.base import BaseSettlementModel
from core.managers.stock.position_manager import StockPositionManager
from core.models import (
    BaseAccount,
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

"""TwStockSettlementModel: 台股的當沖回補、信用交易維持率、公司行為與出場守則"""


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
        fill_model: Optional[BaseFillModel] = None,
    ) -> None:
        super().__init__(fill_model=fill_model)

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
                    position, account, date, quote, event_counts
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
                    position, account, date, quote, event_counts
                )
                continue

            logger.warning(
                f"[Day Trade Cover] {position.symbol} 未回補，以收盤價 {quote.close} 強制回補"
            )
            event_counts["forced_cover_day_trade"] += 1
            self.force_cover_position(position, date, quote.close, quote)

    def check_limit_up_locked(self, quote: StockQuote) -> bool:
        """
        判定是否全日鎖漲停（開高低收皆等於漲停價），此時放空無法回補

        判定式與成交價驗證共用 `TwStockSpec.is_locked_at_limit()`——
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
        quote: StockQuote,
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

            **餘額不足時不可硬轉**：不檢查就扣款會把餘額扣成負數
            （帳戶 10,000 元、當沖放空 500 元 × 1 張轉留倉後餘額是 −442,113），
            而之後的維持率追繳只看單一部位的擔保維持率、不看帳戶現金，
            負餘額會一路留著。現金不夠就是留不了倉，改依追繳政策處理。
        - Parameters:
            - position: StockPosition
                要轉為留倉的當沖空單
            - account: BaseAccount
                虛擬帳戶
            - date: datetime.date
                當前交易日（餘額不足強制回補時的成交日）
            - quote: StockQuote
                當日報價；餘額不足強制回補時以其收盤價成交，並據以檢查成交價區間
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
        close_price: float = quote.close
        if account.balance < required:
            event_counts["forced_cover_insufficient_margin"] += 1
            if self.margin_call_policy == MarginCallPolicy.FORCE_COVER:
                logger.warning(
                    f"[Day Trade Cover] {position.symbol} 轉融券留倉需 {required} 元，"
                    f"帳戶只有 {account.balance} 元，改以收盤價 {close_price} 強制回補"
                )
                self.force_cover_position(position, date, close_price, quote)
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
        quote: Optional[StockQuote],
    ) -> List[StockTradeRecord]:
        """
        - Description:
            以指定價格強制回補放空部位（當沖日終、維持率追繳、超過持有天數共用）
        - Parameters:
            - position: StockPosition
                要回補的放空部位
            - date: datetime.date
                成交日
            - price: float
                回補參考價（滑價前）
            - quote: Optional[StockQuote]
                當根 bar 的報價，供 `apply_fill_price()` 做區間檢查；
                **不給預設值**是刻意的：停牌無報價時要明確傳 `None`，
                而不是讓新增的呼叫端漏傳就靜默跳過檢查
        - Return:
            - List[StockTradeRecord]
                回補產生的交易紀錄
        """

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
        return self.position_manager.close_position(self.apply_fill_price(order, quote))

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
            無報價」對兩個方向都成立。只處理空單的話，做多跨除息收不到現金股利、
            跨配股張數不調整，帳面在除權息日憑空虧一段；而下市的股票會永遠留在
            帳上，以最後一個收盤價計算權益（存活者偏差），還持續佔用
            `max_holdings` 名額。
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

            **持有天數與計提天數都是曆日，不是 bar 數**：週五開的空單到週一
            只過了 1 根 bar，卻是 3 個曆日的借券費。逐根 bar `+= 1` 的話一年
            只計到 252 天，年化費率會低估約 31%（1 − 252/365）；`holding_days`
            在 `TradeRecord` 那邊指的也是曆日，兩處口徑必須一致。

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

            # 除權交易日當天開倉者不含權
            if TimeUtils.to_date(position.date) >= date:
                continue

            # 配股率未知（權息並存拆不出來）：不猜倍率，記 warning 與計數，
            # 讓報表看得見這筆部位的帳面少算了配股那一段
            if math.isnan(ratio):
                logger.warning(
                    f"[Corporate Action] {position.symbol} 於 {date} 除權但配股率未知，"
                    "股數未調整，帳面會低估配股的價值"
                )
                event_counts["share_adjustment_unknown"] = (
                    event_counts.get("share_adjustment_unknown", 0) + 1
                )
                continue

            if ratio <= 0 or ratio == 1.0:
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
            # 走到這裡代表連續無報價，`quote_map.get()` 幾乎必然是 None（見上）
            self.force_exit_long_position(
                position, date, price, quote_map.get(position.symbol)
            )

    def force_exit_long_position(
        self,
        position: StockPosition,
        date: datetime.date,
        price: float,
        quote: Optional[StockQuote],
    ) -> None:
        """以指定價格全量賣出做多部位（引擎強制出場用）；`quote` 供區間檢查，無報價時傳 None"""

        order: StockOrder = StockOrder(
            stock_id=position.symbol,
            date=date,
            action=Action.SELL,
            position_type=PositionType.LONG,
            price=price,
            volume=position.volume,
        )
        self.position_manager.close_long_position(
            position=position,
            stock_order=self.apply_fill_price(order, quote),
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
               約束，其跨除息日的成本改由 `settle_cash_dividend()` 反映；
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
            # 停牌／下市時取不到報價，`apply_fill_price()` 遇到 None 會跳過區間檢查
            quote: Optional[StockQuote] = quote_map.get(position.symbol)

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
                self.force_cover_position(position, date, price, quote)
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
                self.force_cover_position(position, date, price, quote)
                continue

            # 停券強制回補日（使用者指定 ＋ 除權息行事曆推導的融券最後回補日）
            if self.check_force_cover(date, position):
                logger.warning(
                    f"[Force Cover] {position.symbol} 於 {date} 停券，以 {price} 強制回補"
                )
                # 停券與持有天數到期是兩種成因，記到同一個桶會讓「策略設定的持有上限
                # 太短」與「標的停券」無法區分，兩者的因應方式完全不同
                event_counts["forced_cover_suspended"] += 1
                self.force_cover_position(position, date, price, quote)
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
            self.force_cover_position(position, date, price, quote)

    def update_no_quote_days(
        self,
        quote_map: Dict[str, StockQuote],
        positions: List[StockPosition],
    ) -> None:
        """
        - Description:
            更新每個部位的連續無報價天數

            有報價即歸零，無報價則累加。長期停牌或已下市的標的會持續累加，
            成為 `check_long_no_quote_exit()` 與 `check_margin_call()` 的出場依據。
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
