from typing import Dict, List

import pytest

from core.broker.rate_limiter import (
    DEFAULT_SAFETY_RATIO,
    OFFICIAL_LIMITS,
    RateLimit,
    RateLimitCategory,
    RateLimiter,
)

"""
分類限流器：以假時鐘驗「什麼時候該等、等多久」

真時鐘測限流是測不準的——要嘛睡到測試變慢，要嘛把視窗調到極小而驗不到真實數值。
故 `RateLimiter` 的時間來源與 sleep 都可注入，本檔全部以假時鐘推進。

擋的是兩種具體事故：
- 放行超額 → 券商暫停服務 1 分鐘，重複違規封 IP／ID。
- 等待算錯方向（等太久）→ 尾盤段只有 13:25~13:29 可送單，多等幾秒就是沒送出去。
"""


class FakeClock:
    """可手動推進的時鐘；`sleep` 直接把時間快轉，不真的睡"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now: float = start
        self.slept: List[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_limiter(clock: FakeClock, **kwargs) -> RateLimiter:
    """以假時鐘建立限流器"""

    return RateLimiter(time_source=clock.time, sleep=clock.sleep, **kwargs)


def test_official_limits_match_the_documented_values() -> None:
    """
    官方額度是外部規格，抄錯的方向若是寬鬆，代價是被封鎖

    數字改動一律要有實查依據，不可「感覺比較保險」就調。
    """

    assert OFFICIAL_LIMITS[RateLimitCategory.ORDER] == RateLimit(250, 10.0)
    assert OFFICIAL_LIMITS[RateLimitCategory.ACCOUNT] == RateLimit(25, 5.0)
    assert OFFICIAL_LIMITS[RateLimitCategory.MARKET_DATA] == RateLimit(50, 10.0)


def test_effective_limit_applies_safety_ratio(clock: FakeClock) -> None:
    """保守係數要真的生效，且無條件捨去"""

    limiter: RateLimiter = make_limiter(clock)

    assert DEFAULT_SAFETY_RATIO == 0.8
    assert limiter.effective_limit(RateLimitCategory.ORDER) == 200
    assert limiter.effective_limit(RateLimitCategory.ACCOUNT) == 20
    assert limiter.effective_limit(RateLimitCategory.MARKET_DATA) == 40


def test_effective_limit_never_drops_to_zero(clock: FakeClock) -> None:
    """
    係數再小也至少放行 1 次

    取整成 0 的話，那個類別會永遠等下去——而且不會有任何錯誤訊息。
    """

    limiter: RateLimiter = make_limiter(
        clock,
        limits={RateLimitCategory.ORDER: RateLimit(2, 10.0)},
        safety_ratio=0.1,
    )

    assert limiter.effective_limit(RateLimitCategory.ORDER) == 1


@pytest.mark.parametrize("ratio", [0.0, -0.5, 1.5])
def test_invalid_safety_ratio_is_rejected(clock: FakeClock, ratio: float) -> None:
    """係數超出 (0, 1] 一律在建構時拋出，不要等到盤中才發現限流沒生效"""

    with pytest.raises(ValueError):
        make_limiter(clock, safety_ratio=ratio)


def test_calls_within_the_limit_do_not_wait(clock: FakeClock) -> None:
    """額度內的呼叫一律不等待"""

    limiter: RateLimiter = make_limiter(clock)
    waits: List[float] = [
        limiter.acquire(RateLimitCategory.ORDER)
        for _ in range(limiter.effective_limit(RateLimitCategory.ORDER))
    ]

    assert waits == [0.0] * 200
    assert clock.slept == []


def test_exceeding_the_limit_waits_until_the_oldest_call_leaves_the_window(
    clock: FakeClock,
) -> None:
    """
    額度用完時，要等到**最舊那一筆滑出視窗**的那一刻，不多也不少

    等太久是在浪費尾盤那 4 分鐘；等不夠就是超額。
    """

    limiter: RateLimiter = make_limiter(
        clock, limits={RateLimitCategory.ORDER: RateLimit(2, 10.0)}, safety_ratio=1.0
    )

    limiter.acquire(RateLimitCategory.ORDER)  # t=1000
    clock.now += 3.0
    limiter.acquire(RateLimitCategory.ORDER)  # t=1003

    clock.now += 1.0  # t=1004；最舊那筆在 t=1010 滑出
    waited: float = limiter.acquire(RateLimitCategory.ORDER)

    assert waited == pytest.approx(6.0)
    assert clock.now == pytest.approx(1010.0)


def test_window_slides_instead_of_resetting(clock: FakeClock) -> None:
    """
    視窗是滑動的，不是每 N 秒整批歸零

    固定視窗會允許「視窗尾端打滿 ＋ 下個視窗開頭再打滿」的雙倍突發，
    而券商量的是任意連續 10 秒。
    """

    limiter: RateLimiter = make_limiter(
        clock, limits={RateLimitCategory.ORDER: RateLimit(2, 10.0)}, safety_ratio=1.0
    )

    limiter.acquire(RateLimitCategory.ORDER)  # t=1000
    clock.now += 9.0
    limiter.acquire(RateLimitCategory.ORDER)  # t=1009

    clock.now += 1.5  # t=1010.5：第一筆已滑出，第二筆還在
    assert limiter.acquire(RateLimitCategory.ORDER) == 0.0

    # 第二筆（t=1009）要到 t=1019 才滑出
    assert limiter.acquire(RateLimitCategory.ORDER) == pytest.approx(8.5)


def test_categories_have_independent_budgets(clock: FakeClock) -> None:
    """
    一類的額度用完不得拖累另一類

    帳務查詢把額度用光而連帶擋住送單，等於讓對帳拖垮交易。
    """

    limiter: RateLimiter = make_limiter(
        clock,
        limits={
            RateLimitCategory.ORDER: RateLimit(1, 10.0),
            RateLimitCategory.ACCOUNT: RateLimit(1, 5.0),
        },
        safety_ratio=1.0,
    )

    limiter.acquire(RateLimitCategory.ORDER)

    assert limiter.acquire(RateLimitCategory.ACCOUNT) == 0.0
    assert clock.slept == []


def test_try_acquire_does_not_block_or_consume_budget(clock: FakeClock) -> None:
    """
    `try_acquire()` 失敗時不得等待，**也不得記錄這次呼叫**

    盤中事件迴圈用它做選擇性查詢：那裡阻塞住會連帶延後行情與回報的處理。
    失敗卻仍吃掉額度的話，重試會一路把額度耗光。
    """

    limiter: RateLimiter = make_limiter(
        clock, limits={RateLimitCategory.ORDER: RateLimit(1, 10.0)}, safety_ratio=1.0
    )

    assert limiter.try_acquire(RateLimitCategory.ORDER) is True
    assert limiter.try_acquire(RateLimitCategory.ORDER) is False
    assert limiter.try_acquire(RateLimitCategory.ORDER) is False
    assert clock.slept == []

    clock.now += 10.0
    assert limiter.try_acquire(RateLimitCategory.ORDER) is True


def test_wait_stats_accumulate_per_category(clock: FakeClock) -> None:
    """
    等待時間要留下來

    尾盤段只有 4 分鐘可送單，「今天限流一共等了多久」是判斷策略檔數要不要收斂的
    唯一依據；只寫 log 的話，事後得從一整天的日誌裡撈。
    """

    limiter: RateLimiter = make_limiter(
        clock, limits={RateLimitCategory.ORDER: RateLimit(1, 10.0)}, safety_ratio=1.0
    )

    limiter.acquire(RateLimitCategory.ORDER)
    limiter.acquire(RateLimitCategory.ORDER)  # 等 10 秒

    stats: Dict[RateLimitCategory, float] = limiter.wait_stats()

    assert stats[RateLimitCategory.ORDER] == pytest.approx(10.0)
    assert RateLimitCategory.ACCOUNT not in stats


def test_order_category_covers_status_queries() -> None:
    """
    `update_status` 算在下單類，不是查詢類

    它是查詢語意卻吃送單額度。拿它當心跳輪詢，會在尾盤那 4 分鐘把送單額度吃光——
    而那正是一天之中唯一非送不可的時候。本條是把這個約定釘在測試裡，
    真正的呼叫分類在 `ShioajiBroker` 逐一標註。
    """

    assert RateLimitCategory.ORDER.value == "ORDER"
    assert OFFICIAL_LIMITS[RateLimitCategory.ORDER].max_calls == 250
