import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from core.pipeline.tw.cleaners import stock_tick_cleaner as cleaner_module
from core.pipeline.tw.cleaners.stock_tick_cleaner import StockTickCleaner
from core.pipeline.tw.crawlers.stock_tick_crawler import StockTickCrawler
from core.pipeline.tw.loaders.stock_tick_loader import StockTickLoader
from core.pipeline.tw.updaters.stock_tick_updater import StockTickUpdater
from core.pipeline.tw.utils.stock_tick_utils import StockTickUtils

"""
台股 tick 更新流程：續跑判斷、寫入不留洞、已入庫 CSV 的清理，以及收窄後的例外處理

收窄的每一處都驗兩個方向：該接住的資料／外部錯誤要接住，程式自己寫錯要現形。
不連 Shioaji、不連 TimescaleDB：updater 以 `__new__` 建立再塞替身。
"""

D1: datetime.date = datetime.date(2024, 5, 8)
D2: datetime.date = datetime.date(2024, 5, 9)
D3: datetime.date = datetime.date(2024, 5, 10)


# === 續跑判斷 ===
def test_check_date_crawled_skips_up_to_last_loaded_date() -> None:
    """不晚於已入庫最後一天的日期跳過；從未入庫的股票一律要爬"""

    loaded: Dict[str, datetime.date] = {"2330": D2}

    assert StockTickUtils.check_date_crawled(loaded, "2330", D1)
    assert StockTickUtils.check_date_crawled(loaded, "2330", D2)
    assert not StockTickUtils.check_date_crawled(loaded, "2330", D3)
    assert not StockTickUtils.check_date_crawled(loaded, "1101", D1)


class _FakeDAO:
    """只提供 `get_latest_trade_date()` 的替身"""

    def __init__(self, latest: Optional[datetime.date]) -> None:
        """建立替身"""

        self.latest: Optional[datetime.date] = latest

    def get_latest_trade_date(self) -> Optional[datetime.date]:
        """回傳預設的最新日"""

        return self.latest


def test_table_latest_date_falls_back_when_empty() -> None:
    """資料庫還沒有任何 tick 時回傳預設起點，而不是 None"""

    assert StockTickUtils.get_table_latest_date(_FakeDAO(None)) == (
        StockTickUtils.TICK_DEFAULT_FALLBACK_DATE
    )
    assert StockTickUtils.get_table_latest_date(_FakeDAO(D3)) == D3


# === 爬蟲 ===
def _fake_api(ticks: Any = None, error: Optional[Exception] = None) -> Any:
    """Shioaji API 替身：合約表只有 2330；`ticks()` 回傳 `ticks` 或拋出 `error`"""

    def get_ticks(contract: Any, date: str) -> Any:
        if error is not None:
            raise error
        return ticks

    return SimpleNamespace(
        Contracts=SimpleNamespace(
            Stocks=SimpleNamespace(
                get=lambda code: "contract" if code == "2330" else None
            )
        ),
        ticks=get_ticks,
        usage=lambda: SimpleNamespace(remaining_bytes=10**12),
    )


def test_crawler_turns_shioaji_failure_into_connection_error() -> None:
    """
    Shioaji 取資料失敗要拋 `ConnectionError`，不可回 None

    回 None 的話 updater 會當成「這天沒成交」，那一天之後就再也不會重爬。
    """

    crawler: StockTickCrawler = StockTickCrawler.__new__(StockTickCrawler)

    with pytest.raises(ConnectionError, match="RuntimeError"):
        crawler.crawl_stock_tick(_fake_api(error=RuntimeError("timeout")), D1, "2330")


def test_crawler_returns_none_for_unknown_code() -> None:
    """代號不在合約表（例如已下市）是「沒資料」，不是失敗"""

    crawler: StockTickCrawler = StockTickCrawler.__new__(StockTickCrawler)

    assert crawler.crawl_stock_tick(_fake_api(), D1, "9999") is None


# === 清洗 ===
def _cleaner(tmp_path: Path) -> StockTickCleaner:
    """落地目錄指向暫存目錄的 cleaner"""

    cleaner: StockTickCleaner = StockTickCleaner.__new__(StockTickCleaner)
    cleaner.tick_dir = tmp_path
    return cleaner


def _shioaji_ticks(day: datetime.date) -> pd.DataFrame:
    """Shioaji `ticks()` 轉成 DataFrame 之後的欄位"""

    return pd.DataFrame(
        {
            "ts": [pd.Timestamp(f"{day} 09:00:01.123456")],
            "close": [800.0],
            "volume": [1],
            "bid_price": [799.0],
            "bid_volume": [2],
            "ask_price": [800.0],
            "ask_volume": [3],
            "tick_type": [1],
        }
    )


