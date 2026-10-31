import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import pytest

from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.live.factory import make_market_closed_check
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader

"""
券商 session 用的「這一天確定休市」判定：週末＋官方開休市日曆，不連券商

判斷不出來（年度未入庫、研究庫開不了）一律回 False：session 會照原本的嚴格程度擋下，
只有確定休市才放寬本機日期檢查。
"""

FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "twse_holiday_schedule_2026.json"
)


@pytest.fixture
def stock_db(tmp_path: Path) -> Path:
    """灌好 2026 年官方日曆的研究庫檔案"""

    payload: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw: pd.DataFrame = pd.DataFrame(payload["data"], columns=payload["fields"])
    path: Path = tmp_path / "tw_stock.db"
    conn: sqlite3.Connection = sqlite3.connect(path)
    MarketHolidayLoader(dao=MarketHolidayDAO(conn=conn)).add_to_db(
        MarketHolidayCleaner().clean(raw, 2026), 2026
    )
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize(
    ("date", "closed"),
    [
        (datetime.date(2026, 9, 25), True),  # 中秋：官方表上的休市日
        (datetime.date(2026, 10, 17), True),  # 週六
        (datetime.date(2026, 9, 22), False),  # 交易日
        (datetime.date(2027, 3, 3), False),  # 年度未入庫的平日：判斷不出來
        (datetime.date(2027, 3, 6), True),  # 年度未入庫的週六：週末來源仍答得出
    ],
)
def test_closed_day_check(stock_db: Path, date: datetime.date, closed: bool) -> None:
    assert make_market_closed_check(stock_db)(date) is closed


def test_unreadable_research_db_falls_back_to_the_weekend(tmp_path: Path) -> None:
    """研究庫開不了：週末照樣判得出休市，平日判斷不出來就回 False"""

    check = make_market_closed_check(tmp_path / "missing" / "tw_stock.db")

    assert check(datetime.date(2026, 10, 17)) is True
    assert check(datetime.date(2026, 9, 25)) is False


def test_live_broker_session_gets_the_closed_day_check() -> None:
    """實盤組裝的券商 session 一定帶休市判定：漏接的話連續休市照樣以時鐘錯誤中止"""

    from core.broker.rate_limiter import RateLimiter
    from core.live.factory import _build_broker

    broker = _build_broker("shioaji", True, RateLimiter())

    assert broker.session._is_market_closed is not None
