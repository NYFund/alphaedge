import datetime
import sqlite3
from pathlib import Path
from typing import Dict, Optional

import pytest

from core.api.tw.futures_chip_api import FuturesChipAPI
from core.api.tw.futures_margin_api import FuturesMarginAPI
from core.api.tw.futures_stock_universe_api import FuturesStockUniverseAPI
from core.config import (
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_MARGIN_HISTORY_TABLE_NAME,
    FUTURES_STOCK_UNIVERSE_TABLE_NAME,
    STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
)
from core.dao.base import table_exists
from core.pipeline.tw.loaders.futures_chip_loader import FuturesChipLoader
from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

"""
「表還沒建」與「查詢失敗」必須分得開（健檢第四輪 S1，與 F-056 同型）

期貨線原本有 8 處寫成 `except sqlite3.OperationalError: return None`，
於是三種完全不同的狀況長得一模一樣：

1. 尚未跑過該資料集的 ETL（**正常**，該回 `None`／`0`）
2. 資料庫被別的連線鎖住（**不正常**，背景 ETL 正在寫同一個檔）
3. schema 壞掉、欄名打錯（**不正常**）

第 2、3 種被吞掉的代價是靜默的：updater 會從 `DEFAULT_START_DATE` 整段重爬、
入庫統計會印出負數列數、回測則是拿到 `None` 後少開幾筆倉而完全不報錯。

本檔以「表不存在」與「被 EXCLUSIVE 鎖住」兩種情境各驗一次：前者維持回 `None`，
後者一律上拋。不連網路、不碰正式的 `tw_futures.db`。
"""


def _make_locked_db(db_path: Path, table_name: str) -> sqlite3.Connection:
    """
    建好資料表後用另一條連線鎖住整個資料庫，回傳一條讀不到東西的連線

    `BEGIN EXCLUSIVE` 會擋住其他連線的**所有**讀寫（含 `sqlite_master`），
    這正是「背景 ETL 正在寫同一個 DB」時讀取端實際遇到的情況。
    `timeout=0` 讓它立刻拋 `database is locked` 而不是等待，測試才不會卡住。
    """

    writer: sqlite3.Connection = sqlite3.connect(db_path)
    writer.execute(f"CREATE TABLE {table_name} (date TEXT)")
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")

    return sqlite3.connect(db_path, timeout=0)


def _make_empty_db(db_path: Path) -> sqlite3.Connection:
    """回傳一條連到「檔案存在但一張表都沒有」的資料庫的連線（剛 clone 的環境）"""

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    return conn


# -----------------------------------------------------------------------
# === core.dao.base.table_exists ===
# -----------------------------------------------------------------------


def test_table_exists_tells_missing_from_present(tmp_path: Path) -> None:
    """表存在回 True、不存在回 False——這是分流的基礎"""

    conn: sqlite3.Connection = _make_empty_db(tmp_path / "tw_futures.db")

    assert table_exists(conn, "unrelated") is True
    assert table_exists(conn, "not_there") is False


# -----------------------------------------------------------------------
# === Loader：續跑起點與入庫筆數 ===
# -----------------------------------------------------------------------


@pytest.fixture
def chip_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesChipLoader:
    """建一個只碰暫存目錄與暫存 DB 的 loader"""

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_chip_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_chip_loader.FUTURES_CHIP_DOWNLOADS_PATH",
        tmp_path / "chip",
    )
    return FuturesChipLoader()


def test_loader_latest_date_is_none_when_table_missing(
    chip_loader: FuturesChipLoader,
) -> None:
    """表還沒建：回 None，讓 updater 從預設起日開始（首次更新的正常路徑）"""

    assert chip_loader.get_latest_date(FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME) is None


def test_loader_count_rows_is_zero_when_table_missing(
    chip_loader: FuturesChipLoader,
) -> None:
    """表還沒建：回 0"""

    assert chip_loader.count_rows(FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME) == 0


def test_loader_latest_date_raises_when_db_locked(
    chip_loader: FuturesChipLoader, tmp_path: Path
) -> None:
    """
    資料庫被鎖住時必須拋，**不可回 None**

    回 None 會讓 `futures_chip_updater.resolve_start_date()` 退回
    `DEFAULT_START_DATE`，整段歷史重爬好幾個小時，而 log 只顯示一個
    看起來正常的起始日期。
    """

    chip_loader.conn = _make_locked_db(
        tmp_path / "locked.db", FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
    )

    with pytest.raises(sqlite3.OperationalError):
        chip_loader.get_latest_date(FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME)


