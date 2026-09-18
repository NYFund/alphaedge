import sqlite3
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.tw.loaders.stock_chip_loader import StockChipLoader
from core.pipeline.tw.loaders.stock_margin_loader import StockMarginLoader
from core.pipeline.tw.loaders.stock_price_loader import StockPriceLoader
from core.pipeline.utils.exceptions import DataLoadError, SymbolNameConflictError

"""
證券代號的前導 0 不可在入庫途中消失

`0050` 變成 `50` **不會報錯，也不會少列**——兩個代號都查得到，只是查不到
同一檔。`pd.read_csv()` 只要看到某份檔案的代號全是數字就會推斷成整數，
而「全是數字」在早年的檔案裡很常見（實測 `chip` 在 2014-03-11 就有
`50`、`51`、`52`… 一整批，該日沒有 `0050`）。

`margin` 兩層早就有防護，price／chip 沒有，本檔把三者拉齊。

另一半是**入庫前的批次檢查**：型別防護只擋得住「這一次讀檔」，來源本身給錯、
或日後新增的讀檔路徑忘了指定型別，仍會讓 `006201` 變成 `6201`。事後稽核只對主鍵
含證券名稱的表有效（`price`、`chip`），`margin` 的冒名列會被 `INSERT OR IGNORE`
吞掉、表裡不留痕跡——入庫前檢查沒有這個分別，三張表一視同仁。

不連網路、不碰正式 DB。
"""


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """取得正式 schema 的欄位順序——缺欄位會被 `INSERT OR IGNORE` 靜靜擋掉"""

    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def write_csv(
    path: Path,
    columns: List[str],
    stock_ids: List[str],
    names: Optional[List[str]] = None,
) -> None:
    """寫一份「代號全是數字」的 CSV——這正是觸發整數推斷的條件

    `names` 用於製造「同一批裡一個代號兩個名稱」的假資料；未指定時一律填 `測試`。
    """

    names = names or ["測試"] * len(stock_ids)
    rows: List[List[object]] = [
        ["2024-03-11", stock_id, name] + [1] * (len(columns) - 3)
        for stock_id, name in zip(stock_ids, names)
    ]
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def test_price_loader_keeps_leading_zeros(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """price：全數字代號的 CSV 入庫後仍是 `0050`"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dao: StockPriceDAO = dao_factory(StockPriceDAO, records=[], conn=conn)
    write_csv(
        tmp_path / "twse_20240311.csv",
        table_columns(conn, "price"),
        ["0050", "0051"],
    )

    loader: StockPriceLoader = StockPriceLoader.__new__(StockPriceLoader)
    loader.dao = dao
    loader.owns_dao = False
    loader.conn = conn
    loader.price_dir = tmp_path
    loader.add_to_db(remove_files=False)

    assert sorted(
        row[0] for row in conn.execute("SELECT DISTINCT stock_id FROM price")
    ) == ["0050", "0051"]


def test_chip_loader_keeps_leading_zeros(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """chip：同上。實測 2014-03-11 就是被這條路徑寫成 `50`、`51` 的"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dao: StockChipDAO = dao_factory(StockChipDAO, records=[], conn=conn)
    write_csv(
        tmp_path / "twse_20240311.csv",
        table_columns(conn, "chip"),
        ["0050", "0051"],
    )

    loader: StockChipLoader = StockChipLoader.__new__(StockChipLoader)
    loader.dao = dao
    loader.owns_dao = False
    loader.conn = conn
    loader.chip_dir = tmp_path
    loader.add_to_db(remove_files=False)

    assert sorted(
        row[0] for row in conn.execute("SELECT DISTINCT stock_id FROM chip")
    ) == ["0050", "0051"]


@pytest.mark.parametrize("loader_cls", [StockPriceLoader, StockChipLoader])
def test_loaders_read_stock_id_as_text(loader_cls: type) -> None:
    """兩支 loader 都必須明示 `stock_id` 為字串，不依賴 pandas 的型別推斷"""

    source: str = Path(loader_cls.__module__.replace(".", "/") + ".py").read_text()

    assert 'dtype={"stock_id": str}' in source


# (loader 類別, DAO 類別, downloads 目錄屬性名, 資料表名)
LOADER_CASES: List[Tuple[type, type, str, str]] = [
    (StockPriceLoader, StockPriceDAO, "price_dir", "price"),
    (StockChipLoader, StockChipDAO, "chip_dir", "chip"),
    (StockMarginLoader, StockMarginDAO, "margin_dir", "margin"),
]


def build_loader(
    loader_cls: type, dao: BaseDAO, dir_attr: str, downloads: Path
) -> BaseDataLoader:
    """繞過 `__init__` 組出 loader，避免連上正式 DB 與 downloads 目錄"""

    loader: BaseDataLoader = loader_cls.__new__(loader_cls)
    loader.dao = dao
    loader.owns_dao = False
    loader.conn = dao.conn
    setattr(loader, dir_attr, downloads)
    return loader


@pytest.mark.parametrize(
    "loader_cls, dao_cls, dir_attr, table",
    LOADER_CASES,
    ids=[table for _, _, _, table in LOADER_CASES],
)
def test_loader_rejects_batch_with_two_names_for_one_symbol(
    loader_cls: type,
    dao_cls: type,
    dir_attr: str,
    table: str,
    tmp_path: Path,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """同一批裡 `6201` 同時是亞弘電與寶富櫃：整批失敗，一列都不准寫進去

    `006201`（元大富櫃50）少掉兩個 0 之後剛好是合法的上市代號 `6201`（亞弘電），
    代號長度與 `taiwan_stock_info` 都驗不出來；名稱是唯一驗得出來的東西。
    """

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dao: BaseDAO = dao_factory(dao_cls, records=[], conn=conn)
    write_csv(
        tmp_path / "twse_20240311.csv",
        table_columns(conn, table),
        ["6201", "6201", "2330"],
        names=["亞弘電", "寶富櫃", "台積電"],
    )

    loader: BaseDataLoader = build_loader(loader_cls, dao, dir_attr, tmp_path)

    with pytest.raises(DataLoadError) as exc_info:
        loader.add_to_db(remove_files=False)

    assert [Path(name).name for name in exc_info.value.failed_files] == [
        "twse_20240311.csv"
    ]
    # 半份也不能留：沒被冒名的 `2330` 同樣不入庫，整批下次執行重試
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "loader_cls, dao_cls, dir_attr, table",
    LOADER_CASES,
    ids=[table for _, _, _, table in LOADER_CASES],
)
def test_loader_accepts_normal_batch(
    loader_cls: type,
    dao_cls: type,
    dir_attr: str,
    table: str,
    tmp_path: Path,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """正常批次不受影響：不同代號各有各的名稱，照常入庫"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dao: BaseDAO = dao_factory(dao_cls, records=[], conn=conn)
    write_csv(
        tmp_path / "twse_20240311.csv",
        table_columns(conn, table),
        ["006201", "6201", "2330"],
        names=["元大富櫃50", "亞弘電", "台積電"],
    )

    loader: BaseDataLoader = build_loader(loader_cls, dao, dir_attr, tmp_path)
    loader.add_to_db(remove_files=False)

    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 3


def test_check_ignores_name_noise() -> None:
    """空白與全額交割註記只是寫法差異，不可讓整批失敗

    來源的名稱常帶全半形空白與 `*`（全額交割註記），兩張表各寫各的。
    這條若誤判，正常日子會整批退回、資料停止更新——比漏抓更糟。
    """

    df: pd.DataFrame = pd.DataFrame(
        {
            "stock_id": ["2330", "2330", "2330"],
            "證券名稱": ["台積電", "台積電 ", "台積電*"],
        }
    )

    BaseDataLoader.check_symbol_name_uniqueness(df, "twse_20240311.csv")


def test_check_ignores_missing_name() -> None:
    """名稱缺漏的列不參與比對：空字串不是「另一個名稱」"""

    df: pd.DataFrame = pd.DataFrame(
        {"stock_id": ["2330", "2330"], "證券名稱": ["台積電", None]}
    )

    BaseDataLoader.check_symbol_name_uniqueness(df, "twse_20240311.csv")


def test_check_reports_every_conflicting_symbol() -> None:
    """例外要帶出每個出問題的代號與其名稱，人才能直接去來源頁面對照"""

    df: pd.DataFrame = pd.DataFrame(
        {
            "stock_id": ["6201", "6201", "6202", "6202"],
            "證券名稱": ["亞弘電", "寶富櫃", "盛群", "富櫃200"],
        }
    )

    with pytest.raises(SymbolNameConflictError) as exc_info:
        BaseDataLoader.check_symbol_name_uniqueness(df, "twse_20240311.csv")

    assert exc_info.value.conflicts == {
        "6201": ["亞弘電", "寶富櫃"],
        "6202": ["富櫃200", "盛群"],
    }
