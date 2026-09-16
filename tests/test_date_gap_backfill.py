import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import List, Set, Tuple

import pytest

from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared import date_planner as date_planner_module
from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater
from core.utils import TimeUtils

"""
缺口一定要補得回來

舊做法是 `MAX(date) + 1`，於是中間缺的日子永遠不會再被嘗試：
某天因為連線失敗沒抓到，隔天照樣從新的 `MAX(date)+1` 起跑，那個洞就留在資料庫裡，
而回測遇到缺日會當成休市靜默跳過。
"""


def make_table(conn: sqlite3.Connection, dates: List[str]) -> None:
    """建立一張只有 date 欄的資料表並填入指定日期"""

    conn.execute("CREATE TABLE IF NOT EXISTS price (date TEXT, stock_id TEXT)")
    conn.executemany(
        "INSERT INTO price (date, stock_id) VALUES (?, '2330')",
        [(date,) for date in dates],
    )
    conn.commit()


# === 缺口偵測 ===
def test_middle_gap_is_planned_again(tmp_path: Path) -> None:
    """
    S4 的驗收點：刪掉中間一天，該日必須重新進入候選

    這正是「刪掉 `price` 表任一天，`--target price` 會回補該日」的實驗。
    """

    conn = sqlite3.connect(tmp_path / "test.db")
    # 2024-01-01（一）~ 01-05（五）皆為平日，刻意缺 01-03
    make_table(conn, ["2024-01-01", "2024-01-02", "2024-01-04", "2024-01-05"])

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockPriceDAO(conn=conn),
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 1, 5),
    )

    assert datetime.date(2024, 1, 3) in candidates
    conn.close()


def test_existing_dates_are_not_requested_again(tmp_path: Path) -> None:
    """已有的日期不重抓，否則每晚都會重跑整段歷史"""

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, ["2024-01-01", "2024-01-02"])

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockPriceDAO(conn=conn),
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 1, 2),
    )

    assert candidates == []
    conn.close()


def test_weekends_are_excluded_by_default(tmp_path: Path) -> None:
    """沒有日曆時以平日為母集合；週末不送請求"""

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, [])

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockPriceDAO(conn=conn),
        start_date=datetime.date(2024, 1, 6),  # 週六
        end_date=datetime.date(2024, 1, 7),  # 週日
    )

    assert candidates == []
    conn.close()


def test_confirmed_no_data_dates_are_skipped(tmp_path: Path) -> None:
    """
    已確認沒有資料的日期不再重問

    國定假日只需要問一次；不記下來的話每晚都會為了 13 年份的假日多打幾百次請求。
    """

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, [])

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockPriceDAO(conn=conn),
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 1, 2),
        no_data_dates={datetime.date(2024, 1, 1)},
    )

    assert candidates == [datetime.date(2024, 1, 2)]
    conn.close()


def test_calendar_dates_cover_traded_weekends(tmp_path: Path) -> None:
    """
    以 `price` 表為日曆時，補行交易日（開市的週六）必須被納入

    用「非週末」近似會漏掉 2013 年以來的 11 個補行交易日，那幾天整天沒有資料。
    """

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, ["2024-01-06"])  # 週六補行交易日

    calendar: Set[datetime.date] = DatePlanner.get_trading_dates(
        StockPriceDAO(conn=conn), datetime.date(2024, 1, 1), datetime.date(2024, 1, 6)
    )
    conn.execute("CREATE TABLE margin (date TEXT)")
    conn.commit()

    # 迄日就是日曆最後一天，故不會觸發尾端補平日（那條另有測試）
    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockMarginDAO(conn=conn),
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 1, 6),
        calendar_dates=calendar,
    )

    assert candidates == [datetime.date(2024, 1, 6)]
    conn.close()


