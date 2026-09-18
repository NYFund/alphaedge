import datetime
from enum import Enum

# 定義市場（地區）常量
MARKET_TW = "TW"  # 臺灣
MARKET_US = "US"  # 美國

# 定義動作類型常量
ACTION_BUY = "Buy"
ACTION_SELL = "Sell"
ACTION_OPEN = "Open"
ACTION_CLOSE = "Close"

# 定義價格類型常量
STOCK_PRICE_TYPE_LIMITPRICE = "LMT"
STOCK_PRICE_TYPE_MKT = "MKT"
STOCK_PRICE_TYPE_CLOSE = "Close"

# 定義下單類型常量
ORDER_TYPE_ROD = "ROD"
ORDER_TYPE_IOC = "IOC"
ORDER_TYPE_FOK = "FOK"

# 定義報價模式常量
QUOTE_TYPE_TICK = "tick"
QUOTE_TYPE_BIDASK = "bidask"
QUOTE_TYPE_QUOTE = "quote"

# 定義股票下單單位常量
STOCK_ORDER_LOT_COMMON = "Common"  # 整股
STOCK_ORDER_LOT_BLOCKTRADE = "BlockTrade"  # 鉅額
STOCK_ORDER_LOT_FIXING = "Fixing"  # 定盤
STOCK_ORDER_LOT_ODD = "Odd"  # 零股
STOCK_ORDER_LOT_INTRADAY_ODD = "IntradayOdd"  # 零股

# 定義放空管道常量
SHORT_METHOD_DAY_TRADE = "DAY_TRADE"  # 現股當沖沖賣（先賣後買，同日結清）
SHORT_METHOD_MARGIN = "MARGIN"  # 融券賣出（留倉）
SHORT_METHOD_SBL = "SBL"  # 借券賣出（留倉，議定費率）

# 定義單根 K 棒內的執行順序常量
BAR_EXECUTION_ORDER_CLOSE_THEN_OPEN = "CLOSE_THEN_OPEN"  # 先平倉再開倉（日頻再平衡）
BAR_EXECUTION_ORDER_OPEN_THEN_CLOSE = "OPEN_THEN_CLOSE"  # 先開倉再平倉（當沖）

# 定義當沖日終未回補的處理政策常量
DAY_TRADE_UNCOVERED_FORCE_COVER_AT_CLOSE = "FORCE_COVER_AT_CLOSE"  # 以收盤價強制回補
DAY_TRADE_UNCOVERED_CONVERT_TO_MARGIN = "CONVERT_TO_MARGIN"  # 轉為融券留倉
DAY_TRADE_UNCOVERED_RAISE = "RAISE"  # 直接拋出錯誤

# 定義融券維持率追繳的處理政策常量
MARGIN_CALL_FORCE_COVER = "FORCE_COVER"  # 強制回補（斷頭）
MARGIN_CALL_WARN_ONLY = "WARN_ONLY"  # 僅記錄不強制回補

# 定義台期貨商品代碼常量（＝ TAIFEX 每日行情頁的 commodity_id）
# 2026-08-29 自 TAIFEX 表單實查共 30 檔，本表只收**臺股相關的 15 檔**。
# 不收的 15 檔：海外指數（SPF／UDF／UNF／SXF／F1F／TJF）、商品（GDF／TGF／BRF）、
# 匯率（RHF／RTF／XAF／XBF／XEF／XJF）——本專案是台股研究框架，這些商品與
# tw_stock.db 的籌碼、除權息完全對不上，抓進來也沒有下游能用；日後要用再補。
# 股票期貨（295 檔）與 ETF 期貨（24 檔）不在此列：兩者會隨掛牌／下市變動，
# 且乘數會因除權息調整，須走 futures_stock_universe 表而非寫死。
# 定義台期貨交易時段常量
# 夜盤自 2017-05-15 開始，之前僅有日盤。
# **兩個時段是各自獨立的行情**（OHLC 不同、欄位結構也不同），資料層一律分開存，
# 是否合併成單一序列屬回測層的參數：合併是有損操作，分開存隨時可合併、合併存回不去
FUTURES_SESSION_DAY = "day"  # 一般交易時段 08:45–13:45
FUTURES_SESSION_NIGHT = "night"  # 盤後交易時段 15:00–次日 05:00
# 整併後的單一序列。**這個值不會出現在資料表裡**，
# 它是報價層才有的組合結果：前一交易日的夜盤 ＋ 當日日盤合成一根 bar
FUTURES_SESSION_COMBINED = "combined"

