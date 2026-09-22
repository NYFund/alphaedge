import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from core.config import TW_FUTURES_DB_PATH
from core.market.tw.futures_calendar import FuturesCalendar
from core.utils import FuturesSession

"""
台期貨交易日曆測試

**不可沿用股票 calendar 的四個理由**，本檔逐一釘住：

1. **期貨有夜盤且跨日**（15:00 → 次日 05:00）：凌晨 03:00 的那一筆屬於
   前一天開始的那段夜盤，判錯會把成交歸到錯誤的交易日。
2. **有結算日與最後交易日**：契約到期後就不再有報價，沒有日曆只能等報價消失。
3. **最後交易日遇休市要順延，且要順延到「期貨自己」的下一個開盤日**——
   2023-01 契約遇春節連休 12 天，從 01-18 一路順延到 01-30。
4. **休市是事實不是規則**：颱風假與補行交易日推不出來，只能看實際有沒有行情。

真實資料的比對（140 個已到期 TX 月契約）標了 `slow` 並在缺 DB 時 skip。
"""


def make_calendar(dates: List[str]) -> FuturesCalendar:
    """由日期字串建立日曆"""

    return FuturesCalendar([datetime.date.fromisoformat(d) for d in dates])


# 2024-03 的實際交易日（週一到週五，無臨時休市）
MARCH_2024: List[str] = [
    "2024-03-01",
    "2024-03-04",
    "2024-03-05",
    "2024-03-06",
    "2024-03-07",
    "2024-03-08",
    "2024-03-11",
    "2024-03-12",
    "2024-03-13",
    "2024-03-14",
    "2024-03-15",
    "2024-03-18",
    "2024-03-19",
    "2024-03-20",
    "2024-03-21",
    "2024-03-22",
]


@pytest.fixture
def calendar() -> FuturesCalendar:
    """2024-03 的日曆"""

    return make_calendar(MARCH_2024)


# === 交易日 ===
def test_trading_day_comes_from_data_not_from_weekday(calendar) -> None:
    """
    交易日的判準是**有沒有行情**，不是「是不是平日」

    颱風假與補行交易日推不出來——那是公告出來的事實。
    """

    assert calendar.is_trading_day(datetime.date(2024, 3, 1)) is True
    assert calendar.is_trading_day(datetime.date(2024, 3, 2)) is False  # 週六
    # 平日但不在清單內（例如臨時休市）一律視為非交易日
    assert calendar.is_trading_day(datetime.date(2024, 3, 25)) is False


def test_count_trading_days_is_not_calendar_days(calendar) -> None:
    """
    持倉天數要用交易日算

    3/1（五）到 3/4（一）曆日差 3 天，交易日只差 1 天——用曆日算風控會提早出場。
    """

    start: datetime.date = datetime.date(2024, 3, 1)
    end: datetime.date = datetime.date(2024, 3, 4)

    assert (end - start).days == 3
    assert calendar.count_trading_days(start, end) == 2  # 含頭含尾兩天


def test_shift_and_neighbours(calendar) -> None:
    """以交易日為單位平移，跨週末不會落在週六"""

    friday: datetime.date = datetime.date(2024, 3, 1)

    assert calendar.shift_trading_days(friday, 1) == datetime.date(2024, 3, 4)
    assert calendar.get_next_trading_day(friday) == datetime.date(2024, 3, 4)
    assert calendar.get_previous_trading_day(datetime.date(2024, 3, 4)) == friday
    # 超出日曆涵蓋範圍時回 None（代表資料不足以推算，不是「沒有那天」）
    assert calendar.shift_trading_days(friday, 999) is None


# === 結算日與最後交易日 ===
def test_monthly_settlement_is_the_third_wednesday(calendar) -> None:
    """月契約的最後交易日 ＝ 交割月份的第三個星期三"""

    settlement: datetime.date = calendar.get_settlement_date(2024, 3)

    assert settlement == datetime.date(2024, 3, 20)
    assert settlement.weekday() == 2
    assert calendar.get_last_trading_date("202403") == settlement


def test_settlement_postpones_over_a_long_holiday() -> None:
    """
    **順延到期貨自己的下一個開盤日**

    2023-01 契約的第三個星期三是 01-18，遇春節連休 12 天，
    最後交易日順延到 2023-01-30——這種長度推不出來，只能看實際開盤日。
    """

    calendar: FuturesCalendar = make_calendar(
        [
            "2023-01-16",
            "2023-01-17",
            # 01-18 ~ 01-29 春節休市
            "2023-01-30",
            "2023-01-31",
        ]
    )

    assert calendar.get_nth_weekday(2023, 1, 2, 3) == datetime.date(2023, 1, 18)
    assert calendar.get_settlement_date(2023, 1) == datetime.date(2023, 1, 30)


def test_weekly_contracts_use_their_own_week(calendar) -> None:
    """
    週契約的規則與月契約不同：`YYYYMMWn` 是該月第 n 個星期三

    **沒有 W3**——第三週就是月契約本身。
    """

    assert calendar.get_last_trading_date("202403W1") == datetime.date(2024, 3, 6)
    assert calendar.get_last_trading_date("202403W2") == datetime.date(2024, 3, 13)
    assert calendar.get_last_trading_date("202403W4") == datetime.date(2024, 3, 27)


