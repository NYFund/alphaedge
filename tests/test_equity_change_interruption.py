import datetime
import json
import signal
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import pytest

from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.shared.season_planner import SeasonPlanner, SeasonProgressStore
from core.pipeline.tw.cleaners.financial_statement_cleaner import (
    FinancialStatementCleaner,
)
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.utils.exceptions import DataLoadError

"""
權益變動表回補「隨時被中斷」也不掉資料、不重打請求

整段回補是逐檔查詢、十萬次以上請求、數十小時，中斷是常態不是例外。
本檔驗四件事，都不連網路、不連正式 DB：

1. `GracefulStop`：第一次訊號只立旗標、第二次立刻中止，且節流的 sleep 可被打斷。
2. 收到訊號時**手上那批先入庫**——沒有這道收尾，最多 100 檔（約兩分鐘的請求）
   會作廢，而那些公司在資料表裡不留任何列，重跑時無法與「還沒爬」區分。
3. 「查無資料」寫進進度檔，重跑不再重打（2020Q1 是 343 檔、佔全市場 16%），
   但**申報期未關閉的年季不寫入**——那時的查無資料多半只是還沒送件。
4. 單檔清洗失敗、單批入庫失敗都不中止其餘回補，但最後必須讓失敗浮出來。
"""


EQUITY_CHANGE_COLUMNS: List[str] = [
    "year",
    "season",
    "stock_id",
    "權益項目",
    "變動原因",
    "金額",
]

# crawler 的「有資料」回應。內容不重要——版面解析已由
# test_financial_statement_cleaner_equity_change.py 覆蓋，本檔關心的是中斷語意
RAW_TABLES: List[pd.DataFrame] = [pd.DataFrame({"會計項目": ["期初餘額"]})]


def make_db(tmp_path: Path) -> sqlite3.Connection:
    """以正式 schema（含主鍵）建一張暫存 equity_change 表"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    FinancialStatementDAO("equity_change", conn=conn).ensure_table(
        EQUITY_CHANGE_COLUMNS
    )
    return conn


def long_table_row(year: int, season: int, stock_id: str) -> pd.DataFrame:
    """一列長表，模擬 cleaner 攤平後的輸出"""

    return pd.DataFrame(
        [[year, season, stock_id, "普通股股本", "期初餘額", 1000.0]],
        columns=EQUITY_CHANGE_COLUMNS,
    )


def crawled_stock_ids(conn: sqlite3.Connection, year: int, season: int) -> Set[str]:
    """暫存 DB 內該年季已入庫的股票"""

    rows = conn.execute(
        "SELECT DISTINCT stock_id FROM equity_change WHERE year = ? AND season = ?",
        (year, season),
    ).fetchall()
    return {str(stock_id) for (stock_id,) in rows}


class FakeCrawler:
    """
    依對照表回應的假 crawler，並記下實際問過哪些檔

    回傳語意照實際的 `crawl_equity_changes()`：`None` 為站方過載、
    `[]` 為查無資料、非空 list 為正常。
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Optional[List[pd.DataFrame]]]] = None,
        on_request: Optional[object] = None,
    ) -> None:
        self.responses: Dict[str, Optional[List[pd.DataFrame]]] = responses or {}
        self.on_request = on_request
        self.requested: List[str] = []

    def crawl_equity_changes(
        self, year: int, season: int, stock_id: str
    ) -> Optional[List[pd.DataFrame]]:
        """記錄請求並回傳預先安排的結果"""

        self.requested.append(stock_id)
        if self.on_request is not None:
            self.on_request(stock_id)
        return self.responses.get(stock_id, RAW_TABLES)


class RecordingLoader:
    """
    把 CSV 寫進暫存 DB 的假 loader

    寫入用的是**真的** `FinancialStatementDAO.insert_or_ignore()`，
    對帳測試才驗得到 `INSERT OR IGNORE` 的冪等性。
    """

    def __init__(
        self, conn: sqlite3.Connection, fail_file_names: Set[str] = frozenset()
    ) -> None:
        self.conn: sqlite3.Connection = conn
        self.fail_file_names: Set[str] = set(fail_file_names)
        self.loaded: List[Path] = []

    def add_to_db(
        self,
        dir_path: Path,
        table_name: str,
        remove_files: bool = False,
        only_files: Optional[List[Path]] = None,
    ) -> None:
        """入庫指定檔案；失敗的檔案比照 loader 以 DataLoadError 拋出"""

        failed: List[str] = []
        for file_path in only_files or []:
            if file_path.name in self.fail_file_names:
                failed.append(str(file_path))
                continue

            df: pd.DataFrame = pd.read_csv(file_path, dtype={"stock_id": str})
            FinancialStatementDAO(table_name, conn=self.conn).insert_or_ignore(df)
            self.conn.commit()
            self.loaded.append(file_path)

        if failed:
            raise DataLoadError("fs", failed, len(self.loaded))


