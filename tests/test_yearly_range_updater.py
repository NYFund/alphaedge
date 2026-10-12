import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, Type

import pandas as pd
import pytest

from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import YearlyRangeTwoMarketUpdater
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.tw.updaters.corporate_action_updater import CorporateActionUpdater
from core.pipeline.tw.updaters.stock_dividend_updater import StockDividendUpdater

"""
年度區間雙市場 updater 的共用骨架（除權息、公司行動）

兩支原本各抄一份迴圈，差別只在方法名與檔名。這裡釘住三件事：
方法名以 `SOURCE` 組出來、清洗檔名的後綴照各來源原樣保留（loader 依檔名挑檔，
改壞了只會漏入庫）、年與年之間的節流可被中止打斷且中止後仍入庫。
"""

UPDATERS: List[Tuple[Type[YearlyRangeTwoMarketUpdater], str]] = [
    (StockDividendUpdater, ""),
    (CorporateActionUpdater, ".csv"),
]


def _make(
    updater_cls: Type[YearlyRangeTwoMarketUpdater],
    calls: Dict[str, List[Any]],
) -> YearlyRangeTwoMarketUpdater:
    """建一支不連網路、不碰 DB 的 updater，記下爬取、清洗、入庫與 sleep 的呼叫"""

    updater = updater_cls.__new__(updater_cls)  # 跳過 __init__ 的正式連線與 log 設定
    source: str = updater_cls.SOURCE
    table: pd.DataFrame = pd.DataFrame({"a": [1]})

    def crawl(market: str):
        def _crawl(start: datetime.date, end: datetime.date) -> CrawlResult:
            calls["crawl"].append((market, start, end))
            return CrawlResult.ok(table)

        return _crawl

    def clean(market: str):
        def _clean(df: pd.DataFrame, file_name: str) -> pd.DataFrame:
            calls["clean"].append((market, file_name))
            return df

        return _clean

    updater.crawler = SimpleNamespace(
        **{f"crawl_{m}_{source}": crawl(m) for m in ("twse", "tpex")}
    )
    updater.cleaner = SimpleNamespace(
        **{f"clean_{m}_{source}": clean(m) for m in ("twse", "tpex")}
    )
    updater.loader = SimpleNamespace(
        add_to_db=lambda **kwargs: calls["load"].append(kwargs)
    )
    updater.dao = SimpleNamespace(get_latest_date=lambda: "2024-12-31")
    updater.sleep = lambda seconds, stop=None: calls["sleep"].append(stop)
    return updater


def _calls() -> Dict[str, List[Any]]:
    """空的呼叫紀錄"""

    return {"crawl": [], "clean": [], "load": [], "sleep": []}


@pytest.mark.parametrize(("updater_cls", "suffix"), UPDATERS)
def test_crawls_each_year_and_keeps_the_file_suffix(
    updater_cls: Type[YearlyRangeTwoMarketUpdater], suffix: str
) -> None:
    """逐年雙市場爬取；清洗檔名為 `{市場}_{起}_{迄}` 加上各來源原本的後綴"""

    calls: Dict[str, List[Any]] = _calls()
    updater = _make(updater_cls, calls)

    updater.update(datetime.date(2023, 6, 1), datetime.date(2024, 3, 31))

    assert calls["crawl"] == [
        ("twse", datetime.date(2023, 6, 1), datetime.date(2023, 12, 31)),
        ("tpex", datetime.date(2023, 6, 1), datetime.date(2023, 12, 31)),
        ("twse", datetime.date(2024, 1, 1), datetime.date(2024, 3, 31)),
        ("tpex", datetime.date(2024, 1, 1), datetime.date(2024, 3, 31)),
    ]
    assert calls["clean"] == [
        ("twse", f"twse_20230601_20231231{suffix}"),
        ("tpex", f"tpex_20230601_20231231{suffix}"),
        ("twse", f"twse_20240101_20240331{suffix}"),
        ("tpex", f"tpex_20240101_20240331{suffix}"),
    ]
    assert calls["load"] == [{"remove_files": False}]


@pytest.mark.parametrize(("updater_cls", "suffix"), UPDATERS)
def test_throttle_between_years_is_interruptible(
    updater_cls: Type[YearlyRangeTwoMarketUpdater], suffix: str
) -> None:
    """年與年之間的節流帶著中止旗標睡（原本是不可中斷的 `time.sleep`）"""

    calls: Dict[str, List[Any]] = _calls()
    updater = _make(updater_cls, calls)

    updater.update(datetime.date(2023, 1, 1), datetime.date(2024, 12, 31))

    assert len(calls["sleep"]) == 2
    assert all(isinstance(stop, GracefulStop) for stop in calls["sleep"])


@pytest.mark.parametrize(("updater_cls", "suffix"), UPDATERS)
def test_stop_request_ends_after_the_current_year_and_still_loads(
    updater_cls: Type[YearlyRangeTwoMarketUpdater],
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收到中止要求：做完手上這一年就停，已爬的年份照樣入庫"""

    calls: Dict[str, List[Any]] = _calls()
    updater = _make(updater_cls, calls)

    original_crawl = getattr(updater.crawler, f"crawl_tpex_{updater_cls.SOURCE}")
    stops: List[Optional[GracefulStop]] = []

    original_enter = GracefulStop.__enter__

    def remember_stop(self: GracefulStop) -> GracefulStop:
        stops.append(self)
        return original_enter(self)

    monkeypatch.setattr(GracefulStop, "__enter__", remember_stop)

    def crawl_then_stop(start: datetime.date, end: datetime.date) -> CrawlResult:
        stops[0].request("測試")
        return original_crawl(start, end)

    setattr(updater.crawler, f"crawl_tpex_{updater_cls.SOURCE}", crawl_then_stop)

    updater.update(datetime.date(2022, 1, 1), datetime.date(2024, 12, 31))

    crawled_years: List[int] = sorted({start.year for _, start, _ in calls["crawl"]})
    assert crawled_years == [2022]
    assert calls["sleep"] == []
    assert calls["load"] == [{"remove_files": False}]
