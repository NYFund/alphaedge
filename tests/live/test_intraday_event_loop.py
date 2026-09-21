import datetime
import queue
from typing import List, Optional

from core.live.intraday.event_loop import (
    Event,
    EventKind,
    IntradayEventLoop,
    LoopStats,
)
from core.models import ExecutionReport, StockQuote
from core.utils import Action, Scale

"""
盤中事件迴圈

**一個 queue 不是三個**：行情與回報都跑在券商的執行緒上，分開就得輪詢，
而輪詢會讓「同一筆成交是先記進帳戶、還是先被下一筆報價觸發新訊號」變成不確定的。

**時鐘一律注入**：靠真的 `sleep` 驗心跳會讓測試慢且不穩；假時鐘讓「過了 40 秒」
變成一行賦值。
"""

START: datetime.datetime = datetime.datetime(2026, 9, 21, 9, 30, 0)


class FakeClock:
    """
    每讀一次就前進一點點的假時鐘

    **不是固定不動的**：真實時鐘會自己走，而迴圈的結束條件看的就是它。
    固定不動的假時鐘會讓 `run_until()` 永遠不結束——那驗到的是保險絲，
    不是迴圈本身。大幅跳躍（模擬行情中斷）用 `advance()` 明確指定。
    """

    TICK_SECONDS: float = 0.001

    def __init__(self, start: datetime.datetime = START) -> None:
        self.now: datetime.datetime = start

    def __call__(self) -> datetime.datetime:
        current: datetime.datetime = self.now
        self.now = self.now + datetime.timedelta(seconds=self.TICK_SECONDS)
        return current

    def advance(self, seconds: float) -> None:
        self.now = self.now + datetime.timedelta(seconds=seconds)


def make_quote(symbol: str = "2330", price: float = 1000.0) -> StockQuote:
    return StockQuote(
        stock_id=symbol,
        scale=Scale.TICK,
        date=START,
        cur_price=price,
        close=price,
    )


def make_report(symbol: str = "2330") -> ExecutionReport:
    return ExecutionReport(
        broker_seqno="1",
        broker_trade_id="T1",
        symbol=symbol,
        action=Action.BUY,
        price=1000.0,
        volume=1,
        ts=START,
    )


def make_loop(
    clock: FakeClock,
    quotes: Optional[List[StockQuote]] = None,
    reports: Optional[List[ExecutionReport]] = None,
    degradations: Optional[List[str]] = None,
    **kwargs: object,
) -> IntradayEventLoop:
    """把三個 handler 接成可檢查的清單"""

    return IntradayEventLoop(
        on_quote=lambda quote: (quotes if quotes is not None else []).append(quote),
        on_execution=lambda report: (reports if reports is not None else []).append(
            report
        ),
        on_market_data_lost=lambda reason: (
            degradations if degradations is not None else []
        ).append(reason),
        now_provider=clock,
        heartbeat_seconds=0.01,
        **kwargs,
    )


# === 單一 queue、依序處理 ===
def test_quotes_and_executions_share_one_queue_in_arrival_order() -> None:
    """
    兩種事件依**入列順序**處理

    分兩個 queue 的話這個順序就不確定了——同一筆成交是先記進帳戶還是先被下一筆
    報價觸發新訊號，結果會不一樣，而且不會有任何錯誤。
    """

    clock: FakeClock = FakeClock()
    seen: List[str] = []
    loop: IntradayEventLoop = IntradayEventLoop(
        on_quote=lambda quote: seen.append(f"quote:{quote.symbol}"),
        on_execution=lambda report: seen.append(f"fill:{report.symbol}"),
        on_market_data_lost=lambda reason: None,
        now_provider=clock,
        heartbeat_seconds=0.01,
    )

    loop.submit_quote(make_quote("2330"))
    loop.submit_execution(make_report("2317"))
    loop.submit_quote(make_quote("2454"))

    stats: LoopStats = loop.run_until(clock.now + datetime.timedelta(seconds=0.05))

    assert seen == ["quote:2330", "fill:2317", "quote:2454"]
    assert (stats.quotes, stats.executions) == (2, 1)


def test_strategy_hook_is_called_once_per_tick() -> None:
    """逐筆觸發：鉤子被呼叫的次數要等於 tick 筆數"""

    clock: FakeClock = FakeClock()
    quotes: List[StockQuote] = []
    loop: IntradayEventLoop = make_loop(clock, quotes=quotes)

    for price in (1000.0, 1001.0, 1002.0, 1003.0):
        loop.submit_quote(make_quote(price=price))

    loop.run_until(clock.now + datetime.timedelta(seconds=0.05))

    assert len(quotes) == 4
    assert [quote.cur_price for quote in quotes] == [1000.0, 1001.0, 1002.0, 1003.0]


def test_handler_failure_does_not_stop_the_loop() -> None:
    """
    一筆報價處理失敗，後面的成交回報照樣要收得到

    讓例外上拋的話帳戶會停在錯的狀態，而那比少算一筆訊號嚴重得多。
    """

    clock: FakeClock = FakeClock()
    reports: List[ExecutionReport] = []

    def exploding(quote: StockQuote) -> None:
        raise ValueError("報價欄位對不上")

    loop: IntradayEventLoop = IntradayEventLoop(
        on_quote=exploding,
        on_execution=reports.append,
        on_market_data_lost=lambda reason: None,
        now_provider=clock,
        heartbeat_seconds=0.01,
    )

    loop.submit_quote(make_quote())
    loop.submit_execution(make_report())

    stats: LoopStats = loop.run_until(clock.now + datetime.timedelta(seconds=0.05))

    assert stats.handler_errors == 1
    assert len(reports) == 1


