import datetime
import queue
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import pytest
import shioaji as sj

from core.broker.rate_limiter import RateLimitCategory, RateLimiter
from core.broker.tw.shioaji_quote_stream import (
    SNAPSHOT_BATCH_SIZE,
    ShioajiQuoteStream,
)
from core.models import FuturesQuote, StockQuote
from core.utils import FuturesSession, Scale

"""
即時行情：策略讀到的報價物件必須和回測一模一樣

否則同一支策略在兩邊會拿到不同結構，「策略層不分家」這條前提就不成立。

本檔特別盯兩個**不會報錯、只會讓訊號默默不成立**的取值錯誤：
- `volume` 取成「該筆成交量」而不是「當日累計」→ 成交量門檻永遠不達標。
- 時戳當成秒而不是奈秒 → 日期變成 1970 年，而那個日期會一路寫進部位與報表。
"""


class FakeSnapshot:
    """對應 `shioaji.data.Snapshot`"""

    def __init__(
        self,
        code: str = "2330",
        open: float = 990.0,
        high: float = 1010.0,
        low: float = 985.0,
        close: float = 1000.0,
        volume: int = 3,
        total_volume: int = 5200,
        ts: Optional[int] = 1789000000_000_000_000,
    ) -> None:
        self.code: str = code
        self.open: float = open
        self.high: float = high
        self.low: float = low
        self.close: float = close
        self.volume: int = volume  # 該筆成交量
        self.total_volume: int = total_volume  # 當日累計成交量
        self.ts: Optional[int] = ts


class FakeContract:
    def __init__(
        self,
        code: str = "2330",
        symbol: str = "TSE2330",
        multiplier: int = 0,
        unit: int = 0,
    ) -> None:
        self.code: str = code
        self.symbol: str = symbol
        self.multiplier: int = multiplier
        self.unit: int = unit


class FakeApi:
    """
    shioaji 1.7 的形狀：行情回呼與訂閱直接掛在 api 上

    **不提供 `api.quote`**：1.7 起那只是已棄用的轉接層，假物件若還提供，
    程式退回舊寫法時這份測試也驗不出來。
    """

    def __init__(self, snapshots: Optional[List[FakeSnapshot]] = None) -> None:
        self.callbacks: Dict[str, Any] = {}
        self.subscribed: List[tuple] = []
        self.unsubscribed: List[tuple] = []
        self._snapshots: List[FakeSnapshot] = snapshots or []
        self.snapshot_calls: List[int] = []

    def set_on_tick_stk_v1_callback(self, func: Any) -> None:
        self.callbacks["tick_stk"] = func

    def set_on_bidask_stk_v1_callback(self, func: Any) -> None:
        self.callbacks["bidask_stk"] = func

    def set_on_tick_fop_v1_callback(self, func: Any) -> None:
        self.callbacks["tick_fop"] = func

    def set_on_bidask_fop_v1_callback(self, func: Any) -> None:
        self.callbacks["bidask_fop"] = func

    def subscribe(self, contract: Any, quote_type: Any, version: Any) -> None:
        self.subscribed.append((contract.code, quote_type))

    def unsubscribe(self, contract: Any, quote_type: Any, version: Any) -> None:
        self.unsubscribed.append((contract.code, quote_type))

    def snapshots(self, contracts: Sequence[Any]) -> List[FakeSnapshot]:
        self.snapshot_calls.append(len(contracts))
        codes: set = {getattr(c, "code", "") for c in contracts}
        return [s for s in self._snapshots if s.code in codes]


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(time_source=lambda: 0.0, sleep=lambda seconds: None)


def make_stream(api: FakeApi, limiter: RateLimiter) -> ShioajiQuoteStream:
    return ShioajiQuoteStream(api, limiter, queue.Queue())


# === 轉換語意 ===
def test_volume_uses_total_volume(limiter: RateLimiter) -> None:
    """
    `volume` 取**當日累計**成交量

    快照的 `volume` 是該筆成交量。取錯的話，「當日成交量 ≥ 5000 張」這類門檻
    永遠不會成立，策略整天不開倉而且不會有任何錯誤訊息。
    """

    snapshot: FakeSnapshot = FakeSnapshot(volume=3, total_volume=5200)
    quote: StockQuote = make_stream(FakeApi(), limiter).from_stock_snapshot(snapshot)

    assert quote.volume == 5200


