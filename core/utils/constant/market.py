import datetime
from enum import Enum
from typing import List, Tuple

"""
市場制度常量：市場、商品類別、級別、多空、漲跌停與跳動點表

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


# 定義市場（地區）常量
MARKET_TW = "TW"  # 臺灣


MARKET_US = "US"  # 美國


# 定義放空管道常量
SHORT_METHOD_DAY_TRADE = "DAY_TRADE"  # 現股當沖沖賣（先賣後買，同日結清）


SHORT_METHOD_MARGIN = "MARGIN"  # 融券賣出（留倉）


SHORT_METHOD_SBL = "SBL"  # 借券賣出（留倉，議定費率）


# 台股漲跌停幅度：2015-06-01 由 7% 放寬為 10%
#
# 以 23,972 筆交易所公告的漲停／跌停價實測：2015-06-01 前的中位數為 6.92%、
# 之後為 9.91%。單用 10% 會讓 2013-01 ~ 2015-05 的漲跌停區間偏寬約 43%，
# 該期間與官方公告值的相符率為 0.0%。
PRICE_LIMIT_RATIO: float = 0.1  # 現行幅度（2015-06-01 起）


PRICE_LIMIT_RATIO_LEGACY: float = 0.07  # 放寬前的幅度


PRICE_LIMIT_WIDENED_DATE: datetime.date = datetime.date(2015, 6, 1)  # 放寬生效日


# 台股價格檔位：(價格上限, 檔位)，價格小於上限時適用該檔位
PRICE_TICK_TABLE: List[Tuple[float, float]] = [
    (10.0, 0.01),
    (50.0, 0.05),
    (100.0, 0.1),
    (500.0, 0.5),
    (1000.0, 1.0),
    (float("inf"), 5.0),
]


class Market(str, Enum):
    """
    市場（地區）

    與 `InstrumentType` 是**兩條互相正交的軸**，不要混用：
    本軸管地區差異（交易日曆、開盤時間、幣別），`InstrumentType` 管商品差異
    （契約乘數、報價單位、結算規則）。回測的 model 組合由「兩者的組合」決定，
    例如 `TwStockSpec` ＝（`Market.TW`, `InstrumentType.STOCK`）。
    """

    TW = MARKET_TW
    US = MARKET_US


class InstrumentType(str, Enum):
    """
    金融商品類別

    軸線分工見 `Market` 的 docstring。
    """

    STOCK = "Stock"
    FUTURE = "Future"
    OPTION = "Option"


class Scale(str, Enum):
    """Kbar 級別"""

    TICK = "TICK"
    DAY = "DAY"


class PositionType(str, Enum):
    """部位方向"""

    LONG = "LONG"
    SHORT = "SHORT"


class ShortMethod(str, Enum):
    """放空管道：三者的成本結構完全不同"""

    DAY_TRADE = SHORT_METHOD_DAY_TRADE
    MARGIN = SHORT_METHOD_MARGIN
    SBL = SHORT_METHOD_SBL


class Units(int, Enum):
    """股票張數單位"""

    SHARE = 1  # 1 Share = 1 Share
    LOT = 1000  # 1 Lot = 1000 Shares
