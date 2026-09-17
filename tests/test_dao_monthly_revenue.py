import sqlite3
from pathlib import Path
from typing import Callable, List, Tuple

import pandas as pd
import pytest
from loguru import logger

from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO
from core.pipeline.tw.loaders.monthly_revenue_report_loader import (
    MonthlyRevenueReportLoader,
)
from core.pipeline.tw.updaters.monthly_revenue_report_updater import (
    MonthlyRevenueReportUpdater,
)
from core.pipeline.utils.exceptions import DataLoadError

"""
月營收（`monthly_revenue` 表）DAO

1. `get_range` 跨年區間：舊版年、月各自 `BETWEEN`，2023-11～2024-02 一筆都查不到
2. 最新年月與續跑起點：表空時用預設值、查詢錯誤往外拋（舊版吞掉後從預設起點重跑）
3. loader 走 `INSERT OR IGNORE`、逐檔 savepoint，與 updater 共用連線

不連網路、不碰正式的 `tw_stock.db`。
"""

MRR_COLUMNS: List[str] = [
    "year",
    "month",
    "stock_id",
    "公司名稱",
    "當月營收",
    "上月營收",
    "去年當月營收",
    "上月比較增減(%)",
    "去年同月增減(%)",
    "當月累計營收",
    "去年累計營收",
    "前期比較增減(%)",
]


def make_mrr_rows(
    year_months: List[Tuple[int, int]], stock_id: str = "2330"
) -> pd.DataFrame:
    """建立指定年月、單一股票的月營收資料"""

    return pd.DataFrame(
        [
            [year, month, stock_id, f"公司{stock_id}"] + [1.0] * 8
            for year, month in year_months
        ],
        columns=MRR_COLUMNS,
    )


@pytest.fixture
def dao() -> MonthlyRevenueDAO:
    """已建表、內含 2023-10～2024-03 六個月資料的記憶體 DAO"""

    mrr_dao: MonthlyRevenueDAO = MonthlyRevenueDAO(conn=sqlite3.connect(":memory:"))
    mrr_dao.ensure_table(MRR_COLUMNS)
    mrr_dao.insert_or_ignore(
        make_mrr_rows(
            [(2023, 10), (2023, 11), (2023, 12), (2024, 1), (2024, 2), (2024, 3)]
        )
    )
    mrr_dao.commit()
    return mrr_dao


# === DAO ===
def test_range_across_year_boundary(dao: MonthlyRevenueDAO) -> None:
    """2023-11～2024-02 應回 4 個月，兩端皆含"""

    result: pd.DataFrame = dao.get_range(2023, 11, 2024, 2)

    assert sorted(zip(result["year"], result["month"])) == [
        (2023, 11),
        (2023, 12),
        (2024, 1),
        (2024, 2),
    ]


def test_range_reversed_is_empty(dao: MonthlyRevenueDAO) -> None:
    """起點晚於終點時回空表"""

    assert dao.get_range(2024, 2, 2023, 11).empty


def test_api_range_keeps_public_argument_order(dao: MonthlyRevenueDAO) -> None:
    """API 維持 (start_year, end_year, start_month, end_month) 的參數順序"""

    from core.api.tw.monthly_revenue_report_api import MonthlyRevenueReportAPI

    api: MonthlyRevenueReportAPI = MonthlyRevenueReportAPI(conn=dao.conn)

    assert len(api.get_range(2023, 2024, 11, 2)) == 4
    assert len(api.get(2024, 1)) == 1


def test_latest_year_month_orders_numerically() -> None:
    """表不存在時為 None；年優先、月以數值排序（12 月大於 9 月）"""

    mrr_dao: MonthlyRevenueDAO = MonthlyRevenueDAO(conn=sqlite3.connect(":memory:"))
    assert mrr_dao.get_latest_year_month() is None

    mrr_dao.ensure_table(MRR_COLUMNS)
    assert mrr_dao.get_latest_year_month() is None

    mrr_dao.insert_or_ignore(
        make_mrr_rows([(2023, 12), (2024, 9), (2024, 12), (2024, 2)])
    )
    assert mrr_dao.get_latest_year_month() == (2024, 12)


# === updater 續跑起點 ===
def make_updater(conn: sqlite3.Connection) -> MonthlyRevenueReportUpdater:
    """跳過 `__init__`（正式連線與 log 設定），只注入 DAO"""

    updater: MonthlyRevenueReportUpdater = MonthlyRevenueReportUpdater.__new__(
        MonthlyRevenueReportUpdater
    )
    updater.dao = MonthlyRevenueDAO(conn=conn)
    updater.conn = conn
    return updater