def make_updater(
    tmp_path: Path,
    conn: sqlite3.Connection,
    crawler: FakeCrawler,
    loader: RecordingLoader,
    clean_raises_for: Set[str] = frozenset(),
) -> FinancialStatementUpdater:
    """
    組一個只有權益變動表這條路可用的 updater

    `__new__` 跳過 `__init__`／`setup()`，避免連正式 DB 與寫 logs；
    `save_equity_changes()` 用**真的** cleaner 實作，批次檔名與序號才驗得到。
    """

    cleaner: FinancialStatementCleaner = FinancialStatementCleaner.__new__(
        FinancialStatementCleaner
    )
    cleaner.equity_change_dir = tmp_path / "equity_change"
    cleaner.equity_change_dir.mkdir(parents=True, exist_ok=True)

    def clean_equity_changes(
        df_list: List[pd.DataFrame], year: int, season: int, stock_id: str
    ) -> pd.DataFrame:
        """假的清洗：版面異常以例外表達，其餘回一列長表"""

        if stock_id in clean_raises_for:
            raise ValueError(f"版面異常: {stock_id}")
        return long_table_row(year, season, stock_id)

    cleaner.clean_equity_changes = clean_equity_changes

    updater: FinancialStatementUpdater = FinancialStatementUpdater.__new__(
        FinancialStatementUpdater
    )
    updater.conn = conn
    updater.crawler = crawler
    updater.cleaner = cleaner
    updater.loader = loader
    updater.equity_change_dir = cleaner.equity_change_dir
    updater.equity_change_progress_path = tmp_path / "equity_change_progress.json"

    return updater


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """測試不睡：節流值本身不是本檔要驗的東西"""

    monkeypatch.setattr(
        FinancialStatementUpdater, "EQUITY_CHANGE_RANDOM_DELAY_MIN", 0.0
    )
    monkeypatch.setattr(
        FinancialStatementUpdater, "EQUITY_CHANGE_RANDOM_DELAY_MAX", 0.0
    )
    monkeypatch.setattr(
        FinancialStatementUpdater, "EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS", 0
    )


# === GracefulStop：訊號變旗標 ===
def test_first_signal_only_sets_flag() -> None:
    """第一次 Ctrl+C 不能打斷流程，否則手上那批就沒機會入庫"""

    with GracefulStop(label="test") as stop:
        assert stop.requested is False
        signal.raise_signal(signal.SIGINT)
        assert stop.requested is True


def test_second_signal_aborts_immediately() -> None:
    """「收工也要等一下」不能變成「按了沒反應」：第二次必須立刻中止"""

    with pytest.raises(KeyboardInterrupt):
        with GracefulStop(label="test") as stop:
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
            assert stop.requested is True  # pragma: no cover（上一行就會拋）


def test_handlers_are_restored_after_context() -> None:
    """離開 context 要還原處理器，否則後續的 Ctrl+C 會打在已結束的旗標上"""

    original = signal.getsignal(signal.SIGINT)

    with GracefulStop(label="test"):
        assert signal.getsignal(signal.SIGINT) is not original

    assert signal.getsignal(signal.SIGINT) is original


def test_sleep_returns_early_when_stop_requested() -> None:
    """
    節流的 sleep 必須可被打斷

    `time.sleep()` 被訊號打斷會自動續睡（PEP 475），沿用它的話
    「每 50 檔睡 15 秒」那一段按下 Ctrl+C 得等滿 15 秒。
    """

    stop: GracefulStop = GracefulStop(label="test")
    stop.request(reason="test")

    started: float = time.monotonic()
    finished: bool = stop.sleep(5)

    assert finished is False
    assert time.monotonic() - started < 1


def test_sleep_completes_when_not_requested() -> None:
    """沒被要求收工時照睡完，回傳 True"""

    stop: GracefulStop = GracefulStop(label="test")

    assert stop.sleep(0.01) is True


