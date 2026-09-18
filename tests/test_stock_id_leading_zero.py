import sqlite3
from pathlib import Path
from typing import Callable, List

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.tw.loaders.stock_chip_loader import StockChipLoader
from core.pipeline.tw.loaders.stock_price_loader import StockPriceLoader

"""
證券代號的前導 0 不可在入庫途中消失

`0050` 變成 `50` **不會報錯，也不會少列**——兩個代號都查得到，只是查不到
同一檔。`pd.read_csv()` 只要看到某份檔案的代號全是數字就會推斷成整數，
而「全是數字」在早年的檔案裡很常見（實測 `chip` 在 2014-03-11 就有
`50`、`51`、`52`… 一整批，該日沒有 `0050`）。

`margin` 兩層早就有防護，price／chip 沒有，本檔把三者拉齊。
不連網路、不碰正式 DB。
"""


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """取得正式 schema 的欄位順序——缺欄位會被 `INSERT OR IGNORE` 靜靜擋掉"""

    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def write_csv(path: Path, columns: List[str], stock_ids: List[str]) -> None:
    """寫一份「代號全是數字」的 CSV——這正是觸發整數推斷的條件"""

    rows: List[List[object]] = [
        ["2024-03-11", stock_id, "測試"] + [1] * (len(columns) - 3)
        for stock_id in stock_ids
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
