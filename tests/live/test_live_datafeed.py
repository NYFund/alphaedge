import datetime
import sqlite3
from typing import Any, List, Optional, Sequence

import pytest
from loguru import logger

from core.live.datafeed.base import BaseLiveDataFeed, DataFreshnessError
from core.live.datafeed.calendar import (
    BrokerContractCalendarSource,
    PriceTableCalendarSource,
    TradingCalendarSource,
    TradingCalendarUnavailableError,
    WeekendCalendarSource,
    resolve_trading_day,
)
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.datafeed.tw.stock_live_datafeed import TwStockLiveDataFeed
from core.models import BaseQuote, LiveDataUnavailableError, PreOpenStockQuote
from core.utils import ExecutionTiming

"""
實盤資料源：兩道啟動檢查

- **資料新鮮度**：沒有它，策略會拿前天的資料當昨天用，訊號錯了也不會有任何徵兆。
- **交易日判定**：判斷不出來就拒絕啟動，**不預設為開市**。休市日照常跑完整套流程
  會送單被退、對帳全是差異，然後推播一整天的告警。
"""

MONDAY: datetime.date = datetime.date(2026, 9, 21)
FRIDAY: datetime.date = datetime.date(2026, 9, 18)
SATURDAY: datetime.date = datetime.date(2026, 9, 19)


class FixedSource(TradingCalendarSource):
    """回傳固定答案的來源，用來組合各種情境"""

    def __init__(self, name: str, answer: Optional[bool]) -> None:
        self.name = name
        self._answer: Optional[bool] = answer

    def is_trading_day(self, date: datetime.date) -> Optional[bool]:
        return self._answer


# === 交易日來源 ===
def test_weekend_source_only_answers_half() -> None:
    """
    週末來源回答得了「休市」，回答不了「開市」

    平日可能是國定假日而它看不出來；回 True 的話，所有國定假日都會被當成交易日。
    """

    source: WeekendCalendarSource = WeekendCalendarSource()

    assert source.is_trading_day(SATURDAY) is False
    assert source.is_trading_day(MONDAY) is None


def test_broker_contract_source_only_answers_today() -> None:
    """
    合約檔只答得出「今天」

    它是副作用不是契約：問明天或上週都不該給答案。
    """

    source: BrokerContractCalendarSource = BrokerContractCalendarSource(lambda: MONDAY)

    assert source.is_trading_day(MONDAY) is True
    assert source.is_trading_day(FRIDAY) is None


def test_broker_contract_source_handles_missing_date() -> None:
    """取不到更新日期時回 None，不猜"""

    assert BrokerContractCalendarSource(lambda: None).is_trading_day(MONDAY) is None


def test_price_table_source_does_not_conclude_from_absence() -> None:
    """
    `price` 表沒資料**不代表**休市

    今天的資料要到收盤後才進來；從「沒資料」推論休市會讓每個交易日的盤前
    都被判成休市。
    """

    empty: PriceTableCalendarSource = PriceTableCalendarSource(lambda date: False)
    filled: PriceTableCalendarSource = PriceTableCalendarSource(lambda date: True)

    assert empty.is_trading_day(FRIDAY) is None
    assert filled.is_trading_day(FRIDAY) is True


# === 綜合判定 ===
def test_no_definite_answer_refuses_to_start() -> None:
    """
    沒有來源給出答案就拒絕啟動，**不預設為開市**

    這是本模組存在的理由：休市日照常跑完整套流程會送單被退、對帳全是差異。
    """

    with pytest.raises(TradingCalendarUnavailableError, match="不預設為開市"):
        resolve_trading_day(MONDAY, [WeekendCalendarSource()])


def test_conflicting_sources_refuse_to_start() -> None:
    """
    來源衝突就拒絕，**不投票也不取多數**

    衝突代表其中一個來源的語意與我們以為的不同，那時繼續跑只是在賭。
    """

    with pytest.raises(TradingCalendarUnavailableError, match="衝突"):
        resolve_trading_day(MONDAY, [FixedSource("a", True), FixedSource("b", False)])


