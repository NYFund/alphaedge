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
exec "$UV_BIN" run python -m scripts.live_watchdog
