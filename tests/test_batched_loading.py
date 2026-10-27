import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pytest

from core.pipeline.shared.base_loader import BaseDataLoader

"""分批入庫：長時間回補中斷時，只損失最後一批而非全部

原本三個高風險 updater（price／chip／margin）都是「整段日期全部爬完才一次
`add_to_db()`」。2013 起的回補有 3,300 個交易日、數小時，中途失敗等於前功盡棄
——實際發生過一次（margin 回補中斷後 DB 仍為 0 列）。
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


def write_margin_csv(directory: Path, exchange: str, date: str) -> Path:
    """在 downloads 目錄寫一個單列的 margin CSV，檔名比照實際慣例"""

    path: Path = directory / f"{exchange}_{date}.csv"
    pd.DataFrame(
        [
            [f"{date[:4]}-{date[4:6]}-{date[6:]}", "2330", "台積電"]
            + [0] * 12
            + [0, 0.0, ""]
        ],
        columns=MARGIN_COLUMNS,
    ).to_csv(path, index=False)
    return path


# === 檔案挑選 ===
def test_select_csv_files_without_filter_takes_everything(tmp_path: Path) -> None:
    """未指定日期時取整個目錄，維持既有行為"""

    write_margin_csv(tmp_path, "twse", "20240102")
    write_margin_csv(tmp_path, "tpex", "20240102")
    (tmp_path / "not_a_csv.txt").write_text("x")

    files: List[Path] = BaseDataLoader.select_csv_files(tmp_path)

    assert len(files) == 2
    assert all(path.suffix == ".csv" for path in files)


def test_select_csv_files_filters_by_date(tmp_path: Path) -> None:
    """指定日期時只取該批的檔案——分批入庫若每批全掃，回補會退化成 N×M 次讀取"""

    for date in ("20240102", "20240103", "20240104"):
        write_margin_csv(tmp_path, "twse", date)
        write_margin_csv(tmp_path, "tpex", date)

    files: List[Path] = BaseDataLoader.select_csv_files(
        tmp_path, only_dates={"20240103"}
    )

    assert len(files) == 2, "同一天的兩個市場都要選到"
    assert {path.stem for path in files} == {"twse_20240103", "tpex_20240103"}


def test_select_csv_files_ignores_unknown_dates(tmp_path: Path) -> None:
    """指定了目錄裡沒有的日期時回傳空清單，不得誤選"""

    write_margin_csv(tmp_path, "twse", "20240102")

    assert BaseDataLoader.select_csv_files(tmp_path, only_dates={"20991231"}) == []


# === 分批入庫的中斷行為 ===
def make_margin_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """建立指向暫存 DB 與暫存 downloads 的 margin loader"""

    import core.pipeline.tw.loaders.stock_margin_loader as loader_module

    downloads: Path = tmp_path / "margin"
    downloads.mkdir(exist_ok=True)
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "MARGIN_DOWNLOADS_PATH", downloads)

    loader = loader_module.StockMarginLoader()
    loader.margin_dir = downloads
    return loader, downloads


def test_batch_load_only_touches_its_own_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一批入庫不得順手載入其他批次的檔案"""

    loader, downloads = make_margin_loader(tmp_path, monkeypatch)
    for date in ("20240102", "20240103", "20240104"):
        write_margin_csv(downloads, "twse", date)

    loader.add_to_db(only_dates={"20240102", "20240103"})

    conn = sqlite3.connect(tmp_path / "test.db")
    dates = {row[0] for row in conn.execute("select distinct date from margin")}
    conn.close()

    assert dates == {"2024-01-02", "2024-01-03"}, "第三天不該被載入"


def test_interruption_keeps_earlier_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    這是分批入庫存在的理由：中斷後，先前批次的資料仍在資料庫裡

    模擬回補跑到第二批時失敗——第一批必須已經落地，重跑才能接續而非從頭來過。
    """

    loader, downloads = make_margin_loader(tmp_path, monkeypatch)
    batch_one: List[str] = ["20240102", "20240103"]
    batch_two: List[str] = ["20240104", "20240105"]
    for date in batch_one + batch_two:
        write_margin_csv(downloads, "twse", date)

    # 第一批正常入庫
    loader.add_to_db(only_dates=set(batch_one))

    # 第二批爬到一半中斷（模擬 kill，第二批完全沒有入庫）
    conn = sqlite3.connect(tmp_path / "test.db")
    loaded = {row[0] for row in conn.execute("select distinct date from margin")}
    conn.close()

    assert loaded == {"2024-01-02", "2024-01-03"}
    assert "2024-01-04" not in loaded

    # 重跑：第一批已存在會被跳過，第二批補上
    loader2, _ = make_margin_loader(tmp_path, monkeypatch)
    loader2.add_to_db(only_dates=set(batch_one + batch_two))

    conn = sqlite3.connect(tmp_path / "test.db")
    final = {row[0] for row in conn.execute("select distinct date from margin")}
    total: int = conn.execute("select count(*) from margin").fetchone()[0]
    conn.close()

    assert len(final) == 4
    assert total == 4, "重跑不得產生重複列"


def test_updater_load_batch_passes_only_its_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updater 的 load_batch() 必須把本批日期往下傳，而不是讓 loader 全掃"""

    from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater

    received: List[Optional[set]] = []

    class SpyLoader:
        """只記錄收到的 `only_dates`，不真的入庫"""

        def add_to_db(self, remove_files: bool = False, only_dates=None) -> None:
            received.append(only_dates)

    updater = StockMarginUpdater.__new__(StockMarginUpdater)  # 跳過 __init__ 的連線
    updater.loader = SpyLoader()

    updater.load_batch(["20240102", "20240103"])

    assert received == [{"20240102", "20240103"}]


def test_batch_size_is_bounded() -> None:
    """批量必須是有限值，否則等同回到「整段跑完才入庫」"""

    from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
    from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
    from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater

    for updater_cls in (StockPriceUpdater, StockChipUpdater, StockMarginUpdater):
        size: int = updater_cls.LOAD_BATCH_SIZE
        assert 0 < size <= 500, f"{updater_cls.__name__} 的批量不合理：{size}"


def test_all_three_updaters_expose_load_batch() -> None:
    """三個高風險 updater 都要有分批入庫，不可只改其中一個"""

    from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
    from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
    from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater

    for updater_cls in (StockPriceUpdater, StockChipUpdater, StockMarginUpdater):
        assert hasattr(updater_cls, "load_batch"), updater_cls.__name__


def test_date_string_format_matches_filenames() -> None:
    """updater 產生的日期字串必須與 downloads 的檔名後綴一致，否則會選不到檔案"""

    date: datetime.date = datetime.date(2024, 1, 2)

    assert date.strftime("%Y%m%d") == "20240102"
    assert Path("twse_20240102.csv").stem.split("_")[-1] == "20240102"