def test_weekend_is_resolved_without_other_sources() -> None:
    """週末有明確答案，不必問別人"""

    assert resolve_trading_day(SATURDAY, [WeekendCalendarSource()]) is False


def test_agreeing_sources_pass() -> None:
    """兩個來源都說開市就是開市"""

    assert (
        resolve_trading_day(MONDAY, [FixedSource("a", True), FixedSource("b", True)])
        is True
    )


# === 資料新鮮度 ===
class FakeLiveFeed(BaseLiveDataFeed):
    """最小實作；只用來驗新鮮度檢查"""

    def __init__(
        self,
        latest: Optional[datetime.date],
        sources: Optional[Sequence[TradingCalendarSource]] = None,
        now: datetime.datetime = datetime.datetime(2026, 9, 21, 8, 30),
    ) -> None:
        super().__init__(
            broker=None, calendar_sources=sources, now_provider=lambda: now
        )
        self._latest: Optional[datetime.date] = latest

    def get_latest_data_date(self) -> Optional[datetime.date]:
        return self._latest

    def _probe_contract(self, resolver: Any) -> Optional[Any]:
        """本檔的日曆來源由測試直接指定，不經過券商合約檔"""

        return None

    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        return []

    def setup(self, strategy: Any) -> None:
        """本檔只驗新鮮度，不需要真的建 API"""

    def get_quotes(
        self, date: datetime.date, scale: Any, adjusted: bool = False
    ) -> List[BaseQuote]:
        return []


def test_fresh_data_passes() -> None:
    """資料停在上一個交易日：通過"""

    FakeLiveFeed(latest=FRIDAY).verify_data_freshness()


def test_empty_table_is_rejected() -> None:
    """空表要明講「先跑 update_db」，不是丟一個 None 出去"""

    with pytest.raises(DataFreshnessError, match="update_db"):
        FakeLiveFeed(latest=None).verify_data_freshness()


def test_data_dated_today_is_rejected() -> None:
    """
    盤前不該有今天的資料

    有的話多半是有人手動灌了資料或系統日期錯了——兩者都會讓訊號整個錯掉。
    """

    with pytest.raises(DataFreshnessError, match="不早於今天"):
        FakeLiveFeed(latest=MONDAY).verify_data_freshness()


def test_stale_data_is_rejected() -> None:
    """
    間隔過大代表資料停住了（ETL 掛掉、磁碟滿了）

    沒有這道檢查，策略會拿一週前的資料當昨天用。
    """

    with pytest.raises(DataFreshnessError, match="超過容許"):
        FakeLiveFeed(latest=datetime.date(2026, 9, 1)).verify_data_freshness()


def test_long_weekend_still_passes() -> None:
    """
    連假不該被誤判

    誤報會讓人在正常的日子被擋住啟動，然後學會忽略這個檢查。
    """

    feed: FakeLiveFeed = FakeLiveFeed(
        latest=datetime.date(2026, 9, 17), sources=[WeekendCalendarSource()]
    )

    feed.verify_data_freshness()


def test_definite_missing_trading_day_is_rejected() -> None:
    """中間有來源確定是交易日、而資料缺漏時要擋下"""

    feed: FakeLiveFeed = FakeLiveFeed(
        latest=datetime.date(2026, 9, 17), sources=[FixedSource("always", True)]
    )

    with pytest.raises(DataFreshnessError, match="確定是交易日"):
        feed.verify_data_freshness()


# === 盤前報價 ===
class FakeContract:
    def __init__(self, reference: float = 1000.0, limit_up: float = 1100.0) -> None:
        self.reference: float = reference
        self.limit_up: float = limit_up
        self.limit_down: float = 900.0
        self.update_date: str = "2026-09-21"


