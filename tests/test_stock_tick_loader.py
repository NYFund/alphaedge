import datetime
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import pandas as pd
import pytest

from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.tw.loaders.stock_tick_loader import (
    EXCLUDE_NEGATIVE_VOLUME,
    EXCLUDE_NOT_LISTED,
    EXCLUDE_ZERO_CLOSE,
    TICK_COLUMNS,
    StockTickLoader,
    add_trade_date_and_seq,
    filter_tick_rows,
    normalize_tick_frame,
)
from core.pipeline.utils.exceptions import DataLoadError

"""
台股 tick loader 的正規化、排除規則與逐檔流程（不需要 TimescaleDB）

寫入端以替身 DAO 記錄 `replace_day()` 的呼叫；真的寫進 TimescaleDB 的行為由
`tests/test_stock_tick_timescale.py` 驗證（需要資料庫）。
"""

CLEANER_HEADER: str = (
    "stock_id,time,close,volume,bid_price,bid_volume,ask_price,ask_volume,tick_type"
)
# 歷史格式：`ts`、沒有 stock_id、欄序打亂
HISTORY_HEADER: str = (
    "volume,bid_volume,ask_price,tick_type,bid_price,ts,close,ask_volume"
)


def _raw(header: str, rows: List[str]) -> pd.DataFrame:
    """把 CSV 文字列組成和 `pd.read_csv(dtype=str)` 一樣的原始表"""

    return pd.DataFrame(
        [row.split(",") for row in rows], columns=header.split(","), dtype=str
    )


# === 正規化 ===
def test_history_and_cleaner_formats_normalize_to_same_frame() -> None:
    """
    歷史格式（欄序打亂、無 stock_id、`ts`、整數寫成 `105.0`、毫秒時間）與 cleaner 格式
    正規化後必須完全相同
    """

    cleaner: pd.DataFrame = _raw(
        CLEANER_HEADER,
        [
            "0050,2024-05-08 09:00:01.123000,150.5,105,150.45,3,150.5,7,1",
            "0050,2024-05-08 09:00:02.000000,150.45,2,150.45,1,150.5,6,2",
        ],
    )
    history: pd.DataFrame = _raw(
        HISTORY_HEADER,
        [
            "105.0,3.0,150.5,1.0,150.45,2024-05-08 09:00:01.123,150.5,7.0",
            "2,1,150.5,2,150.45,2024-05-08 09:00:02.000000,150.45,6",
        ],
    )

    from_cleaner: pd.DataFrame = normalize_tick_frame(cleaner, "0050")
    from_history: pd.DataFrame = normalize_tick_frame(history, "0050")

    pd.testing.assert_frame_equal(from_cleaner, from_history)
    assert list(from_history.columns) == list(TICK_COLUMNS)
    # 代號的前導 0 不可被吃掉
    assert from_history["stock_id"].tolist() == ["0050", "0050"]
    assert from_history["time"].iloc[0] == pd.Timestamp("2024-05-08 09:00:01.123")
    assert from_history["volume"].dtype == "int64"


def test_float_noise_in_prices_is_rounded() -> None:
    """`6.5600000000000005` 這類浮點誤差四捨五入到 2 位小數"""

    raw: pd.DataFrame = _raw(
        CLEANER_HEADER,
        ["1108,2020-04-06 09:44:21.417117,6.5600000000000005,1,6.55,13,6.6,5,1"],
    )

    assert normalize_tick_frame(raw, "1108")["close"].iloc[0] == 6.56


@pytest.mark.parametrize(
    ("column", "value"),
    [("volume", "1.5"), ("volume", ""), ("tick_type", "abc"), ("time", "not-a-time")],
)
def test_unconvertible_cell_fails_whole_file(column: str, value: str) -> None:
    """轉不過去的格子讓整檔失敗，不默默丟列（丟了 `source_rows` 就對不上）"""

    raw: pd.DataFrame = _raw(
        CLEANER_HEADER, ["2330,2024-05-08 09:00:01.000000,800,1,799,2,800,3,1"]
    )
    raw.loc[0, column] = value

    with pytest.raises(ValueError):
        normalize_tick_frame(raw, "2330")


def test_missing_column_fails() -> None:
    """缺欄整檔失敗，訊息要列出缺哪一欄"""

    raw: pd.DataFrame = _raw(
        "ts,close,volume,bid_price,bid_volume,ask_price,ask_volume",
        ["2024-05-08 09:00:01.000000,800,1,799,2,800,3"],
    )

    with pytest.raises(ValueError, match="tick_type"):
        normalize_tick_frame(raw, "2330")