def test_cleaner_returns_none_on_missing_column(tmp_path: Path) -> None:
    """來源缺欄（KeyError）是資料問題：回 None、不落地"""

    raw: pd.DataFrame = _shioaji_ticks(D1).drop(columns=["tick_type"])

    assert _cleaner(tmp_path).clean_stock_tick(raw, "2330") is None
    assert not (tmp_path / "2330.csv").exists()


def test_cleaner_returns_none_without_timestamp_column(tmp_path: Path) -> None:
    """連 `ts` 都沒有時在時間轉換那一步就接住"""

    raw: pd.DataFrame = _shioaji_ticks(D1).drop(columns=["ts"])

    assert _cleaner(tmp_path).clean_stock_tick(raw, "2330") is None


def test_cleaner_lets_programming_errors_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """程式自己寫錯（AttributeError）不可被當成「這檔沒資料」吞掉"""

    cleaner: StockTickCleaner = _cleaner(tmp_path)

    def broken(df: pd.DataFrame, stock_id: str) -> pd.DataFrame:
        raise AttributeError("bug")

    monkeypatch.setattr(cleaner, "format_tick_data", broken)

    with pytest.raises(AttributeError):
        cleaner.clean_stock_tick(_shioaji_ticks(D1), "2330")


def test_microsecond_formatting_keeps_frame_on_conversion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """時間轉換失敗（ValueError）保留原表交給入庫端；程式錯誤照常拋出"""

    cleaner: StockTickCleaner = StockTickCleaner.__new__(StockTickCleaner)
    df: pd.DataFrame = pd.DataFrame({"time": ["2024-05-08 09:00:01"]})

    def fail_with(error: Exception) -> Any:
        def to_datetime(*args: Any, **kwargs: Any) -> Any:
            raise error

        return to_datetime

    monkeypatch.setattr(cleaner_module.pd, "to_datetime", fail_with(ValueError("x")))
    assert cleaner.format_time_to_microsec(df) is df

    monkeypatch.setattr(
        cleaner_module.pd, "to_datetime", fail_with(AttributeError("bug"))
    )
    with pytest.raises(AttributeError):
        cleaner.format_time_to_microsec(df)


# === updater：寫入不留洞 ===
class _RecordingCleaner:
    """記錄交給 cleaner 的日期，回傳原資料當作清洗成功"""

    def __init__(self) -> None:
        """建立替身"""

        self.days: List[datetime.date] = []

    def clean_stock_tick(self, df: pd.DataFrame, stock_id: str) -> pd.DataFrame:
        """記錄這次要落地的交易日"""

        self.days = sorted(set(df["ts"].dt.date))
        return df


class _ScriptedCrawler:
    """依日期回傳資料或拋出指定的例外"""

    def __init__(self, failures: Dict[datetime.date, Exception]) -> None:
        """建立替身"""

        self.failures: Dict[datetime.date, Exception] = failures

    def crawl_stock_tick(
        self, api: Any, date: datetime.date, code: str
    ) -> Optional[pd.DataFrame]:
        """失敗日拋例外，其餘日子回一筆 tick"""

        if date in self.failures:
            raise self.failures[date]
        return _shioaji_ticks(date)


def _updater(
    failures: Dict[datetime.date, Exception],
    loaded: Optional[Dict[str, datetime.date]] = None,
) -> StockTickUpdater:
    """不登入 Shioaji、不連資料庫的 updater"""

    updater: StockTickUpdater = StockTickUpdater.__new__(StockTickUpdater)
    updater.crawler = _ScriptedCrawler(failures)
    updater.cleaner = _RecordingCleaner()
    updater.loaded_last_dates = loaded or {}
    return updater


def test_failed_day_stops_later_days_from_being_written() -> None:
    """
    中間某天爬取失敗時，只寫失敗日之前的日子，這檔計為失敗

    續跑規則是「不晚於已入庫的最後一天就跳過」：失敗日之後的日子若先寫進去，
    失敗那天就永遠補不回來。
    """

    updater: StockTickUpdater = _updater({D2: ConnectionError("timeout")})

    stats: Dict[str, Any] = updater.update_thread(_fake_api(), [D1, D2, D3], ["2330"])

    assert updater.cleaner.days == [D1]
    assert stats["failed_stocks"] == 1
    assert stats["successful_stocks"] == 0


