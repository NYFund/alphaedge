import sqlite3
from pathlib import Path
from typing import List, Tuple

import pandas as pd
import pytest

from scripts.manual import manual_fix_tpex_dealer_totals as fix

"""
上櫃中段自營商合計欄的一次性修正腳本

它會就地改正式庫，所以三件事一定要先驗過：只改中段且對不上的列、改之前留下
可還原的舊值、驗證不過就整批回滾。全部以記憶體資料庫驗，不碰 `tw_stock.db`。
"""

COLUMNS: List[str] = [
    "date",
    "stock_id",
    "自營商買進股數(自行買賣)",
    "自營商賣出股數(自行買賣)",
    "自營商買進股數(避險)",
    "自營商賣出股數(避險)",
    "自營商買進股數",
    "自營商賣出股數",
    "自營商買賣超股數",
]


def make_conn(rows: List[Tuple[object, ...]]) -> sqlite3.Connection:
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    quoted: str = ",".join(f'"{column}"' for column in COLUMNS)
    conn.execute(f"CREATE TABLE chip ({quoted})")
    conn.executemany(f"INSERT INTO chip VALUES ({','.join('?' * len(COLUMNS))})", rows)
    conn.commit()
    return conn


def totals(conn: sqlite3.Connection, date: str) -> Tuple[int, int]:
    return conn.execute(
        'SELECT "自營商買進股數", "自營商賣出股數" FROM chip WHERE date = ?', (date,)
    ).fetchone()


def test_only_broken_middle_rows_are_fixed_and_backed_up(tmp_path: Path) -> None:
    """中段對不上的列被補成拆分欄加總；中段以外的列不動；舊值留在 CSV"""

    conn: sqlite3.Connection = make_conn(
        [
            # 中段、合計為 0（要修）
            ("2016-06-01", "6488", 1500, 300, 200, 93, 0, 0, 1307),
            # 中段以外、合計為 0（來源本身的問題，不動）
            ("2025-03-03", "8481", 0, 0, 0, 0, 0, 0, -1000),
        ]
    )
    backup: Path = tmp_path / "backup.csv"

    fix.apply_fix(conn, backup)

    assert totals(conn, "2016-06-01") == (1700, 393)
    assert totals(conn, "2025-03-03") == (0, 0)
    saved: pd.DataFrame = pd.read_csv(backup, dtype={"stock_id": str})
    assert saved.to_dict("records") == [
        {"date": "2016-06-01", "stock_id": "6488", "buy_total": 0, "sell_total": 0}
    ]


def test_untrustworthy_split_columns_stop_the_fix(tmp_path: Path) -> None:
    """拆分欄算出的買賣超與庫內不符時，一列都不改、也不留備份檔"""

    conn: sqlite3.Connection = make_conn(
        [("2016-06-01", "6488", 1500, 300, 200, 93, 0, 0, 999)]
    )
    backup: Path = tmp_path / "backup.csv"

    with pytest.raises(RuntimeError, match="不可信"):
        fix.apply_fix(conn, backup)

    assert totals(conn, "2016-06-01") == (0, 0)
    assert not backup.exists()
