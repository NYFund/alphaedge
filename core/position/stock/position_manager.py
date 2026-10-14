import datetime
from typing import Dict, Optional

from loguru import logger

from core.market.tw.cost_model import TwStockCostModel
from core.models import StockAccount, StockOrder, StockPosition, StockTradeRecord
from core.models.cost_config import CostConfig
from core.portfolio import order_rules
from core.position.base.position_manager import BasePositionManager
from core.utils import Action, PositionType, ShortMethod, TimeUtils, Units
from core.utils.instrument import StockUtils

"""
台股部位管理：多空記帳、借券費攤提與逐日結算

- Features:
    1. 開倉（LONG／SHORT）與平倉的記帳
    2. 逐日結算與借券費累計
    3. 部位市值計算
- 使用場景:
    **多空的記帳差異全部收斂在本檔**，費用與損益一律走 `TwStockCostModel`——
    本層只決定「什麼時候收」，費率與公式全在 model。兩處各算一份必然漂移，
    而成本算錯不會有任何徵兆，只會讓整條 PnL 靜默偏掉。
"""


class StockPositionManager(BasePositionManager):
    """
    Stock Position Manager

    多空的記帳差異全部收斂在此，兩邊的費用與損益一律走 `TwStockCostModel`：
    - LONG：手續費、賣出端證交稅、開倉成本等比例攤提
    - SHORT：另含保證金、借券費、融券利息、股利補償與當沖稅率
    """

    def __init__(
        self,
        account: StockAccount,
        cost_model: Optional[TwStockCostModel] = None,
        event_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        super().__init__(account, event_counts)
        self.cost_model: TwStockCostModel = cost_model or TwStockCostModel(
            CostConfig.default()
        )

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Stock Position Manager"""

        pass

    def get_position_unit_size(self, position: StockPosition) -> int:
        """台股的計價單位是「股」（1 張 ＝ 1,000 股）"""

        return StockUtils.convert_lot_to_share(1)

    def _book_open(self, stock_order: StockOrder) -> Optional[StockPosition]:
        """
        - Description:
            開倉入帳（多空分流）：做多付出整筆部位價值，放空只扣保證金與開倉成本

            方向與動作一致、同標的無反向部位，已由 `BasePositionManager.open_position()`
            擋過，這裡不再檢查。
        - Parameters:
            - stock_order: StockOrder
                目標股票的訂單資訊
        - Return:
            - Optional[StockPosition]
                入帳的部位；餘額不足或曝險超限時為 None
        """

        position_value: float = self.calculate_position_value(
            price=stock_order.price,
            volume=stock_order.volume,
        )

        # 放空開倉（賣出）
        if stock_order.position_type == PositionType.SHORT:
            logger.info(
                f"* Open Short Position: {stock_order.stock_id} ({stock_order.volume} lots)"
            )
            return self.open_short_position(stock_order, position_value)

        # 做多開倉（買進）
        logger.info(
            f"* Open Long Position: {stock_order.stock_id} ({stock_order.volume} lots)"
        )

        open_commission: int = self.cost_model.commission(
            price=stock_order.price,
            volume=stock_order.volume,
        )
        # 做多的證交稅課在賣出端，開倉恆為 0（由成本模型依 action 判定）
        open_tax: int = self.cost_model.tax(
            price=stock_order.price,
            volume=stock_order.volume,
            action=Action.BUY,
            date=TimeUtils.to_date(stock_order.date),
        )
        open_cost: int = open_commission + open_tax

        # 做多要付出整筆部位價值，故以「部位價值 ＋ 開倉成本」檢查餘額
        if self.account.balance < position_value + open_cost:
            # **餘額不足一定要留下警告與計數**：只回 None 的話引擎會靜默跳過，
            # 回測結果看起來只是少一筆交易。開了滑價後成交價會高於 sizer
            # 估算的參考價，這條路徑相當容易被觸發，不記錄就查不出成交數變少的原因
            logger.warning(
                f"[Open Long] {stock_order.stock_id} 餘額不足："
                f"需要 {position_value + open_cost}，"
                f"可用 {self.account.balance}，拒絕開倉"
            )
            self.count_event("rejected_insufficient_balance")
            return None

        logger.info(f"* Place Open Order: {stock_order.stock_id}")

        position: StockPosition = StockPosition(
            id=self.account.generate_trade_id(),
            stock_id=stock_order.stock_id,
            is_closed=False,
            position_type=stock_order.position_type,
            date=stock_order.date,
            price=stock_order.price,
            volume=stock_order.volume,
            commission=open_commission,
            tax=open_tax,
            transaction_cost=open_cost,
            unrealized_pnl=0,
            unrealized_roi=0,
        )

        self.account.balance -= position_value + open_cost
        self.account.positions.append(position)

        return position

    def open_short_position(
        self,
        stock_order: StockOrder,
        position_value: float,
    ) -> Optional[StockPosition]:
        """
        - Description:
            開立放空部位（賣出）

            資金規則：賣出價款不計入可用餘額，先留作擔保品記在 position.short_proceeds，
            待回補平倉時才一次結算損益到 account.balance；帳戶當下只扣「保證金 + 開倉成本」。
            當沖（DAY_TRADE）不需保證金，僅扣開倉成本。
        - Parameters:
            - stock_order: StockOrder
                目標股票的訂單資訊
            - position_value: float
                開倉部位價值（賣出價金）
        - Return:
            - position: Optional[StockPosition]
                開倉成功回傳部位；保證金付不出（`rejected_insufficient_balance`）或
                曝險超限（`rejected_short_exposure`）時計數並回傳 None
        """

        short_method: ShortMethod = (
            stock_order.short_method or self.cost_model.config.short_method
        )

        open_commission: int = self.cost_model.commission(
            price=stock_order.price, volume=stock_order.volume
        )
        open_tax: int = self.cost_model.tax(
            price=stock_order.price,
            volume=stock_order.volume,
            action=Action.SELL,
            is_day_trade=stock_order.is_day_trade,
            date=TimeUtils.to_date(stock_order.date),
        )
        borrow_fee: int = self.cost_model.borrow_fee(
            price=stock_order.price,
            volume=stock_order.volume,
            short_method=short_method,
        )
        margin: int = self.cost_model.margin_required(
            price=stock_order.price,
            volume=stock_order.volume,
            short_method=short_method,
        )
        open_cost: int = open_commission + open_tax + borrow_fee

        # 放空不動用賣出價款，帳戶當下只需付得出「保證金 ＋ 開倉成本」
        if self.account.balance < margin + open_cost:
            logger.warning(
                f"[Open Short] {stock_order.stock_id} 餘額不足："
                f"需要 {margin + open_cost}，可用 {self.account.balance}，拒絕開倉"
            )
            # 與做多共用同一個 key：只回 None 的話引擎會靜默跳過，報表上只是少一筆交易
            self.count_event("rejected_insufficient_balance")
            return None

        # 單一標的曝險上限。**公式與實盤共用**
        # （`order_rules.exceeds_symbol_exposure()`），但回測只在 SHORT 分支
        # 取用、超限整筆拒絕，做多不受此限；兩邊口徑的差異寫在該函式的 docstring
        max_ratio: Optional[float] = (
            self.cost_model.config.short_constraint.max_short_exposure_ratio
        )
        if order_rules.exceeds_symbol_exposure(
            position_value, self.account.init_capital, max_ratio
        ):
            # `max_ratio` 必為非 None（否則上面那個判斷不會成立）
            cap: float = self.account.init_capital * float(max_ratio)
            logger.warning(
                f"[Open Short] {stock_order.stock_id} 曝險 {position_value} "
                f"超過上限 {cap}，拒絕開倉"
            )
            self.count_event("rejected_short_exposure")
            return None

        logger.info(f"* Place Open Order: {stock_order.stock_id}")

        position: StockPosition = StockPosition(
            id=self.account.generate_trade_id(),
            stock_id=stock_order.stock_id,
            is_closed=False,
            position_type=stock_order.position_type,
            date=stock_order.date,
            price=stock_order.price,
            volume=stock_order.volume,
            commission=open_commission,
            tax=open_tax,
            transaction_cost=open_cost,
            unrealized_pnl=0,
            unrealized_roi=0,
            short_method=short_method,
            is_day_trade=stock_order.is_day_trade,
            margin=margin,
            short_proceeds=position_value,
            borrow_fee=borrow_fee,
        )

        self.account.balance -= margin + open_cost
        self.account.margin_used += margin
        self.account.positions.append(position)

        return position

    def close_single_position(
        self,
        position: StockPosition,
        stock_order: StockOrder,
        close_volume: int,
    ) -> StockTradeRecord:
        """依部位方向分派記帳；FIFO 拆單主幹在 BasePositionManager"""

        # 平倉腿的滑價同樣要計入；部分平倉以該次的張數為準
        self.accrue_slippage_cost(
            stock_order, close_volume, StockUtils.convert_lot_to_share(1)
        )

        if position.position_type == PositionType.SHORT:
            logger.info(
                f"* Close Short Position: {position.stock_id} ({position.volume} lots)"
            )
            return self.close_short_position(position, stock_order, close_volume)

        logger.info(
            f"* Close Long Position: {position.stock_id} ({position.volume} lots)"
        )
        return self.close_long_position(position, stock_order, close_volume)

    def settle_daily(self, position: StockPosition, settle_price: float) -> None:
        """股票沒有每日結算：損益於平倉才實現，故為 no-op（期貨的掛點）"""

        pass

    def close_long_position(
        self,
        position: StockPosition,
        stock_order: StockOrder,
        close_volume: int,
    ) -> StockTradeRecord:
        """平掉做多部位（賣出），支援部分平倉的等比例攤提"""

        position_value: float = self.calculate_position_value(
            price=stock_order.price,
            volume=close_volume,
        )

        sell_commission: int = self.cost_model.commission(
            price=stock_order.price,
            volume=close_volume,
        )
        # **當沖與否要看實際成交日，不能只看設定值**：`tax()` 遇 `None` 會取
        # `config.is_day_trade`，而那是策略層的開關——放空策略開了當沖、又允許
        # 做多時，做多部位**隔夜**賣出也會吃到減半稅。減半的條件是兩者都成立：
        # 策略確實開了當沖（現股當沖要事先簽署同意書），且開平倉同一天
        is_day_trade: bool = self.cost_model.config.is_day_trade and (
            TimeUtils.to_date(position.date) == TimeUtils.to_date(stock_order.date)
        )
        sell_tax: int = self.cost_model.tax(
            price=stock_order.price,
            volume=close_volume,
            action=Action.SELL,
            is_day_trade=is_day_trade,
            date=TimeUtils.to_date(stock_order.date),
        )

        # 開倉成本依平倉張數等比例攤提。
        # **不可改為「以平倉張數重算手續費」**：那會讓最低手續費在部分平倉時被重複套用，
        # 使同一筆交易的 record.commission 與 realized_pnl 用到兩個不同的開倉手續費
        # （`StockUtils` 那條公式即有此瑕疵，差異量化見
        # `tests/backtest/compare_cost_formula.py`）
        proportional_buy_commission: int = int(
            position.commission * (close_volume / position.volume)
        )
        total_transaction_cost: int = (
            proportional_buy_commission + sell_commission + sell_tax
        )

        # 損益與報酬率都走成本模型基底的同一條公式：台股的計價單位是每張股數，
        # 名目報酬率的分母是開倉價金 ＋ 開倉成本
        realized_pnl: float = self.cost_model.realized_pnl(
            position_type=PositionType.LONG,
            entry_price=position.price,
            exit_price=stock_order.price,
            volume=close_volume,
            unit_size=Units.LOT,
            cost=proportional_buy_commission + sell_commission + sell_tax,
        )
        roi: float = self.cost_model.roi(
            realized_pnl,
            self.cost_model.notional_capital(
                position.price, close_volume, proportional_buy_commission
            ),
        )

        record: StockTradeRecord = StockTradeRecord(
            id=position.id,
            stock_id=position.stock_id,
            is_closed=True,
            position_type=position.position_type,
            buy_date=position.date,
            buy_price=position.price,
            buy_volume=close_volume,
            sell_date=stock_order.date,
            sell_price=stock_order.price,
            sell_volume=close_volume,
            commission=proportional_buy_commission + sell_commission,
            tax=sell_tax,
            transaction_cost=total_transaction_cost,
            realized_pnl=realized_pnl,
            roi=roi,
            # 多單的佔用資金就是名目價金，資金效率與名目報酬率相同；
            # 原本漏傳，交易明細的 `ROI on Capital` 對多單一直是 0
            roi_on_capital=roi,
        )

        position.volume -= close_volume
        position.commission -= proportional_buy_commission
        position.transaction_cost -= proportional_buy_commission
        if position.volume == 0:
            position.is_closed = True

        self.account.balance += position_value - (sell_commission + sell_tax)
        self.account.realized_pnl += record.realized_pnl
        self.account.trade_records.append(record)

        return record

    def accrue_final_borrow_fee(
        self,
        position: StockPosition,
        stock_order: StockOrder,
    ) -> None:
        """
        - Description:
            回補前補計提「上次計提日 → 回補日」的 SBL 借券費

            `SettlementModel.accrue_holding_cost()` 在每根 bar **收盤後**計提，
            而回補發生在盤中——回補當日那根 bar 收盤時部位已經平掉，於是
            最後一段（通常 1 個曆日，跨週末則 3 天）永遠不會被計提。

            補在這裡而不是 settlement，是因為只有平倉路徑知道「借券到哪一天為止」。
            計費價沿用回補價，與 SBL「以計費當日價格計費」的口徑一致。
        - Parameters:
            - position: StockPosition
                被回補的放空部位
            - stock_order: StockOrder
                回補訂單
        """

        if position.short_method != ShortMethod.SBL:
            return

        close_date: Optional[datetime.date] = TimeUtils.to_date(stock_order.date)
        open_date: Optional[datetime.date] = TimeUtils.to_date(position.date)
        if close_date is None or open_date is None:
            return

        last_accrual: datetime.date = position.last_accrual_date or open_date
        elapsed_days: int = (close_date - last_accrual).days
        if elapsed_days <= 0:
            return

        position.last_accrual_date = close_date
        position.accrued_borrow_fee += self.cost_model.borrow_fee(
            price=stock_order.price,
            volume=position.volume,
            holding_days=elapsed_days,
            short_method=ShortMethod.SBL,
        )

    def close_short_position(
        self,
        position: StockPosition,
        stock_order: StockOrder,
        close_volume: int,
    ) -> StockTradeRecord:
        """
        - Description:
            回補放空部位（買進），支援部分回補的等比例攤提

            保證金、擔保價款、借券費與股利補償一律依回補張數比例攤提；
            融券利息於此依 exit_date − entry_date 一次算出，
            不由 accrue_holding_cost() 重複計算。
        - Parameters:
            - position: StockPosition
                被回補的放空部位
            - stock_order: StockOrder
                回補訂單
            - close_volume: int
                本次回補張數（Unit: Lots）
        - Return:
            - record: StockTradeRecord
                本次回補產生的交易紀錄
        """

        # 先補計提「上次計提日 → 回補日」那段借券費，再依張數攤提
        self.accrue_final_borrow_fee(position, stock_order)

        # 依張數比例攤提開倉時的各項成本與擔保品
        ratio: float = close_volume / position.volume
        prop_margin: float = round(position.margin * ratio, 2)
        prop_proceeds: float = round(position.short_proceeds * ratio, 2)
        prop_open_commission: int = int(position.commission * ratio)
        prop_open_tax: int = int(position.tax * ratio)
        prop_borrow_fee: int = int(position.borrow_fee * ratio)
        prop_accrued_borrow_fee: int = int(position.accrued_borrow_fee * ratio)
        prop_dividend_compensation: int = int(position.dividend_compensation * ratio)

        # 買進回補不課證交稅，只有手續費
        close_commission: int = self.cost_model.commission(
            price=stock_order.price, volume=close_volume
        )

        # 融券保證金利息（收入）
        holding_days: int = self.calculate_holding_days(position.date, stock_order.date)
        interest: int = self.cost_model.short_interest(
            proceeds=prop_proceeds,
            margin=prop_margin,
            holding_days=holding_days,
            short_method=position.short_method,
        )

        # 開倉成本、平倉成本與持有期間淨成本（借券費支出 + 股利補償 − 利息收入）
        entry_cost: int = prop_open_commission + prop_open_tax + prop_borrow_fee
        carry_cost: int = (
            prop_accrued_borrow_fee + prop_dividend_compensation - interest
        )

        # 損益與報酬率都走成本模型基底的同一條公式；分母由台股的成本模型決定：
        # 名目報酬率用開倉價金 ＋ 開倉成本，資金效率用實際佔用的資金（保證金 ＋ 開倉成本）
        realized_pnl: float = self.cost_model.realized_pnl(
            position_type=PositionType.SHORT,
            entry_price=position.price,
            exit_price=stock_order.price,
            volume=close_volume,
            unit_size=Units.LOT,
            cost=entry_cost + close_commission + carry_cost,
        )
        roi: float = self.cost_model.roi(
            realized_pnl,
            self.cost_model.notional_capital(position.price, close_volume, entry_cost),
        )
        roi_on_capital: float = self.cost_model.roi(
            realized_pnl,
            self.cost_model.occupied_capital(
                PositionType.SHORT,
                position.price,
                close_volume,
                entry_cost,
                margin=prop_margin,
            ),
        )

        # sell_* 記的是放空開倉那一腿，buy_* 才是回補
        record: StockTradeRecord = StockTradeRecord(
            id=position.id,
            stock_id=position.stock_id,
            is_closed=True,
            position_type=position.position_type,
            sell_date=position.date,
            sell_price=position.price,
            sell_volume=close_volume,
            buy_date=stock_order.date,
            buy_price=stock_order.price,
            buy_volume=close_volume,
            commission=prop_open_commission + close_commission,
            tax=prop_open_tax,
            transaction_cost=entry_cost
            + close_commission
            + prop_accrued_borrow_fee
            + prop_dividend_compensation
            - interest,
            realized_pnl=realized_pnl,
            roi=roi,
            roi_on_capital=roi_on_capital,
            short_method=position.short_method,
            borrow_fee=prop_borrow_fee + prop_accrued_borrow_fee,
            interest=interest,
            dividend_compensation=prop_dividend_compensation,
            margin=prop_margin,
            holding_days=holding_days,
        )

        position.volume -= close_volume
        position.commission -= prop_open_commission
        position.tax -= prop_open_tax
        position.borrow_fee -= prop_borrow_fee
        position.accrued_borrow_fee -= prop_accrued_borrow_fee
        position.dividend_compensation -= prop_dividend_compensation
        position.transaction_cost -= entry_cost
        position.margin -= prop_margin
        position.short_proceeds -= prop_proceeds
        if position.volume == 0:
            position.is_closed = True

        # Update account：釋回保證金並結算損益
        # realized_pnl 已扣掉開倉成本，而開倉成本在開倉當下就從餘額扣過了，
        # 故此處加回 entry_cost，避免同一筆成本被扣兩次（與 LONG 的現金流口徑一致）。
        # 股利補償同理：除息當日就已從餘額扣款（`settle_cash_dividend()`，
        # 是否補償由 `CostConfig.compensate_cash_dividend` 決定），
        # 這裡加回攤提進 realized_pnl 的那一份。
        # **與 accrued_borrow_fee 的差異**：借券費不動餘額、只在平倉時經損益扣一次；
        # 股利補償走即時扣款，除息日的價格跳空才會被同額的現金流出抵銷，
        # 逐日權益曲線不會憑空多出一段假獲利
        self.account.balance += (
            prop_margin + realized_pnl + entry_cost + prop_dividend_compensation
        )
        self.account.margin_used -= prop_margin
        self.account.realized_pnl += realized_pnl
        self.account.trade_records.append(record)

        return record

    def calculate_position_value(self, price: float, volume: int) -> float:
        """
        - Description: 計算股票部位價值
        - Parameters:
            - price: float
                股票價格
            - volume: int
                股票張數
        - Return:
            - position_value: float
                股票部位價值（Unit: 元）
        """

        return price * StockUtils.convert_lot_to_share(volume)
