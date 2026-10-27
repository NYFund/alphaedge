import datetime
from pathlib import Path
from typing import List

import pandas as pd

from scripts.manual import manual_tick_history_import as importer

"""
歷史匯入腳本不需要資料庫的部分：chunk 邊界與依 chunk 切檔

寫入、壓縮與完整性比對的端到端測試在 `tests/test_stock_tick_timescale.py`（需要 TimescaleDB）。
"""

# 歷史格式：`ts`、沒有 stock_id、欄序打亂
HEADER: str = "volume,bid_volume,ask_price,tick_type,bid_price,ts,close,ask_volume"


def _write(path: Path, times: List[str]) -> None:
    """以給定時間寫一個歷史格式 CSV，close 依序遞增方便核對列序"""

    rows: List[str] = [f"1,1,800,1,799,{t},{800 + i},1" for i, t in enumerate(times)]
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")


def test_chunk_start_aligns_to_timescale_chunks() -> None:
    """
    chunk 從 1970-01-01（週四）起算、每 7 天一個

    實際寫入 TimescaleDB 時，2024-05-08 落在 05-02～05-09、05-10 落在 05-09～05-16。
    """

    assert importer.chunk_start(datetime.date(2024, 5, 8)) == datetime.date(2024, 5, 2)
    assert importer.chunk_start(datetime.date(2024, 5, 9)) == datetime.date(2024, 5, 9)
    assert importer.chunk_start(datetime.date(2024, 5, 15)) == datetime.date(2024, 5, 9)
    assert importer.chunk_end(datetime.date(2024, 5, 2)) == datetime.date(2024, 5, 9)


def test_split_by_chunk_keeps_rows_and_counts(tmp_path: Path) -> None:
    """
    切檔依 chunk 分目錄、保留原欄序與列序，並記下每個「股票 × 交易日」的來源列數；
    區間外的列不切
    """

    source: Path = tmp_path / "source"
    source.mkdir()
    _write(
        source / "2330.csv",
        [
            "2024-05-07 09:00:01.000000",  # 區間外
            "2024-05-08 13:30:00.000000",
            "2024-05-08 09:00:01.000",  # 同一天內時間倒退、毫秒格式，都要原樣保留
            "2024-05-10 09:00:01.000000",
        ],
    )
    _write(source / "1101.csv", ["2024-05-09 09:00:01.000000"])
    work: Path = tmp_path / "work"

    counts: pd.DataFrame = importer.split_by_chunk(
        source, work, datetime.date(2024, 5, 8), datetime.date(2024, 5, 10)
    )

    first: pd.DataFrame = pd.read_csv(work / "2024-05-02" / "2330.csv", dtype=str)
    assert list(first.columns) == HEADER.split(",")
    assert first["ts"].tolist() == [
        "2024-05-08 13:30:00.000000",
        "2024-05-08 09:00:01.000",
    ]
    assert sorted(p.name for p in (work / "2024-05-09").glob("*.csv")) == [
        "1101.csv",
        "2330.csv",
    ]
    assert sorted(
        counts.astype({"trade_date": str}).itertuples(index=False, name=None)
    ) == [
        ("1101", "2024-05-09", 1),
        ("2330", "2024-05-08", 2),
        ("2330", "2024-05-10", 1),
    ]


def test_split_is_reused_only_with_same_arguments(tmp_path: Path) -> None:
    """切檔完成後同樣參數沿用；區間不同就重切（回 None）"""

    source: Path = tmp_path / "source"
    source.mkdir()
    _write(source / "2330.csv", ["2024-05-08 09:00:01.000000"])
    work: Path = tmp_path / "work"
    start: datetime.date = datetime.date(2024, 5, 8)
    importer.split_by_chunk(source, work, start, None)

    reused = importer.load_split_if_reusable(work, source, start, None)
    assert reused is not None and reused["stock_id"].tolist() == ["2330"]
    assert importer.load_split_if_reusable(work, source, None, None) is None


def test_interrupted_split_is_not_reused(tmp_path: Path) -> None:
    """沒有完成標記（切到一半中斷）就不沿用，避免拿半套的切檔匯入"""

    work: Path = tmp_path / "work"
    (work / "2024-05-02").mkdir(parents=True)

    assert importer.load_split_if_reusable(work, tmp_path, None, None) is None
