import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.dao.tw.stock_chip_dao import StockChipDAO
from core.dao.tw.stock_margin_dao import StockMarginDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared import date_planner as date_planner_module
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.tw.crawlers import financial_statement_crawler as fs_crawler_module
from core.pipeline.tw.crawlers.financial_statement_crawler import (
    FinancialStatementCrawler,
)
from core.pipeline.tw.updaters.corporate_action_updater import CorporateActionUpdater
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
from core.pipeline.tw.updaters.stock_dividend_updater import StockDividendUpdater
from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater
from core.pipeline.tw.utils.mops_payload import Payload
from core.pipeline.utils import ListingBoard
from core.pipeline.utils.exceptions import CleanFailureError, ColumnLayoutError

"""
多來源拼成的一份資料：要嘛全部入庫、要嘛全部不入庫

台股日頻表是上市＋上櫃兩份拼成一天，財報三表是上市＋上櫃兩份拼成一季。
只入庫問到的那一邊時，資料**看起來是有的**——日期軸正常、年季齊全——
只是少了半個市場，不會有任何錯誤：

- 日頻表：`price` 2026-09-07~09 在 TPEX 回應中斷時只入庫了 TWSE 的 1,350 檔；
  `chip` 2026-09-15 收盤後 TWSE 尚未公布（回覆查無資料），只入庫了 TPEX 的 900 檔。
- 財報：資產負債表 2021Q1 整季缺、現金流量表 2024Q1 上市只剩 1 檔，而且年季續跑
  是 `MAX + 1`，之後永遠不回頭。

本檔不連網路、不碰正式 DB。
"""


DAY_OK: datetime.date = datetime.date(2024, 1, 2)
DAY_PARTIAL: datetime.date = datetime.date(2024, 1, 3)

# 三支日頻 updater 的差別只有方法名稱後綴與進度檔名稱
DAILY_UPDATERS: List[Tuple[Type[BaseDataUpdater], str]] = [
    (StockPriceUpdater, "price"),
    (StockChipUpdater, "chip"),
    (StockMarginUpdater, "margin"),
]

# 各日頻 updater 持有的 DAO；`__new__` 跳過 `__init__` 時要自行注入
DAILY_DAOS: Dict[str, Type[BaseDAO]] = {
    "price": StockPriceDAO,
    "chip": StockChipDAO,
    "margin": StockMarginDAO,
}


def raw_table() -> pd.DataFrame:
    """crawler 的原始表格；內容不重要，列數要超過 price updater 的最小列數"""

    return pd.DataFrame({"證券代號": ["2330", "2317", "1101"]})


def make_daily_updater(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]],
    dao_factory: Callable[..., BaseDAO],
) -> Tuple[Any, List[Set[str]], List[Tuple[str, datetime.date]]]:
    """
    - Description:
        建一支不連網路、不碰正式 DB 的日頻 updater
    - Parameters:
        - updater_cls: Type[BaseDataUpdater]
            待測的 updater 類別
        - kind: str
            `price`／`chip`／`margin`
        - tmp_path: Path
            暫存目錄
        - monkeypatch: pytest.MonkeyPatch
            用來把進度檔改寫到暫存目錄
        - results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]]
            各日期的（TWSE, TPEX）爬取結果
        - dao_factory: Callable[..., BaseDAO]
            以正式 schema 建表的 DAO 工廠
    - Return:
        - Tuple
            （updater, 每次入庫的日期集合, 清洗呼叫紀錄）
    """

    monkeypatch.setattr(date_planner_module, "DOWNLOADS_METADATA_DIR_PATH", tmp_path)

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    # chip／margin 以 price 為交易日曆；price 自己則以平日為母集合，表內不可先有這兩天
    dao_factory(
        StockPriceDAO,
        records=(
            []
            if kind == "price"
            else [
                {"date": DAY_OK.isoformat(), "stock_id": "2330"},
                {"date": DAY_PARTIAL.isoformat(), "stock_id": "2330"},
            ]
        ),
        conn=conn,
    )

    loaded: List[Set[str]] = []
    cleaned: List[Tuple[str, datetime.date]] = []

    def make_clean(market: str):
        def clean(raw: pd.DataFrame, date: datetime.date) -> pd.DataFrame:
            cleaned.append((market, date))
            return raw

        return clean

    updater = updater_cls.__new__(updater_cls)  # 跳過 __init__ 的正式連線與 log 設定
    updater.dao = DAILY_DAOS[kind](conn=conn)
    updater.conn = conn
    updater.crawler = SimpleNamespace(
        **{
            f"crawl_twse_{kind}": lambda date: results[date][0],
            f"crawl_tpex_{kind}": lambda date: results[date][1],
        }
    )
    updater.cleaner = SimpleNamespace(
        **{
            f"clean_twse_{kind}": make_clean("TWSE"),
            f"clean_tpex_{kind}": make_clean("TPEX"),
        }
    )
    updater.loader = SimpleNamespace(
        add_to_db=lambda remove_files, only_dates: loaded.append(set(only_dates))
    )
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0

    return updater, loaded, cleaned


