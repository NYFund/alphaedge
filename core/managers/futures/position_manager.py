import datetime
from dataclasses import dataclass
from typing import Optional, Union

from loguru import logger

from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.backtest.models.cost_model import FuturesCostConfig, TwFuturesCostModel
from core.managers.base.position_manager import BasePositionManager
from core.models import (
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesTradeRecord,
)
from core.utils import Action, MarginCallPolicy, PositionType
from core.utils.constant import FUTURES_MULTIPLIER

"""
FuturesPositionManager: 期貨部位管理（口數、保證金、逐日盯市）

**與 `StockPositionManager` 的三個根本差異**：

1. **開倉只凍結保證金，不買下契約價值**。股票買進是把錢換成股票；期貨開倉只從
   可動用餘額移出保證金，契約價值本身不動用資金。
2. **逐日盯市**。每個交易日以結算價結清當日損益，現金當天就進出帳戶，
   部位的 `price` 隨之重設為結算價。`settle_daily()` 是 `BasePositionManager`
   早就留好的掛點（股票側為 no-op）。
3. **沒有證交稅、沒有股數換算**。PnL = 價格變動 × 乘數 × 口數，方向由多空決定。
"""


@dataclass
class FuturesMarginConfig:
    """
    保證金設定：**預設查表，查不到才退回比率近似**

    真實的 TAIFEX 原始保證金是**每口固定金額**、依波動度每日計算、達門檻就調
    （TX 在 2015~2026 調整 62 次，間隔最短 2 天、最長 372 天），
    且**調整後溯及既往**——未沖銷部位一併適用。

    | 模式 | 何時生效 | 行為 |
    |------|----------|------|
    | 查表（預設） | `api` 已注入 | 查 `futures_margin_history`；**查不到就 raise** |
    | 比率近似 | `use_api=False`，或 `api` 尚未注入 | 契約價值 × `initial_margin_ratio` |

    **`api` 由 DataFeed 注入**（`TwFuturesDataFeed.setup()`），不是在此自行建立
    ——全專案的資料 API 一律由 DataFeed 統一持有並共用同一條連線。要在回測以外
    的地方用查表模式，明確傳 `api=` 或走 `from_api()`。

    **查不到為什麼要 raise 而不是退回比率**：理由同 `FUTURES_MULTIPLIER` 用 `[]`
    而非 `.get()`——靜默套一個近似值會讓資金效率與可開口數整段偏掉卻毫無徵兆，
    中斷比靜默錯誤好查。資料涵蓋 2020-03 起（更早的公告附件是掃描影像，
    取不到數值），更早的區間會當場中止。

    比率模式的近似**跨年份會系統性偏掉**（實測 2020 年 +143% 到 2026 年 −38%，
    而且會變號），故它只適合「跑通流程」而非產出可信績效。
    """

    initial_margin_ratio: float = 0.1  # 原始保證金佔契約價值的比率（比率模式使用）
    api: Optional[FuturesMarginAPI] = None  # 查表用；回測時由 DataFeed 注入
    # 查詢日早於表內所有列時是否退回最早一列。**預設關閉**：
    # 它無法區分「該商品從未被調整過」（退回正確）與「查詢日早於資料涵蓋範圍」
    # （退回錯誤），見 `FuturesMarginAPI` 的說明
    fallback_to_earliest: bool = False
    # 是否讓 DataFeed 注入保證金 API。**預設開啟**：保證金資料已備妥
    # （2020-03 起），用比率近似回測是刻意的降級，應由使用者明確表態
    use_api: bool = True

    # === 追繳 ===
    # 權益低於維持保證金時的處理；**強制平倉是預設**——真實帳戶不會讓部位
    # 在保證金不足的情況下續留，只標記會讓回測高估留倉能力
    margin_call_policy: MarginCallPolicy = MarginCallPolicy.FORCE_COVER
    # 追繳門檻的倍數：權益 < 維持保證金總額 × 本值即觸發。
    # 1.0 為交易所口徑，調高即為「比交易所更早出場」的自訂風控
    margin_call_ratio: float = 1.0

    @staticmethod
    def default() -> "FuturesMarginConfig":
        """預設設定：查表模式（API 由 DataFeed 注入），追繳採強制平倉"""

        return FuturesMarginConfig()

    @staticmethod
    def ratio(initial_margin_ratio: float = 0.1) -> "FuturesMarginConfig":
        """
        比率近似模式：**明確表態不查表**

        只適合「跑通流程」或回測 2020-03 之前的區間（該段沒有保證金資料）。
        """

        return FuturesMarginConfig(
            initial_margin_ratio=initial_margin_ratio, use_api=False
        )

    @staticmethod
    def from_api(
        api: FuturesMarginAPI,
        fallback_to_earliest: bool = False,
    ) -> "FuturesMarginConfig":
        """
        - Description:
            以既有的 `FuturesMarginAPI` 建立查表模式的設定

            **`api` 必須由呼叫端傳入**：舊版未指定時自行 `FuturesMarginAPI()`，暗中開了一條
            沒有人負責關閉的連線。API 的連線應與 DataFeed 共用、由 DataFeed 關閉；
            只要預設查表模式、不自己持有 API 的話用 `default()`，由 DataFeed 注入。
        - Parameters:
            - api: FuturesMarginAPI
                查保證金用的 API（通常共用 DataFeed 的連線）
            - fallback_to_earliest: bool
                查詢日早於表內所有列時是否退回最早一列
        - Return:
            - FuturesMarginConfig
                查表模式的設定
        """

        return FuturesMarginConfig(api=api, fallback_to_earliest=fallback_to_earliest)