# === 心跳：行情中斷 ===
def test_silence_beyond_the_limit_reports_market_data_lost() -> None:
    """超過上限沒有任何報價 → 送出降級事件"""

    clock: FakeClock = FakeClock()
    degradations: List[str] = []
    loop: IntradayEventLoop = make_loop(
        clock, degradations=degradations, silence_limit_seconds=30.0
    )

    loop.submit_quote(make_quote())
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    clock.advance(45)
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    assert len(degradations) == 1
    assert "行情中斷" in degradations[0]
    assert loop.market_data_lost is True


def test_market_data_lost_is_reported_only_once() -> None:
    """
    **只降級一次**

    降級是單向的、恢復一律人工；重複送事件只會洗版告警，
    讓真正需要注意的那一則被淹掉。
    """

    clock: FakeClock = FakeClock()
    degradations: List[str] = []
    loop: IntradayEventLoop = make_loop(
        clock, degradations=degradations, silence_limit_seconds=30.0
    )

    loop.submit_quote(make_quote())
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    for _ in range(3):
        clock.advance(45)
        loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    assert len(degradations) == 1


def test_no_quote_yet_is_not_treated_as_a_disconnection() -> None:
    """
    還沒收過任何報價時不可判中斷

    開盤前本來就沒有報價；當成「靜默很久」會在第一秒就誤判。
    """

    clock: FakeClock = FakeClock()
    degradations: List[str] = []
    loop: IntradayEventLoop = make_loop(
        clock, degradations=degradations, silence_limit_seconds=1.0
    )

    clock.advance(600)
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    assert degradations == []
    assert loop.silent_seconds() is None


# === 報價過期 ===
def test_stale_quote_is_detected_per_symbol() -> None:
    """
    逐筆觸發沒有「片」，過期改以**每檔各自**的最後更新時間判斷

    某一檔不動不代表整個市場不動；用全市場的時間判斷會讓冷門股永遠看起來新鮮。
    """

    clock: FakeClock = FakeClock()
    loop: IntradayEventLoop = make_loop(clock, stale_quote_seconds=30.0)

    loop.submit_quote(make_quote("2330"))
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    clock.advance(40)
    loop.submit_quote(make_quote("2317"))
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    assert loop.is_quote_stale("2330") is True
    assert loop.is_quote_stale("2317") is False
    assert loop.stale_symbols() == ["2330"]


def test_never_seen_symbol_counts_as_stale() -> None:
    """
    沒收過報價的標的一律視為過期

    拿不到報價就不該對它開新倉——預設為新鮮等於用一個不存在的價格下單。
    """

    assert make_loop(FakeClock()).is_quote_stale("9999") is True


def test_quote_freshness_uses_the_event_timestamp() -> None:
    """
    過期判定取**事件自己的時戳**，不是處理當下的時間

    前面塞了一批行情時兩者可能差好幾秒，用處理時間會讓過期的報價看起來很新鮮。
    """

    clock: FakeClock = FakeClock()
    events: queue.Queue = queue.Queue()
    loop: IntradayEventLoop = make_loop(
        clock, stale_quote_seconds=10.0, event_queue=events
    )

    # 40 秒前入列、現在才被處理
    events.put(
        Event(
            EventKind.QUOTE,
            make_quote("2330"),
            clock.now - datetime.timedelta(seconds=40),
        )
    )
    loop.run_until(clock.now + datetime.timedelta(seconds=0.02))

    assert loop.is_quote_stale("2330") is True


def test_loop_stops_at_the_deadline() -> None:
    """時限一到就停，積壓的行情不再處理——過期的訊號送出去只會被退"""

    clock: FakeClock = FakeClock()
    quotes: List[StockQuote] = []
    loop: IntradayEventLoop = make_loop(clock, quotes=quotes)

    for _ in range(5):
        loop.submit_quote(make_quote())

    stats: LoopStats = loop.run_until(clock.now)

    assert stats.quotes == 0
    assert quotes == []


def test_frozen_clock_does_not_hang_the_loop() -> None:
    """
    時鐘停住時迴圈要自己跳出

    結束條件只看時鐘的話，時鐘一停這個迴圈就永遠不會結束——段落不收線、
    盤後作業不會開始，而且沒有任何錯誤訊息。同一個坑 `LiveTrader` 的等待迴圈
    已經踩過一次，故這裡的保險絲**獨立於時鐘**：數的是心跳次數。
    """

    frozen: datetime.datetime = START
    loop: IntradayEventLoop = IntradayEventLoop(
        on_quote=lambda quote: None,
        on_execution=lambda report: None,
        on_market_data_lost=lambda reason: None,
        now_provider=lambda: frozen,
        heartbeat_seconds=0.001,
        stuck_clock_heartbeats=5,
    )

    stats: LoopStats = loop.run_until(frozen + datetime.timedelta(hours=1))

    assert stats.quotes == 0
    assert stats.heartbeats <= 5
