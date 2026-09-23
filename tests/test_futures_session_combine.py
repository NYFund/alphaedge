import datetime
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytest

from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
from core.backtest.datafeed.tw.futures_datafeed import TwFuturesDataFeed
from core.config import TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.market.tw.futures_calendar import FuturesCalendar
from core.models import FuturesQuote
from core.utils import FuturesSession, Scale
from tests.conftest import build_futures_quote

"""
日盤／夜盤整併測試

**整併最容易錯的是「夜盤屬於哪一天」**：TAIFEX 的夜盤 15:00 開盤、次日 05:00
收盤，制度上屬於**次一交易日**——星期五晚上那一段屬於星期一。行情表存的是它
**所屬的交易日**（星期一），不是它開始的曆日，因此整併時取同一個日期。
往前取一個交易日的話，併進來的會是前一天的夜盤，價格看起來仍然合理、
不會有任何異常，但整條序列錯開一天。

第二個重點是**跨盤別跳空必須留在 bar 內**：整併後的 `open` 取夜盤開盤而非
日盤開盤，前一個日盤收盤到夜盤開盤之間的跳空才不會被抹掉——那正是隔夜風險。
"""

DATE: datetime.date = datetime.date(2024, 3, 4)  # 星期一
FRIDAY: datetime.date = datetime.date(2024, 3, 1)


def make_quote(
    close: float,
    open_: float,
    high: float,
    low: float,
    volume: int,
    session: FuturesSession,
    date: datetime.date = DATE,
    settlement: Optional[float] = None,
    expiry: str = "202403",
) -> FuturesQuote:
    """組一筆 TX 報價"""

    return build_futures_quote(
        expiry=expiry,
        date=date,
        close=close,
        open=open_,
        high=high,
        low=low,
        volume=volume,
        session=session,
        settlement_price=settlement,
    )


# === 合併規則 ===
def test_combined_bar_takes_night_open_and_day_close() -> None:
    """
    **open 取夜盤、close 取日盤**

    夜盤先發生、日盤後收盤，這是一根 bar 的頭尾。用日盤開盤當 open 會把
    隔夜跳空整段抹掉。
    """

    day: FuturesQuote = make_quote(
        19314, 19144, 19340, 19137, 97821, FuturesSession.DAY
    )
    night: FuturesQuote = make_quote(
        18954, 18961, 19000, 18891, 52330, FuturesSession.NIGHT
    )

    combined: FuturesQuote = FuturesQuoteAdapter.combine_quote(day, night)

    assert combined.open == 18961  # 夜盤開盤
    assert combined.close == 19314  # 日盤收盤
    assert combined.high == 19340  # 兩盤極值
    assert combined.low == 18891
    assert combined.volume == 97821 + 52330
    assert combined.session == FuturesSession.COMBINED


def test_cross_session_gap_is_preserved() -> None:
    """
    **跨盤別跳空被保留在 bar 內**（本步驟的驗收條件）

    整併後的 open 與日盤 open 之間那 183 點，就是隔夜跳空；
    若整併時取日盤 open，這段風險在回測裡會完全看不見。
    """

    day: FuturesQuote = make_quote(
        19314, 19144, 19340, 19137, 97821, FuturesSession.DAY
    )
    night: FuturesQuote = make_quote(
        18954, 18961, 19000, 18891, 52330, FuturesSession.NIGHT
    )
    day_open_before: float = day.open

    combined: FuturesQuote = FuturesQuoteAdapter.combine_quote(day, night)

    assert combined.open != day_open_before
    assert combined.low < 19137  # 夜盤的低點成為當根 bar 的低點


def test_missing_night_quote_keeps_the_day_bar() -> None:
    """
    夜盤沒有該契約時原樣沿用日盤

    多數月份的契約夜盤根本不交易；補 0 會讓 `low` 變成 0、`open` 變成 0。
    """

    day: FuturesQuote = make_quote(
        19314, 19144, 19340, 19137, 97821, FuturesSession.DAY
    )

    combined: FuturesQuote = FuturesQuoteAdapter.combine_quote(day, None)

    assert combined.open == 19144
    assert combined.low == 19137
    assert combined.volume == 97821
    assert combined.session == FuturesSession.COMBINED