# 定義連續合約的價格調整方式
FUTURES_ADJUST_NONE = "NONE"  # 不調整：直接接起來，換月接點會有假跳空
FUTURES_ADJUST_BACKWARD = "BACKWARD"  # 逆向（差額）調整：最新一段維持原價
FUTURES_ADJUST_RATIO = "RATIO"  # 比例調整：以乘數銜接，報酬率連續

# 定義換月規則（建連續合約與回測轉倉共用）
FUTURES_ROLL_LAST_TRADING_DAY = "LAST_TRADING_DAY"  # 撐到最後交易日收盤才換
FUTURES_ROLL_DAYS_BEFORE_EXPIRY = "DAYS_BEFORE_EXPIRY"  # 到期前 N 個交易日換
FUTURES_ROLL_OPEN_INTEREST = "OPEN_INTEREST"  # 未沖銷契約量交叉時換

FUTURES_PRODUCT_TX = "TX"  # 臺股期貨（大台）
FUTURES_PRODUCT_MTX = "MTX"  # 小型臺指
FUTURES_PRODUCT_TMF = "TMF"  # 微型臺指
FUTURES_PRODUCT_TE = "TE"  # 電子期貨
FUTURES_PRODUCT_ZEF = "ZEF"  # 小型電子期貨
FUTURES_PRODUCT_TF = "TF"  # 金融期貨
FUTURES_PRODUCT_ZFF = "ZFF"  # 小型金融期貨
FUTURES_PRODUCT_XIF = "XIF"  # 非金電期貨
FUTURES_PRODUCT_M1F = "M1F"  # 臺灣中型100期貨
FUTURES_PRODUCT_SOF = "SOF"  # 半導體30期貨
FUTURES_PRODUCT_GTF = "GTF"  # 櫃買期貨
FUTURES_PRODUCT_G2F = "G2F"  # 富櫃200期貨
FUTURES_PRODUCT_BTF = "BTF"  # 臺灣生技期貨
FUTURES_PRODUCT_E4F = "E4F"  # 臺灣永續期貨
FUTURES_PRODUCT_SHF = "SHF"  # 航運期貨

# 定義股票期貨／ETF 期貨的商品類型常量
# 值即為 futures_stock_universe 的 product_type 欄位內容。
#
# **分類依據是「標準型證券股數／受益權單位」而不是商品名稱**：TAIFEX 標的清單頁
# 沒有類型欄位，只有這個數量欄，且四種類型的數量彼此不重疊（2026-08-29 實查：
# 2000 → 249 檔、100 → 47 檔、10000 → 21 檔、1000 → 3 檔），故以它反推類型不會誤判。
#
# ⚠️ **這個數量不等於契約乘數**：它是「掛牌時的標準契約單位」，標的除權息後
# TAIFEX 會調整契約乘數或另掛新契約，實際乘數會偏離本值。
# 算 PnL 一律走 futures_stock_universe 的歷史序列，不要拿這個欄位當乘數用。
STOCK_FUTURES_TYPE_SINGLE = "個股期貨"  # 標準型，2,000 股
STOCK_FUTURES_TYPE_MINI_SINGLE = "小型個股期貨"  # 100 股
STOCK_FUTURES_TYPE_ETF = "ETF期貨"  # 10,000 受益權單位
STOCK_FUTURES_TYPE_MINI_ETF = "小型ETF期貨"  # 1,000 受益權單位

# 現股當沖證交稅減半的**適用區間**（放 module-level：float Enum 無法承載 date）
#
# **起始日同樣重要**：減半優惠自 2017-04-28 起實施，在那之前現股當沖賣出一律
# 課 0.3%。回測若不看日期就一律用減半稅率，2013-01 ~ 2017-04 的每一筆當沖
# 賣出都少算一半的稅——約 4 年 4 個月的區間，而且結果只會偏樂觀。
DAY_TRADE_TAX_START: datetime.date = datetime.date(2017, 4, 28)
DAY_TRADE_TAX_EXPIRY: datetime.date = datetime.date(2027, 12, 31)

# 計息基準日數（放 module-level：float Enum 內混入整數語意會失真）
DAYS_PER_YEAR: int = 365

