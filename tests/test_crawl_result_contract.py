import ast
import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Set, Tuple

import pandas as pd
import pytest

from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import UpdateStats
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.tw.updaters.finmind.stock_info_updater import StockInfoUpdater
from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater
from core.pipeline.utils.exceptions import DataLoadError

"""
crawler 的回傳契約：一律 `CrawlResult` 三態，「沒問到」與「沒有資料」分得開

原本財報、期貨籌碼、期貨保證金、股期清單、FinMind 回的是 `Optional[...]`，
把兩種情況混成 None；財報那條路徑又沒有統計，站方全面失聯時 `fs` target 仍以結束碼 0 收場。
"""

CRAWLER_DIR: Path = Path(__file__).resolve().parents[1] / "core/pipeline/tw/crawlers"

# 不回 `CrawlResult` 的 crawl 方法：各有理由，新增一個就要在這裡寫為什麼
ALLOWED_NON_CRAWL_RESULT: Dict[str, Set[str]] = {
    # 各 crawler 的預設入口：有的只是轉呼叫、有的回四張表的 dict
    "*": {"crawl"},
    # 回的是公告連結清單（不是資料），空 list 就是「沒有公告」
    "futures_margin_crawler.py": {"crawl_announcements"},
    # 總覽表取不到就直接拋錯，沒有「查無資料」這種狀態
    "stock_info_crawler.py": {
        "crawl_twse_stock_info",
        "crawl_tpex_stock_info",
        "crawl_stock_list",
    },
    # tick 走 TimescaleDB 那條線，它的回傳契約在那裡收斂
    "stock_tick_crawler.py": {"crawl_stock_tick"},
}