def loaded_dates(loaded: List[Set[str]]) -> Set[str]:
    """所有入庫批次的日期聯集"""

    return set().union(*loaded) if loaded else set()


# === 日頻三表 ===
@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
def test_day_with_a_failed_market_is_not_loaded(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    上櫃沒問到的那天，上市那半也不能入庫

    舊行為是上市照常清洗入庫、只把該日記成待重試：最終補得齊，但重試成功之前
    回測讀到的是半個市場。
    """

    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (
            CrawlResult.ok(raw_table()),
            CrawlResult.failed("blocked: IncompleteRead"),
        ),
    }
    updater, loaded, cleaned = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    # 反正不入庫，連清洗都不做，downloads 才不會留下半份 CSV
    assert ("TWSE", DAY_PARTIAL) not in cleaned
    progress: DateProgressStore = DateProgressStore(kind)
    assert progress.incomplete == {DAY_PARTIAL}


@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
def test_day_with_a_failed_cleaner_is_not_loaded(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    兩邊都爬到、但上櫃清洗失敗時同樣整天不入庫

    上市那半在清洗時已經寫出 CSV，只擋「爬取失敗」不擋這條的話，它照樣會被入庫。
    """

    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
    }
    updater, loaded, _ = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )
    # 只讓第二天的上櫃清洗失敗
    clean_tpex = getattr(updater.cleaner, f"clean_tpex_{kind}")

    def flaky_clean(raw: pd.DataFrame, date: datetime.date) -> pd.DataFrame:
        if date == DAY_PARTIAL:
            raise ValueError("版面異常")
        return clean_tpex(raw, date)

    setattr(updater.cleaner, f"clean_tpex_{kind}", flaky_clean)

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    assert DateProgressStore(kind).incomplete == {DAY_PARTIAL}


@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
def test_day_with_an_empty_cleaner_result_is_not_loaded(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    清洗回 `None`（版面不符、過濾後無有效列）與拋例外同樣算整天失敗

    舊行為只記一行 warning 就回報成功：站方明明有回資料、清洗後一列都不剩，
    當天卻被記為完成，之後差集判定表內已有這天，永遠不會回頭補。
    """

    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
    }
    updater, loaded, _ = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )

    def empty_clean(raw: pd.DataFrame, date: datetime.date) -> Optional[pd.DataFrame]:
        return None if date == DAY_PARTIAL else raw

    setattr(updater.cleaner, f"clean_tpex_{kind}", empty_clean)

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    assert DateProgressStore(kind).incomplete == {DAY_PARTIAL}


def test_price_day_with_too_few_raw_rows_is_not_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    原始表列數不足門檻時整天不入庫

    `price` 對列數過少的原始表跳過清洗（只剩表頭或合計列時硬清會炸），舊行為
    跳過之後照常入庫另一個市場，於是當天只有半個市場、且被記為完成。
    """

    tiny: pd.DataFrame = pd.DataFrame({"證券代號": ["2330"]})
    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (CrawlResult.ok(tiny), CrawlResult.ok(raw_table())),
    }
    updater, loaded, cleaned = make_daily_updater(
        StockPriceUpdater, "price", tmp_path, monkeypatch, results, dao_factory
    )

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    assert ("TWSE", DAY_PARTIAL) not in cleaned
    assert DateProgressStore("price").incomplete == {DAY_PARTIAL}


