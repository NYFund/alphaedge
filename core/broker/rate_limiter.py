import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Dict, Optional

from loguru import logger

"""
分類限流器：券商的呼叫額度是**依類別分開計算**的，所以限流也要分開

三類額度（Shioaji 官方值）：

| 類別 | 官方上限 | 涵蓋的呼叫 |
|------|----------|------------|
| `ORDER` | 250 次／10 秒 | `place_order`、`update_status`、`update_qty`、`update_price`、`cancel_order` |
| `ACCOUNT` | 25 次／5 秒 | `account_balance`、`list_positions`、`margin`、`list_settlements` |
| `MARKET_DATA` | 50 次／10 秒 | `snapshots`、`ticks`、`kbars`、`credit_enquires`、`short_stock_sources` |

兩件事一定要記住：

1. **`update_status` 算在下單類**。它是查詢語意卻吃送單額度，拿它當心跳輪詢會在
   尾盤那 4 分鐘把送單額度吃光——而那正是一天之中唯一非送不可的時候。
2. **額度是帳戶級的**。多個行程各自持有一個 limiter 時，每個都以為自己還有額度，
   合起來必然超額，而且事後誰都不知道是誰用掉的。故實盤一個帳戶只跑一個行程、
   共用同一個 limiter 實例。

超限的處置是「暫停服務 1 分鐘，重複違規封 IP／ID」，代價遠高於多等幾毫秒，
故預設只用官方上限的 80%。
"""

# 限流類別常量
RATE_LIMIT_CATEGORY_ORDER = "ORDER"
RATE_LIMIT_CATEGORY_ACCOUNT = "ACCOUNT"
RATE_LIMIT_CATEGORY_MARKET_DATA = "MARKET_DATA"

# 保守係數：官方上限乘上它才是實際允許的次數
DEFAULT_SAFETY_RATIO: float = 0.8

# 等待超過這個秒數就記一行 warning。
# **不是錯誤**：等待本來就是限流器該做的事。但尾盤段只有 13:25~13:29 可以送單，
# 單次等待超過 1 秒代表當天的委託筆數已經逼近額度，那是要調整策略檔數的訊號
SLOW_WAIT_WARN_SECONDS: float = 1.0


class RateLimitCategory(str, Enum):
    """限流類別；與券商的額度分類一一對應"""

    ORDER = RATE_LIMIT_CATEGORY_ORDER
    ACCOUNT = RATE_LIMIT_CATEGORY_ACCOUNT
    MARKET_DATA = RATE_LIMIT_CATEGORY_MARKET_DATA


@dataclass(frozen=True)
class RateLimit:
    """單一類別的額度：`window_seconds` 秒內最多 `max_calls` 次"""

    max_calls: int
    window_seconds: float


# 官方上限（2026-09-17 查 sinotrade.github.io）
OFFICIAL_LIMITS: Dict[RateLimitCategory, RateLimit] = {
    RateLimitCategory.ORDER: RateLimit(max_calls=250, window_seconds=10.0),
    RateLimitCategory.ACCOUNT: RateLimit(max_calls=25, window_seconds=5.0),
    RateLimitCategory.MARKET_DATA: RateLimit(max_calls=50, window_seconds=10.0),
}