# 台股漲跌幅限制（單日 ±10%）
# 台股漲跌停幅度：2015-06-01 由 7% 放寬為 10%
#
# 以 23,972 筆交易所公告的漲停／跌停價實測：2015-06-01 前的中位數為 6.92%、
# 之後為 9.91%。單用 10% 會讓 2013-01 ~ 2015-05 的漲跌停區間偏寬約 43%，
# 該期間與官方公告值的相符率為 0.0%。
PRICE_LIMIT_RATIO: float = 0.1  # 現行幅度（2015-06-01 起）
PRICE_LIMIT_RATIO_LEGACY: float = 0.07  # 放寬前的幅度
PRICE_LIMIT_WIDENED_DATE: datetime.date = datetime.date(2015, 6, 1)  # 放寬生效日

# 台股價格檔位：(價格上限, 檔位)，價格小於上限時適用該檔位
PRICE_TICK_TABLE: list = [
    (10.0, 0.01),
    (50.0, 0.05),
    (100.0, 0.1),
    (500.0, 0.5),
    (1000.0, 1.0),
    (float("inf"), 5.0),
]


class Action(str, Enum):
    BUY = ACTION_BUY
    SELL = ACTION_SELL
    OPEN = ACTION_OPEN
    CLOSE = ACTION_CLOSE


class StockPriceType(str, Enum):
    LMT = STOCK_PRICE_TYPE_LIMITPRICE
    MKT = STOCK_PRICE_TYPE_MKT


class OrderType(str, Enum):
    ROD = ORDER_TYPE_ROD
    IOC = ORDER_TYPE_IOC
    FOK = ORDER_TYPE_FOK


class QuoteType(str, Enum):
    Tick = QUOTE_TYPE_TICK
    BidAsk = QUOTE_TYPE_BIDASK
    Quote = QUOTE_TYPE_QUOTE


class StockOrderLot(str, Enum):
    Common = STOCK_ORDER_LOT_COMMON  # 整股
    BlockTrade = STOCK_ORDER_LOT_BLOCKTRADE  # 鉅額
    Fixing = STOCK_ORDER_LOT_FIXING  # 定盤
    Odd = STOCK_ORDER_LOT_ODD  # 零股
    IntradayOdd = STOCK_ORDER_LOT_INTRADAY_ODD  # 盤中零股


class OrderState(str, Enum):
    StockDeal = "SDEAL"
    StockOrder = "SORDER"
    FuturesOrder = "FORDER"
    FuturesDeal = "FDEAL"


class Status(str, Enum):
    Cancelled = "Cancelled"
    Filled = "Filled"
    PartFilled = "PartFilled"
    Inactive = "Inactive"
    Failed = "Failed"
    PendingSubmit = "PendingSubmit"
    PreSubmitted = "PreSubmitted"
    Submitted = "Submitted"


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


# TAIFEX 商品代碼 → Shioaji 期貨分類代碼
#
# **兩邊的代碼不一樣，而且不是加個 F 就好**：小型臺指在 TAIFEX 是 `MTX`、
# 在 Shioaji 是 `MXF`；電子期貨是 `TE` vs `EXF`；金融期貨是 `TF` vs `FXF`。
# 微型臺指與兩檔小型契約（TMF／ZEF／ZFF）則兩邊同名——**沒有規律可循**，
# 只能逐一對照。
#
# 本表於 2026-09-02 以實際登入 Shioaji 列出 `api.Contracts.Futures` 逐一核對，
# **不是從命名規則推的**。要新增商品請照同一種方式查證，不要猜。
#
# Shioaji 的契約 `symbol` 格式為 `{分類}{YYYYMM}`（Ex: `TXF202609`），
# 可直接以 `api.Contracts.Futures[分類][symbol]` 取得；另有 `code` 欄位
# （Ex: `TXFI6`，月份字母 ＋ 年末碼）**不要用它**，字母碼跨年會重複。
SHIOAJI_FUTURES_CATEGORY: dict = {
    FUTURES_PRODUCT_TX: "TXF",  # 臺股期貨
    FUTURES_PRODUCT_MTX: "MXF",  # 小型臺指（**不是 MTXF**）
    FUTURES_PRODUCT_TMF: "TMF",  # 微型臺指（兩邊同名）
    FUTURES_PRODUCT_TE: "EXF",  # 電子期貨（**不是 TEF**）
    FUTURES_PRODUCT_ZEF: "ZEF",  # 小型電子（兩邊同名）
    FUTURES_PRODUCT_TF: "FXF",  # 金融期貨（**不是 TFF**）
    FUTURES_PRODUCT_ZFF: "ZFF",  # 小型金融（兩邊同名）
}


