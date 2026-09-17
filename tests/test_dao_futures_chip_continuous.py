import datetime
import sqlite3
from pathlib import Path
from typing import Callable, Iterator, List

import pandas as pd
import pytest

from core.config import (
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_PRICE_DAILY_TABLE_NAME,
)
from core.dao.base import BaseDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.pipeline.tw.loaders.futures_chip_loader import FuturesChipLoader
from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater
from core.pipeline.tw.updaters.futures_continuous_updater import (
    FuturesContinuousUpdater,
)

"""
期貨籌碼三表與連續合約表 DAO

1. `FuturesChipAPI` 的 `table=` 只接受三張籌碼表（舊版沒檢查就組進 SQL）
2. `get_on_date()` 只在表不存在時回空表（舊版連查詢錯誤一起吞）
3. loader 不再自己 commit，由 updater 控制；寫到一半失敗整批回滾
4. updater 與 loader、行情 API 共用同一條連線

不連網路、不碰正式的 `tw_futures.db`。
"""


def make_institutional(dates: List[str]) -> pd.DataFrame:
    """三大法人籌碼（一天一列）"""

    return pd.DataFrame(
        [
            {
                "date": date,
                "product_name": "臺股期貨",
                "investor": "外資及陸資",
                "多空未平倉口數淨額": -80000.0,
            }
            for date in dates
        ]
    )


def count_committed(db_path: Path, table: str) -> int:
    """以另一條連線數列數：只看得到已 commit 的資料"""

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


# === 籌碼 API ===
def test_chip_api_rejects_unknown_table() -> None:
    """`table=` 不在白名單時當場 `ValueError`，不會組進 SQL"""

    from core.api.tw.futures_chip_api import FuturesChipAPI

    api: FuturesChipAPI = FuturesChipAPI(conn=sqlite3.connect(":memory:"))

    with pytest.raises(ValueError):
        api.get_available(datetime.date(2026, 9, 1), table="price; DROP TABLE x")
    with pytest.raises(ValueError):
        api.get_on_date(datetime.date(2026, 9, 1), table="futures_price_daily")


def test_chip_api_on_date_only_hides_missing_table() -> None:
    """表不存在回空表；表存在但查詢出錯時往外拋"""

    from core.api.tw.futures_chip_api import FuturesChipAPI

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    api: FuturesChipAPI = FuturesChipAPI(conn=conn)
    assert api.get_on_date(datetime.date(2026, 9, 1)).empty

    # 故意缺 date 欄
    conn.execute(
        f"CREATE TABLE {FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME} (investor TEXT)"
    )
    with pytest.raises(pd.errors.DatabaseError):
        api.get_on_date(datetime.date(2026, 9, 1))


