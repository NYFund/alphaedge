import datetime
import queue
from typing import List

from core.broker.base import CallbackQueue
from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_broker import ShioajiBroker
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.factory import build_live_trader
from core.live.intraday.event_loop import Event, EventKind, IntradayEventLoop, LoopStats
from core.live.trader import LiveTrader
from core.models import ExecutionReport, StockQuote

from .conftest import FakeBroker
from .test_intraday_event_loop import (
    START,
    FakeClock,
    make_loop,
    make_quote,
    make_report,
)
from .test_live_factory_and_entry import (  # noqa: F401  in_memory_history_db 是 autouse fixture
    LiveStockStrategy,
    in_memory_history_db,
)
from .test_shioaji_broker import FakeApi, FakeSession

"""
盤中迴圈的兩個缺口：心跳被行情餓死、收線後的回報不入帳

1. 心跳原本只在 queue 空等逾時時才跑。行情連續湧入、每筆間隔都不到心跳間隔時，
   queue 永遠不空，斷線偵測、重連與日終回補都會一直延後。改為依時刻觸發。
2. 迴圈時限（送單迄 13:18）一到就停，而回報仍導向迴圈的 queue；收線前一刻送出的單
   在 13:18～13:19 成交，本行程就看不到。改為收線時回報改回直接的 queue、
   清掉迴圈裡剩下的回報，之後的交給段落收尾在直接的 queue 上收到 `drain_end`。
"""


# === 心跳依時刻觸發 ===
def test_heartbeat_still_runs_while_quotes_keep_arriving() -> None:
    """queue 一直有行情（從不空等逾時）：心跳仍依間隔執行"""

    clock: FakeClock = FakeClock()
    beats: List[datetime.datetime] = []
    loop: IntradayEventLoop = make_loop(
        clock, on_heartbeat=lambda: beats.append(clock.now)
    )
    for _ in range(5000):
        loop.submit_quote(make_quote())

    # 入列本身也會讀時鐘，時限要從入列完的時刻起算
    loop.run_until(clock.now + datetime.timedelta(seconds=1))

    assert not loop.events.empty()  # 整段都沒有空等過
    assert len(beats) >= 10  # 心跳間隔 0.01 秒、跑了約 1 秒


def test_idle_queue_still_beats_on_timeout() -> None:
    """沒有事件時照舊以逾時當心跳"""

    clock: FakeClock = FakeClock()
    beats: List[int] = []
    loop: IntradayEventLoop = make_loop(clock, on_heartbeat=lambda: beats.append(1))

    stats: LoopStats = loop.run_until(START + datetime.timedelta(seconds=0.05))

    assert stats.heartbeats >= 1
    assert beats


# === 收線後的回報 ===
def test_flush_handles_executions_and_drops_quotes() -> None:
    """清 queue：回報照收、行情丟棄（收線後不再送新單）"""

    clock: FakeClock = FakeClock()
    quotes: List[StockQuote] = []
    reports: List[ExecutionReport] = []
    loop: IntradayEventLoop = make_loop(clock, quotes=quotes, reports=reports)
    loop.events.put(Event(EventKind.QUOTE, make_quote(), START))
    loop.events.put(Event(EventKind.EXECUTION, make_report("2330"), START))
    loop.events.put(Event(EventKind.QUOTE, make_quote("2317"), START))
    loop.events.put(Event(EventKind.EXECUTION, make_report("2317"), START))

    stats: LoopStats = loop.flush_executions()

    assert [report.symbol for report in reports] == ["2330", "2317"]
    assert quotes == []
    assert stats.executions == 2
    assert loop.events.empty()


def test_closing_the_loop_restores_direct_reports_and_applies_leftovers(
    dao: LiveTradeDAO,
) -> None:
    """
    收線：回報改回直接的 queue、迴圈裡剩下的回報套進去

    改回之後才進來的回報要讀得到——段落收尾的撤單與收回報走的就是那條 queue。
    """

    broker: FakeBroker = FakeBroker()
    trader: LiveTrader = build_live_trader(
        [LiveStockStrategy()],
        broker=broker,
        dao=dao,
        run_id="20261019090600",
        now_provider=lambda: START,
    )
    broker.connect()
    broker.subscribe_quotes(["2330"])

    clock: FakeClock = FakeClock()
    applied: List[ExecutionReport] = []
    loop: IntradayEventLoop = make_loop(clock, reports=applied)
    broker.route_events(loop.submit_quote, loop.submit_execution)
    assert isinstance(broker.execution_queue, CallbackQueue)

    # 迴圈已在時限停下，queue 裡還留著一筆成交回報
    broker.execution_queue.put(make_report("2330"))
    stats: LoopStats = LoopStats()

    trader.intraday._close_intraday_loop(loop, stats)

    assert [report.symbol for report in applied] == ["2330"]
    assert stats.executions == 1
    assert isinstance(broker.execution_queue, queue.Queue)

    late: ExecutionReport = make_report("2317")
    broker.execution_queue.put(late)
    assert broker.drain_execution_queue() == [late]


def test_shioaji_broker_rebinds_handlers_to_the_restored_queues() -> None:
    """
    Shioaji 閘道改回時要重綁：元件在綁定時拿走的是當下的 queue 物件，
    只換屬性的話回呼仍會往迴圈那個沒人讀的 queue 塞
    """

    limiter: RateLimiter = RateLimiter(time_source=lambda: 0.0, sleep=lambda s: None)
    broker: ShioajiBroker = ShioajiBroker(FakeSession(FakeApi(), limiter), limiter)
    broker.connect()
    broker.route_events(lambda quote: None, lambda report: None)

    broker.restore_event_routing()

    assert isinstance(broker.execution_queue, queue.Queue)
    assert isinstance(broker.quote_queue, queue.Queue)
    assert broker.execution_handler.execution_queue is broker.execution_queue