# === 申報期是否關閉：決定「查無資料」能不能寫進永久名單 ===
@pytest.mark.parametrize(
    "year, season, today, settled",
    [
        # Q1 期限 5/30 ＋ 30 天寬限 = 6/29
        (2026, 1, datetime.date(2026, 6, 1), False),
        (2026, 1, datetime.date(2026, 7, 1), True),
        # Q2 期限 8/31 ＋ 30 天 = 9/30
        (2026, 2, datetime.date(2026, 9, 4), False),
        (2026, 2, datetime.date(2026, 10, 5), True),
        # Q3 期限 11/29 ＋ 30 天 = 12/29
        (2025, 3, datetime.date(2025, 12, 1), False),
        (2025, 3, datetime.date(2026, 1, 5), True),
        # 年報期限落在**次年** 3/31 ＋ 30 天 = 次年 4/30
        (2025, 4, datetime.date(2026, 4, 15), False),
        (2025, 4, datetime.date(2026, 5, 5), True),
        # 久遠的年季一律已關閉
        (2013, 1, datetime.date(2026, 9, 4), True),
    ],
)
def test_is_season_settled(
    year: int, season: int, today: datetime.date, settled: bool
) -> None:
    """申報期未關閉時不得判定已結案，否則會把還沒送件的公司永久排除"""

    assert FinancialStatementUpdater.is_season_settled(year, season, today) is settled


# === 進度檔 ===
def test_no_data_survives_restart(tmp_path: Path) -> None:
    """
    「查無資料」要跨行程記住

    逐檔來源的查無資料佔比極高（2020Q1 是 343/2,086），而它們在資料表裡
    不留任何列——不存檔就等於每次重跑都重打那些無效請求。
    """

    path: Path = tmp_path / "progress.json"
    store: SeasonProgressStore = SeasonProgressStore(source="equity_change", path=path)
    store.record_no_data(2013, 1, "6488", settled=True)
    store.record_incomplete(2013, 1, "2603")
    store.save()

    reloaded: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=path
    )

    assert reloaded.get_no_data(2013, 1) == {"6488"}
    assert reloaded.get_incomplete(2013, 1) == {"2603"}
    assert reloaded.get_no_data(2013, 2) == set()


def test_no_data_not_recorded_before_filing_deadline(tmp_path: Path) -> None:
    """申報期未關閉時不寫入：那時的查無資料多半只是那家公司還沒送件"""

    store: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=tmp_path / "progress.json"
    )
    store.record_no_data(2026, 2, "2330", settled=False)

    assert store.get_no_data(2026, 2) == set()


def test_no_data_and_incomplete_are_mutually_exclusive(tmp_path: Path) -> None:
    """「沒問到」不能被「問過了沒有」蓋掉，兩者是不同的事"""

    store: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=tmp_path / "progress.json"
    )

    store.record_no_data(2013, 1, "2330", settled=True)
    store.record_incomplete(2013, 1, "2330")

    assert store.get_no_data(2013, 1) == set()
    assert store.get_incomplete(2013, 1) == {"2330"}

    store.record_complete(2013, 1, "2330")

    assert store.get_no_data(2013, 1) == set()
    assert store.get_incomplete(2013, 1) == set()


def test_corrupt_progress_file_is_treated_as_empty(tmp_path: Path) -> None:
    """損毀的進度檔只能讓我們多問幾次，不能讓整段回補起不來"""

    path: Path = tmp_path / "progress.json"
    path.write_text("{not json", encoding="utf-8")

    store: SeasonProgressStore = SeasonProgressStore(source="equity_change", path=path)

    assert store.no_data == {}
    assert store.incomplete == {}


def test_progress_file_is_human_readable(tmp_path: Path) -> None:
    """
    進度檔要看得懂

    出事時要能直接打開它回答「哪些公司被排除了」——這正是 2020Q1 少 323 檔
    那次事後撈 log 才發現的原因。
    """

    path: Path = tmp_path / "progress.json"
    store: SeasonProgressStore = SeasonProgressStore(source="equity_change", path=path)
    store.record_no_data(2013, 1, "6488", settled=True)
    store.save()

    payload: Dict[str, Dict[str, List[str]]] = json.loads(
        path.read_text(encoding="utf-8")
    )

    assert payload["no_data"] == {"2013Q1": ["6488"]}