def test_settlement_and_open_interest_come_from_the_day_session() -> None:
    """結算價與未沖銷契約量**只有日盤有**，整併後不可被夜盤的 None 蓋掉"""

    day: FuturesQuote = make_quote(
        19314, 19144, 19340, 19137, 97821, FuturesSession.DAY, settlement=19310
    )
    night: FuturesQuote = make_quote(
        18954, 18961, 19000, 18891, 52330, FuturesSession.NIGHT
    )

    combined: FuturesQuote = FuturesQuoteAdapter.combine_quote(day, night)

    assert combined.settlement_price == 19310


# === 夜盤屬於哪一天 ===
class StubPriceAPI:
    """只回傳指定日期／時段行情的假 API"""

    COLUMNS: List[str] = [
        "date",
        "product",
        "expiry",
        "session",
        FuturesPriceColumn.OPEN.value,
        FuturesPriceColumn.HIGH.value,
        FuturesPriceColumn.LOW.value,
        FuturesPriceColumn.CLOSE.value,
        FuturesPriceColumn.VOLUME.value,
        FuturesPriceColumn.SETTLEMENT.value,
        FuturesPriceColumn.OPEN_INTEREST.value,
    ]

    def __init__(self) -> None:
        self.rows: List[list] = [
            [
                str(FRIDAY),
                "TX",
                "202403",
                "day",
                19100,
                19200,
                19000,
                19144,
                100,
                19144,
                500,
            ],
            # 星期五的夜盤屬於星期五（由星期四 15:00 開始），不該併進星期一
            [
                str(FRIDAY),
                "TX",
                "202403",
                "night",
                17000,
                17000,
                17000,
                17000,
                999,
                None,
                None,
            ],
            [
                str(DATE),
                "TX",
                "202403",
                "day",
                19144,
                19340,
                19137,
                19314,
                200,
                19314,
                600,
            ],
            # 星期一的夜盤：星期五 15:00 開盤、星期一 05:00 收盤，行情表記為星期一
            [
                str(DATE),
                "TX",
                "202403",
                "night",
                18961,
                19000,
                18891,
                18954,
                50,
                None,
                None,
            ],
            # 週日（非交易日）不該被取到
            ["2024-03-03", "TX", "202403", "night", 1, 1, 1, 1, 999, None, None],
        ]
        self.requested: List[tuple] = []

    def get(self, date, product=None, session=None) -> pd.DataFrame:
        self.requested.append((date, session))
        rows = [
            row
            for row in self.rows
            if row[0] == str(date) and (session is None or row[3] == session.value)
        ]
        return pd.DataFrame(rows, columns=self.COLUMNS)

    def get_trading_days(self, start_date, end_date, product=None) -> List:
        return [FRIDAY, DATE]

    def close(self) -> None:
        pass


def make_feed() -> TwFuturesDataFeed:
    """建立注入假 API 與日曆的 feed（整併模式）"""

    feed: TwFuturesDataFeed = TwFuturesDataFeed()
    feed.futures_price = StubPriceAPI()
    feed.products = ["TX"]
    feed.session = FuturesSession.COMBINED
    feed.start_date, feed.end_date = FRIDAY, DATE
    feed.calendar = FuturesCalendar([FRIDAY, DATE])
    return feed


def test_night_session_comes_from_the_same_trading_day() -> None:
    """
    **星期一取的是行情表裡日期為星期一的那列夜盤**

    那一段從星期五 15:00 開始，制度上屬於星期一，行情表也記為星期一。
    往前取一個交易日的話會併進星期五那段（由星期四 15:00 開始），
    整條序列會錯開一天。
    """

    feed: TwFuturesDataFeed = make_feed()

    assert feed.get_night_session_date(DATE) == DATE


