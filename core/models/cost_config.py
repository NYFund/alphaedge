import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.utils import (
    DAYS_PER_YEAR,
    Commission,
    FuturesCost,
    MarginCost,
    ShortCost,
    ShortMethod,
)

"""
成本設定：一次回測（或一次實盤）固定的手續費、稅、借券與保證金參數

**與成本模型本體分開**：`StockCostModel`／`TwFuturesCostModel` 是回測引擎的可插拔
model，而這三個 dataclass 只是參數容器——策略層、部位管理層與實盤組裝層都要建一份。
放在 `core/backtest/models/` 的話，`core/position/` 為了拿一個 dataclass 就得
import 回測套件，那是部位管理層不該有的方向。

三個類別**刻意不合併**：`ShortConstraint` 是「可不可以放空」的市場限制，
`CostConfig` 是台股的費率，`FuturesCostConfig` 是期貨的費率，而股票與期貨
沒有一個欄位可以共用（期交稅買賣各課一次、稅基是契約價值，與證交稅不同）。
"""


@dataclass
class ShortConstraint:
    """
    放空的可成交限制；全部可選，預設全關（關閉時不改動任何訂單）

    呼叫端：
    - `check_borrowable` → `TwStockFillModel.check_short_borrowable()`（融券餘額）
    - `check_short_sale_list`、`check_day_trade_list` → `TwStockFillModel.check_trading_lists()`
      （交易所每日公告的兩份名單）
    - `force_cover_dates`、`auto_force_cover_on_ex_dividend` → `TwStockSettlementModel.check_force_cover()`
    """

    # 依「平盤下得融（借）券賣出名單」檢核融券／借券放空開倉：
    # 名單外不得融券、被註記暫停者不得融券或借券、低於平盤須在名單內且未被註記跌停禁止。
    # **開啟時回測起日不得早於名單起點、區間內不得缺日**，否則拒絕執行（見資料源的
    # `ensure_trading_list_coverage()`）——缺資料不可以默默變成「不限制」
    check_short_sale_list: bool = False
    # 依「現股當沖標的名單」檢核現股當沖放空（先賣後買）；起日不得早於先賣後買開放日
    check_day_trade_list: bool = False
    check_borrowable: bool = False  # 是否檢核券源（由 FillModel 依融券今日餘額檢核）
    force_cover_dates: Optional[Dict[str, List[datetime.date]]] = None  # 停券強制回補日
    # 是否由除權息行事曆自動推導融券最後回補日（**預設開啟**：這是融券制度的規則，
    # 不是可選功能；關掉等於回到「留倉放空不受停券影響」的高估假設）
    auto_force_cover_on_ex_dividend: bool = True
    # 單一空單曝險上限（佔初始本金比例）；`None` ＝ 不限制。
    #
    # **實盤有一份對應的設定**：`RiskConfig.single_symbol_exposure_ratio`，
    # 預設 0.35、多空皆適用、超限是批次截斷而非整筆拒絕。
    # 公式兩邊共用（`order_preprocess.exceeds_symbol_exposure()`），四個面向的
    # 差異寫在那支函式的 docstring——**改這一邊之前先看那份對照表**，
    # 否則會讓回測跑得過的部位規模在實盤被截掉（或反過來）而查不出原因。
    max_short_exposure_ratio: Optional[float] = None

    def get_force_cover_dates(self, stock_id: str) -> List[datetime.date]:
        """
        取得使用者**手動指定**的強制回補日

        由除權息行事曆自動推導的融券最後回補日不在此列，兩者的適用範圍不同
        （見 `TwStockSettlementModel.check_force_cover()`）
        """

        if self.force_cover_dates is None:
            return []

        return self.force_cover_dates.get(stock_id, [])


