import datetime
from enum import Enum

"""
成本與稅費常量：手續費、證交稅、借券費與保證金利息

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


# 現股當沖證交稅減半的**適用區間**（放 module-level：float Enum 無法承載 date）
#
# **起始日同樣重要**：減半優惠自 2017-04-28 起實施，在那之前現股當沖賣出一律
# 課 0.3%。回測若不看日期就一律用減半稅率，2013-01 ~ 2017-04 的每一筆當沖
# 賣出都少算一半的稅——約 4 年 4 個月的區間，而且結果只會偏樂觀。
DAY_TRADE_TAX_START: datetime.date = datetime.date(2017, 4, 28)


DAY_TRADE_TAX_EXPIRY: datetime.date = datetime.date(2027, 12, 31)


# 計息基準日數（放 module-level：float Enum 內混入整數語意會失真）
DAYS_PER_YEAR: int = 365


class Commission(float, Enum):
    """券商手續費相關常數"""

    CommRate = 0.001425  # 券商手續費率（commission rate）
    Discount = 0.3  # 券商手續費折扣（commission discount）
    MinFee = 20.0  # 券商最低手續費限制（minimum fee）
    TaxRate = 0.003  # 證券交易稅（Securities Transaction Tax Rate）
    # 現股當沖證交稅（減半）；適用區間為 DAY_TRADE_TAX_START ~ DAY_TRADE_TAX_EXPIRY
    DayTradeTaxRate = 0.0015


class ShortCost(float, Enum):
    """放空（融券／借券）相關成本常數"""

    MarginRate = 0.9  # 融券保證金成數（賣出價金的 90%）
    MarginBorrowFeeRate = 0.0008  # 融券手續費率（借券費，賣出時一次性收取）
    MarginInterestRate = 0.002  # 融券保證金利息年利率（券商付給客戶，為收入）
    MaintenanceRatio = 1.3  # 融券維持率門檻（低於則追繳／斷頭）
    SBLFeeRate = 0.03  # 借券（SBL）年化費率（議定區間 0.01%~16%，取市場常見值）


class FuturesCost(float, Enum):
    """
    台期貨交易成本常數

    **與 `Commission` 完全不可混用**（`Commission` 是股票的）：

    | 項目 | 股票 | 期貨 |
    |------|------|------|
    | 交易稅 | 證交稅 0.3%，**只課賣出** | 期交稅十萬分之二，**買賣各課一次** |
    | 稅基 | 成交金額 | **契約價值**（價格 × 乘數 × 口數） |
    | 手續費 | 費率 × 折扣、有最低收費 | **每口固定金額**，無最低收費 |

    `TaxRate` 是**法規值**（期貨交易稅條例：股價類期貨契約按契約金額
    十萬分之二課徵，買賣雙方各課一次）；`CommissionPerLot` 是**市場常見值**
    而非法規值——手續費由券商議定，實務上大台單邊常見 30~70 元、小型契約更低，
    取 50 為預設。要精確模擬請在 `FuturesCostConfig` 逐商品指定
    （`commission_per_lot_by_product`），**不要改這裡的預設值**。
    """

    TaxRate = 0.00002  # 期交稅率（股價類期貨契約金額的十萬分之二，買賣各一次）
    CommissionPerLot = 50.0  # 每口手續費（單邊）；券商議定，此為市場常見值


class MarginCost(float, Enum):
    """融資（做多槓桿）相關成本常數；目前僅定義，回測尚未啟用融資"""

    FinancingRate = 0.0635  # 融資年利率（券商常見 6.15%~6.5%）
    ListedFinancingRatio = 0.6  # 上市股票融資成數
    OTCFinancingRatio = 0.5  # 上櫃股票融資成數
