from enum import Enum

"""
回測政策常量：bar 內執行順序、當沖未回補政策、維持率追繳政策

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


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