# === 候選清單（差集） ===
def test_plan_excludes_crawled_and_no_data() -> None:
    """候選 ＝ 目標 − 表內已有 − 確認沒有資料，且維持目標清單的順序"""

    pending: List[str] = SeasonPlanner.plan(
        target_stock_ids=["1101", "2330", "2891", "6488"],
        crawled_stock_ids={"2330"},
        no_data_stock_ids={"6488"},
    )

    assert pending == ["1101", "2891"]


def test_plan_retries_stocks_that_were_never_answered() -> None:
    """沒問到就是沒問到，不能被「確認沒有資料」擋掉"""

    pending: List[str] = SeasonPlanner.plan(
        target_stock_ids=["1101", "2603"],
        crawled_stock_ids=set(),
        no_data_stock_ids={"2603"},
        retry_stock_ids={"2603"},
    )

    assert pending == ["1101", "2603"]


# === 中斷：手上那批要先入庫 ===
def test_stop_signal_flushes_pending_batch(tmp_path: Path) -> None:
    """
    收到中止訊號時，已爬好但還沒湊滿一批的資料必須入庫

    這是「隨時可中斷」的核心：批次大小是 100 檔（約兩分鐘的請求），
    直接讓 KeyboardInterrupt 炸出去等於那 100 檔白爬，而它們在資料表裡
    不留任何列，重跑時與「還沒爬」無法區分。
    """

    conn: sqlite3.Connection = make_db(tmp_path)
    stop: GracefulStop = GracefulStop(label="test")
    crawler: FakeCrawler = FakeCrawler(
        on_request=lambda stock_id: stop.request("test") if stock_id == "1102" else None
    )
    loader: RecordingLoader = RecordingLoader(conn)
    updater: FinancialStatementUpdater = make_updater(tmp_path, conn, crawler, loader)

    stats = updater.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=["1101", "1102", "1103", "1104"],
        progress=SeasonProgressStore(
            source="equity_change", path=updater.equity_change_progress_path
        ),
        stop=stop,
        failed_files=[],
    )

    assert stats.stopped is True
    # 收到訊號後不再問下一檔
    assert crawler.requested == ["1101", "1102"]
    # 而已經問到的兩檔都進了資料庫（批次大小是 100，靠的是收尾那次入庫）
    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "1102"}
    conn.close()