class FakeResolver:
    def __init__(self, contracts: Optional[dict] = None) -> None:
        self._contracts: dict = (
            contracts if contracts is not None else {"2330": FakeContract()}
        )

    def resolve_stock(self, symbol: str) -> FakeContract:
        if symbol not in self._contracts:
            raise LookupError(f"查無 {symbol}")
        return self._contracts[symbol]


class FakeBrokerForFeed:
    def __init__(self, resolver: FakeResolver) -> None:
        self.resolver: FakeResolver = resolver
        self.snapshot_calls: List[List[str]] = []

    def get_snapshots(self, symbols: List[str]) -> List[BaseQuote]:
        self.snapshot_calls.append(symbols)
        return []


@pytest.fixture
def feed() -> TwStockLiveDataFeed:
    broker: FakeBrokerForFeed = FakeBrokerForFeed(FakeResolver())
    return TwStockLiveDataFeed(
        broker,
        calendar_sources=[FixedSource("test", True)],
        now_provider=lambda: datetime.datetime(2026, 9, 21, 8, 30),
    )


def test_open_segment_returns_pre_open_quotes(feed: TwStockLiveDataFeed) -> None:
    """
    開盤段回 `PreOpenStockQuote`

    盤前不存在當日 OHLC；填成參考價的話，以漲幅判斷的策略會永遠算出 0%。
    """

    quotes: List[BaseQuote] = feed.get_live_quotes(ExecutionTiming.AT_OPEN, ["2330"])

    assert len(quotes) == 1
    assert isinstance(quotes[0], PreOpenStockQuote)
    assert quotes[0].reference_price == 1000.0

    with pytest.raises(LiveDataUnavailableError):
        _ = quotes[0].close


def test_close_segment_uses_broker_snapshots(feed: TwStockLiveDataFeed) -> None:
    """尾盤段走券商快照，那裡才有當日 OHLC"""

    feed.get_live_quotes(ExecutionTiming.AT_CLOSE, ["2330"])

    assert feed.broker.snapshot_calls == [["2330"]]


def test_missing_contract_skips_that_symbol_only(feed: TwStockLiveDataFeed) -> None:
    """
    查不到合約只略過該檔

    一個代號打錯不該讓其他標的也收不到報價。
    """

    quotes: List[BaseQuote] = feed.get_live_quotes(
        ExecutionTiming.AT_OPEN, ["2330", "9999"]
    )

    assert [quote.symbol for quote in quotes] == ["2330"]


def test_zero_reference_price_is_skipped_not_zeroed() -> None:
    """
    取不到參考價時略過，**不是給 0**

    0 會讓「市價語意換算」算出 0 元的限價——那是一張永遠不會成交、
    但看起來完全合法的單。
    """

    broker: FakeBrokerForFeed = FakeBrokerForFeed(
        FakeResolver({"2330": FakeContract(reference=0.0)})
    )
    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(
        broker, calendar_sources=[FixedSource("test", True)]
    )

    assert feed.get_live_quotes(ExecutionTiming.AT_OPEN, ["2330"]) == []


def test_historical_quotes_for_today_are_rejected(feed: TwStockLiveDataFeed) -> None:
    """
    查今天的歷史報價要拋出

    靜默回空 list 的話，策略會以為今天全市場都沒有報價。
    """

    from core.utils import Scale

    with pytest.raises(ValueError, match="不早於今天"):
        feed.get_quotes(datetime.date(2026, 9, 21), Scale.DAY)


def test_close_is_idempotent(feed: TwStockLiveDataFeed) -> None:
    """`close()` 可重複呼叫：引擎以 `try/finally` 保證它跑到"""

    feed.conn = sqlite3.connect(":memory:")
    feed.close()
    feed.close()

    assert feed.conn is None


