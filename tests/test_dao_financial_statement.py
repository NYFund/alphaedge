import sqlite3
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pandas as pd
import pytest

from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.dao.tw.stock_info_dao import StockInfoDAO
from core.pipeline.tw.loaders.financial_statement_loader import FinancialStatementLoader
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.utils.exceptions import DataLoadError

"""
財報四表與股票清單 DAO

1. 表名白名單：不在白名單內的表名在開連線之前就擋下
2. `get_range` 跨年區間：舊版年、季各自 `BETWEEN`，2023Q4～2024Q1 一筆都查不到
3. 股票清單與逐檔 resume：表不存在回空、其他查詢錯誤往外拋（舊版一律吞掉）
4. loader 逐檔 savepoint，與 updater 共用連線

不連網路、不碰正式的 `tw_stock.db`。
"""

BALANCE_SHEET_COLUMNS: List[str] = [
    "year",
    "season",
    "stock_id",
    "公司名稱",
    "資產總額",
]
EQUITY_CHANGE_COLUMNS: List[str] = [
    "year",
    "season",
    "stock_id",
    "權益項目",
    "變動原因",
    "金額",
]


def make_balance_rows(year_seasons: List[Tuple[int, int]]) -> pd.DataFrame:
    """建立指定年季、單一股票的資產負債表資料"""

    return pd.DataFrame(
        [[year, season, "2330", "台積電", 1.0] for year, season in year_seasons],
        columns=BALANCE_SHEET_COLUMNS,
    )


@pytest.fixture
def dao() -> FinancialStatementDAO:
    """已建表、內含 2023Q3～2024Q2 四季資料的記憶體 DAO"""

    fs_dao: FinancialStatementDAO = FinancialStatementDAO(
        "balance_sheet", conn=sqlite3.connect(":memory:")
    )
    fs_dao.ensure_table(BALANCE_SHEET_COLUMNS)
    fs_dao.insert_or_ignore(
        make_balance_rows([(2023, 3), (2023, 4), (2024, 1), (2024, 2)])
    )
    fs_dao.commit()
    return fs_dao


# === 白名單 ===
def test_unknown_table_is_rejected_before_connecting(tmp_path: Path) -> None:
    """不在白名單內的表名當場拋出，且不會先建出資料庫檔案"""

    db_path: Path = tmp_path / "should_not_exist.db"

    with pytest.raises(ValueError):
        FinancialStatementDAO("balance_sheet; DROP TABLE price", db_path=db_path)

    assert not db_path.exists()


def test_equity_change_primary_key_includes_flattened_dimensions() -> None:
    """權益變動表的主鍵含攤平出來的兩個維度，且不含公司名稱"""

    fs_dao: FinancialStatementDAO = FinancialStatementDAO(
        "equity_change", conn=sqlite3.connect(":memory:")
    )
    fs_dao.ensure_table(EQUITY_CHANGE_COLUMNS)
    fs_dao.ensure_table(EQUITY_CHANGE_COLUMNS)

    info: List[Tuple[Any, ...]] = fs_dao.conn.execute(
        "PRAGMA table_info('equity_change')"
    ).fetchall()
    primary_keys: List[str] = [
        row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]
    ]

    assert primary_keys == ["year", "season", "stock_id", "權益項目", "變動原因"]


# === 查詢 ===
def test_range_across_year_boundary(dao: FinancialStatementDAO) -> None:
    """2023Q4～2024Q1 應回 2 季，兩端皆含；區間顛倒回空表"""

    result: pd.DataFrame = dao.get_range(2023, 4, 2024, 1)

    assert sorted(zip(result["year"], result["season"])) == [(2023, 4), (2024, 1)]
    assert dao.get_range(2024, 1, 2023, 4).empty


def test_api_range_keeps_public_argument_order(dao: FinancialStatementDAO) -> None:
    """API 維持 (table_name, start_year, end_year, start_season, end_season) 的參數順序"""

    from core.api.tw.financial_statement_api import FinancialStatementAPI

    api: FinancialStatementAPI = FinancialStatementAPI(conn=dao.conn)

    assert len(api.get_range("balance_sheet", 2023, 2024, 4, 1)) == 2
    assert len(api.get("balance_sheet", 2024, 2)) == 1
    with pytest.raises(ValueError):
        api.get("price", 2024, 2)


def test_year_season_queries(dao: FinancialStatementDAO) -> None:
    """最新年季、已有年季與已入庫個股"""

    assert dao.get_latest_year_season() == (2024, 2)
    assert dao.get_existing_year_seasons() == {
        (2023, 3),
        (2023, 4),
        (2024, 1),
        (2024, 2),
    }
    assert dao.get_stock_ids(2024, 1) == {"2330"}
    assert dao.get_stock_ids(2022, 1) == set()


def test_missing_table_returns_empty() -> None:
    """表不存在是初次更新的正常狀態：一律回空，不拋錯"""

    fs_dao: FinancialStatementDAO = FinancialStatementDAO(
        "cash_flow", conn=sqlite3.connect(":memory:")
    )

    assert fs_dao.get_latest_year_season() is None
    assert fs_dao.get_existing_year_seasons() == set()
    assert fs_dao.get_stock_ids(2024, 1) == set()