def test_seq_follows_original_row_order_per_day() -> None:
    """
    `seq` 是每天各自從 0 起算的原始列序，不是依時間排序後的名次

    歷史檔同一天內的列不保證依時間排序（盤後 14:30 的列可能排在 13:30 前面）。
    """

    raw: pd.DataFrame = _raw(
        CLEANER_HEADER,
        [
            "2330,2024-05-08 14:30:00.000000,800,1,799,2,800,3,1",
            "2330,2024-05-08 13:30:00.000000,800,1,799,2,800,3,1",
            "2330,2024-05-09 09:00:01.000000,801,1,800,2,801,3,1",
        ],
    )

    df: pd.DataFrame = add_trade_date_and_seq(normalize_tick_frame(raw, "2330"))

    assert df["seq"].tolist() == [0, 1, 0]
    assert df["trade_date"].tolist() == [
        datetime.date(2024, 5, 8),
        datetime.date(2024, 5, 8),
        datetime.date(2024, 5, 9),
    ]


# === 排除規則 ===
def _frame(rows: List[Tuple[str, float, int]]) -> pd.DataFrame:
    """以 (時間, close, volume) 組出已補 `trade_date`／`seq` 的單一股票資料"""

    raw: pd.DataFrame = _raw(
        CLEANER_HEADER,
        [f"2330,{t},{close},{volume},1,1,1,1,1" for t, close, volume in rows],
    )
    return add_trade_date_and_seq(normalize_tick_frame(raw, "2330"))


D1: datetime.date = datetime.date(2024, 5, 8)
D2: datetime.date = datetime.date(2024, 5, 9)
D3: datetime.date = datetime.date(2024, 5, 10)


def test_filter_excludes_bad_rows_and_not_listed_days() -> None:
    """全零列、負量列逐列排除；`price` 表當天沒有這檔的日子整天排除"""

    df: pd.DataFrame = _frame(
        [
            ("2024-05-08 09:00:01.000000", 800, 1),
            ("2024-05-08 09:00:02.000000", 0, 0),  # 全零列
            ("2024-05-08 09:00:03.000000", 800, -19),  # 負量
            ("2024-05-09 09:00:01.000000", 800, 1000),  # 興櫃日
            ("2024-05-09 09:00:02.000000", 0, 0),  # 興櫃日的壞列只算興櫃
        ]
    )

    kept, stats, failed = filter_tick_rows(df, listed_days={D1}, trading_days={D1, D2})

    assert kept["seq"].tolist() == [0]
    assert stats == {
        EXCLUDE_NOT_LISTED: 2,
        EXCLUDE_ZERO_CLOSE: 1,
        EXCLUDE_NEGATIVE_VOLUME: 1,
    }
    assert failed == []
    # 每一列只算一條規則：排除總數＋留下＝原始列數
    assert sum(stats.values()) + len(kept) == len(df)


def test_day_missing_from_price_calendar_fails_instead_of_excluded() -> None:
    """
    `price` 表整天沒資料的日子是失敗而不是興櫃

    日 K 還沒更新時若當成興櫃排除並登記為已載入，這一天之後就再也不會補。
    """

    df: pd.DataFrame = _frame(
        [
            ("2024-05-08 09:00:01.000000", 800, 1),
            ("2024-05-10 09:00:01.000000", 801, 1),
        ]
    )

    kept, stats, failed = filter_tick_rows(df, listed_days={D1}, trading_days={D1})

    assert failed == [D3]
    assert kept["trade_date"].tolist() == [D1]
    assert stats[EXCLUDE_NOT_LISTED] == 0


# === 逐檔流程（替身 DAO） ===
class FakeTickDAO:
    """記錄 `replace_day()` 呼叫的 tick DAO 替身"""

    def __init__(self) -> None:
        """建立替身"""

        self.conn: None = None
        self.days: Dict[Tuple[str, datetime.date], Tuple[int, int, str]] = {}
        self.create_calls: int = 0

    def create_tables(self) -> None:
        """記錄建表次數"""

        self.create_calls += 1

    def replace_day(
        self,
        stock_id: str,
        trade_date: datetime.date,
        day_df: pd.DataFrame,
        source_rows: int,
        source_file: str,
    ) -> int:
        """以 (股票, 交易日) 為鍵覆寫：(寫入列數, 來源列數, 來源檔名)"""

        self.days[(stock_id, trade_date)] = (len(day_df), source_rows, source_file)
        return len(day_df)

    def close(self) -> None:
        """替身不持有連線"""