def test_close_and_cur_price_are_the_same_provisional_value(
    limiter: RateLimiter,
) -> None:
    """
    盤中的 `close` 是暫定值，與 `cur_price` 同值

    策略拿它算「今天漲幅」得到的是此刻的漲幅，不是收盤漲幅——
    那正是日頻策略要拆成開盤段與尾盤段的原因。
    """

    quote: StockQuote = make_stream(FakeApi(), limiter).from_stock_snapshot(
        FakeSnapshot()
    )

    assert quote.close == quote.cur_price == 1000.0
    assert quote.scale is Scale.DAY


def test_quote_fields_match_the_backtest_shape(limiter: RateLimiter) -> None:
    """轉出的報價物件與回測同款：策略讀到的結構不能因為換了資料來源而變"""

    quote: StockQuote = make_stream(FakeApi(), limiter).from_stock_snapshot(
        FakeSnapshot()
    )

    assert isinstance(quote, StockQuote)
    assert (quote.stock_id, quote.open, quote.high, quote.low) == (
        "2330",
        990.0,
        1010.0,
        985.0,
    )
    assert quote.signal_close == 1000.0  # 未啟用還原時等於 close


def test_timestamp_is_parsed_as_nanoseconds(limiter: RateLimiter) -> None:
    """
    `Snapshot.ts` 是 **epoch 奈秒**

    當成秒來解會得到 1970 年，而那個日期會一路寫進部位與報表。

    **斷言對著 `_resolve_date()` 而不是 `quote.date`**：解碼是這支函式的職責，
    而掛到報價上的那一格只取日期部分（日線報價的 `date` 兩條路徑都是
    `datetime.date`，見 `test_quote_date_parity.py`）。
    對著報價驗的話，時區與時分秒根本看不到。
    """

    moment: datetime.datetime = make_stream(FakeApi(), limiter)._resolve_date(
        FakeSnapshot()
    )

    assert moment.year == 2026
    assert moment.tzinfo is not None


def test_timestamp_is_taipei_wall_clock_not_epoch(limiter: RateLimiter) -> None:
    """
    `Snapshot.ts` 是台北牆上時間當成 UTC 編碼，不是真正的 epoch

    這個值是 2026-09-22 台北 11:33:12 在模擬環境實際取到的快照時戳。
    當成真 epoch 換到台北時區會得到 19:33，盤中報價落到晚上，
    而日期不變，只比日期的檢查看不出來。
    """

    moment: datetime.datetime = make_stream(FakeApi(), limiter)._resolve_date(
        FakeSnapshot(ts=1790076792276623000)
    )

    assert moment.replace(tzinfo=None) == datetime.datetime(
        2026, 9, 22, 11, 33, 12, 276623
    )
    assert moment.utcoffset() == datetime.timedelta(hours=8)


def test_unparsable_timestamp_falls_back_to_now(limiter: RateLimiter) -> None:
    """時戳壞掉時退回目前時間，不可讓整筆報價消失"""

    stream: ShioajiQuoteStream = ShioajiQuoteStream(
        FakeApi(),
        limiter,
        queue.Queue(),
        now_provider=lambda: datetime.datetime(2026, 9, 19, 13, 20),
    )
    assert stream._resolve_date(FakeSnapshot(ts=None)) == datetime.datetime(
        2026, 9, 19, 13, 20
    )

    quote: StockQuote = stream.from_stock_snapshot(FakeSnapshot(ts=None))
    assert quote is not None and quote.date == datetime.date(2026, 9, 19)