class FuturesPositionManager(BasePositionManager):
    """Futures Position Manager"""

    def __init__(
        self,
        account: FuturesAccount,
        cost_model: Optional[TwFuturesCostModel] = None,
        margin_config: Optional[FuturesMarginConfig] = None,
    ) -> None:
        super().__init__(account)
        self.account: FuturesAccount = account
        # 成本一律走 CostModel（與 `StockPositionManager` 同一種接法）：
        # 本層只決定「什麼時候收」，費率與公式全在 model，兩處各算一份必然漂移
        self.cost_model: TwFuturesCostModel = cost_model or TwFuturesCostModel()
        self.margin_config: FuturesMarginConfig = (
            margin_config or FuturesMarginConfig.default()
        )

    @property
    def cost_config(self) -> FuturesCostConfig:
        """費率設定；唯一來源是 `cost_model.config`"""

        return self.cost_model.config

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Futures Position Manager"""
        pass

    # === 共用計算 ===
    @staticmethod
    def get_multiplier(product: str) -> int:
        """
        取得契約乘數

        **刻意用 `[]` 而非 `.get()`**：未登錄的商品讓它當場 KeyError，
        乘數猜錯不會有任何徵兆，只會讓整條 PnL 靜默偏掉（見 `FUTURES_MULTIPLIER`）。
        """

        return FUTURES_MULTIPLIER[product]

    def calculate_contract_value(
        self, price: float, volume: int, multiplier: int
    ) -> float:
        """契約價值 ＝ 價格 × 乘數 × 口數"""

        return price * multiplier * volume

    def calculate_margin(
        self,
        price: float,
        volume: int,
        multiplier: int,
        product: Optional[str] = None,
        date: Optional[Union[datetime.date, datetime.datetime]] = None,
    ) -> float:
        """
        - Description:
            計算應繳的原始保證金

            **帶了 `api` 就查表**（每口金額 × 口數），否則退回
            「契約價值 × 比率」的近似，見 `FuturesMarginConfig`。
        - Parameters:
            - price / volume / multiplier: float, int, int
                成交價、口數、契約乘數
            - product: Optional[str]
                契約代碼；查表模式必填
            - date: Optional[datetime.date]
                交易日；查表模式必填（保證金隨日期變動）
        - Return:
            - float
                應繳的原始保證金
        - Raises:
            - ValueError
                查表模式下查不到該商品在該日期的保證金
        """

        if self.margin_config.api is None:
            contract_value: float = self.calculate_contract_value(
                price, volume, multiplier
            )
            return contract_value * self.margin_config.initial_margin_ratio

        if product is None or date is None:
            raise ValueError("查表模式必須提供 product 與 date——保證金隨商品與日期變動")

        per_lot: Optional[int] = self.margin_config.api.get_initial_margin(
            product,
            self.normalize_date(date),
            fallback_to_earliest=self.margin_config.fallback_to_earliest,
        )
        if per_lot is None:
            covered = self.margin_config.api.get_covered_date_range(product)
            raise ValueError(
                f"查無 {product} 在 {date} 生效的保證金"
                f"（表內涵蓋 {covered}）。"
                f"**刻意不退回近似值**：靜默套一個比率會讓資金效率與可開口數"
                f"整段偏掉卻毫無徵兆。保證金資料只回溯到 2020-03，"
                f"要回測更早的期間請明確改用 `FuturesMarginConfig.ratio()`"
            )

        return float(per_lot * volume)

    def calculate_maintenance_margin(
        self,
        position: FuturesPosition,
        date: Optional[Union[datetime.date, datetime.datetime]] = None,
    ) -> float:
        """
        - Description:
            計算該部位當日的**維持保證金**總額（追繳門檻）

            **維持保證金與原始保證金是兩個獨立的公告值，不可用比率互推**
            （TX 2024-10-31：原始 338,000、維持 259,000，比值 0.766，
            但那個比值本身也會隨公告變動）。

            **沒有 API 時退回「已繳的原始保證金」當門檻**：那比實際的維持保證金
            嚴格（追繳會提早觸發），但總比靜默不做風控好——這種偏保守的替代值
            會讓回測低估留倉能力，方向上不會讓績效變好看。
        - Parameters:
            - position: FuturesPosition
                未平倉部位
            - date: Optional[datetime.date]
                查詢日；查表模式必填
        - Return:
            - float
                該部位的維持保證金總額
        """

        if self.margin_config.api is None or date is None:
            return position.margin

        per_lot: Optional[int] = self.margin_config.api.get_maintenance_margin(
            position.product,
            self.normalize_date(date),
            fallback_to_earliest=self.margin_config.fallback_to_earliest,
        )
        if per_lot is None:
            logger.warning(
                f"[Margin] 查無 {position.product} 在 {date} 生效的維持保證金，"
                f"本次以已繳原始保證金 {position.margin} 當追繳門檻（偏嚴格）"
            )
            return position.margin

        return float(per_lot * position.volume)

    def calculate_commission(self, volume: int, product: Optional[str] = None) -> float:
        """手續費：每口固定金額（可逐商品指定，見 `FuturesCostConfig`）"""

        return self.cost_model.commission(price=0.0, volume=volume, product=product)

    def calculate_tax(self, price: float, volume: int, multiplier: int) -> float:
        """期交稅：對契約價值課徵（**不是證交稅**，費率見 `FuturesCostConfig`）"""

        return self.cost_model.tax(price=price, volume=volume, multiplier=multiplier)

    def calculate_pnl(
        self,
        position_type: PositionType,
        entry_price: float,
        exit_price: float,
        volume: int,
        multiplier: int,
    ) -> float:
        """
        - Description:
            期貨損益：**價格變動 × 乘數 × 口數**，方向由多空決定

            這是本步驟的驗收公式；沒有股數換算、沒有證交稅。
        - Parameters:
            - position_type: PositionType
                部位方向
            - entry_price / exit_price: float
                進場價與出場價
            - volume: int
                口數
            - multiplier: int
                契約乘數（元／點）
        - Return:
            - float
                損益（未扣交易成本）
        """

        # **一律走成本模型**：同一條公式原本在這裡與
        # `FuturesCostModel.realized_pnl()` 各寫一份，兩邊都對只是巧合——
        # 哪天有人改了乘數或方向的處理，另一邊不會跟著改，也不會有測試失敗
        return self.cost_model.realized_pnl(
            entry_price=entry_price,
            exit_price=exit_price,
            volume=volume,
            multiplier=multiplier,
            position_type=position_type,
            transaction_cost=0.0,
        )

    @staticmethod
    def normalize_date(
        date: Union[datetime.date, datetime.datetime],
    ) -> Optional[datetime.date]:
        """Tick 級別的 date 會是 datetime，統一取其日期部分以計算持有曆日數"""

        if date is None:
            return None
        return date.date() if isinstance(date, datetime.datetime) else date

    def calculate_holding_days(
        self,
        entry_date: Union[datetime.date, datetime.datetime],
        exit_date: Union[datetime.date, datetime.datetime],
    ) -> int:
        """計算持有曆日數（非交易日），同日開平倉為 0 天"""

        entry: Optional[datetime.date] = self.normalize_date(entry_date)
        exit_: Optional[datetime.date] = self.normalize_date(exit_date)
        if entry is None or exit_ is None:
            return 0
        return (exit_ - entry).days

    # === 開倉 ===
    def open_position(self, order: FuturesOrder) -> Optional[FuturesPosition]:
        """
        - Description:
            開倉（多空皆走同一條路徑）

            **與股票不同，不檢查「餘額是否足以買下契約價值」**，而是檢查
            「可動用餘額是否足以繳出保證金與交易成本」。

            **同一契約不允許雙向持倉**：已有反向未平倉部位即拒單，與股票端
            同一判準。跨月份的價差部位不受此限。
        - Parameters:
            - order: FuturesOrder
                目標契約的訂單資訊
        - Return:
            - Optional[FuturesPosition]
                開倉成功的部位；保證金不足時為 None
        """

        # 開倉動作與方向必須一致：多單買進開倉、空單賣出開倉
        is_long_open: bool = (
            order.position_type == PositionType.LONG and order.action == Action.BUY
        )
        is_short_open: bool = (
            order.position_type == PositionType.SHORT and order.action == Action.SELL
        )
        if not (is_long_open or is_short_open):
            logger.warning(
                f"[Open Position] 方向與動作不一致：{order.contract_id} "
                f"{order.position_type} / {order.action}"
            )
            return None

        # 同一契約不允許同時持有反向部位（判準沿用股票端「同標的不得雙向持倉，反之亦然」，
        # 股票端已補上對稱檢查）。**期貨端漏了這條**：
        # 同契約多空並存時，`get_open_lots()` 把兩邊相抵成淨口數（＝曝險為 0），
        # 但 `margin_used` 卻各佔一份原始保證金——帳上「沒有曝險卻押著兩份保證金」，
        # 而交易所對沖部位只收單邊，可開口數因此被系統性低估。
        # 擋在開倉端而不是去模擬保證金減收：TAIFEX 的**價差部位保證金**是另一套
        # 費率表，本專案沒有該資料源，
        # 硬寫一個比率只是用一個猜測換掉另一個猜測。
        opposite_type: PositionType = (
            PositionType.SHORT
            if order.position_type == PositionType.LONG
            else PositionType.LONG
        )
        if self.account.check_has_position(order.symbol, opposite_type):
            logger.warning(
                f"[Open Position] {order.contract_id} 已有 {opposite_type.value} 部位，"
                f"不允許同契約雙向持倉，拒絕開倉"
            )
            return None

        multiplier: int = self.get_multiplier(order.product)
        margin: float = self.calculate_margin(
            order.price,
            order.volume,
            multiplier,
            product=order.product,
            date=order.date,
        )
        commission: float = self.calculate_commission(order.volume, order.product)
        tax: float = self.calculate_tax(order.price, order.volume, multiplier)
        open_cost: float = commission + tax

        if self.account.balance < margin + open_cost:
            logger.warning(
                f"[Open Position] 可動用餘額不足：{order.contract_id} "
                f"需要 {margin + open_cost:.0f}，實際 {self.account.balance:.0f}"
            )
            return None

        logger.info(
            f"* Open {order.position_type.value} Position: "
            f"{order.contract_id} ({order.volume} lots)"
        )

        position: FuturesPosition = FuturesPosition(
            id=self.account.generate_trade_id(),
            product=order.product,
            expiry=order.expiry,
            is_closed=False,
            position_type=order.position_type,
            date=order.date,
            price=order.price,
            volume=order.volume,
            commission=commission,
            tax=tax,
            transaction_cost=open_cost,
            unrealized_pnl=0.0,
            unrealized_roi=0.0,
            multiplier=multiplier,
            margin=margin,
        )

        # 保證金由可動用餘額移入佔用，交易成本則直接支出
        self.account.balance -= margin + open_cost
        self.account.margin_used += margin
        self.account.positions.append(position)

        return position

    # === 逐日盯市 ===
    def settle_daily(self, position: FuturesPosition, settle_price: float) -> None:
        """
        - Description:
            以結算價結清當日損益（逐日盯市）

            **這是期貨與股票最根本的記帳差異**：損益當天就進出帳戶，不等到平倉。
            結算後 `position.price` 重設為結算價，下一日的結算才不會重複計算同一段。
            累計金額記在 `position.settled_pnl`，平倉時併入交易紀錄的總損益。

            `settle_price` 為 None（夜盤沒有結算價）時**不做任何事**，
            不可當成 0 —— 那會讓部位在一天內被結算成歸零。
        - Parameters:
            - position: FuturesPosition
                待結算的部位
            - settle_price: float
                當日結算價
        """

        if settle_price is None or position.is_closed:
            return

        daily_pnl: float = self.calculate_pnl(
            position_type=position.position_type,
            entry_price=position.price,
            exit_price=settle_price,
            volume=position.volume,
            multiplier=position.multiplier,
        )

        position.settled_pnl += daily_pnl
        position.price = settle_price
        position.unrealized_pnl = position.settled_pnl

        self.account.balance += daily_pnl

    # === 平倉 ===
    def close_single_position(
        self,
        position: FuturesPosition,
        order: FuturesOrder,
        close_volume: int,
    ) -> Optional[FuturesTradeRecord]:
        """
        - Description:
            平掉單一部位的指定口數；FIFO 拆單主幹在 `BasePositionManager`

            **損益由兩段組成**：平倉前逐日盯市已結算的 `settled_pnl`（依平倉口數
            等比例攤提）＋ 最後一段（最近結算價 → 平倉價）。只算後者會漏掉前面
            所有交易日的損益。
        - Parameters:
            - position: FuturesPosition
                被平倉的部位
            - order: FuturesOrder
                平倉訂單
            - close_volume: int
                本次平倉口數
        - Return:
            - Optional[FuturesTradeRecord]
                本次平倉產生的交易紀錄
        """

        logger.info(
            f"* Close {position.position_type.value} Position: "
            f"{position.contract_id} ({close_volume} lots)"
        )

        close_ratio: float = close_volume / position.volume

        # 最後一段：最近一次結算價（未結算過則為開倉價）→ 平倉價
        final_leg_pnl: float = self.calculate_pnl(
            position_type=position.position_type,
            entry_price=position.price,
            exit_price=order.price,
            volume=close_volume,
            multiplier=position.multiplier,
        )
        # 先前各段：依平倉口數等比例攤提
        settled_pnl: float = position.settled_pnl * close_ratio

        # 開倉成本依平倉口數等比例攤提（與股票側同一套做法）
        proportional_open_commission: float = position.commission * close_ratio
        proportional_open_tax: float = position.tax * close_ratio
        close_commission: float = self.calculate_commission(
            close_volume, position.product
        )
        close_tax: float = self.calculate_tax(
            order.price, close_volume, position.multiplier
        )
        total_transaction_cost: float = (
            proportional_open_commission
            + proportional_open_tax
            + close_commission
            + close_tax
        )

        realized_pnl: float = settled_pnl + final_leg_pnl - total_transaction_cost
        released_margin: float = position.margin * close_ratio

        # LONG 是先買後賣、SHORT 是先賣後買
        is_long: bool = position.position_type == PositionType.LONG
        record: FuturesTradeRecord = FuturesTradeRecord(
            id=position.id,
            product=position.product,
            expiry=position.expiry,
            is_closed=True,
            position_type=position.position_type,
            buy_date=position.date if is_long else order.date,
            buy_price=position.entry_price if is_long else order.price,
            buy_volume=close_volume,
            sell_date=order.date if is_long else position.date,
            sell_price=order.price if is_long else position.entry_price,
            sell_volume=close_volume,
            commission=proportional_open_commission + close_commission,
            tax=proportional_open_tax + close_tax,
            transaction_cost=total_transaction_cost,
            realized_pnl=realized_pnl,
            roi=self.calculate_roi(realized_pnl, released_margin),
            multiplier=position.multiplier,
            margin=released_margin,
            settled_pnl=settled_pnl,
            holding_days=self.calculate_holding_days(position.date, order.date),
        )

        # Update position（等比例攤提後扣減）
        position.volume -= close_volume
        position.commission -= proportional_open_commission
        position.tax -= proportional_open_tax
        position.transaction_cost -= (
            proportional_open_commission + proportional_open_tax
        )
        position.settled_pnl -= settled_pnl
        position.margin -= released_margin
        if position.volume == 0:
            position.is_closed = True

        # Update account：釋回保證金、最後一段損益進帳、扣平倉成本
        self.account.margin_used -= released_margin
        self.account.balance += (
            released_margin + final_leg_pnl - (close_commission + close_tax)
        )
        self.account.realized_pnl += realized_pnl
        self.account.trade_records.append(record)

        return record

    @staticmethod
    def calculate_roi(realized_pnl: float, margin: float) -> float:
        """
        報酬率以**保證金**為分母，不是契約價值

        期貨是保證金交易，投入的資金就是保證金；用契約價值當分母會把槓桿效果抹掉。
        保證金為 0（例如比率設成 0）時回傳 0，不拋除零錯誤。
        """

        if margin == 0:
            return 0.0
        return round(realized_pnl / margin * 100, 2)