def _crawl_methods(path: Path) -> List[Tuple[str, str]]:
    """列出檔案內所有 `crawl*` 方法與其回傳型別標註"""

    tree: ast.Module = ast.parse(path.read_text())
    found: List[Tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("crawl"):
            returns: str = ast.unparse(node.returns) if node.returns else ""
            found.append((node.name, returns))
    return found


def test_every_crawl_method_returns_a_crawl_result() -> None:
    """`core/pipeline/tw/crawlers/` 的 `crawl*` 方法一律回 `CrawlResult`，例外要列在白名單並寫理由"""

    offenders: List[str] = []
    for path in sorted(CRAWLER_DIR.glob("*.py")):
        allowed: Set[str] = ALLOWED_NON_CRAWL_RESULT[
            "*"
        ] | ALLOWED_NON_CRAWL_RESULT.get(path.name, set())
        for name, returns in _crawl_methods(path):
            if name in allowed or returns == "CrawlResult":
                continue
            offenders.append(f"{path.name}:{name} -> {returns or '（無標註）'}")

    assert offenders == [], f"回傳型別不是 CrawlResult：{offenders}"


def test_text_result_carries_the_csv_body() -> None:
    """CSV 原文放 `text`；`ok_text()` 與 `ok()` 一樣是 `OK`"""

    result: CrawlResult = CrawlResult.ok_text("日期,商品\n2026/08/28,TX\n")

    assert result.is_ok
    assert result.text.startswith("日期")
    assert result.data is None and result.tables == []


# === 財報：來源失聯時 target 要失敗 ===
def _fs_updater(results_by_season: Dict[Tuple[int, int], CrawlResult]) -> Any:
    """只跑一張全市場報表的財報 updater，爬取結果由對照表決定"""

    loaded: List[Dict[str, Any]] = []
    updater: FinancialStatementUpdater = FinancialStatementUpdater.__new__(
        FinancialStatementUpdater
    )
    updater.plan_pending_year_seasons = lambda **_: sorted(results_by_season)
    updater.loader = SimpleNamespace(add_to_db=lambda **kwargs: loaded.append(kwargs))
    updater.log_latest_year_season = lambda *args: None
    updater.update_equity_changes = lambda *args, **kwargs: None
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    spec = SimpleNamespace(
        table_name="balance_sheet",
        crawl=lambda year, season: results_by_season[(year, season)],
        clean=lambda tables, year, season: tables[0],
        dir_path=Path("unused"),
        label="Balance Sheet",
        lowercase_label="balance sheet",
        log_label="Balance sheet",
    )
    updater.statement_specs = lambda: [spec]
    updater.loaded = loaded
    return updater


def test_fs_target_fails_when_every_season_is_unreachable() -> None:
    """三張全市場報表一個年季都沒拿到、且都是連不上：`update()` 要拋 `DataLoadError`"""

    updater = _fs_updater(
        {
            (2024, 1): CrawlResult.failed("HTTP 403"),
            (2024, 2): CrawlResult.failed("HTTP 403"),
        }
    )

    with pytest.raises(DataLoadError) as exc_info:
        updater.update(2024, 2024, 1, 2)

    assert exc_info.value.source == "fs"
    assert any("balance_sheet" in item for item in exc_info.value.failed_files)
    assert len(updater.loaded) == 1, "拿到的部分（這裡是零）仍要走完入庫步驟"


def test_fs_partial_failure_keeps_the_run_green_and_loads_the_rest() -> None:
    """只有部分年季沒拿到：照常入庫、不拋出——沒拿到的年季下次是差集的候選"""

    updater = _fs_updater(
        {
            (2024, 1): CrawlResult.ok_tables([pd.DataFrame({"a": [1]})]),
            (2024, 2): CrawlResult.failed("HTTP 403"),
        }
    )

    updater.update(2024, 2024, 1, 2)

    assert len(updater.loaded) == 1


def test_update_statement_reports_stats_per_season() -> None:
    """統計的單位是年季：兩季一成功一失敗 → requested 2、ok 1、unreachable 1"""

    updater = _fs_updater(
        {
            (2024, 1): CrawlResult.ok_tables([pd.DataFrame({"a": [1]})]),
            (2024, 2): CrawlResult.failed("HTTP 403"),
        }
    )

    stats: UpdateStats = updater.update_statement(
        updater.statement_specs()[0], 2024, 2024, 1, 2
    )

    assert (stats.requested, stats.ok, stats.unreachable) == (2, 1, 1)


# === 期貨籌碼：有交易日卻沒拿到才是 FAILED ===
def test_chip_window_without_trading_days_is_no_data() -> None:
    """crawler 分不出非交易日與被擋；沒有交易日的月份一律是 NO_DATA，不重試"""

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.has_trading_days = lambda start, end: False
    day: datetime.date = datetime.date(2026, 8, 30)

    result: CrawlResult = updater.crawl_window(
        lambda start, end: CrawlResult.no_data("回的是 HTML"), "三大法人", day, day
    )

    assert result.is_no_data


def test_chip_window_with_trading_days_becomes_failed_after_retries() -> None:
    """有交易日卻一直拿不到 CSV：重試耗盡後是 FAILED，呼叫端據此讓 target 失敗"""

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.BLOCKED_RETRY_DELAY_SECONDS = 0
    updater.has_trading_days = lambda start, end: True
    day: datetime.date = datetime.date(2026, 8, 28)

    result: CrawlResult = updater.crawl_window(
        lambda start, end: CrawlResult.no_data("回的是 HTML"), "三大法人", day, day
    )

    assert result.is_failed
    assert "該有交易日卻沒拿到" in result.reason


# === FinMind：空表是 NO_DATA，不入庫也不報錯 ===
def test_finmind_no_data_skips_the_load() -> None:
    """API 正常回傳空表：不清洗、不入庫；有資料時才走完整條流程"""

    calls: List[str] = []
    context = SimpleNamespace(
        crawler=SimpleNamespace(
            crawl_stock_info=lambda: CrawlResult.no_data("API 回傳空表")
        ),
        cleaner=SimpleNamespace(
            clean_stock_info=lambda df: calls.append("clean") or df
        ),
        loader=SimpleNamespace(
            load_stock_info=lambda: calls.append("load"),
            commit=lambda: calls.append("commit"),
        ),
        ensure_loader_connected=lambda: None,
    )

    StockInfoUpdater(context).update_stock_info()
    assert calls == []

    context.crawler.crawl_stock_info = lambda: CrawlResult.ok(pd.DataFrame({"a": [1]}))
    StockInfoUpdater(context).update_stock_info()
    assert calls == ["clean", "load", "commit"]


def _chip_updater(has_trading_days: bool) -> FuturesChipUpdater:
    """只跑 `update_dataset()` 的籌碙 updater：不等、不入庫、交易日判定可控"""

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.BLOCKED_RETRY_DELAY_SECONDS = 0
    updater.MIN_DELAY_SECONDS = 0
    updater.MAX_DELAY_SECONDS = 0
    updater.has_trading_days = lambda start, end: has_trading_days
    updater.loader = SimpleNamespace(
        save_csv=lambda df, name: None,
        add_to_db=lambda table, df: len(df),
        commit=lambda: None,
    )
    return updater


@pytest.mark.parametrize(
    ("has_trading_days", "expected_blocked"), [(False, 0), (True, 1)]
)
def test_chip_dataset_records_a_blocked_month_only_when_it_had_trading_days(
    has_trading_days: bool, expected_blocked: int
) -> None:
    """NO_DATA 的月份：沒有交易日就是真的沒資料，不記缺口；有交易日才是被擋"""

    updater: FuturesChipUpdater = _chip_updater(has_trading_days)
    start: datetime.date = datetime.date(2026, 8, 1)
    end: datetime.date = datetime.date(2026, 8, 31)

    inserted, blocked = updater.update_dataset(
        "futures_institutional_chip",
        "三大法人",
        lambda s, e: CrawlResult.no_data("回的是 HTML"),
        lambda text: pd.DataFrame(),
        start,
        end,
    )

    assert inserted == 0
    assert len(blocked) == expected_blocked