# === 期貨 ===
def test_futures_quote_keeps_settlement_fields_none(limiter: RateLimiter) -> None:
    """
    結算價與未沖銷契約量留 `None`

    快照沒有這兩項。填 0 會讓它們看起來像「今天是 0」而不是「沒有資料」，
    而回測那邊夜盤的這兩欄本來就是 None。
    """

    quote: FuturesQuote = make_stream(FakeApi(), limiter).from_futures_snapshot(
        FakeSnapshot(code="TXFA6"), FakeContract(code="TXFA6", symbol="TXF202601")
    )

    assert quote.settlement_price is None
    assert quote.open_interest is None


def test_futures_expiry_comes_from_symbol_not_code(limiter: RateLimiter) -> None:
    """
    到期月份取自合約的 `symbol`，不是快照的 `code`

    `code`（`TXFA6`＝月份字母 ＋ 年末碼）**跨年會重複**，拆不出可靠的月份。
    """

    quote: FuturesQuote = make_stream(FakeApi(), limiter).from_futures_snapshot(
        FakeSnapshot(code="TXFA6"), FakeContract(code="TXFA6", symbol="TXF202601")
    )

    assert (quote.product, quote.expiry) == ("TXF", "202601")


@pytest.mark.parametrize(
    "hour, expected",
    [
        (9, FuturesSession.DAY),
        (13, FuturesSession.DAY),
        (14, FuturesSession.DAY),  # 日夜盤之間的空檔歸日盤
        (15, FuturesSession.NIGHT),
        (23, FuturesSession.NIGHT),
        (3, FuturesSession.NIGHT),
        (6, FuturesSession.DAY),
    ],
)
def test_session_resolution(hour: int, expected: FuturesSession) -> None:
    """
    日盤 08:45~13:45、夜盤 15:00~次日 05:00

    兩段之間的空檔歸日盤：那段時間沒有行情，但歸夜盤會讓 13:45 收盤後的快照
    被記成次一交易日的帳。
    """

    moment: datetime.datetime = datetime.datetime(2026, 9, 19, hour, 30)

    assert ShioajiQuoteStream.resolve_session(moment) is expected


def test_index_futures_multiplier_comes_from_the_table() -> None:
    """指數期貨的乘數查登錄表"""

    assert ShioajiQuoteStream.resolve_multiplier("TX") == 200


def test_stock_futures_multiplier_comes_from_the_contract() -> None:
    """
    股期的乘數取自合約

    它會隨除權息調整，寫死必錯——而錯掉不會報錯，只會讓整條 PnL 靜默偏掉。
    """

    assert (
        ShioajiQuoteStream.resolve_multiplier("CDF", FakeContract(multiplier=2000))
        == 2000
    )
    assert ShioajiQuoteStream.resolve_multiplier("CDF", FakeContract(unit=100)) == 100


def test_unknown_multiplier_is_zero_not_a_guess() -> None:
    """
    取不到乘數時回 0 並 warning，不猜

    **這條只是釘住現行行為，不是主張它是對的**：回測那一側同名的
    `FuturesQuoteAdapter.resolve_multiplier()` 直接 `KeyError`，而
    `FuturesPositionManager.get_multiplier()` 也明訂「查不到一律中斷、
    不退回近似值」。乘數 0 會讓 PnL 全部算成 0，那沒有任何徵兆——
    改成中斷會動到實盤主流程，故目前先保留並釘住。
    """

    assert ShioajiQuoteStream.resolve_multiplier("UNKNOWN") == 0


# === 快照批次與限流 ===
def test_snapshots_are_batched(limiter: RateLimiter) -> None:
    """快照分批呼叫，每批算一次行情類額度"""

    contracts: List[FakeContract] = [
        FakeContract(code=f"{index:04d}") for index in range(SNAPSHOT_BATCH_SIZE + 5)
    ]
    api: FakeApi = FakeApi()
    make_stream(api, limiter).get_stock_snapshots(contracts)

    assert api.snapshot_calls == [SNAPSHOT_BATCH_SIZE, 5]
    assert limiter.try_acquire(RateLimitCategory.MARKET_DATA) is True