def test_unexpected_error_still_flushes_pending_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    往上拋的例外也要先把手上那批寫下來

    請求成本已經付出去了；例外該往上傳（這裡走的是斷路器那條路），
    但不該連帶把已爬好的資料丟掉。
    """

    monkeypatch.setattr(
        FinancialStatementUpdater, "EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS", 1
    )

    conn: sqlite3.Connection = make_db(tmp_path)

    def explode(stock_id: str) -> None:
        if stock_id == "1103":
            raise RuntimeError("連線池炸了")

    crawler: FakeCrawler = FakeCrawler(on_request=explode)
    loader: RecordingLoader = RecordingLoader(conn)
    updater: FinancialStatementUpdater = make_updater(tmp_path, conn, crawler, loader)

    with pytest.raises(RuntimeError):
        updater.update_equity_changes_season(
            year=2013,
            season=1,
            stock_ids=["1101", "1102", "1103"],
            progress=SeasonProgressStore(
                source="equity_change", path=updater.equity_change_progress_path
            ),
            stop=GracefulStop(label="test"),
            failed_files=[],
        )

    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "1102"}
    conn.close()


def test_rerun_after_stop_only_fills_the_gap(tmp_path: Path) -> None:
    """中斷後重跑的成本等於「還沒問到的部分」，不是整季重來"""

    conn: sqlite3.Connection = make_db(tmp_path)
    target: List[str] = ["1101", "1102", "1103", "1104"]

    stop: GracefulStop = GracefulStop(label="test")
    first_crawler: FakeCrawler = FakeCrawler(
        on_request=lambda stock_id: stop.request("test") if stock_id == "1102" else None
    )
    first: FinancialStatementUpdater = make_updater(
        tmp_path, conn, first_crawler, RecordingLoader(conn)
    )
    first.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=target,
        progress=SeasonProgressStore(
            source="equity_change", path=first.equity_change_progress_path
        ),
        stop=stop,
        failed_files=[],
    )

    second_crawler: FakeCrawler = FakeCrawler()
    second: FinancialStatementUpdater = make_updater(
        tmp_path, conn, second_crawler, RecordingLoader(conn)
    )
    second.update_equity_changes(
        start_year=2013,
        end_year=2013,
        start_season=1,
        end_season=1,
        stock_ids=target,
    )

    # 已入庫的兩檔不重打（該年季已有資料，連 is_season_filed 的試探都省下）
    assert second_crawler.requested == ["1103", "1104"]
    assert crawled_stock_ids(conn, 2013, 1) == set(target)
    conn.close()


# === 「查無資料」不重打 ===
def test_no_data_stocks_are_not_requested_again(tmp_path: Path) -> None:
    """
    確認沒有資料的公司，下次重跑不再問

    2020Q1 全市場有 343 檔（16%）當時尚未上市，中斷重跑是常態，
    這些無效請求會一再累積。
    """

    conn: sqlite3.Connection = make_db(tmp_path)
    target: List[str] = ["1101", "1102"]
    progress_path: Path = tmp_path / "equity_change_progress.json"

    first_crawler: FakeCrawler = FakeCrawler(responses={"1102": []})
    first: FinancialStatementUpdater = make_updater(
        tmp_path, conn, first_crawler, RecordingLoader(conn)
    )
    first.equity_change_progress_path = progress_path
    first.update_equity_changes(2013, 2013, 1, 1, stock_ids=target)

    assert "1102" in first_crawler.requested

    second_crawler: FakeCrawler = FakeCrawler(responses={"1102": []})
    second: FinancialStatementUpdater = make_updater(
        tmp_path, conn, second_crawler, RecordingLoader(conn)
    )
    second.equity_change_progress_path = progress_path
    second.update_equity_changes(2013, 2013, 1, 1, stock_ids=target)

    # 整季已無待補（1101 在表內、1102 確認沒資料），連一次請求都不該有
    assert second_crawler.requested == []
    conn.close()


def test_unreachable_stocks_are_retried_on_rerun(tmp_path: Path) -> None:
    """站方過載不是「這檔沒資料」：下次一定要再問"""

    conn: sqlite3.Connection = make_db(tmp_path)
    target: List[str] = ["1101", "2603"]
    progress_path: Path = tmp_path / "equity_change_progress.json"

    first: FinancialStatementUpdater = make_updater(
        tmp_path, conn, FakeCrawler(responses={"2603": None}), RecordingLoader(conn)
    )
    first.equity_change_progress_path = progress_path
    first.update_equity_changes(2013, 2013, 1, 1, stock_ids=target)

    store: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=progress_path
    )
    assert store.get_incomplete(2013, 1) == {"2603"}

    second_crawler: FakeCrawler = FakeCrawler()
    second: FinancialStatementUpdater = make_updater(
        tmp_path, conn, second_crawler, RecordingLoader(conn)
    )
    second.equity_change_progress_path = progress_path
    second.update_equity_changes(2013, 2013, 1, 1, stock_ids=target)

    assert second_crawler.requested == ["2603"]
    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "2603"}
    conn.close()


# === 磁碟對帳：CSV 已落地、還沒入庫 ===
def test_orphan_csv_is_loaded_instead_of_recrawled(tmp_path: Path) -> None:
    """
    落地與入庫是兩步，中間被中止會留下沒入庫的 CSV

    resume 的依據是資料表，於是那一批（最多 100 檔）會被當成「還沒爬」
    再打一次——已經付過的請求成本白付。
    """

    conn: sqlite3.Connection = make_db(tmp_path)
    crawler: FakeCrawler = FakeCrawler()
    loader: RecordingLoader = RecordingLoader(conn)
    updater: FinancialStatementUpdater = make_updater(tmp_path, conn, crawler, loader)

    # 模擬「CSV 寫完、入庫前被中止」的殘留檔
    long_table_row(2013, 1, "1101").to_csv(
        updater.equity_change_dir / "equity_change_2013Q1_0000.csv", index=False
    )

    updater.update_equity_changes(2013, 2013, 1, 1, stock_ids=["1101", "1102"])

    # 1101 由磁碟補回，只有 1102 需要請求
    assert crawler.requested == ["1102"]
    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "1102"}
    conn.close()


def test_reconcile_is_idempotent_on_complete_season(tmp_path: Path) -> None:
    """整季已完成時不該再掃磁碟：2020Q1 有 16 個檔、23 萬列"""

    conn: sqlite3.Connection = make_db(tmp_path)
    loader: RecordingLoader = RecordingLoader(conn)
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, FakeCrawler(), loader
    )
    long_table_row(2013, 1, "1101").to_csv(
        updater.equity_change_dir / "equity_change_2013Q1_0000.csv", index=False
    )
    conn.execute(
        "INSERT INTO equity_change VALUES (2013, 1, '1101', '普通股股本', '期初餘額', 1000.0)"
    )
    conn.commit()

    updater.update_equity_changes(2013, 2013, 1, 1, stock_ids=["1101"])

    assert loader.loaded == []
    conn.close()


# === 失敗隔離：不中止其餘回補，但要浮出來 ===
def test_clean_failure_does_not_abort_the_season(tmp_path: Path) -> None:
    """一頁版面異常不該中止十萬次請求的回補，但那一檔要留待重試"""

    conn: sqlite3.Connection = make_db(tmp_path)
    crawler: FakeCrawler = FakeCrawler()
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, crawler, RecordingLoader(conn), clean_raises_for={"1102"}
    )
    progress: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=updater.equity_change_progress_path
    )

    stats = updater.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=["1101", "1102", "1103"],
        progress=progress,
        stop=GracefulStop(label="test"),
        failed_files=[],
    )

    assert stats.requested == 3
    assert stats.clean_failed == 1
    assert progress.get_incomplete(2013, 1) == {"1102"}
    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "1103"}
    conn.close()


def test_batch_load_failure_does_not_abort_but_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    單批入庫失敗不中止回補，但整段跑完必須讓行程非零結束

    「`except` 之後不吭聲」是本專案實際出過事的樣式：
    2 個檔案入庫失敗、行程仍回報成功，缺的 1,553 列是事後對帳才發現的。
    """

    monkeypatch.setattr(FinancialStatementUpdater, "EQUITY_CHANGE_LOAD_BATCH_SIZE", 2)

    conn: sqlite3.Connection = make_db(tmp_path)
    target: List[str] = ["1101", "1102", "1103", "1104"]
    crawler: FakeCrawler = FakeCrawler()
    loader: RecordingLoader = RecordingLoader(
        conn, fail_file_names={"equity_change_2013Q1_0000.csv"}
    )
    updater: FinancialStatementUpdater = make_updater(tmp_path, conn, crawler, loader)

    with pytest.raises(DataLoadError) as error:
        updater.update_equity_changes(2013, 2013, 1, 1, stock_ids=target)

    # 壞批次沒有中止其餘的爬取（requested 開頭是 is_season_filed 的試探標的）
    assert [stock_id for stock_id in crawler.requested if stock_id in target] == target
    assert crawled_stock_ids(conn, 2013, 1) == {"1103", "1104"}
    assert len(error.value.failed_files) == 1
    conn.close()


