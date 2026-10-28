import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import pytest

from core.dao.tw.market_holiday_dao import MarketHolidayDAO
from core.pipeline.tw.cleaners.market_holiday_cleaner import MarketHolidayCleaner
from core.pipeline.tw.loaders.market_holiday_loader import MarketHolidayLoader
from scripts.manual.manual_market_closure import add_closure, remove_closure

"""
臨時休市登記腳本：預設只列計畫、週末與官方休市不登記、撤銷只動登記過的日子

腳本是颱風夜一行指令就要生效的工具，誤登記的代價是少跑一天，漏登記的代價是
當天照交易日跑、隔天新鮮度檢查中止——兩者都要在登記當下就看得出來。
"""

FIXTURE_PATH: Path = (
    Path(__file__).parent / "fixtures" / "twse_holiday_schedule_2026.json"
)

TYPHOON: datetime.date = datetime.date(2026, 10, 23)


@pytest.fixture
def dao() -> MarketHolidayDAO:
    """已載入 2026 年官方日曆、尚未建臨時休市表的 in-memory DAO"""

    payload: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw: pd.DataFrame = pd.DataFrame(payload["data"], columns=payload["fields"])
    holiday_dao: MarketHolidayDAO = MarketHolidayDAO(conn=sqlite3.connect(":memory:"))
    MarketHolidayLoader(dao=holiday_dao).add_to_db(
        MarketHolidayCleaner().clean(raw, 2026), 2026
    )
    return holiday_dao


def test_plan_only_writes_nothing(dao: MarketHolidayDAO) -> None:
    """不帶 `--apply` 只列計畫：連表都不建"""

    assert add_closure(dao, TYPHOON, "颱風停止交易", apply=False) is True

    assert not dao.adhoc_table_exists()


def test_apply_registers_the_closure(dao: MarketHolidayDAO) -> None:
    """帶 `--apply` 才寫入；同一天再登記一次不重複寫"""

    assert add_closure(dao, TYPHOON, "颱風停止交易", apply=True) is True

    assert dao.get_adhoc_closures(TYPHOON, TYPHOON) == {TYPHOON}
    assert add_closure(dao, TYPHOON, "颱風停止交易", apply=True) is False
    assert len(dao.list_adhoc_closures()) == 1


@pytest.mark.parametrize(
    "date",
    [
        datetime.date(2026, 10, 24),  # 週六
        datetime.date(2026, 10, 26),  # 光復節補假（官方公告）
    ],
)
def test_weekend_and_official_closure_are_not_registered(
    dao: MarketHolidayDAO, date: datetime.date
) -> None:
    """本來就休市的日子不登記，臨時休市清單只留真正的臨時休市"""

    assert add_closure(dao, date, "颱風停止交易", apply=True) is False

    assert dao.list_adhoc_closures().empty


def test_remove_only_touches_registered_days(dao: MarketHolidayDAO) -> None:
    """撤銷沒登記過的日子不做事；撤銷登記過的日子要帶 `--apply` 才刪"""

    assert remove_closure(dao, TYPHOON, apply=True) is False

    add_closure(dao, TYPHOON, "颱風停止交易", apply=True)
    assert remove_closure(dao, TYPHOON, apply=False) is True
    assert dao.get_adhoc_closures(TYPHOON, TYPHOON) == {TYPHOON}

    assert remove_closure(dao, TYPHOON, apply=True) is True
    assert dao.get_adhoc_closures(TYPHOON, TYPHOON) == set()