def test_start_year_month_uses_default_when_table_is_empty() -> None:
    """表不存在或為空時從預設年月開始，不可先跳過一個月"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    updater: MonthlyRevenueReportUpdater = make_updater(conn)

    assert updater.get_actual_update_start_year_month(2013, 1) == (2013, 1)

    updater.dao.ensure_table(MRR_COLUMNS)
    assert updater.get_actual_update_start_year_month(2013, 1) == (2013, 1)


def test_start_year_month_rolls_over_december(dao: MonthlyRevenueDAO) -> None:
    """最新為 2024/12 時下一個是 2025/1"""

    dao.insert_or_ignore(make_mrr_rows([(2024, 12)]))
    updater: MonthlyRevenueReportUpdater = make_updater(dao.conn)

    assert updater.get_actual_update_start_year_month(2013, 1) == (2025, 1)


def test_start_year_month_query_error_is_raised() -> None:
    """查詢錯誤往外拋，不可當成「表是空的」從預設起點重跑整段"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    # 故意缺 month 欄
    conn.execute("CREATE TABLE monthly_revenue (year INT, stock_id TEXT)")
    updater: MonthlyRevenueReportUpdater = make_updater(conn)

    with pytest.raises(sqlite3.OperationalError):
        updater.get_actual_update_start_year_month(2013, 1)


# === loader ===
def make_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[MonthlyRevenueReportLoader, Path]:
    """建立指向暫存 DB 與暫存 downloads 的月營收 loader"""

    import core.pipeline.tw.loaders.monthly_revenue_report_loader as loader_module

    downloads: Path = tmp_path / "mrr"
    downloads.mkdir(exist_ok=True)
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        loader_module, "MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH", downloads
    )

    loader: MonthlyRevenueReportLoader = loader_module.MonthlyRevenueReportLoader()
    loader.mrr_dir = downloads
    return loader, downloads


def count_rows(tmp_path: Path) -> int:
    """暫存 DB 的月營收列數"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    count: int = conn.execute("SELECT COUNT(*) FROM monthly_revenue").fetchone()[0]
    conn.close()
    return count


def test_loader_skips_existing_and_in_file_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    重跑不重複寫入；同一檔內自己重複的列也不會讓整檔失敗

    舊版 `to_sql(append)` 遇到檔內重複列會撞主鍵、整檔進失敗清單。
    """

    loader, downloads = make_loader(tmp_path, monkeypatch)
    make_mrr_rows([(2024, 1), (2024, 1), (2024, 2)]).to_csv(
        downloads / "mrr_2024.csv", index=False
    )

    loader.add_to_db()
    assert count_rows(tmp_path) == 2

    loader2, _ = make_loader(tmp_path, monkeypatch)
    loader2.add_to_db()
    assert count_rows(tmp_path) == 2


def test_loader_failed_file_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫到一半出錯的檔案不可留下任何列，其他檔案照常入庫並拋 `DataLoadError`"""

    loader, downloads = make_loader(tmp_path, monkeypatch)
    make_mrr_rows([(2024, 1)]).to_csv(downloads / "mrr_202401.csv", index=False)
    make_mrr_rows([(2024, 2), (2024, 3)]).to_csv(
        downloads / "mrr_202402.csv", index=False
    )

    original_insert: Callable[..., int] = MonthlyRevenueDAO.insert_or_ignore

    def insert_then_fail(self: MonthlyRevenueDAO, df: pd.DataFrame) -> int:
        """照常寫入後，遇到 2 月的檔案就模擬寫到一半失敗"""

        result: int = original_insert(self, df)
        if (df["month"] == 2).any():
            raise OSError("disk I/O error")
        return result

    monkeypatch.setattr(MonthlyRevenueDAO, "insert_or_ignore", insert_then_fail)

    with pytest.raises(DataLoadError) as exc_info:
        loader.add_to_db()

    assert [Path(name).name for name in exc_info.value.failed_files] == [
        "mrr_202402.csv"
    ]
    assert count_rows(tmp_path) == 1


def test_updater_and_loader_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    import core.pipeline.tw.loaders.monthly_revenue_report_loader as loader_module
    import core.pipeline.tw.updaters.monthly_revenue_report_updater as updater_module

    monkeypatch.setattr(
        loader_module, "MONTHLY_REVENUE_REPORT_DOWNLOADS_PATH", tmp_path / "mrr"
    )
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))

    updater: MonthlyRevenueReportUpdater = updater_module.MonthlyRevenueReportUpdater()

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_rerun_summary_counts_existing_files_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    同一批 CSV 入庫兩次：第二次摘要為「新寫入 0 檔、已存在跳過 N 檔」

    舊版整檔已存在也計入「新寫入」——2026-09-16 實跑月營收 14 個 CSV 全部
    `all records already exist`，摘要卻是「新寫入 14 檔、已存在跳過 0 檔」。
    """

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda message: messages.append(message.record["message"]),
        filter=lambda record: "入庫完成" in record["message"],
    )
    try:
        loader, downloads = make_loader(tmp_path, monkeypatch)
        make_mrr_rows([(2024, 1)]).to_csv(downloads / "mrr_202401.csv", index=False)
        make_mrr_rows([(2024, 2)]).to_csv(downloads / "mrr_202402.csv", index=False)
        loader.add_to_db()

        rerun_loader, _ = make_loader(tmp_path, monkeypatch)
        rerun_loader.add_to_db()
    finally:
        logger.remove(sink_id)

    assert messages[0].endswith("新寫入 2 檔、已存在跳過 0 檔、失敗 0 檔")
    assert messages[1].endswith("新寫入 0 檔、已存在跳過 2 檔、失敗 0 檔")
