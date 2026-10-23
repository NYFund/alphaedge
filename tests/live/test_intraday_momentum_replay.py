import datetime
import queue
from pathlib import Path
from typing import List

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.quote_replay import replay_quotes
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.models import BaseQuote, StockPosition
from core.portfolio.signal import Signal
from core.utils import Action, PositionType
from strategies.stock.intraday_momentum_strategy import IntradayMomentumStrategy
from tests.test_intraday_momentum_strategy import STOP_PRICE, make_strategy

"""
盤中動能策略的錄製行情回放：觸發與停損走完整的實盤轉換路徑

錄製檔與 `fixtures/recorded_ticks.jsonl` 同一個格式（欄位取自真實推播），內容是手排的
一段走勢：試撮假突破 → 量不夠的觸發 → 量到門檻進場 → 跌回 8% 以下停損。
**經過 `ShioajiQuoteStream.from_tick_message()`**，所以試撮過濾、當日累計量、
帶時區的報價時刻都與實盤同一條路徑；策略看到的就是實盤會看到的報價。

平盤價 100（觸發價 109、停損價 107.5）由 `make_strategy()` 的假價格表提供。
"""

FIXTURE: Path = Path(__file__).parent / "fixtures" / "intraday_momentum_ticks.jsonl"


def replay() -> List[BaseQuote]:
    """走實盤那份轉換把錄製檔還原成報價；試撮會在這一步被濾掉"""

    stream: ShioajiQuoteStream = ShioajiQuoteStream(
        object(), RateLimiter(), queue.Queue()
    )
    return replay_quotes(FIXTURE, stream)


def run_session(strategy: IntradayMomentumStrategy) -> List[Signal]:
    """
    把報價逐筆餵給開倉與停損鉤子（與實盤 `IMMEDIATE` 段落相同：一次一筆）

    進場訊號視為立即全數成交：實盤由成交回報建立部位，日期是成交時刻（帶時區）。
    """

    signals: List[Signal] = []
    for quote in replay():
        opened: List[Signal] = strategy.generate_open_signals([quote])
        for signal in opened:
            strategy.account.positions.append(
                StockPosition(
                    id=len(strategy.account.positions) + 1,
                    stock_id=quote.symbol,
                    position_type=PositionType.LONG,
                    date=quote.date,
                    price=signal.order_price,
                    volume=2,
                )
            )
        signals.extend(opened)
        signals.extend(strategy.generate_stop_loss_signals([quote]))
    return signals


def test_replay_filters_the_pre_open_trial_match() -> None:
    """08:59 的試撮 109.9／9,000 張不是成交，不可觸發進場"""

    quotes: List[BaseQuote] = replay()

    assert len(quotes) == 7
    assert quotes[0].date.time() == datetime.time(9, 10)


def test_replay_enters_once_and_stops_out() -> None:
    """
    量到門檻那一筆才進場，之後只進場一次；跌到停損價時停損一次，10 秒後不重送

    09:30 現價 109 已到觸發價但累計量 4,000 張，不進場；09:31 量到 5,200 張才進。
    """

    signals: List[Signal] = run_session(make_strategy())

    assert [(s.action, s.order_price, s.quote.date.time()) for s in signals] == [
        (Action.BUY, 109.5, datetime.time(9, 31)),
        (Action.SELL, STOP_PRICE, datetime.time(10, 5)),
    ]


def test_replay_is_deterministic() -> None:
    """同一份錄製餵兩次，訊號完全相同（逐筆策略只能用重放驗確定性）"""

    first: List[Signal] = run_session(make_strategy())
    second: List[Signal] = run_session(make_strategy())

    assert [(s.action, s.order_price, s.quote.date) for s in first] == [
        (s.action, s.order_price, s.quote.date) for s in second
    ]
