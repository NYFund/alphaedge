import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import FUTURES_PRICE_DAILY_TABLE_NAME
from core.pipeline.tw.loaders.futures_price_loader import FuturesPriceLoader

"""
台期貨行情入庫測試

以暫存 DB 與暫存 downloads 目錄驗證建表、NULL 保留與重跑冪等，
不連網路、不碰正式的 tw_futures.db。
"""


@pytest.fixture
def loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesPriceLoader:
    """入庫器 fixture，DB 與 downloads 目錄都改為暫存"""

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_price_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    futures_price_loader: FuturesPriceLoader = FuturesPriceLoader()
    futures_price_loader.futures_price_dir = tmp_path / "price"
    futures_price_loader.futures_price_dir.mkdir(parents=True, exist_ok=True)
    return futures_price_loader


def make_csv(directory: Path, name: str, rows: str) -> None:
    """寫出一份 cleaner 格式的 CSV"""

    header: str = (
        "date,product,expiry,session,開盤價,最高價,最低價,收盤價,"
        "成交量,結算價,未沖銷契約量,最後最佳買價,最後最佳賣價\n"
    )
    (directory / name).write_text(header + rows, encoding="utf-8")


DAY_ROW: str = (
    "2026-08-27,TX,202609,day,46175,46517,46006,46078,50701,46064,104881,46077,46088\n"
)
# 夜盤沒有結算價與未沖銷契約量，CSV 內為空欄
NIGHT_ROW: str = (
    "2026-08-27,TX,202609,night,46002,46142,45766,45993,26057,,,45983,45993\n"
)


def test_table_is_created(loader: FuturesPriceLoader) -> None:
    """setup() 應已建好資料表"""

    loader.connect()
    cols = loader.conn.execute(
        f"PRAGMA table_info('{FUTURES_PRICE_DAILY_TABLE_NAME}')"
    ).fetchall()

    assert [c[1] for c in cols] == [
        "date",
        "product",
        "expiry",
        "session",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "成交量",
        "結算價",
        "未沖銷契約量",
        "最後最佳買價",
        "最後最佳賣價",
    ]


def test_settlement_is_nullable(loader: FuturesPriceLoader) -> None:
    """
    結算價與未沖銷契約量必須允許 NULL

    宣告 NOT NULL 會逼 cleaner 填 0 才寫得進來，而結算價 0 會讓損益與
    維持率整段歸零且無任何徵兆。
    """

    loader.connect()
    cols = {
        c[1]: c[3]  # notnull flag
        for c in loader.conn.execute(
            f"PRAGMA table_info('{FUTURES_PRICE_DAILY_TABLE_NAME}')"
        )
    }

    assert cols["結算價"] == 0
    assert cols["未沖銷契約量"] == 0
    assert cols["開盤價"] == 0
    # 成交量沒成交就是 0 口，語意明確，維持 NOT NULL
    assert cols["成交量"] == 1


def test_night_row_keeps_null(loader: FuturesPriceLoader, tmp_path: Path) -> None:
    """夜盤的空欄入庫後必須是 NULL，不可變成 0"""

    make_csv(loader.futures_price_dir, "TX_night_20260827.csv", NIGHT_ROW)
    loader.add_to_db()

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    row = conn.execute(
        f"SELECT 結算價, 未沖銷契約量, 成交量 FROM {FUTURES_PRICE_DAILY_TABLE_NAME}"
    ).fetchone()

    assert row[0] is None
    assert row[1] is None
    assert row[2] == 26057


def test_day_and_night_coexist(loader: FuturesPriceLoader, tmp_path: Path) -> None:
    """同日同契約的日盤與夜盤是兩列，session 在主鍵內才不會互相覆蓋"""

    make_csv(loader.futures_price_dir, "TX_day_20260827.csv", DAY_ROW)
    make_csv(loader.futures_price_dir, "TX_night_20260827.csv", NIGHT_ROW)
    loader.add_to_db()

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    count = conn.execute(
        f"SELECT COUNT(*) FROM {FUTURES_PRICE_DAILY_TABLE_NAME}"
    ).fetchone()[0]

    assert count == 2


def test_reload_is_idempotent(loader: FuturesPriceLoader, tmp_path: Path) -> None:
    """重跑不得產生重複列——loader 每次都掃全目錄，重跑是常態不是例外"""

    make_csv(loader.futures_price_dir, "TX_day_20260827.csv", DAY_ROW)
    loader.add_to_db()
    loader.add_to_db()

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    count = conn.execute(
        f"SELECT COUNT(*) FROM {FUTURES_PRICE_DAILY_TABLE_NAME}"
    ).fetchone()[0]

    assert count == 1


def test_expiry_stays_string(loader: FuturesPriceLoader, tmp_path: Path) -> None:
    """
    週契約 202609W1 與月契約 202609 都要以字串入庫

    未指定 dtype 時 pandas 會把全數字的 expiry 讀成 202609.0，主鍵直接走樣。
    """

    rows: str = (
        "2026-08-27,MTX,202609W1,day,45900,45990,45850,45968,59,45968,69,45960,45975\n"
        "2026-08-27,MTX,202609,day,45880,46000,45800,45950,200,45950,500,45940,45960\n"
    )
    make_csv(loader.futures_price_dir, "MTX_day_20260827.csv", rows)
    loader.add_to_db()

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    expiries = [
        r[0]
        for r in conn.execute(
            f"SELECT expiry FROM {FUTURES_PRICE_DAILY_TABLE_NAME} ORDER BY expiry"
        )
    ]

    assert expiries == ["202609", "202609W1"]


def test_only_dates_filters_batch(loader: FuturesPriceLoader, tmp_path: Path) -> None:
    """分批入庫時只處理該批日期的檔案，不重掃整個目錄"""

    make_csv(
        loader.futures_price_dir,
        "TX_day_20260826.csv",
        DAY_ROW.replace("08-27", "08-26"),
    )
    make_csv(loader.futures_price_dir, "TX_day_20260827.csv", DAY_ROW)

    loader.add_to_db(only_dates={"20260827"})

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    dates = [
        r[0] for r in conn.execute(f"SELECT date FROM {FUTURES_PRICE_DAILY_TABLE_NAME}")
    ]

    assert dates == ["2026-08-27"]


def test_dataframe_columns_match_table(loader: FuturesPriceLoader) -> None:
    """cleaner 的輸出欄位須與資料表完全一致，否則 insert 會錯位"""

    from core.pipeline.tw.cleaners.futures_price_cleaner import FuturesPriceCleaner

    loader.connect()
    table_cols = [
        c[1]
        for c in loader.conn.execute(
            f"PRAGMA table_info('{FUTURES_PRICE_DAILY_TABLE_NAME}')"
        )
    ]

    assert FuturesPriceCleaner().futures_price_cleaned_cols == table_cols


def test_empty_dataframe_is_noop(loader: FuturesPriceLoader) -> None:
    """空 DataFrame 不應寫入任何列，也不應拋錯"""

    loader.connect()
    inserted, skipped = loader.dao.insert_or_ignore(pd.DataFrame())

    assert (inserted, skipped) == (0, 0)
