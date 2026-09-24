import datetime
import queue
from typing import Any, List, Optional

import pandas as pd
import pytest

from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter
from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.models import BaseOrder, FuturesQuote, StockQuote
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1

"""
報價日期的型別在回測與實盤必須一致

**同一支策略要能不改寫地跑兩邊**，而 `BaseQuote.date` 宣告成
`Union[datetime.date, datetime.datetime]`——型別註解本身就允許兩種，
於是兩條路徑給了不同的東西而沒有人發現：

| 路徑 | `StockQuote.date` |
|------|-------------------|
| 回測（價格表） | `datetime.date` |
| 實盤（券商快照） | 帶時區的 `datetime.datetime` |

症狀是策略拿 `quote.date` 去跟交易日清單比較時拋
`TypeError: can't compare datetime.datetime to datetime.date`——
**回測一路綠燈，實盤在尾盤段當場掛掉並把策略降級**（2026-09-24 演練實測）。
開盤段踩不到：那裡是 `PreOpenStockQuote`，日期由本地時鐘給，本來就是 `date`。

不變式：**日線級別的報價，兩條路徑都帶 `datetime.date`**。
快照的精確時刻不會遺失——它在事件封包的 `ts` 上，實盤委託另有 `created_at`。
"""

TAIPEI_NOON_NS: int = 1790076792276623000  # 2026-09-22 台北 11:33:12（實測值）


class FakeSnapshot:
    """對應 `shioaji.data.Snapshot`"""

    def __init__(self, code: str = "2330", ts: Optional[int] = TAIPEI_NOON_NS) -> None:
        self.code: str = code
        self.open: float = 990.0
        self.high: float = 1010.0
        self.low: float = 985.0
        self.close: float = 1000.0
        self.volume: int = 3
        self.total_volume: int = 5200
        self.ts: Optional[int] = ts


class FakeFuturesSnapshot(FakeSnapshot):
    """期貨版的快照替身；欄位與股票相同，只有代碼換成月份字母碼"""

    def __init__(self) -> None:
        super().__init__(code="TXFJ6")


class FakeContract:
    """對應 `shioaji.contracts.Future`，只帶報價轉換讀得到的欄位"""

    def __init__(self, code: str = "TXFJ6") -> None:
        self.code: str = code
        self.symbol: str = "TXF202610"
        self.multiplier: int = 200
        self.unit: int = 0


@pytest.fixture
def stream() -> ShioajiQuoteStream:
    """快照轉換不會用到 api，給一個佔位物件即可"""

    return ShioajiQuoteStream(object(), RateLimiter(), queue.Queue())


def backtest_day_quote() -> StockQuote:
    """回測那一側的日線報價：由價格表的一列建出來"""

    frame: pd.DataFrame = pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "開盤價": 990.0,
                "最高價": 1010.0,
                "最低價": 985.0,
                "收盤價": 1000.0,
                "成交股數": 5_200_000,
            }
        ]
    )
    return StockQuoteAdapter.from_day_rows(frame, datetime.date(2026, 9, 22))[0]


# === 不變式 ===
def test_live_day_quote_carries_a_plain_date(stream: ShioajiQuoteStream) -> None:
    """
    券商快照建出來的日線報價，`date` 必須是 `datetime.date`

    **`type(...) is` 而不是 `isinstance`**：`datetime.datetime` 是
    `datetime.date` 的子類別，`isinstance` 對兩者都成立——
    用它來釘這條不變式等於沒有釘。
    """

    quote: Optional[StockQuote] = stream.from_stock_snapshot(FakeSnapshot())

    assert quote is not None
    assert type(quote.date) is datetime.date


def test_both_paths_agree_on_the_date_type(stream: ShioajiQuoteStream) -> None:
    """兩條路徑給的型別要一模一樣——策略才可能不改寫地跑兩邊"""

    live: Optional[StockQuote] = stream.from_stock_snapshot(FakeSnapshot())

    assert live is not None
    assert type(live.date) is type(backtest_day_quote().date)


def test_live_futures_day_quote_carries_a_plain_date(
    stream: ShioajiQuoteStream,
) -> None:
    """期貨同樣：策略基底的 `normalize_quote_date()` 不該是必要的防護"""

    quote: Optional[FuturesQuote] = stream.from_futures_snapshot(
        FakeFuturesSnapshot(), FakeContract()
    )

    assert quote is not None
    assert type(quote.date) is datetime.date