class FuturesAdjustMethod(str, Enum):
    """
    連續合約的價格調整方式

    三種都會產生「一條可以跨月的序列」，但**它們回答的問題不同**：

    | 方式 | 保留什麼 | 犧牲什麼 | 適合 |
    |------|----------|----------|------|
    | `NONE` | 每一天都是當時的真實成交價 | 換月接點有假跳空 | 對照組、抓錯用 |
    | `BACKWARD` | **價差**連續（點數差可直接相減） | 舊價格不是當時的真實價 | 技術指標、點數型停損 |
    | `RATIO` | **報酬率**連續（百分比可直接相乘） | 舊價格連比例都被改過 | 波動度、報酬率統計 |

    **沒有一種是「正確」的**，選錯的後果是靜默的：用 `NONE` 算移動平均會在每個
    換月接點吃到一根假跳空；用 `BACKWARD` 算年化報酬率，早年被減成負數的價格會
    讓百分比失真。故本專案把方式存進主鍵，三種可以並存於同一張表。
    """

    NONE = FUTURES_ADJUST_NONE
    BACKWARD = FUTURES_ADJUST_BACKWARD
    RATIO = FUTURES_ADJUST_RATIO


class FuturesRollRule(str, Enum):
    """
    換月規則

    **換月時點會直接改變績效**，不是實作細節：撐到最後交易日會吃到結算日的
    流動性與價格行為，提前換月則會錯過近月的最後一段行情。三種規則並存於
    連續合約表的主鍵中，策略層以同一組規則決定何時轉倉。
    """

    LAST_TRADING_DAY = FUTURES_ROLL_LAST_TRADING_DAY
    DAYS_BEFORE_EXPIRY = FUTURES_ROLL_DAYS_BEFORE_EXPIRY
    OPEN_INTEREST = FUTURES_ROLL_OPEN_INTEREST


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
    """融資（做多槓桿）相關成本常數，本階段僅定義不啟用"""

    FinancingRate = 0.0635  # 融資年利率（券商常見 6.15%~6.5%）
    ListedFinancingRatio = 0.6  # 上市股票融資成數
    OTCFinancingRatio = 0.5  # 上櫃股票融資成數


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


class BarExecutionOrder(str, Enum):
    """單根 K 棒內開平倉的執行順序"""

    CLOSE_THEN_OPEN = BAR_EXECUTION_ORDER_CLOSE_THEN_OPEN
    OPEN_THEN_CLOSE = BAR_EXECUTION_ORDER_OPEN_THEN_CLOSE


class DayTradeUncoveredPolicy(str, Enum):
    """當沖放空於日終仍未回補時的處理政策"""

    FORCE_COVER_AT_CLOSE = DAY_TRADE_UNCOVERED_FORCE_COVER_AT_CLOSE
    CONVERT_TO_MARGIN = DAY_TRADE_UNCOVERED_CONVERT_TO_MARGIN
    RAISE = DAY_TRADE_UNCOVERED_RAISE


class MarginCallPolicy(str, Enum):
    """融券維持率低於門檻時的處理政策"""

    FORCE_COVER = MARGIN_CALL_FORCE_COVER
    WARN_ONLY = MARGIN_CALL_WARN_ONLY


class Units(int, Enum):
    """股票張數單位"""

    SHARE = 1  # 1 Share = 1 Share
    LOT = 1000  # 1 Lot = 1000 Shares


class FileEncoding(str, Enum):
    """檔案編碼類型"""

    UTF8 = "utf-8"
    UTF8_SIG = "utf-8-sig"  # UTF-8 with BOM，用於 Excel 等軟體正確識別中文
    BIG5 = "big5"


