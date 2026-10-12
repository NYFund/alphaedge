import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pandas as pd
import pytest

from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.shared.request_utils import RateLimitPolicy
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.tw.updaters.finmind.common import FinMindContext
from tests.test_futures_margin import make_history_updater

"""
原本不可中斷的三條長迴圈：財報三張全市場報表、期貨保證金公告回補、FinMind quota 等待

這三處原本各自用裸 `time.sleep()`（或根本沒有中止點），按下 Ctrl+C 要等滿一段
才有反應，等於逼人用 `kill -9`。這裡釘住「收到中止要求後，做完手上這一筆就停，
已處理的部分不遺失」。
"""

# 中止後允許的最長反應時間（`GracefulStop.SLEEP_SLICE_SECONDS` 是 0.2 秒）
MAX_STOP_LATENCY_SECONDS: float = 1.0


@pytest.fixture
def captured_stops(monkeypatch: pytest.MonkeyPatch) -> List[GracefulStop]:
    """攔下迴圈自己建立的 `GracefulStop`，讓替身能在途中要求收工"""

    stops: List[GracefulStop] = []
    original_enter = GracefulStop.__enter__

    def remember(self: GracefulStop) -> GracefulStop:
        stops.append(self)
        return original_enter(self)

    monkeypatch.setattr(GracefulStop, "__enter__", remember)
    return stops


# === 財報三張全市場報表 ===
def test_statement_loop_stops_after_the_current_season_and_still_loads(
    monkeypatch: pytest.MonkeyPatch, captured_stops: List[GracefulStop]
) -> None:
    """第一個年季爬到一半收到中止：不再爬下一季，已清洗的年季照常入庫"""

    monkeypatch.setattr(
        RateLimitPolicy, "tick", lambda self, stop=None, count=None: True
    )

    crawled: List[Tuple[int, int]] = []
    loaded: List[Any] = []

    def crawl(year: int, season: int) -> List[pd.DataFrame]:
        crawled.append((year, season))
        captured_stops[0].request("測試")
        return [pd.DataFrame({"a": [1]})]

    updater: FinancialStatementUpdater = FinancialStatementUpdater.__new__(
        FinancialStatementUpdater
    )
    updater.plan_pending_year_seasons = lambda **_: [(2024, 1), (2024, 2), (2024, 3)]
    updater.loader = SimpleNamespace(add_to_db=lambda **kwargs: loaded.append(kwargs))
    updater.log_latest_year_season = lambda *args: None
    spec = SimpleNamespace(
        table_name="balance_sheet",
        crawl=crawl,
        clean=lambda df_list, year, season: df_list[0],
        dir_path=Path("unused"),
        label="Balance Sheet",
        lowercase_label="balance sheet",
        log_label="Balance Sheet",
    )

    updater.update_statement(spec, 2024, 2024, 1, 3)

    assert crawled == [(2024, 1)]
    assert len(loaded) == 1


# === 期貨保證金公告回補 ===
def test_margin_backfill_stops_between_announcements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_stops: List[GracefulStop],
) -> None:
    """處理第一則公告時收到中止：只下載那一則，已入庫的列與處理紀錄都保留"""

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    original_download = crawler.crawl_announcement_csv

    def download_then_stop(csv_url: str) -> Optional[str]:
        captured_stops[0].request("測試")
        return original_download(csv_url)

    crawler.crawl_announcement_csv = download_then_stop

    updater.update_history()

    assert crawler.downloaded_urls == ["https://taifex/attach/index.csv"]
    assert updater.dao.count_rows() == 2
    assert (tmp_path / "meta" / "announcements.json").exists()


def test_margin_backfill_does_not_load_half_resolved_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_stops: List[GracefulStop],
) -> None:
    """
    解析附件網址途中收到中止：一則都不下載

    共用網址的判斷要看到全部公告才成立（站方會覆寫固定檔名的附件），
    只解析了一半就入庫，可能把被覆寫的內容寫成舊公告的歷史。
    """

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    original_resolve = crawler.resolve_announcement_csv

    def resolve_then_stop(link: str) -> Optional[str]:
        captured_stops[0].request("測試")
        return original_resolve(link)

    crawler.resolve_announcement_csv = resolve_then_stop

    updater.update_history()

    assert crawler.resolved_links == ["index"]
    assert crawler.downloaded_urls == []
    assert updater.dao.count_rows() == 0


# === FinMind quota 等待 ===
def test_quota_wait_returns_promptly_once_stop_is_requested() -> None:
    """quota 還沒恢復、正在等下一次檢查時收到中止：立刻回 False，不等滿一個間隔"""

    context: FinMindContext = FinMindContext.__new__(FinMindContext)
    context.get_api_remaining_quota = lambda: None
    context.quota_reset_time = time.time() + 3600
    stop: GracefulStop = GracefulStop(label="test")
    stop.request("測試")

    started: float = time.monotonic()
    restored: bool = context.wait_for_quota_reset(stop)
    elapsed: float = time.monotonic() - started

    assert restored is False
    assert elapsed < MAX_STOP_LATENCY_SECONDS, f"等了 {elapsed:.2f} 秒才回來"