class RateLimiter:
    """
    - Description:
        滑動視窗限流器

        用滑動視窗而不是 token bucket：券商的規則原文就是「每 N 秒合計 M 次」，
        滑動視窗與它逐字對應。token bucket 要另外挑一組 capacity 與 refill rate
        去逼近，逼近得不準的方向若是寬鬆，代價是被封 ID。

        視窗內存的是每次呼叫的時戳（最多 250 筆，記憶體可忽略），
        額度用完時睡到最舊那一筆滑出視窗為止。
    """

    def __init__(
        self,
        limits: Optional[Dict[RateLimitCategory, RateLimit]] = None,
        safety_ratio: float = DEFAULT_SAFETY_RATIO,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        - Description:
            建立限流器
        - Parameters:
            - limits: Optional[Dict[RateLimitCategory, RateLimit]]
                各類別的官方上限；預設為 `OFFICIAL_LIMITS`
            - safety_ratio: float
                保守係數（0 < ratio ≤ 1）。超限的處置是暫停服務甚至封鎖，
                代價遠高於多等幾毫秒
            - time_source: Callable[[], float]
                取得目前時刻的函式。**預設是 `time.monotonic` 而不是 `time.time`**：
                限流量的是經過的時間，系統時鐘被 NTP 往回調時，牆上時鐘會讓
                視窗突然變寬而放行超額的呼叫
            - sleep: Callable[[float], None]
                等待函式；測試注入假時鐘用
        - Raise:
            - ValueError
                `safety_ratio` 不在 (0, 1] 之間
        """

        if not 0 < safety_ratio <= 1:
            raise ValueError(f"safety_ratio 必須在 (0, 1] 之間，收到 {safety_ratio}")

        self.safety_ratio: float = safety_ratio
        self.limits: Dict[RateLimitCategory, RateLimit] = dict(
            limits if limits is not None else OFFICIAL_LIMITS
        )
        self._time_source: Callable[[], float] = time_source
        self._sleep: Callable[[float], None] = sleep

        # 回呼跑在券商自己的執行緒上，可能與主執行緒同時進來；視窗的讀寫要上鎖。
        # **睡覺時不持有鎖**，否則等待的那一方會把其他類別一起擋住
        self._lock: threading.Lock = threading.Lock()
        self._calls: Dict[RateLimitCategory, Deque[float]] = defaultdict(deque)
        self._wait_seconds: Dict[RateLimitCategory, float] = defaultdict(float)

    def effective_limit(self, category: RateLimitCategory) -> int:
        """
        - Description:
            套用保守係數後實際允許的次數；至少為 1

            無條件捨去而不四捨五入：往上取整會讓保守係數在某些數值下失效。
        - Parameters:
            - category: RateLimitCategory
                限流類別
        - Return:
            - int
                視窗內允許的呼叫次數
        """

        return max(int(self.limits[category].max_calls * self.safety_ratio), 1)

    def _wait_needed(self, category: RateLimitCategory, now: float) -> float:
        """計算還要等多久才有額度；有額度時回傳 0 並就地記錄本次呼叫（需持有鎖）"""

        window: float = self.limits[category].window_seconds
        calls: Deque[float] = self._calls[category]

        while calls and now - calls[0] >= window:
            calls.popleft()

        if len(calls) < self.effective_limit(category):
            calls.append(now)
            return 0.0

        # 最舊那一筆滑出視窗的時刻就是下一個有額度的時刻
        return window - (now - calls[0])

    def acquire(self, category: RateLimitCategory) -> float:
        """
        - Description:
            取得一次呼叫額度；額度不足時阻塞等待

            **阻塞而不是拋出**：委託送不出去比晚幾百毫秒送出嚴重得多。
            真正需要知道「等了多久」的是盤後檢討，故等待時間會累計下來。
        - Parameters:
            - category: RateLimitCategory
                限流類別
        - Return:
            - float
                本次實際等待的秒數（沒等到就是 0.0）
        """

        waited: float = 0.0
        while True:
            with self._lock:
                wait: float = self._wait_needed(category, self._time_source())
                if wait <= 0:
                    self._wait_seconds[category] += waited
                    break

            self._sleep(wait)
            waited += wait

        if waited >= SLOW_WAIT_WARN_SECONDS:
            logger.warning(
                f"限流等待 {waited:.2f} 秒（{category.value}）："
                f"目前額度為 {self.effective_limit(category)} 次／"
                f"{self.limits[category].window_seconds:.0f} 秒"
            )
        return waited

    def try_acquire(self, category: RateLimitCategory) -> bool:
        """
        - Description:
            非阻塞地取得額度；沒有額度時回傳 False，**不等待也不記錄呼叫**

            給「等不起」的呼叫端用，例如盤中事件迴圈裡的選擇性查詢：
            那裡阻塞住會連帶延後行情與回報的處理。
        - Parameters:
            - category: RateLimitCategory
                限流類別
        - Return:
            - bool
                是否取得額度
        """

        with self._lock:
            return self._wait_needed(category, self._time_source()) <= 0

    def wait_stats(self) -> Dict[RateLimitCategory, float]:
        """各類別累計的等待秒數；盤後報表用"""

        with self._lock:
            return dict(self._wait_seconds)

    def reset(self) -> None:
        """清空視窗與統計；**只給測試與換日使用**，盤中呼叫等於繞過限流"""

        with self._lock:
            self._calls.clear()
            self._wait_seconds.clear()