def test_season_not_filed_is_skipped_without_full_sweep(tmp_path: Path) -> None:
    """
    尚未申報的年季只花試探的請求，不掃全市場

    但判斷依據必須是長期上市的權值股，不能是「連續 N 檔查無資料」——
    後者曾讓 2020Q1 少抓 323 檔。
    """

    conn: sqlite3.Connection = make_db(tmp_path)
    crawler: FakeCrawler = FakeCrawler(
        responses={stock_id: [] for stock_id in ("2330", "2317", "1101", "9999")}
    )
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, crawler, RecordingLoader(conn)
    )

    updater.update_equity_changes(2013, 2013, 1, 1, stock_ids=["9999"])

    assert crawler.requested == list(
        FinancialStatementUpdater.EQUITY_CHANGE_PROBE_STOCK_IDS
    )
    assert crawled_stock_ids(conn, 2013, 1) == set()
    conn.close()


def test_cleaned_empty_is_counted_separately(tmp_path: Path) -> None:
    """
    「抓到了卻清成空表」必須自成一個數字

    2026-09-03 的 2020Q2 全市場回補打了 2,087 次請求、跑 1.5 小時、入庫 0 列，
    而統計行是 `2087 requested, 244 no data, 0 unreachable`——三個數字全都正常。
    原因是統計只數請求層的結果，1,843 檔清成空表完全不顯示。
    """

    conn: sqlite3.Connection = make_db(tmp_path)
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, FakeCrawler(), RecordingLoader(conn)
    )
    updater.cleaner.clean_equity_changes = lambda df_list, year, season, stock_id: (
        pd.DataFrame(columns=EQUITY_CHANGE_COLUMNS)
    )
    progress: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=updater.equity_change_progress_path
    )

    stats = updater.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=["1101", "1102"],
        progress=progress,
        stop=GracefulStop(label="test"),
        failed_files=[],
    )

    assert stats.cleaned_empty == 2
    assert stats.ok == 0
    assert "2 cleaned empty" in stats.summary_line(2013, 1, pending=2)
    # 清成空表不是「沒有資料」：下次一定要再問，不能寫進永久名單
    assert progress.get_incomplete(2013, 1) == {"1101", "1102"}
    assert progress.get_no_data(2013, 1) == set()
    conn.close()


