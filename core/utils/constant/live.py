from enum import Enum

"""
實盤鉤子常量：策略鉤子與執行段落

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


# 定義實盤執行時點常量
#
# 日 K 在實盤不存在：回測一次呼叫就同時拿到當日 OHLC，實盤在開盤前不知道 close、
# 在收盤前不知道完整 OHLC。故同一支策略的鉤子要拆成兩個時點送單
EXECUTION_TIMING_AT_OPEN = "AT_OPEN"  # 盤前委託，進開盤集合競價


EXECUTION_TIMING_AT_CLOSE = "AT_CLOSE"  # 尾盤取快照算訊號，進收盤集合競價


EXECUTION_TIMING_IMMEDIATE = "IMMEDIATE"  # 盤中逐筆觸發，算完就送


# 定義實盤策略鉤子名稱常量（`live_schedule` 的鍵）
LIVE_HOOK_OPEN = "open"  # 開倉訊號


LIVE_HOOK_CLOSE = "close"  # 平倉訊號


LIVE_HOOK_STOP_LOSS = "stop_loss"  # 停損訊號


# 定義實盤執行方式常量（`live_execution` 的值）
EXECUTION_STYLE_MARKET = "MARKET"  # 要成交：這筆一定要進出場，價格交給市場決定


EXECUTION_STYLE_LIMIT = "LIMIT"  # 照價掛單：只接受策略給的價，沒成交就算了


class LiveHook(str, Enum):
    """
    策略鉤子；`live_schedule` 以它宣告「哪個鉤子在哪一段被呼叫」

    停損與一般平倉分開列出，是因為它們在實盤**可能落在不同段落**：
    停損要盤中就反應，一般平倉可以等到尾盤算完訊號再送。
    """

    OPEN = LIVE_HOOK_OPEN
    CLOSE = LIVE_HOOK_CLOSE
    STOP_LOSS = LIVE_HOOK_STOP_LOSS


class ExecutionTiming(str, Enum):
    """
    實盤的執行時點；**回測忽略此欄位**，仍用策略給的價成交

    台股的兩個日頻段落：
    - `AT_OPEN`：08:30~09:00 盤前委託，進開盤集合競價。
    - `AT_CLOSE`：13:20 起取快照算訊號，**13:25:00 之後才送單**、13:29:00 前送完，
      讓委託進收盤集合競價。13:25 前送出的限價單會在逐筆交易時段就成交，
      成交價不是收盤價，和回測「以收盤價成交」的假設對不上。

    期貨的段落時點由 `InstrumentSpec` 提供，不寫死在引擎裡。
    """

    AT_OPEN = EXECUTION_TIMING_AT_OPEN
    AT_CLOSE = EXECUTION_TIMING_AT_CLOSE
    IMMEDIATE = EXECUTION_TIMING_IMMEDIATE


class ExecutionStyle(str, Enum):
    """
    實盤執行方式：策略只表達「要成交」還是「照價掛單」，**不碰券商的價格類型**

    換成券商接受的委託（限價／範圍市價、ROD／IOC、委託價）是執行層的事
    （`core/live/execution/`），依段落與商品決定。策略直接指定券商列舉值的話，
    同一支策略換個段落就可能送出交易所不收的組合——集合競價時段不收市價單，
    期交所不收「市價＋ROD」。

    **`MARKET` 是意圖，不保證送出的是市價單**：執行層會換成帶保護價的委託
    （集合競價掛漲跌停、連續交易時段加減幅度的限價或期貨的範圍市價），
    與券商列舉 `StockPriceType.MKT` 分屬不同型別，兩者不可混用。

    **回測忽略此欄位**：回測一律以策略給的價成交，正是 `MARKET` 的假設；
    `LIMIT` 在回測因此偏樂觀，差異由盤後 parity 量化。
    """

    MARKET = EXECUTION_STYLE_MARKET
    LIMIT = EXECUTION_STYLE_LIMIT