class FuturesProduct(str, Enum):
    """
    台期貨商品代碼（＝ TAIFEX 每日行情頁查詢時要帶的 commodity_id）

    分組即為爬取範圍，見 `FUTURES_TARGET_PRODUCTS`（`core/config.py`）
    """

    # 大盤指數
    TX = FUTURES_PRODUCT_TX
    MTX = FUTURES_PRODUCT_MTX
    TMF = FUTURES_PRODUCT_TMF

    # 類股指數
    TE = FUTURES_PRODUCT_TE
    ZEF = FUTURES_PRODUCT_ZEF
    TF = FUTURES_PRODUCT_TF
    ZFF = FUTURES_PRODUCT_ZFF

    # 其他臺股指數（尚未排入任何 Phase，代碼先登錄）
    XIF = FUTURES_PRODUCT_XIF
    M1F = FUTURES_PRODUCT_M1F
    SOF = FUTURES_PRODUCT_SOF
    GTF = FUTURES_PRODUCT_GTF
    G2F = FUTURES_PRODUCT_G2F
    BTF = FUTURES_PRODUCT_BTF
    E4F = FUTURES_PRODUCT_E4F
    SHF = FUTURES_PRODUCT_SHF


# 台期貨契約乘數（元／點）：PnL = 價格變動 × 乘數 × 口數
#
# **只登錄已查證且乘數未曾變動的商品**。查表一律直接用 `FUTURES_MULTIPLIER[code]`，
# **不要用 `.get(code, 預設值)`**——未登錄者讓它 KeyError 當場炸掉，是刻意的設計：
# 乘數猜錯不會有任何徵兆，只會讓整條 PnL 靜默偏掉，那比中斷難查得多。
#
# 為什麼放程式碼而不是 DB：回測的 `InstrumentSpec` 是純規則層、不持有連線
# （見 `TwStockSpec.to_units()` 也是寫死的「張 → 股 ×1000」）。乘數改成查 DB，
# 就得讓 `InstrumentSpec` 抱著連線，會破壞它的定位。
#
# ⚠️ **未登錄清單與原因**（登錄前必須先查證，不可憑印象填）：
# - XIF 非金電：TAIFEX 現行規格為每點 10 元，但**曾為 100 元**。乘數變更過的商品
#   不能用單一數值表達，否則跨越變更日的回測會靜默算錯。要登錄它必須先查到
#   變更生效日，並比照 `PRICE_LIMIT_RATIO` ／ `_LEGACY` ／ `_WIDENED_DATE`
#   改成帶生效日的表達方式。
# - M1F／SOF／GTF／G2F／BTF／E4F／SHF：尚未查證，且未排入任何 Phase。
FUTURES_MULTIPLIER: dict = {
    FUTURES_PRODUCT_TX: 200,
    FUTURES_PRODUCT_MTX: 50,
    FUTURES_PRODUCT_TMF: 10,
    FUTURES_PRODUCT_TE: 4000,
    FUTURES_PRODUCT_ZEF: 500,
    FUTURES_PRODUCT_TF: 1000,
    FUTURES_PRODUCT_ZFF: 250,
}


# 台期貨最小升降單位（跳動點，單位：指數點）：滑價的 `slippage_ticks_*` 以此換算成價差
#
# **只登錄已查證的商品**，規矩與 `FUTURES_MULTIPLIER` 完全相同：查表一律直接用
# `FUTURES_TICK_SIZE[code]`，**不要 `.get(code, 預設值)`**——跳動點猜錯不會有徵兆，
# 只會讓滑價靜默偏掉，而且偏很多（TE 的一檔是 0.05 點，當成 1 點就是 20 倍）。
#
# 查證來源：TAIFEX 各商品契約規格頁的「最小升降單位」（2026-09-17 查）
#   TX   https://www.taifex.com.tw/cht/2/tX    指數 1 點（相當於新臺幣 200 元）
#   MTX  https://www.taifex.com.tw/cht/2/mTX   指數 1 點（相當於新臺幣 50 元）
#   TMF  https://www.taifex.com.tw/cht/2/tMF   指數 1 點（相當於新臺幣 10 元）
#   TE   https://www.taifex.com.tw/cht/2/tE    指數 0.05 點（相當於新臺幣 200 元）
#   ZEF  https://www.taifex.com.tw/cht/2/zEF   指數 0.05 點（相當於新臺幣 25 元）
#   TF   https://www.taifex.com.tw/cht/2/tF    指數 0.2 點（相當於新臺幣 200 元）
#   ZFF  https://www.taifex.com.tw/cht/2/zFF   指數 0.2 點（相當於新臺幣 50 元）
#
# 交叉驗證：跳動點 × `FUTURES_MULTIPLIER` ＝ 規格頁標示的每跳金額，七項全數相符
# （例：TE 0.05 × 4000 ＝ 200 元）。兩張表因此互為對方的護欄，改一張要同時對帳。
#
# ⚠️ **跳動點曾變更過的商品不可用單一數值表達**（理由同 `FUTURES_MULTIPLIER` 的
# XIF 乘數）：跨越變更日的回測會靜默算錯，須比照 `PRICE_LIMIT_RATIO` ／
# `_LEGACY` ／ `_WIDENED_DATE` 改為帶生效日的表達方式。上列七項在查證當下皆為
# 現行規格；未登錄的商品（XIF／M1F／SOF／GTF／G2F／BTF／SHF／E4F 與股票期貨）
# 一律不猜，要用它們得先查證後登錄，或在建構 `TwFuturesSpec` 時明確指定 tick_size。
FUTURES_TICK_SIZE: dict = {
    FUTURES_PRODUCT_TX: 1.0,
    FUTURES_PRODUCT_MTX: 1.0,
    FUTURES_PRODUCT_TMF: 1.0,
    FUTURES_PRODUCT_TE: 0.05,
    FUTURES_PRODUCT_ZEF: 0.05,
    FUTURES_PRODUCT_TF: 0.2,
    FUTURES_PRODUCT_ZFF: 0.2,
}


