import sqlite3
from pathlib import Path
from typing import List, Tuple

import pandas as pd
import pytest

from core.config import MONTHLY_REVENUE_TABLE_NAME
from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO
from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.tw.crawlers.monthly_revenue_report_crawler import (
    MonthlyRevenueReportCrawler,
)
from core.pipeline.tw.updaters.monthly_revenue_report_updater import (
    MonthlyRevenueReportUpdater,
)

"""
月營收更新的兩條防線

1. **候選年月是差集，不是「表內最新 +1」**：中間某個月失敗被跳過之後，只要下個月
   成功入庫，`MAX` 就越過它，那個月從此不會再被請求。財報三表已經是差集，
   月營收在此之前沒有跟進，而 `docs/pipeline/etl-ingestion.md` 的對照表寫的是差集。
2. **一邊查無資料、另一邊有資料要判失敗**：兩個市場的公布時間不同，先公布的
   那一邊若照常入庫，該月就只有半個市場——2026/04 補回時「只有 26 檔」即此成因，
   當時只修了資料、程式沒改。

不連網路、不碰正式 DB。
"""


def make_updater(
    tmp_path: Path, existing: List[Tuple[int, int]]
) -> MonthlyRevenueReportUpdater:
    """組一支只有 DAO、表內已有指定年月的 updater"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        f"CREATE TABLE {MONTHLY_REVENUE_TABLE_NAME} "
        f"(year INT, month INT, stock_id TEXT, 當月營收 INT)"
    )
    conn.executemany(
        f"INSERT INTO {MONTHLY_REVENUE_TABLE_NAME} VALUES (?, ?, '2330', 1)",
        existing,
    )
    conn.commit()

    updater = MonthlyRevenueReportUpdater.__new__(MonthlyRevenueReportUpdater)
    updater.dao = MonthlyRevenueDAO(conn=conn)
    return updater


# === 候選年月 ===
def test_missing_month_in_the_middle_is_planned_again(tmp_path: Path) -> None:
    """中間缺的月份要重新進入候選——這正是 `MAX + 1` 補不回來的那一種"""

    updater: MonthlyRevenueReportUpdater = make_updater(
        tmp_path, [(2025, 1), (2025, 3)]
    )

    pending: List[Tuple[int, int]] = updater.plan_pending_year_months(2025, 1, 2025, 4)

    assert pending == [(2025, 2), (2025, 4)]


def test_year_boundary_is_not_a_cartesian_product(tmp_path: Path) -> None:
    """
    跨年區間不可用 years × months 的笛卡兒積

    起點 2025/03、終點 2026/02 時，笛卡兒積只會產出 3~12 月，
    2026/01、2026/02 整整兩個月不會被爬而且沒有任何錯誤。
    """

    updater: MonthlyRevenueReportUpdater = make_updater(tmp_path, [])

    pending: List[Tuple[int, int]] = updater.plan_pending_year_months(2025, 11, 2026, 2)

    assert pending == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_empty_table_plans_every_month(tmp_path: Path) -> None:
    """初次更新（表是空的）要全部納入候選"""

    updater: MonthlyRevenueReportUpdater = make_updater(tmp_path, [])

    assert updater.plan_pending_year_months(2025, 1, 2025, 3) == [
        (2025, 1),
        (2025, 2),
        (2025, 3),
    ]


# === 半個市場 ===
@pytest.fixture
def crawler() -> MonthlyRevenueReportCrawler:
    """只取用 `crawl()` 的合併邏輯，兩個市場的結果由測試指定"""

    return MonthlyRevenueReportCrawler.__new__(MonthlyRevenueReportCrawler)


def ok_result() -> CrawlResult:
    """一份有資料的結果"""

    return CrawlResult.ok_tables([pd.DataFrame({"公司代號": ["2330"]})])


@pytest.mark.parametrize("no_data_market", ["twse", "tpex"])
def test_one_market_without_data_fails_the_month(
    crawler: MonthlyRevenueReportCrawler,
    monkeypatch: pytest.MonkeyPatch,
    no_data_market: str,
) -> None:
    """一邊查無資料、另一邊有資料時整個年月判失敗，不入庫半個市場"""

    monkeypatch.setattr(
        crawler,
        "crawl_twse_monthly_revenue",
        lambda year, month: (
            CrawlResult.no_data("查無資料") if no_data_market == "twse" else ok_result()
        ),
    )
    monkeypatch.setattr(
        crawler,
        "crawl_tpex_monthly_revenue",
        lambda year, month: (
            CrawlResult.no_data("查無資料") if no_data_market == "tpex" else ok_result()
        ),
    )

    assert crawler.crawl(2026, 4).status is CrawlStatus.FAILED


def test_both_markets_without_data_stay_no_data(
    crawler: MonthlyRevenueReportCrawler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兩邊都查無資料維持 `NO_DATA`——當月尚未公布是正常狀態，不是失敗"""

    monkeypatch.setattr(
        crawler,
        "crawl_twse_monthly_revenue",
        lambda year, month: CrawlResult.no_data("查無資料"),
    )
    monkeypatch.setattr(
        crawler,
        "crawl_tpex_monthly_revenue",
        lambda year, month: CrawlResult.no_data("查無資料"),
    )

    assert crawler.crawl(2026, 9).status is CrawlStatus.NO_DATA


def test_both_markets_with_data_are_combined(
    crawler: MonthlyRevenueReportCrawler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兩邊都有資料時照常合併（防止改過頭）"""

    monkeypatch.setattr(
        crawler, "crawl_twse_monthly_revenue", lambda year, month: ok_result()
    )
    monkeypatch.setattr(
        crawler, "crawl_tpex_monthly_revenue", lambda year, month: ok_result()
    )

    result: CrawlResult = crawler.crawl(2026, 4)

    assert result.status is CrawlStatus.OK
    assert len(result.tables) == 2