def test_price_requests_traded_weekends_known_to_other_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `chip` 有、`price` 沒有的補行交易日，`price` 要重新請求

    `price` 自己就是交易日曆，母集合只能是平日；沒有這條，被刪掉的補行交易日
    （例如 2017-06-03）永遠不會再被請求，只能手動重爬。
    """

    monkeypatch.setattr(date_planner_module, "DOWNLOADS_METADATA_DIR_PATH", tmp_path)

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, ["2017-06-02"])  # 週五有、週六補行交易日缺
    conn.execute("CREATE TABLE chip (date TEXT, stock_id TEXT)")
    conn.executemany(
        "INSERT INTO chip VALUES (?, '2330')", [("2017-06-02",), ("2017-06-03",)]
    )
    conn.commit()

    requested: List[datetime.date] = []

    def crawl(date: datetime.date) -> CrawlResult:
        requested.append(date)
        return CrawlResult.no_data("站方回覆查無資料")

    updater = StockPriceUpdater.__new__(StockPriceUpdater)  # 跳過 __init__ 的連線
    updater.dao = StockPriceDAO(conn=conn)
    updater.conn = conn
    updater.crawler = SimpleNamespace(crawl_twse_price=crawl, crawl_tpex_price=crawl)
    updater.loader = SimpleNamespace(add_to_db=lambda **_: None)
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0

    updater.update(
        start_date=datetime.date(2017, 6, 1), end_date=datetime.date(2017, 6, 4)
    )

    # 06-01（四）不在表內、06-03（六）由 chip 補回；06-04（日）沒有任何表有
    assert sorted(set(requested)) == [
        datetime.date(2017, 6, 1),
        datetime.date(2017, 6, 3),
    ]
    conn.close()


def test_weekend_dates_ignore_missing_tables(tmp_path: Path) -> None:
    """來源表不存在時視為沒有週末日期，不可拋錯（margin 表可能還沒建）"""

    conn = sqlite3.connect(tmp_path / "test.db")

    assert (
        DatePlanner.get_weekend_dates(
            [StockChipDAO(conn=conn), StockMarginDAO(conn=conn)],
            datetime.date(2017, 6, 1),
            datetime.date(2017, 6, 4),
        )
        == set()
    )
    conn.close()


def test_missing_table_is_not_an_error(tmp_path: Path) -> None:
    """初次更新時目標表還不存在，應視為「全部都要抓」"""

    conn = sqlite3.connect(tmp_path / "test.db")

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockChipDAO(conn=conn),
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, 1, 2),
    )

    assert candidates == [datetime.date(2024, 1, 1), datetime.date(2024, 1, 2)]
    conn.close()


# === 進度紀錄的持久化 ===
def test_progress_store_round_trip(tmp_path: Path) -> None:
    """寫入後再讀回要拿到同一組日期"""

    path: Path = tmp_path / "price_date_progress.json"
    store: DateProgressStore = DateProgressStore("price", path=path)
    store.record_no_data(datetime.date(2024, 1, 1), today=datetime.date(2024, 3, 1))
    store.record_no_data(datetime.date(2024, 2, 28), today=datetime.date(2024, 3, 1))
    store.record_incomplete(datetime.date(2024, 2, 29))
    store.save()

    reloaded: DateProgressStore = DateProgressStore("price", path=path)

    assert reloaded.no_data == {datetime.date(2024, 1, 1), datetime.date(2024, 2, 28)}
    assert reloaded.incomplete == {datetime.date(2024, 2, 29)}


def test_corrupt_progress_store_is_treated_as_empty(tmp_path: Path) -> None:
    """
    檔案損毀時當成空集合

    最壞的結果只是多問幾次；把「沒問到」誤認成「問過了沒有」才是真正的風險。
    """

    path: Path = tmp_path / "price_date_progress.json"
    path.write_text("{ not json")

    store: DateProgressStore = DateProgressStore("price", path=path)

    assert store.no_data == set()
    assert store.incomplete == set()


def test_today_is_never_written_to_the_permanent_no_data_list(tmp_path: Path) -> None:
    """
    盤中跑一次更新，不可把今天永久列為「沒有資料」

    `NO_DATA` 同時代表「休市」與「盤後尚未公布」，兩者在回應上無法區分。
    寫進去的話，收盤後那天的資料**再也不會被抓**。
    """

    today: datetime.date = datetime.date(2024, 1, 10)
    store: DateProgressStore = DateProgressStore("price", path=tmp_path / "p.json")

    store.record_no_data(today, today=today)

    assert store.no_data == set()


def test_incomplete_day_is_requested_again_even_though_data_exists(
    tmp_path: Path,
) -> None:
    """
    同一天的多個來源只成功了一半時，下次一定要重來

    price／chip／margin 每天都打上市與上櫃兩次請求。上市成功、上櫃失敗時，
    上市那批已經進了資料表——差集會把這天當成「已經有了」而排除，
    **上櫃那半永遠補不回來**（本檔存在的最主要理由）。
    """

    conn = sqlite3.connect(tmp_path / "test.db")
    make_table(conn, ["2024-01-02"])  # 只有上市那半入庫

    candidates: List[datetime.date] = DatePlanner.plan(
        dao=StockPriceDAO(conn=conn),
        start_date=datetime.date(2024, 1, 2),
        end_date=datetime.date(2024, 1, 2),
        incomplete_dates={datetime.date(2024, 1, 2)},
    )

    assert candidates == [datetime.date(2024, 1, 2)]
    conn.close()


def test_progress_record_dispatches_on_status(tmp_path: Path) -> None:
    """`UpdateStats.record()` 的回傳值可以直接餵進進度紀錄"""

    today: datetime.date = datetime.date(2024, 3, 1)
    store: DateProgressStore = DateProgressStore("price", path=tmp_path / "p.json")

    store.record(datetime.date(2024, 1, 2), CrawlStatus.FAILED, today=today)
    store.record(datetime.date(2024, 1, 3), CrawlStatus.NO_DATA, today=today)
    store.record(datetime.date(2024, 1, 4), CrawlStatus.OK, today=today)

    assert store.incomplete == {datetime.date(2024, 1, 2)}
    assert store.no_data == {datetime.date(2024, 1, 3)}


def test_success_clears_a_previous_retry_mark(tmp_path: Path) -> None:
    """補成功之後就不該再重試"""

    store: DateProgressStore = DateProgressStore("price", path=tmp_path / "p.json")
    date: datetime.date = datetime.date(2024, 1, 2)

    store.record_incomplete(date)
    store.record_complete(date)

    assert store.incomplete == set()
    assert store.no_data == set()


def test_calendar_tail_is_extended_to_end_date(tmp_path: Path) -> None:
    """
    日曆來源（`price`）落後時，尾端要補上平日

    否則 chip／margin 會**永遠落後 price 一天**：今天不在日曆裡、今天不被請求，
    要等明天那一輪才補上。
    """

    calendar: Set[datetime.date] = {datetime.date(2024, 1, 2)}

    extended: Set[datetime.date] = DatePlanner.extend_calendar_tail(
        calendar, datetime.date(2024, 1, 4)
    )

    assert extended == {
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        datetime.date(2024, 1, 4),
    }


def test_calendar_tail_extension_skips_weekends() -> None:
    """補的是平日，不是每一個曆日"""

    calendar: Set[datetime.date] = {datetime.date(2024, 1, 5)}  # 週五

    extended: Set[datetime.date] = DatePlanner.extend_calendar_tail(
        calendar, datetime.date(2024, 1, 8)
    )

    assert extended == {datetime.date(2024, 1, 5), datetime.date(2024, 1, 8)}


# === 年 × 期的笛卡兒積===
def test_year_season_range_covers_every_quarter() -> None:
    """
    起點 2024Q3、終點 2026Q2 必須涵蓋中間每一季

    舊寫法 `for year in years: for season in seasons` 的 `seasons` 只會是 [3, 4]，
    2025Q1／Q2 與 2026Q1／Q2 整整四季不會被爬，而且不會有任何錯誤。
    """

    result: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
        2024, 3, 2026, 2, periods_per_year=4
    )

    assert result == [
        (2024, 3),
        (2024, 4),
        (2025, 1),
        (2025, 2),
        (2025, 3),
        (2025, 4),
        (2026, 1),
        (2026, 2),
    ]


def test_year_month_range_crosses_the_year_boundary() -> None:
    """月營收同理：跨年時 1~2 月不可被漏掉"""

    result: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
        2025, 11, 2026, 2, periods_per_year=12
    )

    assert result == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_year_period_range_is_empty_when_reversed() -> None:
    """起點晚於終點時回空清單，不可回半組資料"""

    assert TimeUtils.generate_year_period_range(2026, 2, 2025, 1, 4) == []


@pytest.mark.parametrize("period", [0, 5])
def test_year_period_range_rejects_out_of_range_period(period: int) -> None:
    """期別超出範圍要當場拋出，不要安靜地產出錯誤序列"""

    with pytest.raises(ValueError):
        TimeUtils.generate_year_period_range(2024, period, 2025, 1, periods_per_year=4)