def test_missing_snapshots_are_skipped_not_misaligned(limiter: RateLimiter) -> None:
    """
    查無資料的標的直接略過

    配對一律以代號進行。用位置索引的話，停牌一檔就會讓後面所有標的的乘數
    集體錯位，而每一筆看起來都還是合法報價。
    """

    api: FakeApi = FakeApi(snapshots=[FakeSnapshot(code="2330")])
    quotes: List[StockQuote] = make_stream(api, limiter).get_stock_snapshots(
        [FakeContract(code="2330"), FakeContract(code="9999")]
    )

    assert [quote.stock_id for quote in quotes] == ["2330"]


def test_futures_snapshots_pair_by_code(limiter: RateLimiter) -> None:
    """期貨快照也以代號配回合約，乘數才不會張冠李戴"""

    api: FakeApi = FakeApi(
        snapshots=[FakeSnapshot(code="TXFA6"), FakeSnapshot(code="CDFA6")]
    )
    quotes: List[FuturesQuote] = make_stream(api, limiter).get_futures_snapshots(
        [
            FakeContract(code="CDFA6", symbol="CDF202601", multiplier=2000),
            FakeContract(code="TXFA6", symbol="TXF202601"),
        ]
    )
    by_product: Dict[str, FuturesQuote] = {q.product: q for q in quotes}

    assert by_product["TXF"].multiplier == 0  # TXF 不在登錄表裡（表裡是 TAIFEX 的 TX）
    assert by_product["CDF"].multiplier == 2000


# === 訂閱 ===
def test_subscribe_registers_tick_and_bidask(limiter: RateLimiter) -> None:
    """盤中策略要 bid/ask 才算得出可成交價，兩種都要訂"""

    api: FakeApi = FakeApi()
    make_stream(api, limiter).subscribe([FakeContract(code="2330")])

    assert set(api.subscribed) == {
        ("2330", sj.QuoteType.Tick),
        ("2330", sj.QuoteType.BidAsk),
    }


def test_subscription_limit_raises_before_subscribing(limiter: RateLimiter) -> None:
    """
    超過上限在訂閱**之前**就拋出

    先訂到滿再失敗的話，前面那幾檔會訂閱成功、後面的默默訂不到，
    於是策略只收得到一部分標的的行情，而它不會知道。
    """

    api: FakeApi = FakeApi()
    stream: ShioajiQuoteStream = make_stream(api, limiter)
    contracts: List[FakeContract] = [
        FakeContract(code=f"{index:04d}")
        for index in range(ShioajiQuoteStream.MAX_SUBSCRIPTIONS + 1)
    ]

    with pytest.raises(ValueError, match="上限"):
        stream.subscribe(contracts)

    assert api.subscribed == []
    assert stream.subscribed == set()


def test_unsubscribe_clears_the_set(limiter: RateLimiter) -> None:
    """取消訂閱要同步更新本地紀錄，否則上限檢查會愈算愈多"""

    api: FakeApi = FakeApi()
    stream: ShioajiQuoteStream = make_stream(api, limiter)
    contract: FakeContract = FakeContract(code="2330")

    stream.subscribe([contract])
    stream.unsubscribe([contract])

    assert stream.subscribed == set()


# === 回呼 ===
def test_callbacks_only_enqueue(limiter: RateLimiter) -> None:
    """
    四個回呼都要掛，且只做入列

    少掛一個不會報錯，只會讓那一類行情安靜地收不到。
    """

    api: FakeApi = FakeApi()
    quote_queue: queue.Queue = queue.Queue()
    stream: ShioajiQuoteStream = ShioajiQuoteStream(api, limiter, quote_queue)
    stream.register_callbacks()

    assert set(api.callbacks) == {
        "tick_stk",
        "bidask_stk",
        "tick_fop",
        "bidask_fop",
    }

    # shioaji 1.7 的回呼只收一個參數；交易所改從行情物件自身的 `exchange` 取
    tick: Any = type("Tick", (), {"code": "2330", "exchange": "TSE"})()
    api.callbacks["tick_stk"](tick)
    kind, exchange, message = quote_queue.get_nowait()

    assert (kind, exchange) == ("tick_stk", "TSE")
    assert message is tick