def test_live_feed_exposes_the_same_apis_as_the_backtest_feed() -> None:
    """
    **實盤資料源建立的 API 必須與回測資料源相同**

    少建一個的話，用到它的策略會在 `setup_apis()` 當場 `AttributeError`，
    而那個訊息只會說「物件沒有某個屬性」，完全看不出是實盤資料源漏建了。
    這正是 `MomentumStrategy1` 撞到的——它取 `feed.mrr`，而實盤那邊沒建。

    `tick` 不比對：那是盤中（Scale.TICK）才要的，屬 Phase5。
    """

    import inspect

    from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed

    def assigned_api_names(cls: Any) -> set:
        source: str = inspect.getsource(cls.setup)
        return {
            line.split("=")[0].strip().removeprefix("self.")
            for line in source.splitlines()
            if line.strip().startswith("self.") and "API(" in line
        }

    backtest_apis: set = assigned_api_names(TwStockDataFeed) - {"tick"}
    live_apis: set = assigned_api_names(TwStockLiveDataFeed)

    assert backtest_apis <= live_apis, (
        f"實盤資料源少建了這些 API：{sorted(backtest_apis - live_apis)}"
    )


# === 交易日佐證的探測合約 ===
class RecordingResolver:
    """記下被呼叫過哪些方法；`resolve_stock` 一被碰到就爆炸"""

    def __init__(self, expiries: Optional[List[str]] = None) -> None:
        self.calls: List[str] = []
        self._expiries: List[str] = (
            expiries if expiries is not None else ["202610", "202611"]
        )

    def resolve_stock(self, stock_id: str) -> Any:
        self.calls.append(f"resolve_stock:{stock_id}")
        return FakeContract()

    def list_index_futures_expiries(self, product: str) -> List[str]:
        self.calls.append(f"list_index_futures_expiries:{product}")
        return self._expiries

    def resolve_index_futures(self, product: str, expiry: str) -> Any:
        self.calls.append(f"resolve_index_futures:{product}{expiry}")
        return FakeContract()


def make_futures_feed(resolver: Any) -> TwFuturesLiveDataFeed:
    """只需要 broker 的期貨資料源；不建任何連線"""

    return TwFuturesLiveDataFeed(
        FakeBrokerForFeed(resolver),
        calendar_sources=[FixedSource("test", True)],
        now_provider=lambda: datetime.datetime(2026, 9, 21, 8, 30),
    )


def test_futures_never_probes_with_a_stock_contract() -> None:
    """
    **期貨的交易日佐證不可以用股票合約**

    `BrokerContractCalendarSource` 是期貨在平日的唯一佐證（官方日曆的年度
    未入庫時），而它只回 True 或 None——拿股票合約去問，證券與期貨開休市
    不一致的那天判定方向是「誤判為開市」，且不會有任何錯誤訊息。
    """

    resolver: RecordingResolver = RecordingResolver()
    feed: TwFuturesLiveDataFeed = make_futures_feed(resolver)

    feed._broker_contract_update_date()

    assert not any(call.startswith("resolve_stock") for call in resolver.calls), (
        f"期貨資料源碰了股票合約：{resolver.calls}"
    )
    assert resolver.calls == [
        "list_index_futures_expiries:TX",
        "resolve_index_futures:TX202610",
    ]


def test_futures_probe_takes_the_nearest_listed_expiry() -> None:
    """
    取**最近的掛牌月**，不寫死月份

    寫死一個月份的話，該月交割後合約就永遠查不到，
    平日的交易日判定會靜默失去唯一佐證。
    """

    resolver: RecordingResolver = RecordingResolver(expiries=["202701", "202702"])
    feed: TwFuturesLiveDataFeed = make_futures_feed(resolver)

    assert feed._broker_contract_update_date() == datetime.date(2026, 9, 21)
    assert "resolve_index_futures:TX202701" in resolver.calls


