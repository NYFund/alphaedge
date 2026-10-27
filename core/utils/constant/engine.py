from enum import Enum

"""
引擎政策常量：bar 內執行順序、當沖未回補政策、維持率追繳政策、TICK 回測的標的範圍

前三項回測與實盤兩個引擎都依它運作（實盤的段落排程、盤中當沖守門、期貨保證金設定都會讀），
不是回測專屬，故不以 backtest 命名；標的範圍只有 TICK 回測用，對應實盤的訂閱清單。

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


# 定義單根 K 棒內的執行順序常量
BAR_EXECUTION_SEQUENCE_CLOSE_THEN_OPEN = "CLOSE_THEN_OPEN"  # 先平倉再開倉（日頻再平衡）


BAR_EXECUTION_SEQUENCE_OPEN_THEN_CLOSE = "OPEN_THEN_CLOSE"  # 先開倉再平倉（當沖）


# 定義當沖日終未回補的處理政策常量
DAY_TRADE_UNCOVERED_FORCE_COVER_AT_CLOSE = "FORCE_COVER_AT_CLOSE"  # 以收盤價強制回補


DAY_TRADE_UNCOVERED_CONVERT_TO_MARGIN = "CONVERT_TO_MARGIN"  # 轉為融券留倉


DAY_TRADE_UNCOVERED_RAISE = "RAISE"  # 直接拋出錯誤


# 定義融券維持率追繳的處理政策常量
MARGIN_CALL_FORCE_COVER = "FORCE_COVER"  # 強制回補（斷頭）


MARGIN_CALL_WARN_ONLY = "WARN_ONLY"  # 僅記錄不強制回補


# 定義 TICK 回測的標的範圍常量
TICK_UNIVERSE_STRATEGY = "strategy"  # 策略盤前宣告的清單＋持倉（與實盤訂閱一致）


TICK_UNIVERSE_ALL = "all"  # 當天有成交的全市場


class BarExecutionSequence(str, Enum):
    """
    單根 K 棒內開平倉的執行順序；引擎依策略的 `allow_day_trade` 推導，策略不直接設定

    名稱用 sequence 而不用 order：交易系統裡 order 一律指訂單，同一個字兼指順序會讓
    `order_rules.get_execution_sequence()` 這類呼叫讀起來有兩種意思。
    """

    CLOSE_THEN_OPEN = BAR_EXECUTION_SEQUENCE_CLOSE_THEN_OPEN
    OPEN_THEN_CLOSE = BAR_EXECUTION_SEQUENCE_OPEN_THEN_CLOSE


class DayTradeUncoveredPolicy(str, Enum):
    """當沖放空於日終仍未回補時的處理政策"""

    FORCE_COVER_AT_CLOSE = DAY_TRADE_UNCOVERED_FORCE_COVER_AT_CLOSE
    CONVERT_TO_MARGIN = DAY_TRADE_UNCOVERED_CONVERT_TO_MARGIN
    RAISE = DAY_TRADE_UNCOVERED_RAISE


class MarginCallPolicy(str, Enum):
    """融券維持率低於門檻時的處理政策"""

    FORCE_COVER = MARGIN_CALL_FORCE_COVER
    WARN_ONLY = MARGIN_CALL_WARN_ONLY


class TickUniverse(str, Enum):
    """
    TICK 回測每天要讀哪些標的的逐筆資料，回測前由 `--tick-universe` 選定

    - `STRATEGY`：策略 `get_live_symbols()` 的清單（以前一交易日呼叫）加上當時的持倉，
      與實盤盤前篩選、訂閱持倉的規則相同；前一天冷門、當天才爆量的股票會漏掉，那正是實盤的限制。
    - `ALL`：當天有成交的全市場，看策略本身的潛力；一天上百萬筆，比較慢。
    """

    STRATEGY = TICK_UNIVERSE_STRATEGY
    ALL = TICK_UNIVERSE_ALL
