from enum import Enum

"""
下單相關常量：動作、價格型態、委託種類、委託條件與委託狀態

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


# 定義動作類型常量
ACTION_BUY = "Buy"


ACTION_SELL = "Sell"


ACTION_OPEN = "Open"


ACTION_CLOSE = "Close"


# 定義價格類型常量
STOCK_PRICE_TYPE_LIMITPRICE = "LMT"


STOCK_PRICE_TYPE_MKT = "MKT"


STOCK_PRICE_TYPE_CLOSE = "Close"


# 定義期貨價格類型常量（比股票多一個 MKP 範圍市價）
FUTURES_PRICE_TYPE_LIMITPRICE = "LMT"


FUTURES_PRICE_TYPE_MKT = "MKT"


FUTURES_PRICE_TYPE_MKP = "MKP"  # 範圍市價


# 定義下單類型常量
ORDER_TYPE_ROD = "ROD"


ORDER_TYPE_IOC = "IOC"


ORDER_TYPE_FOK = "FOK"


# 定義報價模式常量
QUOTE_TYPE_TICK = "tick"


QUOTE_TYPE_BIDASK = "bid_ask"


QUOTE_TYPE_QUOTE = "quote"


# 定義股票下單單位常量
STOCK_ORDER_LOT_COMMON = "Common"  # 整股


STOCK_ORDER_LOT_BLOCKTRADE = "BlockTrade"  # 鉅額


STOCK_ORDER_LOT_FIXING = "Fixing"  # 定盤


STOCK_ORDER_LOT_ODD = "Odd"  # 零股


STOCK_ORDER_LOT_INTRADAY_ODD = "IntradayOdd"  # 盤中零股


# 定義股票委託條件常量（現股／融資／融券／借券）
#
# **策略不填這個欄位**：它由 `order_preprocess` 依 `position_type` ＋ `short_method`
# ＋ `is_day_trade` 推導，才能保證和回測的成本路徑走同一組假設
STOCK_ORDER_COND_CASH = "Cash"  # 現股


STOCK_ORDER_COND_MARGIN_TRADING = "MarginTrading"  # 融資


STOCK_ORDER_COND_SHORT_SELLING = "ShortSelling"  # 融券


STOCK_ORDER_COND_SBL_SHORT = "SBLShort"  # 借券賣出


# 定義期貨開平倉別常量
FUTURES_OC_TYPE_AUTO = "Auto"


FUTURES_OC_TYPE_NEW = "New"  # 開倉


FUTURES_OC_TYPE_COVER = "Cover"  # 平倉


FUTURES_OC_TYPE_DAY_TRADE = "DayTrade"  # 當沖


# 定義實盤委託狀態常量（OMS 狀態機的狀態）
LIVE_ORDER_STATUS_PENDING_SUBMIT = "PENDING_SUBMIT"  # 已寫入本地，尚未送出券商


LIVE_ORDER_STATUS_SUBMITTED = "SUBMITTED"  # 券商已收單


LIVE_ORDER_STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交


LIVE_ORDER_STATUS_FILLED = "FILLED"  # 全部成交


LIVE_ORDER_STATUS_CANCELLED = "CANCELLED"  # 已撤單（含日終失效）


LIVE_ORDER_STATUS_REJECTED = "REJECTED"  # 券商拒單


LIVE_ORDER_STATUS_FAILED = "FAILED"  # 送出失敗，或刷新後在券商端查無此單


class Action(str, Enum):
    """下單動作；股票用 BUY／SELL，期貨的開平倉另見 `FuturesOCType`"""

    BUY = ACTION_BUY
    SELL = ACTION_SELL
    OPEN = ACTION_OPEN
    CLOSE = ACTION_CLOSE


class StockPriceType(str, Enum):
    """股票價格類型（限價／市價）；期貨另見 `FuturesPriceType`"""

    LMT = STOCK_PRICE_TYPE_LIMITPRICE
    MKT = STOCK_PRICE_TYPE_MKT


class FuturesPriceType(str, Enum):
    """
    期貨價格類型

    與 `StockPriceType` 分開而不合併成一個：期貨多一個 `MKP`（範圍市價），
    合在一起會讓股票訂單也長出一個它送不出去的值，而錯誤要到券商退單才出現。
    """

    LMT = FUTURES_PRICE_TYPE_LIMITPRICE
    MKT = FUTURES_PRICE_TYPE_MKT
    MKP = FUTURES_PRICE_TYPE_MKP


class OrderType(str, Enum):
    """委託種類：ROD 當日有效、IOC 立即成交否則取消、FOK 全部成交否則取消"""

    ROD = ORDER_TYPE_ROD
    IOC = ORDER_TYPE_IOC
    FOK = ORDER_TYPE_FOK


class QuoteType(str, Enum):
    """行情訂閱的報價模式"""

    Tick = QUOTE_TYPE_TICK
    BidAsk = QUOTE_TYPE_BIDASK
    Quote = QUOTE_TYPE_QUOTE


class StockOrderLot(str, Enum):
    """股票下單單位；整股與零股的成交規則與撮合時段皆不同"""

    Common = STOCK_ORDER_LOT_COMMON  # 整股
    BlockTrade = STOCK_ORDER_LOT_BLOCKTRADE  # 鉅額
    Fixing = STOCK_ORDER_LOT_FIXING  # 定盤
    Odd = STOCK_ORDER_LOT_ODD  # 零股
    IntradayOdd = STOCK_ORDER_LOT_INTRADAY_ODD  # 盤中零股


class StockOrderCond(str, Enum):
    """
    股票委託條件（現股／融資／融券／借券）

    成員名與值都對齊 shioaji 的 `StockOrderCond`，由
    `tests/test_order_state_parity.py` 盯住。

    shioaji 另有 `SBLShortPriceExempt`／`Netting`／`Emerging`，本專案沒有對應的交易路徑，
    故不鏡像。若 shioaji 某版缺少這裡的成員，mapper 必須當場拋出，
    不可退回 `ShortSelling`——那會變成用融券的券源與成本送出一張以為是借券的單。
    """

    Cash = STOCK_ORDER_COND_CASH
    MarginTrading = STOCK_ORDER_COND_MARGIN_TRADING
    ShortSelling = STOCK_ORDER_COND_SHORT_SELLING
    SBLShort = STOCK_ORDER_COND_SBL_SHORT


class FuturesOCType(str, Enum):
    """
    期貨開平倉別

    **不使用 `Auto`**：它在同時有多空部位或換月時的行為不透明，
    開平倉別一律由訂單的 `Action` 與持倉推導。成員仍保留 `Auto` 以對齊券商值域。
    """

    Auto = FUTURES_OC_TYPE_AUTO
    New = FUTURES_OC_TYPE_NEW
    Cover = FUTURES_OC_TYPE_COVER
    DayTrade = FUTURES_OC_TYPE_DAY_TRADE


class LiveOrderStatus(str, Enum):
    """
    實盤委託在本專案 OMS 狀態機中的狀態

    **與 `Status` 分開**：`Status` 是券商回報值的鏡像（券商說什麼就是什麼），
    `LiveOrderStatus` 是本地狀態機的狀態（本地認為這張單走到哪了）。
    兩者由 mapper 轉換。合併的話，「還沒送出去」與「券商說還沒送出去」會變成
    同一個值，而重啟接管要靠的正是這兩者的差別。
    """

    PENDING_SUBMIT = LIVE_ORDER_STATUS_PENDING_SUBMIT
    SUBMITTED = LIVE_ORDER_STATUS_SUBMITTED
    PARTIALLY_FILLED = LIVE_ORDER_STATUS_PARTIALLY_FILLED
    FILLED = LIVE_ORDER_STATUS_FILLED
    CANCELLED = LIVE_ORDER_STATUS_CANCELLED
    REJECTED = LIVE_ORDER_STATUS_REJECTED
    FAILED = LIVE_ORDER_STATUS_FAILED


class OrderState(str, Enum):
    """券商回報的事件種類（委託回報／成交回報 × 股票／期貨）"""

    StockDeal = "SDEAL"
    StockOrder = "SORDER"
    FuturesOrder = "FORDER"
    FuturesDeal = "FDEAL"


class Status(str, Enum):
    """券商回報的委託狀態原值；本地狀態機的狀態另見 `LiveOrderStatus`"""

    Cancelled = "Cancelled"
    Filled = "Filled"
    PartFilled = "PartFilled"
    Inactive = "Inactive"
    Failed = "Failed"
    PendingSubmit = "PendingSubmit"
    PreSubmitted = "PreSubmitted"
    Submitted = "Submitted"