@pytest.fixture
def price_dao() -> Iterator[StockPriceDAO]:
    """`price` 表只有 2330 在 5/8、5/9 有日 K、另一檔讓 5/9 成為交易日"""

    dao: StockPriceDAO = StockPriceDAO(conn=sqlite3.connect(":memory:"))
    dao.create_table()
    dao.conn.executemany(
        'INSERT INTO price ("date", stock_id, "證券名稱") VALUES (?, ?, ?)',
        [("2024-05-08", "2330", "台積電"), ("2024-05-09", "1101", "台泥")],
    )
    yield dao
    dao.conn.close()


def _write_csv(directory: Path, name: str, header: str, rows: List[str]) -> Path:
    """寫一個 tick CSV"""

    path: Path = directory / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_load_csv_records_every_source_day(
    tmp_path: Path, price_dao: StockPriceDAO
) -> None:
    """
    整天被排除的日子也要登記（`row_count = 0`），`source_rows` 是排除前的列數

    不登記的話續跑會以為那天沒載過，每次都重做。
    """

    tick_dao: FakeTickDAO = FakeTickDAO()
    loader: StockTickLoader = StockTickLoader(dao=tick_dao, price_dao=price_dao)
    csv_path: Path = _write_csv(
        tmp_path,
        "2330.csv",
        HISTORY_HEADER,
        [
            "1,1,800,1,799,2024-05-08 09:00:01.000000,800,1",
            "0,0,0,0,0,2024-05-08 09:00:02.000000,0,0",
            "1000,1,800,1,799,2024-05-09 09:00:01.000,800,1",
        ],
    )

    written: int = loader.load_csv(csv_path)

    assert written == 1
    assert tick_dao.days == {
        ("2330", D1): (1, 2, "2330.csv"),
        ("2330", D2): (0, 1, "2330.csv"),
    }


def test_add_to_db_isolates_bad_file_and_reports(
    tmp_path: Path, price_dao: StockPriceDAO
) -> None:
    """壞檔不擋其他檔，全部跑完才以 `DataLoadError` 列出失敗檔名"""

    tick_dao: FakeTickDAO = FakeTickDAO()
    loader: StockTickLoader = StockTickLoader(dao=tick_dao, price_dao=price_dao)
    _write_csv(
        tmp_path,
        "1101.csv",
        HISTORY_HEADER,
        ["1,1,800,1,799,2024-05-08 09:00:01.000000,800,abc"],
    )
    _write_csv(
        tmp_path,
        "2330.csv",
        HISTORY_HEADER,
        ["1,1,800,1,799,2024-05-08 09:00:01.000000,800,1"],
    )

    with pytest.raises(DataLoadError) as excinfo:
        loader.add_to_db(dir_path=tmp_path)

    assert excinfo.value.failed_files == ["1101.csv"]
    assert ("2330", D1) in tick_dao.days
    assert ("1101", D1) not in tick_dao.days


def test_day_missing_from_price_is_not_logged_and_file_fails(
    tmp_path: Path, price_dao: StockPriceDAO
) -> None:
    """日 K 沒更新的那天不寫、不登記，其他天照寫，該檔列為失敗"""

    tick_dao: FakeTickDAO = FakeTickDAO()
    loader: StockTickLoader = StockTickLoader(dao=tick_dao, price_dao=price_dao)
    _write_csv(
        tmp_path,
        "2330.csv",
        HISTORY_HEADER,
        [
            "1,1,800,1,799,2024-05-08 09:00:01.000000,800,1",
            "1,1,800,1,799,2024-05-10 09:00:01.000000,800,1",
        ],
    )

    with pytest.raises(DataLoadError):
        loader.add_to_db(dir_path=tmp_path)

    assert set(tick_dao.days) == {("2330", D1)}


def test_remove_files_refuses_explicit_directory(
    tmp_path: Path, price_dao: StockPriceDAO
) -> None:
    """指定資料夾時不可要求刪檔：歷史存檔是唯一的原始資料"""

    loader: StockTickLoader = StockTickLoader(dao=FakeTickDAO(), price_dao=price_dao)
    csv_path: Path = _write_csv(
        tmp_path,
        "2330.csv",
        HISTORY_HEADER,
        ["1,1,800,1,799,2024-05-08 09:00:01.000000,800,1"],
    )

    with pytest.raises(ValueError, match="remove_files"):
        loader.add_to_db(remove_files=True, dir_path=tmp_path)

    assert csv_path.exists()


def test_shared_daos_are_not_closed_by_loader(price_dao: StockPriceDAO) -> None:
    """傳入的 DAO 由建立者負責關閉，loader 的 `disconnect()` 不可關掉它們"""

    loader: StockTickLoader = StockTickLoader(dao=FakeTickDAO(), price_dao=price_dao)
    loader.disconnect()

    listed: Set[datetime.date] = price_dao.get_stock_trading_days("2330", D1, D3)
    assert listed == {D1}