@dataclass
class CostConfig:
    """一次回測固定的成本參數；由策略提供或使用 default() 的市場常見值"""

    # 手續費
    comm_rate: float = float(Commission.CommRate)
    comm_discount: float = float(Commission.Discount)
    min_fee: int = int(Commission.MinFee)

    # 證交稅
    tax_rate: float = float(Commission.TaxRate)
    day_trade_tax_rate: float = float(Commission.DayTradeTaxRate)

    # 放空管道與其成本
    short_method: ShortMethod = ShortMethod.MARGIN
    is_day_trade: bool = False
    margin_rate: float = float(ShortCost.MarginRate)
    borrow_fee_rate: float = float(
        ShortCost.MarginBorrowFeeRate
    )  # MARGIN 一次性／SBL 年化
    interest_rate: float = float(ShortCost.MarginInterestRate)
    maintenance_ratio: float = float(ShortCost.MaintenanceRatio)

    # 融資（做多槓桿）：目前僅保留參數，尚未有任何路徑取用
    financing_rate: float = float(MarginCost.FinancingRate)

    days_per_year: int = DAYS_PER_YEAR

    # 除息日是否對留倉空單扣股利補償（**預設開啟**：放空者補償出借方當期現金股利
    # 是實際發生的現金流，不計會系統性高估長天期放空的績效）
    compensate_cash_dividend: bool = True

    # 可成交限制
    short_constraint: ShortConstraint = field(default_factory=ShortConstraint)

    # 回測區間（由 factory 依策略注入）。**只用於稅制邊界的警示**，不參與任何計算；
    # 拿不到時一律靜默，見 `StockCostModel.check_day_trade_tax_expiry()`
    backtest_start_date: Optional[datetime.date] = None
    backtest_end_date: Optional[datetime.date] = None

    @classmethod
    def default(
        cls,
        short_method: ShortMethod = ShortMethod.MARGIN,
        is_day_trade: bool = False,
    ) -> "CostConfig":
        """依放空管道與是否當沖，組出市場常見值的成本設定"""

        # 當沖一律走現股當沖沖賣。
        # 費率不可歸零：當沖單漲停無法回補時會轉為融券留倉，
        # 屆時仍需以正常的保證金成數與券費率計算，歸零會讓維持率永遠不足而誤觸斷頭。
        if is_day_trade:
            return cls(short_method=ShortMethod.DAY_TRADE, is_day_trade=True)

        if short_method == ShortMethod.SBL:
            return cls(
                short_method=ShortMethod.SBL,
                is_day_trade=False,
                borrow_fee_rate=float(ShortCost.SBLFeeRate),
                interest_rate=0.0,
            )

        return cls(short_method=short_method, is_day_trade=is_day_trade)


@dataclass
class FuturesCostConfig:
    """
    台期貨交易成本設定

    **與股票的 `CostConfig` 沒有一個欄位可以共用**（期交稅買賣各課一次、稅基是契約價值，不可複用證交稅）：

    | 項目 | 股票 | 期貨 |
    |------|------|------|
    | 交易稅 | 證交稅 0.3%，**只課賣出** | 期交稅十萬分之二，**買賣各課一次** |
    | 稅基 | 成交金額 | **契約價值**（價格 × 乘數 × 口數） |
    | 手續費 | 費率 × 折扣、有最低收費 | **每口固定金額**，無最低收費 |

    **`tax_rate` 是法規值、`commission_per_lot` 是市場常見值**，兩者的可信度不同
    （見 `FuturesCost`）。手續費由券商議定，要精確模擬請用
    `commission_per_lot_by_product` 逐商品指定——小型契約（MTX／TMF）的實務行情
    明顯低於大台，用同一個數字會高估小台的成本。

    **本設定不含滑價**：滑價屬「成交假設」不是「費用」，與台股同一種切法放在
    `FuturesFillConfig`（`core/models/fill_config.py`），
    且期貨以**跳動點**表達而非基點。
    """

    commission_per_lot: float = float(
        FuturesCost.CommissionPerLot
    )  # 每口手續費（單邊）
    tax_rate: float = float(FuturesCost.TaxRate)  # 期交稅率（對契約價值課徵）
    # 逐商品的每口手續費；未列的商品沿用 `commission_per_lot`
    commission_per_lot_by_product: Optional[Dict[str, float]] = None

    @staticmethod
    def default() -> "FuturesCostConfig":
        """預設設定：期交稅為法規值、手續費取市場常見值（見 class docstring）"""

        return FuturesCostConfig()

    @staticmethod
    def free() -> "FuturesCostConfig":
        """
        零成本設定：**只用於驗證引擎接線**

        此時 PnL 恰好等於「價格變動 × 乘數 × 口數」，任何偏差都必定來自記帳本身。
        **不可拿來評估策略績效**——期貨的成本佔比在短線策略上不小，
        零成本回測會系統性高估。
        """

        return FuturesCostConfig(commission_per_lot=0.0, tax_rate=0.0)

    def get_commission_per_lot(self, product: Optional[str] = None) -> float:
        """取得該商品的每口手續費；未逐商品指定時沿用共用值"""

        if product is None or not self.commission_per_lot_by_product:
            return self.commission_per_lot

        return self.commission_per_lot_by_product.get(product, self.commission_per_lot)