def test_futures_probe_without_listed_expiries_returns_none_not_a_guess() -> None:
    """查不到掛牌月份時回 None；**不猜**，交由其他來源作答或拒絕啟動"""

    feed: TwFuturesLiveDataFeed = make_futures_feed(RecordingResolver(expiries=[]))

    assert feed._broker_contract_update_date() is None


def test_stock_still_probes_with_a_stock_contract() -> None:
    """股票版維持用股票合約——兩邊各問自己的市場，這才是重點"""

    resolver: RecordingResolver = RecordingResolver()
    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(
        FakeBrokerForFeed(resolver), calendar_sources=[FixedSource("test", True)]
    )

    assert feed._broker_contract_update_date() == datetime.date(2026, 9, 21)
    assert resolver.calls == ["resolve_stock:2330"]


def test_probe_failure_is_visible_not_swallowed() -> None:
    """
    探測失敗要留下 warning

    這是平日交易日判定的唯一佐證，它失效等於整個判定失效——
    原本記在 debug 等級，正式部署一定看不到。
    """

    class BrokenResolver:
        def list_index_futures_expiries(self, product: str) -> List[str]:
            raise RuntimeError("連線斷了")

    feed: TwFuturesLiveDataFeed = make_futures_feed(BrokenResolver())

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda m: messages.append(str(m)), level="WARNING", format="{message}"
    )
    try:
        assert feed._broker_contract_update_date() is None
    finally:
        logger.remove(sink_id)

    assert any("取合約檔更新日期失敗" in message for message in messages)


# === DB 生命週期骨架 ===
def test_each_market_reads_its_own_table() -> None:
    """
    最新日查的是各自的表

    表名收進 `LATEST_DATE_TABLE` 之後，查錯表的症狀會是「新鮮度檢查永遠通過」
    或「永遠失敗」，兩者都不會說出真正的原因。
    """

    assert TwStockLiveDataFeed.LATEST_DATE_TABLE == "price"
    assert TwFuturesLiveDataFeed.LATEST_DATE_TABLE == "futures_price_daily"


def test_latest_date_reads_the_declared_table() -> None:
    """骨架真的照 `LATEST_DATE_TABLE` 查，不是各自寫一份查詢"""

    feed: TwStockLiveDataFeed = TwStockLiveDataFeed(
        FakeBrokerForFeed(RecordingResolver())
    )
    feed.conn = sqlite3.connect(":memory:")
    feed.conn.execute("CREATE TABLE price (date TEXT)")
    feed.conn.execute("INSERT INTO price VALUES ('2026-09-18'), ('2026-09-21')")

    assert feed.get_latest_data_date() == datetime.date(2026, 9, 21)

    feed.close()


def test_feed_without_a_declared_table_refuses_instead_of_guessing() -> None:
    """
    沒宣告表名就拋出

    靜默回 None 的話，新鮮度檢查會說「歷史資料表是空的」——
    那個訊息會把人帶去查 ETL，而真正的原因是這個類別少宣告了一行。
    """

    feed: FakeLiveFeed = FakeLiveFeed(latest=None)
    feed.LATEST_DATE_TABLE = ""

    with pytest.raises(NotImplementedError, match="LATEST_DATE_TABLE"):
        BaseLiveDataFeed.get_latest_data_date(feed)


def test_futures_close_releases_both_connections() -> None:
    """
    期貨要關**兩條**連線

    官方開休市日曆的 `market_holiday` 表在 `tw_stock.db`，與期貨歷史資料不同庫。
    只關一條的話，每天重跑的行程會一天洩一條連線——而且完全沒有徵兆。
    """

    feed: TwFuturesLiveDataFeed = make_futures_feed(RecordingResolver())
    feed.conn = sqlite3.connect(":memory:")
    feed.stock_conn = sqlite3.connect(":memory:")

    feed.close()

    assert feed.conn is None
    assert feed.stock_conn is None

    # 可重複呼叫：引擎以 `try/finally` 保證它跑到
    feed.close()