def test_crawled_stock_ids_query_error_is_raised() -> None:
    """逐檔 resume 的查詢錯誤往外拋，不可當成「整季一檔都還沒爬」"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    # 故意缺 season 欄
    conn.execute("CREATE TABLE equity_change (year INT, stock_id TEXT)")

    with pytest.raises(pd.errors.DatabaseError):
        FinancialStatementDAO("equity_change", conn=conn).get_stock_ids(2024, 1)


# === 股票清單 ===
def test_stock_info_listed_common_stocks() -> None:
    """只取上市櫃、非 ETF 的四碼代號；表不存在回空清單"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    stock_info_dao: StockInfoDAO = StockInfoDAO(conn=conn)
    assert stock_info_dao.get_listed_common_stock_ids() == []
    assert stock_info_dao.get_stock_ids() == []

    pd.DataFrame(
        [
            {"stock_id": "2330", "type": "twse", "industry_category": "半導體業"},
            {"stock_id": "6488", "type": "tpex", "industry_category": "半導體業"},
            {"stock_id": "0050", "type": "twse", "industry_category": "ETF"},
            {"stock_id": "7777", "type": "emerging", "industry_category": "其他"},
            {"stock_id": "00631L", "type": "twse", "industry_category": "其他"},
        ]
    ).to_sql("taiwan_stock_info", conn, index=False)

    assert stock_info_dao.get_listed_common_stock_ids() == ["2330", "6488"]
    assert stock_info_dao.get_stock_ids() == ["0050", "00631L", "2330", "6488", "7777"]


def test_target_stock_ids_query_error_is_raised() -> None:
    """
    查股票清單失敗往外拋

    舊版 `except Exception: return []`，「DB 被鎖住、欄位打錯」會變成
    「沒有目標股票，略過」，整段權益變動表回補一檔都沒跑，行程照樣成功結束。
    """

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    # 故意缺 type 欄
    conn.execute("CREATE TABLE taiwan_stock_info (stock_id TEXT)")

    updater: FinancialStatementUpdater = FinancialStatementUpdater.__new__(
        FinancialStatementUpdater
    )
    updater.conn = conn

    # pandas 把 `sqlite3.OperationalError` 包成自己的 DatabaseError
    with pytest.raises(pd.errors.DatabaseError):
        updater.get_target_stock_ids()


# === loader／updater ===
def make_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[FinancialStatementLoader, Path]:
    """建立指向暫存 DB 與暫存 downloads 的財報 loader"""

    import core.pipeline.tw.loaders.financial_statement_loader as loader_module

    downloads: Path = tmp_path / "fs"
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "FINANCIAL_STATEMENT_DOWNLOADS_PATH", downloads)

    loader: FinancialStatementLoader = loader_module.FinancialStatementLoader()
    return loader, downloads / "balance_sheet"


def test_loader_failed_file_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫到一半出錯的檔案不可留下任何列，其他檔案照常入庫並拋 `DataLoadError`"""

    loader, balance_dir = make_loader(tmp_path, monkeypatch)
    make_balance_rows([(2024, 1)]).to_csv(balance_dir / "bs_2024Q1.csv", index=False)
    make_balance_rows([(2024, 2)]).to_csv(balance_dir / "bs_2024Q2.csv", index=False)

    original_insert: Callable[..., int] = FinancialStatementDAO.insert_or_ignore

    def insert_then_fail(self: FinancialStatementDAO, df: pd.DataFrame) -> int:
        """照常寫入後，遇到第 2 季的檔案就模擬寫到一半失敗"""

        result: int = original_insert(self, df)
        if (df["season"] == 2).any():
            raise OSError("disk I/O error")
        return result

    monkeypatch.setattr(FinancialStatementDAO, "insert_or_ignore", insert_then_fail)

    with pytest.raises(DataLoadError) as exc_info:
        loader.add_to_db(dir_path=balance_dir, table_name="balance_sheet")

    assert [Path(name).name for name in exc_info.value.failed_files] == [
        "bs_2024Q2.csv"
    ]
    assert loader.conn is None

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    rows: List[Tuple[Any, ...]] = conn.execute(
        "SELECT year, season FROM balance_sheet"
    ).fetchall()
    conn.close()

    assert rows == [(2024, 1)]


def test_updater_and_loader_share_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 與其 loader 必須是同一條連線；loader 入庫後不可關掉它，`close()` 才關"""

    import core.pipeline.tw.loaders.financial_statement_loader as loader_module
    import core.pipeline.tw.updaters.financial_statement_updater as updater_module

    downloads: Path = tmp_path / "fs"
    monkeypatch.setattr(loader_module, "FINANCIAL_STATEMENT_DOWNLOADS_PATH", downloads)
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))

    updater: FinancialStatementUpdater = updater_module.FinancialStatementUpdater()
    assert updater.loader.conn is updater.conn

    make_balance_rows([(2024, 1)]).to_csv(
        downloads / "balance_sheet" / "bs_2024Q1.csv", index=False
    )
    updater.loader.add_to_db(
        dir_path=downloads / "balance_sheet", table_name="balance_sheet"
    )

    assert updater.loader.conn is updater.conn
    assert updater.get_existing_year_seasons("balance_sheet") == {(2024, 1)}

    updater.close()
    assert updater.conn is None