class StockFuturesType(str, Enum):
    """
    股票期貨（single stock futures）／ETF 期貨的商品類型

    名稱是「Stock Futures」而非「Futures Stock」——它是**期貨**的一種，
    以個股／ETF 為標的，不是股票的一種。

    由標的清單頁的「標準型證券股數／受益權單位」反推，見
    `STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE`
    """

    SINGLE = STOCK_FUTURES_TYPE_SINGLE
    MINI_SINGLE = STOCK_FUTURES_TYPE_MINI_SINGLE
    ETF = STOCK_FUTURES_TYPE_ETF
    MINI_ETF = STOCK_FUTURES_TYPE_MINI_ETF


# 標準型證券股數／受益權單位 → 商品類型
#
# 查表一律直接用 `STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE[size]`，**不要 `.get()` 帶預設值**：
# 出現沒見過的數量代表 TAIFEX 新增了商品類型，那時該當場中斷讓人去查，
# 而不是靜靜歸到某個既有類型裡（理由同 `FUTURES_MULTIPLIER`）。
STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE: dict = {
    2000: STOCK_FUTURES_TYPE_SINGLE,
    100: STOCK_FUTURES_TYPE_MINI_SINGLE,
    10000: STOCK_FUTURES_TYPE_ETF,
    1000: STOCK_FUTURES_TYPE_MINI_ETF,
}


class FuturesSession(str, Enum):
    """
    台期貨交易時段

    `DAY` 與 `NIGHT` 的值即為 `futures_price_daily` 的 `session` 欄位內容；
    **`COMBINED` 不是資料表裡的值**，而是日夜盤整併的結果——
    「前一交易日的夜盤 ＋ 當日日盤」合成的一根 bar，只存在於報價層。
    拿 `COMBINED` 去查資料庫一律查不到東西，那是刻意的。

    **為什麼整併要用「前一交易日的夜盤」**：TAIFEX 的夜盤 15:00 開盤、
    次日 05:00 收盤，它在制度上屬於**次一交易日**的一部分——星期五晚上的那一段
    屬於星期一。資料表為了忠實記錄來源，把夜盤存在它開始的那個日曆日，
    整併時因此要往前取一個交易日，不是取同一天。
    """

    DAY = FUTURES_SESSION_DAY
    NIGHT = FUTURES_SESSION_NIGHT
    COMBINED = FUTURES_SESSION_COMBINED

    @classmethod
    def data_sessions(cls) -> tuple:
        """
        **來源真的有的兩個時段**（日盤與夜盤）

        ETL 要「逐時段爬一次」時一律用本方法，**不要直接 `for s in FuturesSession`**
        ——那會把 `COMBINED` 也算進去，於是去爬一個不存在的時段。
        2026-09-02 加入 `COMBINED` 時就是這樣讓爬蟲與清洗器一起壞掉的
        （`KeyError: 'combined'`）。
        """

        return (cls.DAY, cls.NIGHT)