# === 爬取層的例外隔離 ===
def test_crawl_error_does_not_abort_the_season(tmp_path: Path) -> None:
    """
    一頁的例外不該炸掉幾十小時的回補

    2026-09-04 的整段回補實際炸在這裡：某一檔的頁面讓 lxml 解不動，
    `pd.read_html` 回退到 bs4 flavor 時發現缺 html5lib 而拋 ImportError，
    一路炸穿整個回補（當時已跑完 2013Q1、正在 2013Q2 的第 5 檔）。
    """

    conn: sqlite3.Connection = make_db(tmp_path)

    def explode(stock_id: str) -> None:
        if stock_id == "1102":
            raise ImportError("`Import html5lib` failed.")

    crawler: FakeCrawler = FakeCrawler(on_request=explode)
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, crawler, RecordingLoader(conn)
    )
    progress: SeasonProgressStore = SeasonProgressStore(
        source="equity_change", path=updater.equity_change_progress_path
    )

    stats = updater.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=["1101", "1102", "1103"],
        progress=progress,
        stop=GracefulStop(label="test"),
        failed_files=[],
    )

    assert stats.requested == 3
    assert stats.unreachable == 1
    # 例外的那檔算「沒問到」，下次重試；不能寫進查無資料的永久名單
    assert progress.get_incomplete(2013, 1) == {"1102"}
    assert crawled_stock_ids(conn, 2013, 1) == {"1101", "1103"}
    conn.close()


def test_consecutive_crawl_errors_abort_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    但**連續**的例外不是「某一頁怪」，是環境或程式壞了，要停下來

    隔離若沒有上限，缺套件之類的問題會用 2 秒/檔的速度把幾十小時的回補
    變成一長串失敗。注意這與舊版「連續 N 檔查無資料就早退」不同：
    那裡拿正常結果當統計樣本，這裡數的是例外。
    """

    monkeypatch.setattr(
        FinancialStatementUpdater, "EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS", 3
    )

    conn: sqlite3.Connection = make_db(tmp_path)

    def always_explode(stock_id: str) -> None:
        raise ImportError("`Import html5lib` failed.")

    crawler: FakeCrawler = FakeCrawler(on_request=always_explode)
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, crawler, RecordingLoader(conn)
    )

    with pytest.raises(ImportError):
        updater.update_equity_changes_season(
            year=2013,
            season=1,
            stock_ids=["1101", "1102", "1103", "1104", "1105"],
            progress=SeasonProgressStore(
                source="equity_change", path=updater.equity_change_progress_path
            ),
            stop=GracefulStop(label="test"),
            failed_files=[],
        )

    # 第 3 檔就停，不會把整份清單跑完
    assert crawler.requested == ["1101", "1102", "1103"]
    conn.close()


def test_isolated_crawl_errors_do_not_trip_the_breaker(tmp_path: Path) -> None:
    """零星的例外不該累積成中止：斷路器只認**連續**"""

    conn: sqlite3.Connection = make_db(tmp_path)

    def explode_every_other(stock_id: str) -> None:
        if stock_id in {"1101", "1103", "1105"}:
            raise ValueError("版面異常")

    crawler: FakeCrawler = FakeCrawler(on_request=explode_every_other)
    updater: FinancialStatementUpdater = make_updater(
        tmp_path, conn, crawler, RecordingLoader(conn)
    )

    stats = updater.update_equity_changes_season(
        year=2013,
        season=1,
        stock_ids=["1101", "1102", "1103", "1104", "1105"],
        progress=SeasonProgressStore(
            source="equity_change", path=updater.equity_change_progress_path
        ),
        stop=GracefulStop(label="test"),
        failed_files=[],
    )

    assert stats.requested == 5
    assert stats.unreachable == 3
    assert crawled_stock_ids(conn, 2013, 1) == {"1102", "1104"}
    conn.close()