def test_loader_count_rows_raises_when_db_locked(
    chip_loader: FuturesChipLoader, tmp_path: Path
) -> None:
    """
    同上：回 0 會讓入庫統計變成負數

    `add_to_db()` 用「入庫後列數 − 入庫前列數」算新增筆數，
    後一次查詢失敗就會印出「新增 -1234 列」。
    """

    chip_loader.conn = _make_locked_db(
        tmp_path / "locked.db", FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
    )

    with pytest.raises(sqlite3.OperationalError):
        chip_loader.count_rows(FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME)


# -----------------------------------------------------------------------
# === 讀取層：回測期間被鎖住不可靜默少開倉 ===
# -----------------------------------------------------------------------


def test_chip_api_latest_available_date_raises_when_db_locked(tmp_path: Path) -> None:
    """三大法人籌碼：被鎖住時拋，而不是回 None 讓策略以為「這天之前沒有籌碼」"""

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
    )
    api: FuturesChipAPI = FuturesChipAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_latest_available_date(datetime.date(2026, 9, 1))


def test_chip_api_covered_date_range_raises_when_db_locked(tmp_path: Path) -> None:
    """涵蓋範圍查詢同樣不吞錯（人工確認回補進度時才不會誤判成沒資料）"""

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
    )
    api: FuturesChipAPI = FuturesChipAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_covered_date_range()


def test_margin_api_get_margin_raises_when_db_locked(tmp_path: Path) -> None:
    """
    保證金金額：被鎖住時拋

    回 None 會讓 `FuturesMarginConfig` 退回固定比率近似，而實資料量化出的誤差是
    2020 年 +143% 到 2026 年 −38%——**跨年份還會變號**。
    """

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", FUTURES_MARGIN_HISTORY_TABLE_NAME
    )
    api: FuturesMarginAPI = FuturesMarginAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_margin(product="臺股期貨", date=datetime.date(2026, 9, 1))


def test_margin_api_get_margin_rates_raises_when_db_locked(tmp_path: Path) -> None:
    """股票期貨保證金比例：與金額同一套語意"""

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME
    )
    api: FuturesMarginAPI = FuturesMarginAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_margin_rates(product_id="CDF", date=datetime.date(2026, 9, 1))


def test_margin_api_covered_date_range_raises_when_db_locked(tmp_path: Path) -> None:
    """生效日範圍查詢同樣不吞錯"""

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", FUTURES_MARGIN_HISTORY_TABLE_NAME
    )
    api: FuturesMarginAPI = FuturesMarginAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_covered_date_range(product="臺股期貨")


def test_universe_api_snapshot_date_raises_when_db_locked(tmp_path: Path) -> None:
    """股票期貨標的池：被鎖住時拋，否則契約單位會靜默退回最早的一份快照"""

    conn: sqlite3.Connection = _make_locked_db(
        tmp_path / "tw_futures.db", FUTURES_STOCK_UNIVERSE_TABLE_NAME
    )
    api: FuturesStockUniverseAPI = FuturesStockUniverseAPI(conn=conn)

    with pytest.raises(sqlite3.OperationalError):
        api.get_snapshot_date()


def test_margin_api_returns_none_when_table_missing(tmp_path: Path) -> None:
    """
    表還沒建仍要回 None，不可改成拋

    全新環境（CI、剛 clone）本來就沒跑過 `--target futures_margin`，
    那是「還沒有資料」而不是錯誤——這條確保修正沒有把正常路徑一起改掉。
    """

    conn: sqlite3.Connection = _make_empty_db(tmp_path / "tw_futures.db")
    api: FuturesMarginAPI = FuturesMarginAPI(conn=conn)

    result: Optional[Dict[str, int]] = api.get_margin(
        product="臺股期貨", date=datetime.date(2026, 9, 1)
    )
    assert result is None


# -----------------------------------------------------------------------
# === 首次更新（表還沒建）必須走得完 ===
# -----------------------------------------------------------------------


def test_updater_falls_back_to_default_start_when_table_missing(
    chip_loader: FuturesChipLoader,
) -> None:
    """
    表還沒建時，續跑起點退回 `DEFAULT_START_DATE`——這是全新環境的正常路徑

    修 S1 時最容易連帶改壞的就是這條：把「表不存在」也改成拋例外，
    剛 clone 的機器第一次跑 `--target futures_chip` 就會當場失敗。

    **只組出 `loader` 而不走 `__init__`**：`FuturesChipUpdater.setup()` 會一併建
    crawler、cleaner 與 `FuturesPriceAPI`（連正式的 `tw_futures.db`），
    而本測試要驗的只有 `resolve_start_date()` 這一個純決策。
    """

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.loader = chip_loader

    start: datetime.date = updater.resolve_start_date(
        table=FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
        start_date=None,
        resume=True,
    )

    assert start == FuturesChipUpdater.DEFAULT_START_DATE
