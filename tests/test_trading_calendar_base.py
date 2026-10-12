import datetime
from typing import List

import pytest

from core.market.calendar import BaseTradingCalendar
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.market_calendar import MarketCalendar

"""
交易日曆的對稱性：交易日運算只有基底一份，兩個市場只決定清單從哪張表來

原本 `MarketCalendar` 與 `FuturesCalendar` 沒有繼承關係，建構子、`is_trading_day`、
`shift_trading_days`、`get_previous_trading_day` 逐行相同；期貨多出的區間與計數方法
對台股同樣成立。
"""

DAYS: List[datetime.date] = [
    datetime.date(2024, 3, 1),  # 五
    datetime.date(2024, 3, 4),  # 一
    datetime.date(2024, 3, 5),
    datetime.date(2024, 3, 7),  # 3/6 缺
]


class _Api:
    """只回固定清單的行情 API 替身"""

    def __init__(self, days: List[datetime.date]) -> None:
        self.days: List[datetime.date] = days
        self.calls: List = []

    def get_trading_days(self, start, end, product=None) -> List[datetime.date]:
        self.calls.append((start, end, product))
        return [day for day in self.days if start <= day <= end]


@pytest.mark.parametrize("calendar_cls", [MarketCalendar, FuturesCalendar])
def test_both_calendars_share_the_base(calendar_cls: type) -> None:
    """兩個市場都繼承基底，交易日運算不可再在子類覆寫"""

    assert issubclass(calendar_cls, BaseTradingCalendar)
    for name in (
        "__init__",
        "is_trading_day",
        "get_trading_days",
        "count_trading_days",
        "shift_trading_days",
        "get_next_trading_day",
        "get_previous_trading_day",
        "previous_trading_day_from_api",
    ):
        assert name in BaseTradingCalendar.__dict__
        assert name not in calendar_cls.__dict__, (
            f"{calendar_cls.__name__} 覆寫了 {name}"
        )
    assert "from_api" in calendar_cls.__dict__


@pytest.mark.parametrize("calendar_cls", [MarketCalendar, FuturesCalendar])
def test_trading_day_arithmetic_is_identical(calendar_cls: type) -> None:
    """平移、前後一日、區間與計數在兩個市場給同一個答案"""

    calendar = calendar_cls(DAYS)

    assert calendar.is_trading_day(datetime.date(2024, 3, 4))
    assert not calendar.is_trading_day(datetime.date(2024, 3, 6))
    assert calendar.shift_trading_days(datetime.date(2024, 3, 4), -1) == DAYS[0]
    assert calendar.shift_trading_days(datetime.date(2024, 3, 6), 0) == DAYS[3]
    assert calendar.shift_trading_days(datetime.date(2024, 3, 7), 1) is None
    assert calendar.get_previous_trading_day(datetime.date(2024, 3, 1)) is None
    assert calendar.get_next_trading_day(datetime.date(2024, 3, 5)) == DAYS[3]
    assert (
        calendar.get_trading_days(datetime.date(2024, 3, 2), datetime.date(2024, 3, 6))
        == DAYS[1:3]
    )
    assert calendar.count_trading_days(DAYS[0], DAYS[-1]) == 4


def test_base_from_api_is_left_to_the_markets() -> None:
    """基底不知道清單從哪張表來"""

    with pytest.raises(NotImplementedError):
        BaseTradingCalendar.from_api(_Api(DAYS), DAYS[0], DAYS[-1])


@pytest.mark.parametrize("calendar_cls", [MarketCalendar, FuturesCalendar])
def test_previous_trading_day_from_api_uses_the_lookback_bound(
    calendar_cls: type,
) -> None:
    """查前一交易日只往回看 `MAX_LOOKBACK_DAYS` 個曆日；找不到就 `LookupError` 而不是一直找"""

    api: _Api = _Api(DAYS)
    date: datetime.date = datetime.date(2024, 3, 6)

    assert calendar_cls.previous_trading_day_from_api(api, date) == DAYS[2]
    start, end, _ = api.calls[-1]
    assert (date - start).days == calendar_cls.MAX_LOOKBACK_DAYS
    assert end == date - datetime.timedelta(days=1)

    with pytest.raises(LookupError):
        calendar_cls.previous_trading_day_from_api(_Api([]), date)


def test_futures_from_api_still_filters_by_product() -> None:
    """期貨的 `from_api()` 多一個商品參數，其餘與台股同形"""

    api: _Api = _Api(DAYS)

    FuturesCalendar.from_api(api, DAYS[0], DAYS[-1], product="TX")
    MarketCalendar.from_api(api, DAYS[0], DAYS[-1])

    assert api.calls[0][2] == "TX"
    assert api.calls[1][2] is None
