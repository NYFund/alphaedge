import datetime
import importlib
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, List, Type

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.pipeline.tw.loaders.stock_margin_loader import StockMarginLoader
from core.pipeline.utils.exceptions import DataLoadError

"""
`chip`／`margin` 表 DAO

1. DAO 本身：建表冪等、查詢區間邊界、表不存在的回傳約定
2. loader／updater 共用同一個 DAO：一次更新只開一條連線，入庫後不可把它關掉
3. 單檔失敗整檔回滾：寫到一半出錯的檔案不可留下任何列

不連網路、不碰正式的 `tw_stock.db`。
"""

MARGIN_COLUMNS: List[str] = [
    "date",
    "stock_id",
    "證券名稱",
    "融資買進",
    "融資賣出",
    "融資現金償還",
    "融資前日餘額",
    "融資今日餘額",
    "融資限額",
    "融券買進",
    "融券賣出",
    "融券現券償還",
    "融券前日餘額",
    "融券今日餘額",
    "融券限額",
    "資券互抵",
    "券資比",
    "註記",
]


def make_margin_rows(date: str, stock_ids: List[str], short: int = 5) -> pd.DataFrame:
    """建立多列最小可用的 margin 資料；融券今日餘額為 `short`"""

    return pd.DataFrame(
        [
            [date, sid, f"股票{sid}"] + [0] * 10 + [short, 0, 0, 0.0, ""]
            for sid in stock_ids
        ],
        columns=MARGIN_COLUMNS,
    )


@pytest.fixture
def margin_dao() -> StockMarginDAO:
    """已建表、內含兩天資料的記憶體 DAO"""

    dao: StockMarginDAO = StockMarginDAO(conn=sqlite3.connect(":memory:"))
    dao.ensure_table()
    dao.insert_or_ignore(make_margin_rows("2024-01-02", ["2330", "2317"], short=5))
    dao.insert_or_ignore(make_margin_rows("2024-01-03", ["2330"], short=7))
    dao.commit()
    return dao


# === DAO ===
@pytest.mark.parametrize("dao_cls", [StockChipDAO, StockMarginDAO])
def test_ensure_table_is_idempotent_and_creates_index(dao_cls: Type[BaseDAO]) -> None:
    """重複建表不報錯，且 `(stock_id, date)` 索引存在"""

    dao: BaseDAO = dao_cls(conn=sqlite3.connect(":memory:"))
    dao.ensure_table()
    dao.ensure_table()

    indexes: List[str] = [
        row[1]
        for row in dao.conn.execute(f"PRAGMA index_list('{dao.TABLE_NAME}')").fetchall()
    ]
    assert f"idx_{dao.TABLE_NAME}_stock_id_date" in indexes


@pytest.mark.parametrize("dao_cls", [StockChipDAO, StockMarginDAO])
def test_latest_date_is_none_without_table(dao_cls: Type[BaseDAO]) -> None:
    """表不存在或為空時回 None（初次更新的正常狀態）"""

    dao: BaseDAO = dao_cls(conn=sqlite3.connect(":memory:"))
    assert dao.get_latest_date() is None

    dao.ensure_table()
    assert dao.get_latest_date() is None


def test_margin_date_queries_include_both_ends(margin_dao: StockMarginDAO) -> None:
    """`BETWEEN` 兩端皆含；區間顛倒回空表"""

    start: datetime.date = datetime.date(2024, 1, 2)
    end: datetime.date = datetime.date(2024, 1, 3)

    assert len(margin_dao.get_by_date(start)) == 2
    assert len(margin_dao.get_range(start, end)) == 3
    assert len(margin_dao.get_by_stock("2330", start, end)) == 2
    assert margin_dao.get_range(end, start).empty
    assert margin_dao.get_latest_date() == "2024-01-03"


def test_margin_short_balance_queries(margin_dao: StockMarginDAO) -> None:
    """券源檢核用的兩個查詢：全市場欄位固定、個股查無資料回空表"""

    day: datetime.date = datetime.date(2024, 1, 3)

    assert list(margin_dao.get_short_balance(day).columns) == [
        "date",
        "stock_id",
        "證券名稱",
        "融券今日餘額",
        "融券限額",
        "券資比",
        "註記",
    ]
    assert margin_dao.get_stock_short_balance("2330", day).iloc[0, 0] == 7
    assert margin_dao.get_stock_short_balance("2317", day).empty


