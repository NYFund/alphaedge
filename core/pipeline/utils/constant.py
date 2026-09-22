from enum import Enum


class DataType(str, Enum):
    """資料類型"""

    PRICE = "Price"
    CHIP = "Chip"
    MARGIN = "Margin"  # 信用交易（融資融券餘額）
    DIVIDEND = "Dividend"  # 除權除息計算結果表
    # 非除權息的公司行動（減資／面額變更／分割）；與 DIVIDEND 分表，理由見 schema.py
    CORPORATE_ACTION = "CORPORATE_ACTION"
    TICK = "Tick"
    MRR = "MONTHLY_REVENUE_REPORT"
    FS = "FINANCIAL_STATEMENT"
    FINMIND = "FINMIND"
    FUTURES_PRICE = "FUTURES_PRICE"  # 台期貨每日行情（寫入 tw_futures.db）
    FUTURES_STOCK_UNIVERSE = "FUTURES_STOCK_UNIVERSE"  # 股票期貨標的池
    FUTURES_MARGIN = "FUTURES_MARGIN"  # 台期貨保證金（變動序列，寫入 tw_futures.db）
    # 連續合約：**衍生表**，來源是同一個 DB 的 futures_price_daily，不連網路
    FUTURES_CONTINUOUS = "FUTURES_CONTINUOUS"
    # 台期貨籌碼：三大法人 ＋ 大額交易人 ＋ 選擇權 PCR（皆為盤後公布）
    FUTURES_CHIP = "FUTURES_CHIP"
    # 股票期貨行情：商品清單來自標的池而非字面值常數
    FUTURES_STOCK_PRICE = "FUTURES_STOCK_PRICE"
    # 期貨逐筆成交（Shioaji → DolphinDB）；需要 `[tick]` 選用相依與 Shioaji 金鑰
    FUTURES_TICK = "FUTURES_TICK"
    # 市場開休市日期（TWSE 公告，一年一次請求）；實盤盤前判定交易日的主來源
    MARKET_HOLIDAY = "MARKET_HOLIDAY"


class ListingBoard(str, Enum):
    """
    掛牌板別（值即為公開資訊觀測站的 `TYPEK` 查詢參數）

    僅台股適用。與「發行人國別」是兩條獨立的軸，後者見 `IssuerOrigin`——
    兩者曾被合併在同一個 Enum 裡（`SII0`／`OTC0` 皆為 `"0"`），值相同會讓
    Python Enum 把後者摺成前者的 alias，是靜默的語意汙染。
    """

    SII = "sii"  # 上市（Securities Investment Information）
    OTC = "otc"  # 上櫃
    ROTC = "rotc"  # 興櫃
    PUB = "pub"  # 公開發行
    ALL = "all"  # 全部


class IssuerOrigin(str, Enum):
    """
    發行人國別（值即為月營收頁 URL 末碼）

    國外發行者即市場俗稱的 F 股／KY 股。本軸與 `ListingBoard` 正交：
    上市與上櫃各自都有國內、國外兩種發行人。
    """

    DOMESTIC = "0"  # 國內
    FOREIGN = "1"  # 國外


class FinancialStatementType(str, Enum):
    """財報類別"""

    BALANCE_SHEET = "BALANCE_SHEET"
    COMPREHENSIVE_INCOME = "COMPREHENSIVE_INCOME"
    CASH_FLOW = "CASH_FLOW"
    EQUITY_CHANGE = "EQUITY_CHANGE"


class FinMindDataType(str, Enum):
    """FinMind 資料子類型"""

    STOCK_INFO = "STOCK_INFO"
    STOCK_INFO_WITH_WARRANT = "STOCK_INFO_WITH_WARRANT"
    BROKER_INFO = "BROKER_INFO"
    BROKER_TRADING = "BROKER_TRADING"


class UpdateStatus(str, Enum):
    """資料更新狀態"""

    SUCCESS = "success"  # 成功更新
    NO_DATA = "no_data"  # 沒有資料（API 返回空結果）
    ALREADY_UP_TO_DATE = "already_up_to_date"  # 資料庫已是最新
    ERROR = "error"  # 發生錯誤
