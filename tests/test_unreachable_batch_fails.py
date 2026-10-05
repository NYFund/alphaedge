import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional, Tuple, Type

import pytest

from core.dao.base import BaseDAO
from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.tw.updaters.corporate_action_updater import CorporateActionUpdater
from core.pipeline.tw.updaters.stock_dividend_updater import StockDividendUpdater
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater
from core.pipeline.utils.exceptions import DataLoadError
from tests.test_partial_market_guard import (
    DAY_OK,
    DAY_PARTIAL,
    loaded_dates,
    make_daily_updater,
    make_yearly_updater,
    raw_table,
)

"""
整批取不到資料時，target 必須記為失敗

2026-10-02 主機開著 VPN 跑排程資料更新，櫃買中心與期交所全數回 HTTP 403，
`price` 等五個 target 的統計全是 `0 ok / N unreachable`，卻都被列在「成功」——
`unreachable` 原本只會變成一行 WARNING。`price` 一筆都沒更新，是隔天實盤尾盤段
被資料新鮮度擋下才發現。

判準（`UpdateStats.failure_reason()`）：
- 逐日來源：**最近一個應有資料的日子**取不到就算失敗；休市日（查無資料）與台北的今天不算。
- 以年、月為單位查詢的來源：一個單位都沒拿到、且有取不到的，就算失敗。
"""

TODAY: datetime.date = datetime.date(2026, 10, 5)


def stats_of(*days: tuple) -> UpdateStats:
    """依（日期, 結果）建一份逐日統計"""

    stats: UpdateStats = UpdateStats()
    for date, status in days:
        stats.mark_day(date, status)
    return stats


# === 判準：逐日來源 ===
def test_latest_trading_day_unreachable_fails() -> None:
    """最近一天取不到：表停在舊資料，必須失敗"""

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 10, 1), CrawlStatus.OK),
        (datetime.date(2026, 10, 2), CrawlStatus.FAILED),
    )

    reason: Optional[str] = stats.failure_reason(TODAY)

    assert reason is not None
    assert "2026-10-02" in reason


def test_whole_batch_unreachable_fails() -> None:
    """2026-10-02 那次的形狀：每一天都取不到"""

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 9, 30), CrawlStatus.FAILED),
        (datetime.date(2026, 10, 1), CrawlStatus.FAILED),
    )

    assert stats.failure_reason(TODAY) is not None


def test_sporadic_gap_before_latest_day_is_left_for_retry() -> None:
    """回補中偶發連不上的那天下次會重試；最新一天拿到了就不算失敗"""

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 9, 29), CrawlStatus.OK),
        (datetime.date(2026, 9, 30), CrawlStatus.FAILED),
        (datetime.date(2026, 10, 1), CrawlStatus.OK),
    )

    assert stats.failure_reason(TODAY) is None


def test_holiday_after_failed_day_does_not_hide_it() -> None:
    """休市日查無資料不是「拿到了」：往前找到的最近交易日取不到，仍要失敗"""

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 10, 1), CrawlStatus.FAILED),
        (datetime.date(2026, 10, 2), CrawlStatus.NO_DATA),
    )

    assert stats.failure_reason(TODAY) is not None


def test_holidays_only_do_not_fail() -> None:
    """整批都是休市日：沒有應有資料的日子，不是失敗"""

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 10, 1), CrawlStatus.NO_DATA),
        (datetime.date(2026, 10, 2), CrawlStatus.NO_DATA),
    )

    assert stats.failure_reason(TODAY) is None


def test_today_is_not_expected_yet() -> None:
    """
    台北的今天不算：資料可能還沒公布

    櫃買中心在未公布時會回別天的頁面，日期核對不符而判為 FAILED；
    盤中手動執行不該因此誤報失敗。
    """

    stats: UpdateStats = stats_of(
        (datetime.date(2026, 10, 2), CrawlStatus.OK),
        (TODAY, CrawlStatus.FAILED),
    )

    assert stats.failure_reason(TODAY) is None


