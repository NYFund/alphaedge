import datetime
from pathlib import Path
from typing import List, Optional

import pytest

from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.live.datafeed.tw.futures_live_datafeed import TwFuturesLiveDataFeed
from core.live.intraday.session_guard import is_night_session, session_open_date
from core.market.tw.futures_calendar import FuturesCalendar
from core.utils import FuturesSession

"""
期貨日／夜盤判定只有一份：四個呼叫端在每個邊界都要給出同一個答案

這條規則曾經寫了四份，其中一份對 13:45–15:00 的空檔回 None、其餘三份歸日盤，
05:00 整點也有一份歸夜盤、兩份歸日盤——沒有任何東西保證它們一致。
"""

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

DAY: datetime.date = datetime.date(2026, 9, 21)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime.datetime:
    """同一個曆日的某個時刻"""

    return datetime.datetime.combine(DAY, datetime.time(hour, minute, second))


# (時刻, 嚴格判定, 實盤判定)
BOUNDARIES: List[tuple] = [
    (at(8, 44, 59), None, FuturesSession.DAY),  # 開盤前的空檔
    (at(8, 45), FuturesSession.DAY, FuturesSession.DAY),
    (at(13, 45), FuturesSession.DAY, FuturesSession.DAY),  # 日盤收盤時點算日盤
    (at(13, 45, 1), None, FuturesSession.DAY),  # 日夜盤之間的空檔
    (at(14, 59, 59), None, FuturesSession.DAY),
    (at(15, 0), FuturesSession.NIGHT, FuturesSession.NIGHT),
    (at(23, 59, 59), FuturesSession.NIGHT, FuturesSession.NIGHT),
    (at(0, 0), FuturesSession.NIGHT, FuturesSession.NIGHT),  # 跨午夜
    (at(4, 59, 59), FuturesSession.NIGHT, FuturesSession.NIGHT),
    (at(5, 0), FuturesSession.NIGHT, FuturesSession.NIGHT),  # 夜盤收盤時點算夜盤
    (at(5, 0, 1), None, FuturesSession.DAY),  # 夜盤收盤後的空檔
]


@pytest.mark.parametrize(("moment", "strict", "live"), BOUNDARIES)
def test_all_callers_agree_on_every_boundary(
    moment: datetime.datetime,
    strict: Optional[FuturesSession],
    live: FuturesSession,
) -> None:
    """市場結構走嚴格判定，券商行情、實盤資料源、日終日期歸屬走「空檔歸日盤」"""

    assert FuturesSession.resolve(moment) is strict
    assert FuturesCalendar.resolve_session(moment) is strict

    assert FuturesSession.resolve_or_day(moment) is live
    assert ShioajiQuoteStream.resolve_session(moment) is live
    assert TwFuturesLiveDataFeed._resolve_session(moment) is live
    assert is_night_session(moment) is (live is FuturesSession.NIGHT)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(0, 30), DAY - datetime.timedelta(days=1)),  # 跨午夜仍屬前一天開始的那段
        (at(5, 0), DAY - datetime.timedelta(days=1)),  # 收盤時點與夜盤判定一致
        (at(5, 0, 1), DAY),
        (at(15, 0), DAY),
    ],
)
def test_session_open_date_agrees_with_the_night_rule(
    moment: datetime.datetime, expected: datetime.date
) -> None:
    """夜盤開始日的跨日界線與夜盤判定用同一個收盤時點"""

    assert session_open_date(moment) == expected


def test_session_hours_are_not_hard_coded_elsewhere() -> None:
    """時段起訖只寫在 `core/utils/constant/futures.py`，其他地方不得再寫死時數"""

    patterns: List[str] = ["hour >= 15", "hour < 5", "time(15, 0)", "time(5, 0)"]
    offenders: List[str] = []
    for path in (_PROJECT_ROOT / "core").rglob("*.py"):
        if path.name == "futures.py" and path.parent.name == "constant":
            continue
        text: str = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(_PROJECT_ROOT)}：{pattern}"
            for pattern in patterns
            if pattern in text
        )

    assert not offenders, "期貨時段寫死在權威常數之外：" + "；".join(offenders)