# === 實際會炸的那條路徑 ===
def test_strategy_can_compare_a_live_quote_date_with_its_calendar(
    stream: ShioajiQuoteStream,
) -> None:
    """
    策略拿實盤報價的日期去比對交易日清單，不可拋型別錯誤

    這是 2026-09-24 演練尾盤段的實際崩潰點：
    `MomentumStrategy1.get_previous_trading_date()` 的
    `date <= self.trading_days[-1]`。
    """

    quote: Optional[StockQuote] = stream.from_stock_snapshot(FakeSnapshot())
    assert quote is not None

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.trading_days = [
        datetime.date(2026, 9, 18),
        datetime.date(2026, 9, 21),
        datetime.date(2026, 9, 22),
    ]

    assert strategy.get_previous_trading_date(quote.date) == datetime.date(2026, 9, 21)


def test_orders_built_from_live_quotes_match_the_backtest_shape(
    stream: ShioajiQuoteStream,
) -> None:
    """
    訂單的 `date` 直接取自報價，型別會一路傳下去

    `StockOrder(date=signal.quote.date)`——兩邊不一致的話，同一支策略
    在實盤與回測寫出形狀不同的委託紀錄，而 parity 比對正是逐欄位比這些。
    """

    from core.models import StockOrder
    from core.utils import Action, PositionType

    live: Optional[StockQuote] = stream.from_stock_snapshot(FakeSnapshot())
    assert live is not None

    def build(quote: StockQuote) -> BaseOrder:
        return StockOrder(
            stock_id=quote.stock_id,
            date=quote.date,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=quote.close,
            volume=1,
        )

    assert type(build(live).date) is type(build(backtest_day_quote()).date)


# === 時戳本身仍要解得對 ===
def test_the_snapshot_timestamp_is_still_decoded_correctly(
    stream: ShioajiQuoteStream,
) -> None:
    """
    **不可以用「丟掉時間」來達成型別一致**

    `Snapshot.ts` 是把台北牆上時間當成 UTC 編出來的奈秒值，不是真 epoch。
    當成真 epoch 換算會整條偏 8 小時（盤中報價落到晚上），當成秒則得到 1970 年。
    這個值是 2026-09-22 台北 11:33:12 在模擬環境實際取到的。

    解碼本身由 `_resolve_date()` 負責，仍要保留完整時刻；
    只有掛到報價上的那一格取日期部分。
    """

    moment: datetime.datetime = stream._resolve_date(FakeSnapshot())

    assert moment.replace(tzinfo=None) == datetime.datetime(
        2026, 9, 22, 11, 33, 12, 276623
    )
    assert moment.utcoffset() == datetime.timedelta(hours=8)


def test_quote_date_agrees_with_the_decoded_timestamp(
    stream: ShioajiQuoteStream,
) -> None:
    """取的是同一個時刻的日期部分，不是本地時鐘的今天"""

    snapshot: FakeSnapshot = FakeSnapshot()
    quote: Optional[StockQuote] = stream.from_stock_snapshot(snapshot)

    assert quote is not None
    assert quote.date == stream._resolve_date(snapshot).date()
    assert quote.date == datetime.date(2026, 9, 22)


def test_pre_open_futures_quote_carries_a_plain_date() -> None:
    """
    期貨盤前報價同樣要帶 `date`

    股票盤前取的是 `self._now().date()`，期貨一度直接用 `self._now()`——
    同一個段落的兩個市場給出不同型別，而這種不對稱沒有理由，
    只是兩邊各寫一次時沒有對齊。
    """

    from core.models import PreOpenFuturesQuote
    from core.utils import FuturesSession

    quote: PreOpenFuturesQuote = PreOpenFuturesQuote(
        product="TX",
        expiry="202610",
        date=datetime.datetime(2026, 9, 24, 8, 45),
        reference_price=24000.0,
        session=FuturesSession.DAY,
        multiplier=200,
    )

    # 模型本身不限制 `date` 的型別（這裡刻意餵 `datetime` 也照收），
    # 這條只確認盤前報價建得出來；不變式由下面資料源那一側的組裝路徑把關
    assert quote.reference_price == 24000.0

    feed_quotes: List[Any] = build_pre_open_futures_quotes()
    assert feed_quotes, "資料源沒有產出盤前報價"
    assert type(feed_quotes[0].date) is datetime.date


def build_pre_open_futures_quotes() -> List[Any]:
    """走資料源真正的盤前組裝路徑，而不是自己建一個報價物件"""

    from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed

    class Contract:
        symbol: str = "TX202610"
        reference: float = 24000.0
        limit_up: float = 26400.0
        limit_down: float = 21600.0
        multiplier: int = 200

    feed: TwFuturesLiveDataFeed = TwFuturesLiveDataFeed(
        broker=None,
        calendar_sources=[],
        now_provider=lambda: datetime.datetime(2026, 9, 24, 8, 45),
    )
    return feed._build_pre_open_quotes([Contract()])
