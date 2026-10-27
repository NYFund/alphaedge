#!/bin/sh
# 存活監控只在台北 08:00～15:00 執行。
#
# launchd 只能固定每 5 分鐘觸發，不能指定時段；時段外直接結束，
# 否則半夜也會一直檢查「今天的段落有沒有跑」而誤報。
# 由 scripts/launchd/rehearsal_schedule.py 安裝的排程呼叫。

now=$(TZ=Asia/Taipei date +%H%M)
if [ "$now" -lt 0800 ] || [ "$now" -ge 1500 ]; then
    exit 0
fi

cd "$(dirname "$0")/../.." || exit 1
# 帶 --no-sync 的理由同 rehearsal_schedule.py 的 _UV_RUN_ARGS：不讓排程依賴網路。
# 段落表逐市場指定，與 rehearsal_schedule.py 的 _DAILY_JOBS 對齊：股票與期貨是兩個行程、
# 段落名相同，不帶市場的話股票尾盤的紀錄會把「期貨尾盤沒跑」遮住
# parity 補比排在 06:00 的資料更新之後（股票線兩個行程、期貨線一個）；
# watchdog 08:00 才開始跑，到時候它們早該結束
exec "$UV_BIN" run --no-sync python -m scripts.live_watchdog --expect \
    parity:stock=06:00 parity:futures=06:00 \
    open:stock=08:30 open:futures=08:40 intraday:stock=09:06 \
    close:stock=13:20 close:futures=13:28 \
    after_close:stock=14:30 after_close:futures=14:35