# === 籌碼 loader／updater ===
@pytest.fixture
def chip_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FuturesChipUpdater]:
    """DB 與 downloads 都指向暫存區的籌碼 updater"""

    import core.pipeline.tw.loaders.futures_chip_loader as loader_module
    import core.pipeline.tw.updaters.futures_chip_updater as updater_module

    monkeypatch.setattr(
        updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(loader_module, "FUTURES_CHIP_DOWNLOADS_PATH", tmp_path / "chip")

    updater: FuturesChipUpdater = updater_module.FuturesChipUpdater()
    yield updater
    updater.close()


def test_chip_updater_shares_one_connection(chip_updater: FuturesChipUpdater) -> None:
    """loader 與行情 API 都用 updater 的連線，`close()` 一併關閉"""

    assert chip_updater.loader.conn is chip_updater.conn
    assert chip_updater.price_api.conn is chip_updater.conn

    conn: sqlite3.Connection = chip_updater.conn
    chip_updater.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_chip_loader_leaves_commit_to_the_updater(
    chip_updater: FuturesChipUpdater, tmp_path: Path
) -> None:
    """loader 寫完不 commit，另一條連線看不到；updater 呼叫 `commit()` 後才落地"""

    inserted: int = chip_updater.loader.add_to_db(
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
        make_institutional(["2026-08-27", "2026-08-28"]),
    )

    assert inserted == 2
    db_path: Path = tmp_path / "tw_futures.db"
    assert count_committed(db_path, FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME) == 0

    chip_updater.loader.commit()
    assert count_committed(db_path, FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME) == 2
    # 重跑不產生第二份
    assert (
        chip_updater.loader.add_to_db(
            FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, make_institutional(["2026-08-28"])
        )
        == 0
    )


def test_chip_loader_failed_insert_rolls_back(
    chip_updater: FuturesChipUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫到一半失敗時只回滾這一批，同一交易內先前的批次不受影響"""

    loader: FuturesChipLoader = chip_updater.loader
    loader.add_to_db(
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, make_institutional(["2026-08-27"])
    )

    original_insert: Callable[..., int] = FuturesChipDAO.insert_new_rows

    def insert_then_fail(self: FuturesChipDAO, df: pd.DataFrame) -> int:
        """照常寫入後模擬寫到一半失敗"""

        original_insert(self, df)
        raise OSError("disk I/O error")

    monkeypatch.setattr(FuturesChipDAO, "insert_new_rows", insert_then_fail)

    with pytest.raises(OSError):
        loader.add_to_db(
            FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, make_institutional(["2026-08-28"])
        )

    assert loader.count_rows(FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME) == 1


def test_trading_day_check_treats_missing_price_table_as_trading(
    chip_updater: FuturesChipUpdater, dao_factory: Callable[..., BaseDAO]
) -> None:
    """行情表還沒建時一律視為有交易日（寧可重試，也不要把被擋當成沒資料）"""

    day: datetime.date = datetime.date(2026, 8, 28)
    assert chip_updater.has_trading_days(day, day) is True

    price_dao: FuturesPriceDAO = dao_factory(FuturesPriceDAO, conn=chip_updater.conn)
    assert chip_updater.has_trading_days(day, day) is False

    price_dao.insert_or_ignore(
        pd.DataFrame(
            [
                {
                    "date": "2026-08-28",
                    "product": "TX",
                    "expiry": "202609",
                    "session": "day",
                    "成交量": 1,
                }
            ]
        )
    )
    assert chip_updater.has_trading_days(day, day) is True


def test_trading_day_check_raises_on_query_error(
    chip_updater: FuturesChipUpdater,
) -> None:
    """行情表存在但查詢出錯時往外拋（舊版 `except Exception` 一律回 True）"""

    # 故意缺 date 欄
    chip_updater.conn.execute(
        f"CREATE TABLE {FUTURES_PRICE_DAILY_TABLE_NAME} (product TEXT)"
    )
    day: datetime.date = datetime.date(2026, 8, 28)

    with pytest.raises(pd.errors.DatabaseError):
        chip_updater.has_trading_days(day, day)


# === 連續合約 ===
@pytest.fixture
def continuous_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FuturesContinuousUpdater]:
    """DB 與 downloads 都指向暫存區的連續合約 updater"""

    import core.pipeline.tw.loaders.futures_continuous_loader as loader_module
    import core.pipeline.tw.updaters.futures_continuous_updater as updater_module

    monkeypatch.setattr(
        updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(
        loader_module, "FUTURES_CONTINUOUS_DOWNLOADS_PATH", tmp_path / "continuous"
    )

    updater: FuturesContinuousUpdater = updater_module.FuturesContinuousUpdater()
    yield updater
    updater.close()


def test_continuous_updater_shares_one_connection(
    continuous_updater: FuturesContinuousUpdater,
) -> None:
    """行情 API 與 loader 都用 updater 的 DAO 連線"""

    assert continuous_updater.loader.dao is continuous_updater.dao
    assert continuous_updater.price_api.conn is continuous_updater.dao.conn

    continuous_updater.close()
    assert continuous_updater.dao is None


def test_continuous_series_is_all_or_nothing(
    continuous_updater: FuturesContinuousUpdater,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    同一組（商品, 換月規則）的幾種調整方式要嘛一起落地、要嘛一起不落地

    以替身讓第二種調整方式寫入時失敗：第一種已寫入的列必須跟著回滾。
    """

    from core.pipeline.tw.loaders.futures_continuous_loader import (
        FuturesContinuousLoader,
    )
    from core.utils import FuturesAdjustMethod, FuturesRollRule, FuturesSession

    series: pd.DataFrame = pd.DataFrame(
        [
            {
                "date": "2026-08-28",
                "expiry": "202609",
                "開盤價": 1.0,
                "最高價": 1.0,
                "最低價": 1.0,
                "收盤價": 1.0,
                "成交量": 1,
                "結算價": 1.0,
                "未沖銷契約量": 1,
                "roll_flag": 0,
                "roll_gap": 0.0,
                "roll_ratio": 1.0,
            }
        ]
    )
    monkeypatch.setattr(
        continuous_updater.price_api,
        "get_range",
        lambda *args, **kwargs: pd.DataFrame({"date": ["2026-08-28"]}),
    )
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.futures_continuous_updater.FuturesCalendar.from_api",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.futures_continuous_updater.FuturesRollPlanner",
        lambda *args, **kwargs: type(
            "Planner", (), {"build_roll_schedule": lambda self, **_: {1: "202609"}}
        )(),
    )
    monkeypatch.setattr(continuous_updater, "build_expiries_by_date", lambda df: {})
    monkeypatch.setattr(
        continuous_updater, "build_open_interest_by_date", lambda df: {}
    )
    monkeypatch.setattr(continuous_updater, "build_series", lambda df, schedule: series)

    original_add: Callable[..., int] = FuturesContinuousLoader.add_to_db
    calls: List[int] = []

    def add_then_fail_on_second(self: FuturesContinuousLoader, df: pd.DataFrame) -> int:
        """第一個商品照常寫入，第二個商品寫入時模擬失敗"""

        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk I/O error")
        return original_add(self, df)

    monkeypatch.setattr(FuturesContinuousLoader, "add_to_db", add_then_fail_on_second)

    with pytest.raises(OSError):
        continuous_updater.update_product(
            product="TX",
            start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2026, 8, 31),
            session=FuturesSession.DAY,
            methods=[FuturesAdjustMethod.NONE, FuturesAdjustMethod.BACKWARD],
            roll_rule=FuturesRollRule.LAST_TRADING_DAY,
            days_before_expiry=3,
        )

    continuous_updater.dao.commit()
    assert (
        continuous_updater.dao.conn.execute(
            f"SELECT COUNT(*) FROM {FuturesContinuousDAO.TABLE_NAME}"
        ).fetchone()[0]
        == 0
    )