def test_failure_on_first_day_writes_nothing() -> None:
    """第一天就失敗時不落地任何資料"""

    updater: StockTickUpdater = _updater({D1: ConnectionError("timeout")})

    stats: Dict[str, Any] = updater.update_thread(_fake_api(), [D1, D2], ["2330"])

    assert updater.cleaner.days == []
    assert stats["failed_stocks"] == 1


def test_already_loaded_days_are_not_crawled() -> None:
    """已入庫的日子不爬（失敗設定在那天也不會觸發）"""

    updater: StockTickUpdater = _updater(
        {D1: ConnectionError("不該被爬到")}, loaded={"2330": D1}
    )

    stats: Dict[str, Any] = updater.update_thread(_fake_api(), [D1, D2], ["2330"])

    assert updater.cleaner.days == [D2]
    assert stats["successful_stocks"] == 1


def test_crawler_bug_is_not_counted_as_failed_day() -> None:
    """crawler 的程式錯誤（TypeError）要往外拋給 thread 邊界，不可混成某天爬取失敗"""

    updater: StockTickUpdater = _updater({D1: TypeError("bug")})

    with pytest.raises(TypeError):
        updater.update_thread(_fake_api(), [D1], ["2330"])


# === updater：清理已入庫的 CSV ===
class _FakeLoader:
    """依檔名回傳 `is_fully_loaded()` 的結果或拋出指定例外"""

    def __init__(self, results: Dict[str, Any]) -> None:
        """建立替身"""

        self.results: Dict[str, Any] = results

    def is_fully_loaded(self, csv_path: Path) -> bool:
        """查表回傳；值是例外就拋出"""

        result: Any = self.results[csv_path.name]
        if isinstance(result, Exception):
            raise result
        return result


def test_remove_loaded_csv_files_keeps_unloaded_and_unreadable(
    tmp_path: Path,
) -> None:
    """只刪完整入庫的檔；沒入庫、讀不了的檔都留著"""

    for name in ("1101.csv", "2330.csv", "2317.csv", "notes.csv"):
        (tmp_path / name).write_text("time\n", encoding="utf-8")
    updater: StockTickUpdater = StockTickUpdater.__new__(StockTickUpdater)
    updater.tick_dir = tmp_path
    updater.loader = _FakeLoader(
        {"1101.csv": True, "2330.csv": False, "2317.csv": ValueError("壞檔")}
    )

    deleted: int = updater.remove_loaded_csv_files()

    assert deleted == 1
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == [
        "2317.csv",
        "2330.csv",
        "notes.csv",
    ]


def test_remove_loaded_csv_files_lets_database_errors_surface(tmp_path: Path) -> None:
    """資料庫錯誤不是「這個檔有問題」：連不上就不該往下爬"""

    (tmp_path / "1101.csv").write_text("time\n", encoding="utf-8")
    updater: StockTickUpdater = StockTickUpdater.__new__(StockTickUpdater)
    updater.tick_dir = tmp_path
    updater.loader = _FakeLoader({"1101.csv": RuntimeError("connection refused")})

    with pytest.raises(RuntimeError):
        updater.remove_loaded_csv_files()


# === loader：CSV 是否已完整入庫 ===
class _SourceRowsDAO:
    """`get_source_rows()` 回傳預設值的替身"""

    def __init__(self, rows: Dict[datetime.date, int]) -> None:
        """建立替身"""

        self.rows: Dict[datetime.date, int] = rows

    def get_source_rows(self, stock_id: str) -> Dict[datetime.date, int]:
        """回傳預設的來源列數"""

        return self.rows


@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        ({D1: 2, D2: 1}, True),
        ({D1: 2}, False),  # 少一天
        ({D1: 1, D2: 1}, False),  # 列數不同
    ],
)
def test_is_fully_loaded_compares_rows_per_day(
    tmp_path: Path, loaded: Dict[datetime.date, int], expected: bool
) -> None:
    """每個交易日都要登記且來源列數一致才算已入庫；兩種時間欄名都認得"""

    loader: StockTickLoader = StockTickLoader.__new__(StockTickLoader)
    loader.dao = _SourceRowsDAO(loaded)
    for header in ("stock_id,time,close", "ts,close"):
        prefix: str = "2330," if header.startswith("stock_id") else ""
        csv_path: Path = tmp_path / "2330.csv"
        csv_path.write_text(
            "\n".join(
                [
                    header,
                    f"{prefix}2024-05-08 09:00:01.000000,800",
                    f"{prefix}2024-05-08 09:00:02.000000,800",
                    f"{prefix}2024-05-09 09:00:01.000000,801",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        assert loader.is_fully_loaded(csv_path) is expected