# === 逐筆成交 → StockQuote（欄位取自 2026-09-21 模擬環境實錄）===
class FakeTick:
    """
    `TickSTKv1` 的替身；**欄位名與值取自實錄**

    執行期的真品是 C 擴充物件（沒有 `__dict__`、`dict()`、`model_dump()`，
    連 `dir()` 都是空的），所以只能照名字 `getattr`——這個替身複製的正是那個形狀。
    """

    def __init__(self, **overrides: Any) -> None:
        # 2026-09-21 11:30 的 2454 實際推送值
        self.code: str = "2454"
        self.datetime: datetime.datetime = datetime.datetime(
            2026, 9, 21, 11, 30, 39, 724374
        )
        self.open: Decimal = Decimal("4800")
        self.high: Decimal = Decimal("5035")
        self.low: Decimal = Decimal("4780")
        self.close: Decimal = Decimal("4930")
        self.volume: int = 1
        self.total_volume: int = 6263
        self.tick_type: int = 2
        self.simtrade: bool = False
        self.intraday_odd: bool = False
        self.suspend: bool = False
        for key, value in overrides.items():
            setattr(self, key, value)


def make_tick_stream() -> ShioajiQuoteStream:
    """轉換不需要 api，給一個佔位物件即可"""

    return ShioajiQuoteStream(object(), RateLimiter(), queue.Queue())


def test_tick_is_converted_with_the_recorded_values() -> None:
    """轉換結果要對得上實錄的那一筆"""

    quote = make_tick_stream().from_tick_message(FakeTick())

    assert quote is not None
    assert quote.stock_id == "2454"
    assert quote.scale is Scale.TICK
    assert (quote.cur_price, quote.close) == (4930.0, 4930.0)
    assert (quote.open, quote.high, quote.low) == (4800.0, 5035.0, 4780.0)


def test_prices_are_floats_not_decimals() -> None:
    """
    來源是 `Decimal`，模型要 float

    混用會在某些路徑靜默降精度，而回測那邊一律是 float。
    """

    quote = make_tick_stream().from_tick_message(FakeTick())

    assert isinstance(quote.close, float)
    assert isinstance(quote.cur_price, float)


def test_volume_takes_the_daily_total_not_this_tick() -> None:
    """
    要取 `total_volume` 不是 `volume`

    取錯的話「當日成交量 ≥ N 張」這類門檻永遠不成立——而且不會報錯。
    """

    quote = make_tick_stream().from_tick_message(FakeTick())

    assert quote.volume == 6263


def test_naive_datetime_gets_taipei_timezone() -> None:
    """
    回呼給的是 **naive** datetime，專案一律用 Asia/Taipei aware

    直接拿去跟 aware 的時間比較會 `TypeError`，被當成 UTC 則整條時間軸偏 8 小時。
    """

    quote = make_tick_stream().from_tick_message(FakeTick())

    assert quote.date.tzinfo is not None
    assert quote.date.utcoffset() == datetime.timedelta(hours=8)


def test_simtrade_is_not_a_quote() -> None:
    """
    **試撮不是成交**

    開盤前與收盤前的試撮價格會跳動，拿它產生訊號等於對著假資料交易。
    這個欄位不在任何規劃文件裡，是實錄才發現的。
    """

    assert make_tick_stream().from_tick_message(FakeTick(simtrade=True)) is None


def test_intraday_odd_is_not_a_quote() -> None:
    """
    盤中零股的 `volume` 單位是**股**不是張

    混進來的話成交量差 1000 倍，門檻型訊號會整組失效。
    """

    assert make_tick_stream().from_tick_message(FakeTick(intraday_odd=True)) is None


def test_missing_field_fails_loudly() -> None:
    """
    欄位改名要當場炸，**不可以給預設值**

    給預設值會讓報價靜默變成 0，策略從此不產生任何訊號而沒有人知道。
    本專案已經在 `Snapshot.ts`、`total_volume` 與合約檔日期上各踩過一次。
    """

    broken: FakeTick = FakeTick()
    del broken.total_volume

    with pytest.raises(AttributeError, match="total_volume"):
        make_tick_stream().from_tick_message(broken)