def test_margin_api_uses_the_shared_connection(margin_dao: StockMarginDAO) -> None:
    """API 以傳入的連線建 DAO，公開方法回傳值不變"""

    from core.api.tw.stock_margin_api import StockMarginAPI

    api: StockMarginAPI = StockMarginAPI(conn=margin_dao.conn)

    assert api.dao.conn is margin_dao.conn
    assert api.get_short_balance_map(datetime.date(2024, 1, 2)) == {
        "2330": 5,
        "2317": 5,
    }
    assert api.get_stock_short_balance("2330", datetime.date(2024, 1, 3)) == 7
    assert api.get_stock_short_balance("2317", datetime.date(2024, 1, 3)) is None


# === loader／updater 共用連線 ===
@pytest.mark.parametrize("kind", ["chip", "margin"])
def test_updater_and_loader_share_one_connection(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    loader_module: ModuleType = importlib.import_module(
        f"core.pipeline.tw.loaders.stock_{kind}_loader"
    )
    updater_module: ModuleType = importlib.import_module(
        f"core.pipeline.tw.updaters.stock_{kind}_updater"
    )

    monkeypatch.setattr(
        loader_module, f"{kind.upper()}_DOWNLOADS_PATH", tmp_path / kind
    )
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))

    # 參數化的 updater 類別只能以名稱取得，型別依 `kind` 而定
    updater: Any = getattr(updater_module, f"Stock{kind.capitalize()}Updater")()

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_margin_loader_keeps_shared_dao_open_after_load(tmp_path: Path) -> None:
    """共用 DAO 下入庫完成不可關連線：updater 接著還要用它查最新日期"""

    downloads: Path = tmp_path / "margin"
    downloads.mkdir()
    make_margin_rows("2024-01-02", ["2330"]).to_csv(
        downloads / "twse_20240102.csv", index=False
    )

    dao: StockMarginDAO = StockMarginDAO(db_path=tmp_path / "test.db")
    loader: StockMarginLoader = StockMarginLoader(dao=dao)
    loader.margin_dir = downloads

    loader.add_to_db()

    assert dao.conn is not None
    assert dao.get_latest_date() == "2024-01-02"
    dao.close()


def test_margin_failed_file_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    寫到一半出錯的檔案不可留下任何列，其他檔案照常入庫並拋 `DataLoadError`

    以替身模擬「寫進一部分之後才失敗」（例如磁碟 I/O 錯誤）：先真的寫入，再拋出。
    """

    import core.pipeline.tw.loaders.stock_margin_loader as loader_module

    downloads: Path = tmp_path / "margin"
    downloads.mkdir()
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "MARGIN_DOWNLOADS_PATH", downloads)
    make_margin_rows("2024-01-02", ["2330", "2317"]).to_csv(
        downloads / "twse_20240102.csv", index=False
    )
    make_margin_rows("2024-01-03", ["2330", "2317"]).to_csv(
        downloads / "twse_20240103.csv", index=False
    )

    original_insert: Callable[..., int] = StockMarginDAO.insert_or_ignore

    def insert_then_fail(self: StockMarginDAO, df: pd.DataFrame) -> int:
        """照常寫入後，遇到 2024-01-03 的檔案就模擬寫到一半失敗"""

        result: int = original_insert(self, df)
        if (df["date"] == "2024-01-03").any():
            raise OSError("disk I/O error")
        return result

    monkeypatch.setattr(StockMarginDAO, "insert_or_ignore", insert_then_fail)

    loader: StockMarginLoader = loader_module.StockMarginLoader()
    loader.margin_dir = downloads

    with pytest.raises(DataLoadError) as exc_info:
        loader.add_to_db()

    assert [Path(name).name for name in exc_info.value.failed_files] == [
        "twse_20240103.csv"
    ]

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dates: List[str] = [
        row[0] for row in conn.execute("SELECT DISTINCT date FROM margin").fetchall()
    ]
    conn.close()

    assert dates == ["2024-01-02"]
