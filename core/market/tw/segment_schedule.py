import datetime

from core.market.segment import SegmentSchedule, SegmentWindow
from core.utils import ExecutionTiming

"""
台灣市場的段落時窗表：實盤送單與 TICK 回測共用

回測依台股這張表決定逐筆鉤子在哪些 tick 呼叫（`IMMEDIATE` 的送單區間）、
收盤段的快照取哪個時點（`AT_CLOSE` 開始送單的時刻），與實盤是同一套時點。
"""


# 台股的段落時窗。
#
# 尾盤段 **13:25 才開始送單**：13:25 之前送出的限價單會在逐筆交易時段就成交，
# 成交價不是收盤價，與回測「以收盤價成交」的假設對不上，而且看起來完全正常。
# 13:29 停止送單讓委託進得了收盤集合競價；**13:35 才收線**，因為收盤集合競價的
# 成交回報要 13:30 之後才會進來——送完單就收線會讓當日成交明細出現一段空窗。
#
# 盤中逐筆段落**夾在開盤段收線（09:05）與尾盤段啟動（13:20）之間**：同一個帳號的
# 委託回報會推給每一條連線，兩個行程同時在場上時，各自都會收到對方的回報。
# 代價是 09:00～09:05 與 13:19 之後的盤中觸發看不到。
TW_STOCK_SEGMENTS: SegmentSchedule = {
    ExecutionTiming.AT_OPEN: SegmentWindow(
        submit_start=datetime.time(8, 30),
        submit_end=datetime.time(8, 59),
        drain_end=datetime.time(9, 5),
    ),
    ExecutionTiming.AT_CLOSE: SegmentWindow(
        submit_start=datetime.time(13, 25),
        submit_end=datetime.time(13, 29),
        drain_end=datetime.time(13, 35),
    ),
    ExecutionTiming.IMMEDIATE: SegmentWindow(
        submit_start=datetime.time(9, 5),
        submit_end=datetime.time(13, 18),
        drain_end=datetime.time(13, 19),
    ),
}

# 台期貨的段落時窗。
#
# ⚠️ **這組時點尚未以 TAIFEX 公告逐條核對**，用的是日盤 08:45~13:45 的常識值。
# 上正式環境前要在模擬環境實際驗一次：收盤規則寫錯不會報錯，只會讓整段安靜地不送單。
TW_FUTURES_SEGMENTS: SegmentSchedule = {
    ExecutionTiming.AT_OPEN: SegmentWindow(
        submit_start=datetime.time(8, 45),
        submit_end=datetime.time(8, 59),
        drain_end=datetime.time(9, 5),
    ),
    ExecutionTiming.AT_CLOSE: SegmentWindow(
        submit_start=datetime.time(13, 30),
        submit_end=datetime.time(13, 44),
        drain_end=datetime.time(13, 50),
    ),
}