def test_nothing_requested_does_not_fail() -> None:
    """已是最新、本批 0 requested：不是失敗"""

    assert UpdateStats().failure_reason(TODAY) is None


# === 判準：以年、月為單位查詢的來源 ===
def test_range_batch_with_nothing_ok_fails() -> None:
    """除權息等區間查詢：一年都沒拿到就是整批被擋"""

    stats: UpdateStats = UpdateStats()
    for _ in range(3):
        stats.record(CrawlResult.failed("HTTP 403"), CrawlResult.failed("HTTP 403"))

    assert stats.failure_reason(TODAY) is not None


def test_range_batch_with_some_ok_is_left_for_retry() -> None:
    """有拿到的單位就照常入庫，取不到的下次重試"""

    stats: UpdateStats = UpdateStats()
    stats.record(CrawlResult.ok(None), CrawlResult.ok(None))
    stats.record(CrawlResult.failed("HTTP 403"), CrawlResult.failed("HTTP 403"))

    assert stats.failure_reason(TODAY) is None


# === updater：整批 403 ===
def test_daily_updater_raises_when_latest_day_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """拿到的那天照常入庫，但最新一天取不到，updater 收尾拋 `DataLoadError`"""

    results = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (CrawlResult.failed("HTTP 403"), CrawlResult.failed("HTTP 403")),
    }
    updater, loaded, _ = make_daily_updater(
        StockPriceUpdater, "price", tmp_path, monkeypatch, results, dao_factory
    )

    with pytest.raises(DataLoadError) as exc_info:
        updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    assert "2024-01-03" in exc_info.value.failed_files[0]


@pytest.mark.parametrize(
    ("updater_cls", "crawl_names"),
    [
        (StockDividendUpdater, ("crawl_twse_dividend", "crawl_tpex_dividend")),
        (
            CorporateActionUpdater,
            ("crawl_twse_capital_reduction", "crawl_tpex_capital_reduction"),
        ),
    ],
)
def test_range_updater_raises_when_every_year_is_unreachable(
    updater_cls: Type[BaseDataUpdater],
    crawl_names: Tuple[str, str],
    tmp_path: Path,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """以年為單位查詢的來源：每一年都被擋時收尾拋 `DataLoadError`"""

    updater = make_yearly_updater(
        updater_cls, crawl_names, SimpleNamespace(), tmp_path, dao_factory
    )
    blocked: CrawlResult = CrawlResult.failed("HTTP 403")
    updater.crawler = SimpleNamespace(
        **{name: lambda start, end: blocked for name in crawl_names}
    )

    with pytest.raises(DataLoadError):
        updater.update(
            start_date=datetime.date(2023, 1, 1), end_date=datetime.date(2024, 12, 31)
        )


def test_update_db_lists_the_target_as_failed_and_exits_non_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """端到端：整批 403 的 target 進失敗清單，`update_db` 非零結束"""

    import tasks.update_db as update_db

    updater, loaded, _ = make_daily_updater(
        StockPriceUpdater, "price", tmp_path, monkeypatch, {}, dao_factory
    )
    blocked: CrawlResult = CrawlResult.failed("HTTP 403")
    updater.crawler = SimpleNamespace(
        crawl_twse_price=lambda date: blocked, crawl_tpex_price=lambda date: blocked
    )
    updater.BATCH_SLEEP_EVERY_N_FILES = 10_000
    updater.close = lambda: None

    errors: List[str] = []
    monkeypatch.setattr(update_db, "StockPriceUpdater", lambda: updater)
    monkeypatch.setattr(update_db.logger, "error", errors.append)
    monkeypatch.setattr(
        update_db,
        "parse_arguments",
        lambda: SimpleNamespace(
            target=["price"],
            from_date=datetime.date.today() - datetime.timedelta(days=10),
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        update_db.main()

    assert exc_info.value.code == 1
    assert loaded == []
    assert any("失敗的 target：price" in message for message in errors)