@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
@pytest.mark.parametrize(
    "no_data_market", ["TWSE", "TPEX"], ids=["twse_no_data", "tpex_no_data"]
)
def test_day_with_one_market_no_data_is_not_loaded(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    no_data_market: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    一邊回覆查無資料、另一邊有資料的那天，同樣整天不入庫

    站方的「查無資料」涵蓋「尚未公布」：兩個市場公布時間不同，收盤後先公布的
    那一邊會單獨入庫。舊行為把這種組合當成正常日，於是當日只有半個市場。
    """

    ok: CrawlResult = CrawlResult.ok(raw_table())
    no_data: CrawlResult = CrawlResult.no_data("站方回覆查無資料（休市或尚未公布）")
    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (no_data, ok) if no_data_market == "TWSE" else (ok, no_data),
    }
    updater, loaded, cleaned = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102"}
    # 有資料的那一邊也不清洗，downloads 才不會留下半份 CSV
    assert [date for _, date in cleaned if date == DAY_PARTIAL] == []
    progress: DateProgressStore = DateProgressStore(kind)
    assert progress.incomplete == {DAY_PARTIAL}
    assert DAY_PARTIAL not in progress.no_data


@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
def test_complete_days_are_still_loaded(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """防止改過頭：兩個市場都問到，照常入庫"""

    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
    }
    updater, loaded, _ = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    assert loaded_dates(loaded) == {"20240102", "20240103"}
    assert DateProgressStore(kind).incomplete == set()


@pytest.mark.parametrize(("updater_cls", "kind"), DAILY_UPDATERS)
def test_day_with_both_markets_no_data_is_holiday(
    updater_cls: Type[BaseDataUpdater],
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """防止改過頭：兩個市場都回覆查無資料仍記為休市，不列入待重試"""

    results: Dict[datetime.date, Tuple[CrawlResult, CrawlResult]] = {
        DAY_OK: (CrawlResult.ok(raw_table()), CrawlResult.ok(raw_table())),
        DAY_PARTIAL: (
            CrawlResult.no_data("站方回覆查無資料"),
            CrawlResult.no_data("站方回覆查無資料"),
        ),
    }
    updater, _, _ = make_daily_updater(
        updater_cls, kind, tmp_path, monkeypatch, results, dao_factory
    )

    updater.update(start_date=DAY_OK, end_date=DAY_PARTIAL)

    progress: DateProgressStore = DateProgressStore(kind)
    assert progress.no_data == {DAY_PARTIAL}
    assert progress.incomplete == set()


# === 財報三表：crawler ===
TABLE_HTML: str = "<table><tr><th>公司代號</th></tr><tr><td>2330</td></tr></table>"


def make_fs_crawler(
    monkeypatch: pytest.MonkeyPatch, otc_response: Any
) -> FinancialStatementCrawler:
    """
    - Description:
        建一支不連網路的財報 crawler：上市一律回表格，上櫃的回應由參數決定
    - Parameters:
        - monkeypatch: pytest.MonkeyPatch
            用來替換 `RequestUtils.requests_post`
        - otc_response: Any
            上櫃的回應；為 Exception 實例時改為拋出
    - Return:
        - FinancialStatementCrawler
    """

    def fake_post(url: str, data: Dict[str, str]) -> Any:
        if data.get("TYPEK") == ListingBoard.SII.value:
            return SimpleNamespace(text=TABLE_HTML)
        if isinstance(otc_response, Exception):
            raise otc_response
        return otc_response

    monkeypatch.setattr(fs_crawler_module.RequestUtils, "requests_post", fake_post)

    crawler = FinancialStatementCrawler.__new__(FinancialStatementCrawler)
    crawler.payload = Payload(
        firstin="1", step="1", TYPEK="sii", co_id=None, year="113", season="1"
    )
    crawler.listing_boards = [ListingBoard.SII, ListingBoard.OTC]
    return crawler


@pytest.mark.parametrize(
    "crawl_name",
    ["crawl_balance_sheet", "crawl_comprehensive_income", "crawl_cash_flow"],
)
@pytest.mark.parametrize(
    "otc_response",
    [
        None,  # requests_post 對 HTTP 失敗、重試耗盡一律回 None
        ConnectionError("RemoteDisconnected"),
        SimpleNamespace(text="<html>查詢過於頻繁，請稍後再試</html>"),
    ],
    ids=["no_response", "raised", "no_table"],
)
def test_fs_crawler_fails_the_whole_season_when_one_market_fails(
    monkeypatch: pytest.MonkeyPatch, crawl_name: str, otc_response: Any
) -> None:
    """
    上櫃沒問到時整季回 None，不可回傳上市那半

    舊行為是 `continue` 之後回傳上市的表格，updater 照常清洗入庫。
    """

    crawler: FinancialStatementCrawler = make_fs_crawler(monkeypatch, otc_response)

    assert getattr(crawler, crawl_name)(2024, 1) is None


def test_fs_crawler_returns_both_markets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防止改過頭：兩個市場都問到時兩份表格都要回傳"""

    crawler: FinancialStatementCrawler = make_fs_crawler(
        monkeypatch, SimpleNamespace(text=TABLE_HTML)
    )

    tables: Optional[List[pd.DataFrame]] = crawler.crawl_balance_sheet(2024, 1)

    assert tables is not None
    assert len(tables) == 2


# === 財報三表：年季續跑 ===
def make_fs_updater(
    tmp_path: Path,
    year_seasons: List[Tuple[int, int]],
    dao_factory: Callable[..., BaseDAO],
) -> FinancialStatementUpdater:
    """建一支只連暫存 DB 的財報 updater，資產負債表預先放入指定年季"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    dao_factory(
        FinancialStatementDAO,
        records=[
            {"year": year, "season": season, "stock_id": "2330", "公司名稱": "台積電"}
            for year, season in year_seasons
        ],
        conn=conn,
        table_name="balance_sheet",
    )

    updater = FinancialStatementUpdater.__new__(FinancialStatementUpdater)
    updater.conn = conn
    return updater


def test_fs_season_gap_is_planned_again(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """
    中間缺的年季要重新進入候選

    舊做法以 `MAX + 1` 起跑：2024Q2 失敗被跳過、2024Q3 成功入庫之後，
    下一輪從 2024Q4 開始，2024Q2 永遠不會再被請求。
    """

    updater: FinancialStatementUpdater = make_fs_updater(
        tmp_path, [(2024, 1), (2024, 3)], dao_factory
    )

    pending: List[Tuple[int, int]] = updater.plan_pending_year_seasons(
        table_name="balance_sheet",
        start_year=2024,
        start_season=1,
        end_year=2024,
        end_season=4,
    )

    assert pending == [(2024, 2), (2024, 4)]


def test_fs_missing_table_plans_every_season(tmp_path: Path) -> None:
    """初次更新時表還不存在，應視為全部都要抓"""

    updater = FinancialStatementUpdater.__new__(FinancialStatementUpdater)
    updater.conn = sqlite3.connect(tmp_path / "test.db")

    pending: List[Tuple[int, int]] = updater.plan_pending_year_seasons(
        table_name="balance_sheet",
        start_year=2024,
        start_season=3,
        end_year=2025,
        end_season=1,
    )

    assert pending == [(2024, 3), (2024, 4), (2025, 1)]


# === 年度來源（除權息、公司行動）===
def make_yearly_updater(
    updater_cls: Type[BaseDataUpdater],
    crawl_names: Tuple[str, str],
    clean: Any,
    tmp_path: Path,
    dao_factory: Callable[..., BaseDAO],
) -> Any:
    """建一支不連網路、不碰正式 DB 的年度 updater（除權息／公司行動共用）"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    updater = updater_cls.__new__(updater_cls)  # 跳過 __init__ 的正式連線與 log 設定
    updater.dao = SimpleNamespace(get_latest_date=lambda: None)
    updater.conn = conn
    updater.crawler = SimpleNamespace(
        **{name: lambda start, end: CrawlResult.ok(raw_table()) for name in crawl_names}
    )
    updater.cleaner = clean
    updater.loader = SimpleNamespace(add_to_db=lambda **_: None)
    updater.YEAR_REQUEST_DELAY_MIN = 0
    updater.YEAR_REQUEST_DELAY_MAX = 0

    return updater


def test_dividend_layout_change_fails_the_run(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """
    除權息版面改制時整批更新以失敗收尾

    這張表每輪重掃整個區間，所以資料不會永久缺漏——但清洗一直失敗的話，
    它會**無聲地停止更新**，而除權息是還原價的輸入。
    """

    def raise_layout(df: pd.DataFrame, file_name: str) -> pd.DataFrame:
        raise ColumnLayoutError("TWSE dividend", expected=15, actual=14)

    updater = make_yearly_updater(
        StockDividendUpdater,
        ("crawl_twse_dividend", "crawl_tpex_dividend"),
        SimpleNamespace(
            clean_twse_dividend=raise_layout, clean_tpex_dividend=raise_layout
        ),
        tmp_path,
        dao_factory,
    )

    with pytest.raises(CleanFailureError):
        updater.update(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 12, 31)
        )


def test_corporate_action_layout_change_fails_the_run(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """公司行動版面改制時整批更新以失敗收尾（理由同除權息）"""

    def raise_layout(df: pd.DataFrame, source: str, file_name: str) -> pd.DataFrame:
        raise ColumnLayoutError("[corporate_action] twse", expected=11, actual=2)

    updater = make_yearly_updater(
        CorporateActionUpdater,
        ("crawl_twse_capital_reduction", "crawl_tpex_capital_reduction"),
        SimpleNamespace(clean=raise_layout),
        tmp_path,
        dao_factory,
    )

    with pytest.raises(CleanFailureError):
        updater.update(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 12, 31)
        )


# === 申報期內的年季不算收齊 ===
def test_fs_season_inside_the_filing_window_is_planned_again(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """
    申報期還沒關閉的年季要重新進入候選

    財報是逐家公司申報的，申報期內入庫的只是「當下已送件的那批公司」。
    把它當成收齊的話，年季差集從此跳過它，後送件的公司永遠補不進來——
    而每季申報期間跑一次日常更新就會踩到。
    """

    updater: FinancialStatementUpdater = make_fs_updater(
        tmp_path, [(2026, 1), (2026, 2)], dao_factory
    )
    # 把「今天」設在 2026Q2 的申報期內（Q2 期限 8/31 ＋ 30 天寬限）
    updater.is_season_settled = lambda year, season: (year, season) != (2026, 2)

    pending: List[Tuple[int, int]] = updater.plan_pending_year_seasons(
        table_name="balance_sheet",
        start_year=2026,
        start_season=1,
        end_year=2026,
        end_season=2,
    )

    assert pending == [(2026, 2)]


def test_fs_settled_season_is_not_planned_again(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """申報期關閉之後就不再重問（防止改過頭，每輪重爬整段歷史）"""

    updater: FinancialStatementUpdater = make_fs_updater(
        tmp_path, [(2026, 1), (2026, 2)], dao_factory
    )
    updater.is_season_settled = lambda year, season: True

    pending: List[Tuple[int, int]] = updater.plan_pending_year_seasons(
        table_name="balance_sheet",
        start_year=2026,
        start_season=1,
        end_year=2026,
        end_season=2,
    )

    assert pending == []