def test_feed_produces_combined_quotes() -> None:
    """整併模式下 feed 回傳的報價已合併，且標記為 `COMBINED`"""

    feed: TwFuturesDataFeed = make_feed()

    quotes: List[FuturesQuote] = feed.get_quotes(DATE, Scale.DAY)

    assert len(quotes) == 1
    assert quotes[0].session == FuturesSession.COMBINED
    assert quotes[0].open == 18961  # 星期一夜盤的開盤（星期五 15:00 那段）
    assert quotes[0].close == 19314  # 星期一日盤的收盤
    assert quotes[0].volume == 250


def test_no_night_session_before_2017() -> None:
    """
    2017-05-15 之前沒有夜盤，整併結果等於日盤本身

    那是制度不是資料缺漏；當成缺漏處理會一路找不到原因。
    """

    feed: TwFuturesDataFeed = make_feed()
    feed.calendar = FuturesCalendar(
        [datetime.date(2016, 6, 1), datetime.date(2016, 6, 2)]
    )

    assert feed.get_night_session_date(datetime.date(2016, 6, 2)) is None


def test_day_only_mode_is_unchanged() -> None:
    """未指定整併時行為完全不變（預設仍是純日盤）"""

    feed: TwFuturesDataFeed = make_feed()
    feed.session = FuturesSession.DAY

    quotes: List[FuturesQuote] = feed.get_quotes(DATE, Scale.DAY)

    assert quotes[0].session == FuturesSession.DAY
    assert quotes[0].open == 19144
    assert quotes[0].volume == 200


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗整併"
)
def test_real_combined_bar_contains_the_night_session() -> None:
    """以真實資料確認整併後的 bar 真的納入了夜盤（且沒有漏量）"""

    from core.api.tw.futures_price_api import FuturesPriceAPI

    api: FuturesPriceAPI = FuturesPriceAPI()
    try:
        day_quotes: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
            api.get(DATE, product="TX", session=FuturesSession.DAY), DATE
        )
        night_quotes: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
            api.get(DATE, product="TX", session=FuturesSession.NIGHT), DATE
        )
        # **合併前先把日盤的數字抄下來**：`combine_quote()` 是**就地修改**日盤
        # 那一筆再回傳（見其 docstring），合併後 `day_quotes` 裡的物件與 `combined`
        # 裡的是同一個。合併後才建對照表的話，`quote.volume == day.volume + ...`
        # 會變成 `X == X + night.volume`，只有夜盤量為 0 時才成立——
        # 這條測試因此長期是紅的，而它帶 `@pytest.mark.slow`，
        # 平常的 `-m "not slow"` 看不到。
        day_before: Dict[str, Tuple[int, float, float, float]] = {
            quote.contract_id: (quote.volume, quote.high, quote.low, quote.close)
            for quote in day_quotes
        }
        night_before: Dict[str, Tuple[int, float, float, float]] = {
            quote.contract_id: (quote.volume, quote.high, quote.low, quote.close)
            for quote in night_quotes
        }

        combined: List[FuturesQuote] = FuturesQuoteAdapter.combine_sessions(
            day_quotes, night_quotes
        )
    finally:
        api.close()

    if not day_before or not night_before:
        pytest.skip("該日期尚無行情資料")

    for quote in combined:
        day_volume, day_high, day_low, day_close = day_before[quote.contract_id]
        night = night_before.get(quote.contract_id)

        assert quote.session == FuturesSession.COMBINED
        assert quote.close == day_close
        if night is not None and night[3]:
            night_volume, night_high, night_low, _ = night
            assert quote.volume == day_volume + night_volume
            assert quote.high >= max(day_high, night_high)
            assert quote.low <= min(day_low, night_low)