def test_unparsable_expiry_returns_none(calendar) -> None:
    """代碼格式不對時回 None 並記 warning，不可拋錯中斷整場回測"""

    assert calendar.get_last_trading_date("2024-03") is None
    assert calendar.get_last_trading_date("") is None


def test_days_to_expiry_counts_trading_days(calendar) -> None:
    """
    距離最後交易日的天數以**交易日**計

    這是換月規則（「提前 N 日換月」）的輸入；用曆日算會在連假整段位移。
    """

    assert (
        calendar.get_trading_days_to_expiry(datetime.date(2024, 3, 20), "202403") == 0
    )
    assert (
        calendar.get_trading_days_to_expiry(datetime.date(2024, 3, 18), "202403") == 2
    )
    # 已過期為負數
    assert (
        calendar.get_trading_days_to_expiry(datetime.date(2024, 3, 22), "202403") == -2
    )


def test_is_settlement_date(calendar) -> None:
    """結算日判斷要綁契約——同一天對不同到期月的意義不同"""

    assert calendar.is_settlement_date(datetime.date(2024, 3, 20), "202403") is True
    assert calendar.is_settlement_date(datetime.date(2024, 3, 20), "202404") is False


# === 交易時段 ===
def test_day_session_window() -> None:
    """日盤 08:45–13:45"""

    start, end = FuturesCalendar.get_session_window(
        datetime.date(2024, 3, 1), FuturesSession.DAY
    )

    assert start == datetime.datetime(2024, 3, 1, 8, 45)
    assert end == datetime.datetime(2024, 3, 1, 13, 45)


def test_night_session_crosses_midnight() -> None:
    """
    **夜盤跨日**：當天 15:00 開始，到次一曆日 05:00 結束

    回傳的結束時間落在下一天是刻意的，不是計算錯誤。
    """

    start, end = FuturesCalendar.get_session_window(
        datetime.date(2024, 3, 1), FuturesSession.NIGHT
    )

    assert start == datetime.datetime(2024, 3, 1, 15, 0)
    assert end == datetime.datetime(2024, 3, 2, 5, 0)


def test_resolve_session_handles_after_midnight() -> None:
    """凌晨 03:00 屬於**前一天開始**的夜盤，這是最容易搞錯的一點"""

    assert (
        FuturesCalendar.resolve_session(datetime.datetime(2024, 3, 1, 9, 0))
        == FuturesSession.DAY
    )
    assert (
        FuturesCalendar.resolve_session(datetime.datetime(2024, 3, 1, 20, 0))
        == FuturesSession.NIGHT
    )
    assert (
        FuturesCalendar.resolve_session(datetime.datetime(2024, 3, 2, 3, 0))
        == FuturesSession.NIGHT
    )
    # 非交易時間
    assert FuturesCalendar.resolve_session(datetime.datetime(2024, 3, 1, 14, 0)) is None
    assert FuturesCalendar.resolve_session(datetime.datetime(2024, 3, 1, 7, 0)) is None


def test_night_session_did_not_exist_before_2017() -> None:
    """
    **2017-05-15 之前沒有夜盤**

    那是制度不是資料缺漏；當成缺漏處理會一路找不到原因。
    """

    calendar: FuturesCalendar = make_calendar(["2016-06-01", "2018-06-01"])

    assert calendar.has_night_session(datetime.date(2016, 6, 1)) is False
    assert calendar.has_night_session(datetime.date(2018, 6, 1)) is True


# === 真實資料比對 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能比對行事曆"
)
def test_last_trading_dates_match_the_real_table() -> None:
    """
    以真實行情表驗證最後交易日規則

    對每個**已到期**的 TX 月契約，本日曆算出的最後交易日必須等於該契約在行情表中
    出現的最後一天。2015-01 ~ 2026-08 共 140 個契約，逐一比對。
    """

    import sqlite3

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        trading_days: List[datetime.date] = [
            datetime.date.fromisoformat(row[0])
            for row in conn.execute(
                "SELECT DISTINCT date FROM futures_price_daily "
                "WHERE product = 'TX' AND session = 'day' ORDER BY date"
            )
        ]
        last_dates: Dict[str, str] = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT expiry, MAX(date) FROM futures_price_daily "
                "WHERE product = 'TX' AND session = 'day' AND expiry NOT LIKE '%W%' "
                "GROUP BY expiry"
            )
        }
    finally:
        conn.close()

    calendar: FuturesCalendar = FuturesCalendar(trading_days)
    latest: str = str(trading_days[-1])

    mismatched: List[str] = []
    compared: int = 0
    for expiry, last_seen in sorted(last_dates.items()):
        # 尚未到期的契約其「最後一天」只是資料的結束日，不能拿來比
        if last_seen >= latest:
            continue

        compared += 1
        expected: Optional[datetime.date] = calendar.get_last_trading_date(expiry)
        if str(expected) != last_seen:
            mismatched.append(f"{expiry}: 算出 {expected}、實際 {last_seen}")

    assert compared >= 100, f"可比對的已到期契約只有 {compared} 個，樣本不足"
    assert not mismatched, "最後交易日與實際行情不符：" + "; ".join(mismatched[:10])
