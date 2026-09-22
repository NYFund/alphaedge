import datetime
import sqlite3
from pathlib import Path
from typing import Callable, List

import pandas as pd
import pytest

from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.pipeline.tw.updaters.futures_margin_updater import FuturesMarginUpdater

"""
期貨保證金兩張表 DAO

1. 「生效日」查詢收斂為一份：`inclusive=True`（`<=`，這一天適用多少）與
   `inclusive=False`（`<`，這次調整之前是多少）兩種語意都要成立
2. 表名白名單、寫入回傳實際新增列數、表不存在回 None
3. updater 與 loader 共用同一個 DAO；`FuturesMarginConfig.from_api()` 不再暗開連線

不連網路、不碰正式的 `tw_futures.db`。
"""

MARGIN_COLUMNS: List[str] = [
    "effective_date",
    "product",
    "product_name",
    "結算保證金",
    "維持保證金",
    "原始保證金",
    "source",
]


def make_margin(rows: List[list]) -> pd.DataFrame:
    """rows 為 [生效日, 商品, 原始保證金, 來源]"""

    return pd.DataFrame(
        [
            [date, product, product, initial, initial, initial, source]
            for date, product, initial, source in rows
        ],
        columns=MARGIN_COLUMNS,
    )


@pytest.fixture
def dao() -> FuturesMarginDAO:
    """TX 在 2024-08-09、2024-08-22 各調整一次，08-22 同時有一覽表那一列"""

    margin_dao: FuturesMarginDAO = FuturesMarginDAO(conn=sqlite3.connect(":memory:"))
    margin_dao.ensure_tables()
    margin_dao.insert_rows(
        FuturesMarginDAO.TABLE_NAME,
        make_margin(
            [
                ["2024-08-09", "TX", 265000, "announcement"],
                ["2024-08-22", "TX", 292000, "snapshot"],
            ]
        ),
    )
    margin_dao.commit()
    return margin_dao


# === 生效日查詢 ===
def test_inclusive_answers_what_applies_on_the_day(dao: FuturesMarginDAO) -> None:
    """`<=`：調整生效日當天就適用新值"""

    assert dao.get_margin_in_effect("TX", datetime.date(2024, 8, 22))[2] == 292000
    assert dao.get_margin_in_effect("TX", datetime.date(2024, 8, 21))[2] == 265000


def test_exclusive_answers_what_it_was_before_the_adjustment(
    dao: FuturesMarginDAO,
) -> None:
    """
    `<`：同一生效日已有列（一覽表、或重跑時的本次公告）時，仍要拿到調整**前**的值

    用 `<=` 會拿到 292,000，與公告載明的「調整前 265,000」必然不符。
    """

    assert (
        dao.get_margin_in_effect("TX", datetime.date(2024, 8, 22), inclusive=False)[2]
        == 265000
    )
    assert (
        dao.get_margin_in_effect("TX", datetime.date(2024, 8, 9), inclusive=False)
        is None
    )


def test_updater_consistency_check_uses_the_exclusive_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 的「調整前」查詢走 `<`：同日已有一覽表列時不可拿到新值"""

    updater: FuturesMarginUpdater = make_updater(tmp_path, monkeypatch)
    updater.dao.insert_rows(
        FuturesMarginDAO.TABLE_NAME,
        make_margin(
            [
                ["2024-08-09", "TX", 265000, "announcement"],
                ["2024-08-22", "TX", 292000, "snapshot"],
            ]
        ),
    )

    assert updater.get_margin_in_effect("TX", "2024-08-22") == 265000
    updater.close()


def test_fallback_to_earliest(dao: FuturesMarginDAO) -> None:
    """查詢日早於表內所有列時，只有明確要求才退回最早一列"""

    early: datetime.date = datetime.date(2020, 1, 1)

    assert dao.get_margin_in_effect("TX", early) is None
    assert dao.get_margin_in_effect("TX", early, fallback_to_earliest=True)[2] == 265000


def test_missing_tables_return_none() -> None:
    """尚未跑過保證金 ETL：金額、比例、涵蓋範圍一律回 None"""

    margin_dao: FuturesMarginDAO = FuturesMarginDAO(conn=sqlite3.connect(":memory:"))

    assert margin_dao.get_margin_in_effect("TX", datetime.date(2024, 8, 22)) is None
    assert margin_dao.get_rates_in_effect("CDF", datetime.date(2024, 8, 22)) is None
    assert margin_dao.get_covered_date_range("TX") is None


# === 寫入 ===
def test_insert_counts_only_new_rows_and_replace_overwrites(
    dao: FuturesMarginDAO,
) -> None:
    """重複列不計入；`replace=True` 覆蓋同主鍵列且不計入新增"""

    duplicated: pd.DataFrame = make_margin(
        [["2024-08-09", "TX", 265000, "announcement"]]
    )
    corrected: pd.DataFrame = make_margin(
        [["2024-08-22", "TX", 292000, "announcement"]]
    )

    assert dao.insert_rows(FuturesMarginDAO.TABLE_NAME, duplicated) == 0
    assert dao.insert_rows(FuturesMarginDAO.TABLE_NAME, corrected, replace=True) == 0
    assert dao.get_announcement_margins("TX") == [
        ("2024-08-09", 265000),
        ("2024-08-22", 292000),
    ]
    assert dao.get_latest_snapshot_margin("TX") is None


def test_table_name_whitelist(dao: FuturesMarginDAO) -> None:
    """表名只能是兩張保證金表之一"""

    with pytest.raises(ValueError):
        dao.count_rows("price")
    with pytest.raises(ValueError):
        dao.insert_rows("price; DROP TABLE x", make_margin([]))


# === updater／loader／設定 ===
def make_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FuturesMarginUpdater:
    """DB 與 downloads 都指向暫存區的保證金 updater"""

    import core.pipeline.tw.loaders.futures_margin_loader as loader_module
    import core.pipeline.tw.updaters.futures_margin_updater as updater_module

    monkeypatch.setattr(
        updater_module, "TW_FUTURES_DB_PATH", tmp_path / "tw_futures.db"
    )
    monkeypatch.setattr(
        loader_module, "FUTURES_MARGIN_DOWNLOADS_PATH", tmp_path / "margin"
    )
    return updater_module.FuturesMarginUpdater()


def test_updater_and_loader_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    updater: FuturesMarginUpdater = make_updater(tmp_path, monkeypatch)

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_loader_failed_insert_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫到一半失敗時整批回滾，例外往外拋"""

    updater: FuturesMarginUpdater = make_updater(tmp_path, monkeypatch)
    original_insert: Callable[..., int] = FuturesMarginDAO.insert_rows

    def insert_then_fail(
        self: FuturesMarginDAO, table: str, df: pd.DataFrame, replace: bool = False
    ) -> int:
        """照常寫入後模擬寫到一半失敗"""

        original_insert(self, table, df, replace=replace)
        raise OSError("disk I/O error")

    monkeypatch.setattr(FuturesMarginDAO, "insert_rows", insert_then_fail)

    with pytest.raises(OSError):
        updater.loader.add_to_db(
            make_margin([["2024-08-09", "TX", 265000, "snapshot"]])
        )

    assert updater.dao.count_rows() == 0
    updater.close()


def test_margin_config_from_api_requires_an_api() -> None:
    """`from_api()` 不再暗開一條沒人關的連線：未傳 API 直接 TypeError"""

    from core.market.tw.futures_margin_config import FuturesMarginConfig

    with pytest.raises(TypeError):
        FuturesMarginConfig.from_api()  # type: ignore[call-arg]