@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗時序"
)
def test_real_night_open_follows_the_previous_day_close() -> None:
    """
    以真實資料固定「夜盤屬於哪一天」的口徑

    行情表裡日期為 D 的夜盤，是 D−1 日盤收盤後 15:00 開始的那一段，所以它的
    開盤價會**貼著 D−1 的日盤收盤**，而不是貼著 D 的日盤收盤。整併若往前取一個
    交易日，序列會整條錯開一天——價格仍然合理，不會有任何異常。

    以近月合約、2023 年起的樣本比中位數：貼著前一交易日收盤的那一組要明顯小。
    """

    from core.api.tw.futures_price_api import FuturesPriceAPI

    api: FuturesPriceAPI = FuturesPriceAPI()
    try:
        trading_days: List[datetime.date] = api.get_trading_days(
            datetime.date(2023, 1, 1), datetime.date(2026, 9, 1), product="TX"
        )
        gap_to_previous_close: List[float] = []
        gap_to_same_day_close: List[float] = []

        for previous_day, day in zip(trading_days, trading_days[1:]):
            night: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
                api.get(day, product="TX", session=FuturesSession.NIGHT), day
            )
            today: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
                api.get(day, product="TX", session=FuturesSession.DAY), day
            )
            yesterday: List[FuturesQuote] = FuturesQuoteAdapter.from_day_rows(
                api.get(previous_day, product="TX", session=FuturesSession.DAY),
                previous_day,
            )
            if not night or not today or not yesterday:
                continue

            # 近月合約＝到期月代碼最小的那一檔
            night_open: float = min(night, key=lambda q: q.expiry).open
            gap_to_previous_close.append(
                abs(night_open - min(yesterday, key=lambda q: q.expiry).close)
            )
            gap_to_same_day_close.append(
                abs(night_open - min(today, key=lambda q: q.expiry).close)
            )
    finally:
        api.close()

    if len(gap_to_previous_close) < 100:
        pytest.skip("樣本不足，無法判斷時序口徑")

    assert statistics.median(gap_to_previous_close) < statistics.median(
        gap_to_same_day_close
    )


# === 整併模式的常見陷阱 ===
def test_combined_session_is_not_used_for_price_queries() -> None:
    """
    **`COMBINED` 不可拿去查資料表**

    它是報價層的組合結果，不是 `session` 欄位裡的值。策略若直接用
    `self.session` 查歷史行情，會得到空結果——而空結果在策略裡表現為
    「訊號永遠不成立」，整場零交易卻沒有任何錯誤訊息。
    2026-09-02 實測踩到過，故固化為測試。
    """

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()

    strategy.session = FuturesSession.COMBINED
    assert strategy.price_query_session == FuturesSession.DAY

    strategy.session = FuturesSession.NIGHT
    assert strategy.price_query_session == FuturesSession.NIGHT


def test_strategy_filters_combined_quotes() -> None:
    """整併模式下 `filter_session()` 要留下 `COMBINED` 報價，不可整批濾掉"""

    from core.strategies.futures.momentum_futures_strategy import (
        MomentumFuturesStrategy,
    )

    strategy: MomentumFuturesStrategy = MomentumFuturesStrategy()
    strategy.session = FuturesSession.COMBINED

    quotes: List[FuturesQuote] = [
        make_quote(19314, 19144, 19340, 19137, 100, FuturesSession.COMBINED),
        make_quote(19314, 19144, 19340, 19137, 100, FuturesSession.DAY),
    ]

    filtered: List[FuturesQuote] = strategy.filter_session(quotes)

    assert len(filtered) == 1
    assert filtered[0].session == FuturesSession.COMBINED


def test_etl_only_iterates_the_two_real_sessions() -> None:
    """
    **ETL 逐時段爬取時只能走 `data_sessions()`**

    直接 `for s in FuturesSession` 會把整併用的 `COMBINED` 也算進去，
    於是去爬一個來源根本沒有的時段——加入 `COMBINED` 的當下就是這樣讓
    爬蟲與清洗器一起壞掉的（`KeyError: 'combined'`）。
    """

    import inspect

    from core.pipeline.tw.crawlers import futures_price_crawler
    from core.pipeline.tw.updaters import futures_price_updater

    assert FuturesSession.data_sessions() == (FuturesSession.DAY, FuturesSession.NIGHT)

    for module in (futures_price_crawler, futures_price_updater):
        source: str = inspect.getsource(module)
        assert "for session in FuturesSession:" not in source, (
            f"{module.__name__} 直接迭代 FuturesSession，會爬到不存在的 COMBINED"
        )
