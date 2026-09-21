import datetime
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytest

from core.api.tw.futures_chip_api import FuturesChipAPI
from core.config import (
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_LARGE_TRADER_TABLE_NAME,
    FUTURES_PUT_CALL_RATIO_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.base import BaseDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.pipeline.tw.cleaners.futures_chip_cleaner import FuturesChipCleaner
from core.pipeline.tw.crawlers.futures_chip_crawler import FuturesChipCrawler

"""
台期貨籌碼 ETL 與前視偏差測試

**本組資料的核心風險不是抓不到，是「抓到了但用錯時間」**：三大法人、大額交易人
與 PCR **全部是盤後公布**，回測若在當日訊號裡讀到當日籌碼，等於用收盤後才知道的
資訊去下當天的單。那不會報錯，只會讓績效好得不合理——本檔的一半測試在釘這件事。

另外三個來源格式的坑也逐一釘住（都是實測踩到的）：

1. **非交易日回的是 HTTP 200 ＋ 一整頁 HTML**，不是空 CSV 也不是 404。
2. **PCR 每列結尾多一個逗號**，pandas 會把第一欄當索引，整列往左位移且不報錯。
3. **CSV 檔尾有三行說明文字**，會被解析成「主鍵有值、其餘全 NULL」的垃圾列。
"""

DATE: datetime.date = datetime.date(2026, 8, 28)

INSTITUTIONAL_CSV: str = (
    "日期,商品名稱,身份別,多方交易口數,多方未平倉口數,空方未平倉口數,多空未平倉口數淨額\n"
    "2026/08/28,臺股期貨,自營商,3568,3992,3335,657\n"
    "2026/08/28,臺股期貨,外資及陸資,41057,8732,92387,-83655\n"
    "2026/08/28,臺股期貨,合計,44664,92760,99006,-6246\n"
)

LARGE_TRADER_CSV: str = (
    "日期,商品(契約),商品名稱(契約名稱),到期月份(週別),交易人類別,前五大交易人買方,全市場未沖銷部位數\n"
    "2026/08/28,TX     ,臺股期貨,202609  ,0,75412,120000\n"
    "2026/08/28,TX     ,臺股期貨,999999  ,1,30000,120000\n"
    "\n"
    "月份類別格式: 666666為所有週到期契約合計，yyyymm為近月契約，999999 為所有契約合計。\n"
    "交易人類別格式： 0 為部位排序前五大或前十大交易人，1 為其中屬於特定法人者\n"
)

# 每列結尾都多一個逗號——這正是讓 pandas 誤判索引的來源格式
PCR_CSV: str = (
    "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%\n"
    "2026/08/28,308922,306713,100.72,42600,41994,101.44,\n"
)

HTML_RESPONSE: str = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html>\n<head></head>\n'
    "<body>查無資料</body>\n</html>\n"
)


@pytest.fixture
def cleaner() -> FuturesChipCleaner:
    """清洗器"""

    return FuturesChipCleaner()


# === 來源格式的坑 ===
def test_html_response_is_not_treated_as_csv() -> None:
    """
    **非交易日回的是 HTTP 200 ＋ 一整頁 HTML**

    行數檢查完全擋不住（那頁有 19 行），`csv` 解析出來會是一堆亂七八糟的欄位
    而不會報錯。故一律檢查第一行是不是真的 CSV 表頭。
    """

    crawler: FuturesChipCrawler = FuturesChipCrawler()
    lines: List[str] = [line for line in HTML_RESPONSE.splitlines() if line.strip()]

    assert crawler.CSV_HEADER_KEYWORD not in lines[0]


def test_pcr_trailing_comma_does_not_shift_columns(cleaner) -> None:
    """
    **PCR 每列結尾多一個逗號**

    資料欄比表頭多一欄時，pandas 會自作主張把第一欄當索引，整列往左位移——
    賣權成交量變成買權成交量、比率欄變成 NaN，而且完全不會報錯。
    """

    df: pd.DataFrame = cleaner.clean_put_call_ratio(PCR_CSV, DATE)

    assert df.iloc[0]["賣權成交量"] == 308922
    assert df.iloc[0]["買權成交量"] == 306713
    assert df.iloc[0]["買賣權成交量比率%"] == 100.72


def test_footnote_lines_are_dropped(cleaner) -> None:
    """
    **CSV 檔尾的說明文字會被解析成資料列**

    它們沒有逗號分隔，於是變成「第一欄有值、其餘全 NaN」；`date` 又被覆寫成
    查詢日，看起來完全合法。不濾掉就會靠資料庫的 NOT NULL 擋，而
    `INSERT OR IGNORE` 不會為此發出任何訊息。
    """

    df: pd.DataFrame = cleaner.clean_large_trader(LARGE_TRADER_CSV, DATE)

    assert len(df) == 2
    assert df["product"].notna().all()


def test_code_columns_are_stripped(cleaner) -> None:
    """
    代碼欄是補空白的固定寬度格式（`"TX     "`、`"202609  "`）

    不 strip 的話主鍵會多出看不見的字元，join 一定對不上而且查不出原因。
    """

    df: pd.DataFrame = cleaner.clean_large_trader(LARGE_TRADER_CSV, DATE)

    assert set(df["product"]) == {"TX"}
    assert set(df["expiry"]) == {"202609", "999999"}


def test_institutional_keeps_only_the_three_investor_types(cleaner) -> None:
    """
    只留三大法人，濾掉「合計」列

    合計是前三者的加總，留著會讓任何 `SUM()` 都變成兩倍。
    """

    df: pd.DataFrame = cleaner.clean_institutional(INSTITUTIONAL_CSV, DATE)

    assert set(df["investor"]) == {"自營商", "外資及陸資"}
    assert "合計" not in set(df["investor"])


def test_date_is_normalised_to_iso(cleaner) -> None:
    """來源給 `2026/08/28`，入庫一律 ISO 格式（否則字串比較會出錯）"""

    df: pd.DataFrame = cleaner.clean_institutional(INSTITUTIONAL_CSV, DATE)

    assert set(df["date"]) == {"2026-08-28"}


def test_numeric_columns_are_numeric(cleaner) -> None:
    """數值欄要轉成數字；轉不動的留 NaN**不猜 0**"""

    df: pd.DataFrame = cleaner.clean_institutional(INSTITUTIONAL_CSV, DATE)

    assert df.iloc[0]["多空未平倉口數淨額"] == 657
    assert pd.api.types.is_numeric_dtype(df["多方未平倉口數"])


# === 前視偏差對齊 ===
@pytest.fixture
def chip_api(dao_factory: Callable[..., BaseDAO]) -> FuturesChipAPI:
    """建一個只有兩天籌碼的記憶體資料庫"""

    chip_dao: FuturesChipDAO = dao_factory(
        FuturesChipDAO,
        records=[
            {
                "date": date,
                "product_name": "臺股期貨",
                "investor": "外資及陸資",
                "多空未平倉口數淨額": net,
            }
            for date, net in (("2026-08-27", -80000), ("2026-08-28", -83655))
        ],
        table_name=FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    )

    return FuturesChipAPI(conn=chip_dao.conn)


def test_available_data_excludes_the_same_day(chip_api: FuturesChipAPI) -> None:
    """
    **`get_available()` 取的是「查詢日之前」，不是「小於等於」**

    籌碼盤後才公布，當天的資料當天不可能知道。那一個等號就是前視偏差，
    而且不會有任何錯誤訊息，只會讓回測績效好得不合理。
    """

    assert (
        chip_api.get_latest_available_date(datetime.date(2026, 8, 28)) == "2026-08-27"
    )
    assert (
        chip_api.get_institutional_net(
            datetime.date(2026, 8, 28), "臺股期貨", "外資及陸資"
        )
        == -80000
    )


def test_datetime_is_treated_as_its_date(chip_api: FuturesChipAPI) -> None:
    """
    傳 `datetime` 與傳 `date` 結果一致

    資料表的 `date` 欄是 `TEXT`，若直接把 `datetime` 丟進去比較，
    `'2026-08-28' < '2026-08-28 09:00:00'` 會成立——盤中查詢就拿到當天盤後
    才公布的籌碼，而這支 API 存在的理由正是防前視。
    """

    assert chip_api.get_latest_available_date(
        datetime.datetime(2026, 8, 28, 9, 0)
    ) == chip_api.get_latest_available_date(datetime.date(2026, 8, 28))


def test_available_data_carries_forward_over_holidays(chip_api: FuturesChipAPI) -> None:
    """連假期間沿用最近一次公布的籌碼——那確實是當下唯一知道的資訊"""

    assert chip_api.get_latest_available_date(datetime.date(2026, 9, 5)) == "2026-08-28"


def test_no_data_before_the_first_publication(chip_api: FuturesChipAPI) -> None:
    """第一筆籌碼公布之前一律回空，不可回傳未來的資料"""

    assert chip_api.get_latest_available_date(datetime.date(2026, 8, 27)) is None
    assert chip_api.get_available(datetime.date(2026, 8, 27)).empty


def test_on_date_is_the_research_only_entry(chip_api: FuturesChipAPI) -> None:
    """`get_on_date()` 取當天實際公布的資料——研究用，不可用於產生訊號"""

    df: pd.DataFrame = chip_api.get_on_date(datetime.date(2026, 8, 28))

    assert len(df) == 1
    assert df.iloc[0]["多空未平倉口數淨額"] == -83655


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗籌碼表"
)
def test_real_chip_tables_have_no_orphan_rows() -> None:
    """三張表都不該有主鍵為空的垃圾列（來源檔尾說明文字造成的那種）"""

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        checks: Dict[str, str] = {
            FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME: "product_name",
            FUTURES_LARGE_TRADER_TABLE_NAME: "product",
        }
        for table, column in checks.items():
            try:
                count: Optional[int] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE {column} IS NULL OR TRIM({column}) = ''"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pytest.skip(f"{table} 尚未建立")
            assert count == 0, f"{table} 有 {count} 列主鍵為空"
    finally:
        conn.close()


@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗籌碼表"
)
def test_real_pcr_ratio_is_consistent() -> None:
    """PCR 的比率欄要與成交量欄對得上（抓得到「整列位移」這類靜默錯誤）"""

    api: FuturesChipAPI = FuturesChipAPI()
    try:
        coverage: Optional[Dict[str, str]] = api.get_covered_date_range(
            FUTURES_PUT_CALL_RATIO_TABLE_NAME
        )
        if coverage is None:
            pytest.skip("PCR 表尚無資料")

        row: Optional[Dict] = api.get_put_call_ratio(
            datetime.date.fromisoformat(coverage["latest"]) + datetime.timedelta(days=1)
        )
    finally:
        api.close()

    assert row is not None
    expected: float = round(row["賣權成交量"] / row["買權成交量"] * 100, 2)
    assert abs(expected - row["買賣權成交量比率%"]) < 0.05


# === 月批次與「被擋」的辨識（2026-09-02 回補事故後補強）===
def test_date_comes_from_the_source_not_the_query(cleaner) -> None:
    """
    **日期以來源的 `日期` 欄為準**

    改用區間查詢之後，一次回應涵蓋一整個月；若沿用「把 date 覆寫成查詢起日」，
    整批資料的日期會全錯——而且錯得很整齊，看起來完全正常。
    """

    two_days: str = (
        "日期,商品名稱,身份別,多方交易口數\n"
        "2026/08/27,臺股期貨,自營商,100\n"
        "2026/08/28,臺股期貨,自營商,200\n"
    )

    df: pd.DataFrame = cleaner.clean_institutional(two_days, datetime.date(2026, 8, 1))

    assert sorted(df["date"]) == ["2026-08-27", "2026-08-28"]


def test_months_are_split_with_original_endpoints() -> None:
    """月批次切分要保留原始起訖日，不可擴張到整月（會多抓未來的日期）"""

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    windows = FuturesChipUpdater.split_months(
        datetime.date(2026, 7, 15), datetime.date(2026, 9, 2)
    )

    assert windows == [
        (datetime.date(2026, 7, 15), datetime.date(2026, 7, 31)),
        (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)),
        (datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)),
    ]


def test_blocked_window_is_retried_not_recorded_as_empty() -> None:
    """
    **「該有交易日卻沒拿到 CSV」＝ 被擋，要重試**

    TAIFEX 擋流量時回 HTTP 200 ＋ HTML，與非交易日一模一樣。2026-09-02 的回補
    就是這樣把 250 個交易日記成「查無資料」——事後單獨重查每一天都有資料。
    """

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.BLOCKED_RETRY_DELAY_SECONDS = 0  # 測試不要真的等
    updater.has_trading_days = lambda start, end: True

    attempts: List[int] = []

    def flaky_crawl(start, end):
        attempts.append(1)
        return "日期,商品名稱\n2026/08/28,臺股期貨\n" if len(attempts) >= 2 else None

    assert updater.crawl_window(flaky_crawl, "三大法人", DATE, DATE) is not None
    assert len(attempts) == 2  # 第一次被擋、第二次成功


def test_no_retry_when_the_window_has_no_trading_day() -> None:
    """整段都不是交易日就不必重試——那是真的沒資料，重試只是白等"""

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.has_trading_days = lambda start, end: False

    attempts: List[int] = []

    def always_blocked(start, end):
        attempts.append(1)
        return None

    assert updater.crawl_window(always_blocked, "三大法人", DATE, DATE) is None
    assert len(attempts) == 1


def test_institutional_start_date_is_clamped_to_source_earliest() -> None:
    """
    **三大法人的歷史只回溯到 2023-09-04**（2026-09-05 實測）

    不夾的話會白打上千次請求，而且每一次都被記成「查無資料」，
    看起來像是那幾年真的沒有籌碼。其餘兩個資料集有完整歷史，不受此限。

    **斷言刻意鎖死在那個常數上，而不是寫成「距今 N 年以內」。** 舊版斷言是
    `(today - clamped).days <= 365 * 2`——它對「今天往回推兩年」與「固定在
    2024-08-17」都成立，所以夾擠點整整晚了一年多也不會變紅。
    """

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    old_start: datetime.date = datetime.date(2015, 1, 1)

    clamped: datetime.date = updater.clamp_start_date(
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, "institutional", old_start
    )
    untouched: datetime.date = updater.clamp_start_date(
        FUTURES_LARGE_TRADER_TABLE_NAME, "large_trader", old_start
    )
    after_earliest: datetime.date = updater.clamp_start_date(
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
        "institutional",
        FuturesChipUpdater.INSTITUTIONAL_EARLIEST_DATE + datetime.timedelta(days=1),
    )

    assert clamped == FuturesChipUpdater.INSTITUTIONAL_EARLIEST_DATE
    assert untouched == old_start
    # 已經晚於下限的起點不可以被往後推
    assert after_earliest == FuturesChipUpdater.INSTITUTIONAL_EARLIEST_DATE + (
        datetime.timedelta(days=1)
    )


def test_update_resolves_start_dates_for_every_dataset(monkeypatch) -> None:
    """
    `update()` 會替三個資料集各自解出起點並跑一輪

    **這條測試存在的理由**：改寫 `update_dataset()` 時曾把 `resolve_start_date()`
    一起刪掉，而所有既有測試都只測個別方法，沒有一條會呼叫 `update()`，
    於是問題直到實跑回補才炸出來（`AttributeError`）。
    """

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)

    class StubLoader:
        def get_latest_date(self, table):
            return None

    updater.loader = StubLoader()
    updater.get_datasets = lambda: [
        (FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, "institutional", None, None),
        (FUTURES_LARGE_TRADER_TABLE_NAME, "large_trader", None, None),
        (FUTURES_PUT_CALL_RATIO_TABLE_NAME, "pcr", None, None),
    ]

    called: List[tuple] = []

    def record_dataset(table, label, crawl, clean, start, end):
        """`update_dataset()` 回傳「（新增列數, 被擋的月份）」，替身要照著回"""

        called.append((table, start, end))
        return 0, []

    updater.update_dataset = record_dataset

    updater.update(
        start_date=datetime.date(2015, 1, 1),
        end_date=datetime.date(2026, 9, 2),
        resume=False,
    )

    assert [row[0] for row in called] == [
        FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
        FUTURES_LARGE_TRADER_TABLE_NAME,
        FUTURES_PUT_CALL_RATIO_TABLE_NAME,
    ]
    # 三大法人被夾到來源下限，其餘兩個維持 2015
    assert called[0][1] > datetime.date(2015, 1, 1)
    assert called[1][1] == datetime.date(2015, 1, 1)


def test_gap_month_inside_the_table_is_backfilled_on_resume() -> None:
    """
    表內中間缺的月份，下一次續跑會先補

    被擋的月份以前只記 error、叫人「稍後重跑」，但預設 `resume=True` 的起點是
    表內最新 +1，那個月被後面已入庫的月份蓋過去——結束碼 0、缺口仍在。
    """

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    chip_dates: List[datetime.date] = [
        datetime.date(2026, 6, 1),
        datetime.date(2026, 8, 3),
    ]
    trading_days: List[datetime.date] = chip_dates + [datetime.date(2026, 7, 1)]

    class StubLoader:
        def get_earliest_date(self, table):
            return "2026-06-01"

        def get_latest_date(self, table):
            return "2026-08-03"

        def get_dates(self, table, start, end):
            return chip_dates

    class StubPriceAPI:
        dao = type("DAO", (), {"table_exists": staticmethod(lambda: True)})()

        def get_trading_days(self, start, end):
            return sorted(trading_days)

    updater.loader = StubLoader()
    updater.price_api = StubPriceAPI()
    updater.get_datasets = lambda: [
        (FUTURES_PUT_CALL_RATIO_TABLE_NAME, "pcr", None, None),
    ]
    called: List[tuple] = []

    def record_dataset(table, label, crawl, clean, start, end):
        called.append((start, end))
        return 0, []

    updater.update_dataset = record_dataset

    updater.update(end_date=datetime.date(2026, 8, 3))

    assert called[0] == (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
    # 之後照常從表內最新 +1 接續
    assert called[-1][0] == datetime.date(2026, 8, 4)


def test_update_only_touches_requested_tables(monkeypatch) -> None:
    """
    `tables` 指定時只跑那幾個資料集

    **這條測試守的是歷史回補的成本。** 要補 `futures_institutional_chip` 就得
    `resume=False` ＋ 指定 `start_date`，而那個組合套在三張表上會讓另外兩張
    已經完整到 2015 的表整段重抓。少了這個開關，一次回補就是上百次多餘請求。
    """

    from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater

    updater: FuturesChipUpdater = FuturesChipUpdater.__new__(FuturesChipUpdater)
    updater.get_datasets = lambda: [
        (FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME, "institutional", None, None),
        (FUTURES_LARGE_TRADER_TABLE_NAME, "large_trader", None, None),
        (FUTURES_PUT_CALL_RATIO_TABLE_NAME, "pcr", None, None),
    ]

    called: List[tuple] = []

    def record_dataset(table, label, crawl, clean, start, end):
        called.append((table, start, end))
        return 0, []

    updater.update_dataset = record_dataset

    updater.update(
        start_date=datetime.date(2023, 9, 4),
        end_date=datetime.date(2024, 10, 3),
        resume=False,
        tables=[FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME],
    )

    assert [row[0] for row in called] == [FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME]
    assert called[0][1] == FuturesChipUpdater.INSTITUTIONAL_EARLIEST_DATE

    # `tables` 對不到任何資料集時安靜收工，不要當成「三張全做」
    called.clear()
    updater.update(tables=["no_such_table"])
    assert called == []


@pytest.mark.parametrize(
    "clean_name, csv_text, expected",
    [
        (
            "clean_institutional",
            "日期,商品名稱,身份別,多方交易口數\n"
            "2026/08/27,臺股期貨,自營商,100\n2026/08/28,臺股期貨,自營商,200\n",
            ["2026-08-27", "2026-08-28"],
        ),
        (
            "clean_large_trader",
            "日期,商品(契約),到期月份(週別),交易人類別,前五大交易人買方\n"
            "2026/08/27,TX     ,999999  ,0,100\n2026/08/28,TX     ,999999  ,0,200\n",
            ["2026-08-27", "2026-08-28"],
        ),
        (
            "clean_put_call_ratio",
            "日期,賣權成交量,買權成交量\n2026/08/27,1,2,\n2026/08/28,3,4,\n",
            ["2026-08-27", "2026-08-28"],
        ),
    ],
)
def test_every_cleaner_uses_the_source_date(
    cleaner, clean_name: str, csv_text: str, expected: List[str]
) -> None:
    """
    **三個清洗器都要以來源的 `日期` 欄為準**

    這條是 parametrize 的，因為只驗其中一個會漏：改成月批次時，
    `clean_put_call_ratio` 的修改實際上沒套用到（比對字串沒對上），
    於是整個月的資料被寫成同一個主鍵 `'None'`——`INSERT OR IGNORE` 之下
    每個月只留下一列垃圾，其餘全部被吞掉，而且不會有任何錯誤訊息。
    """

    df: pd.DataFrame = getattr(cleaner, clean_name)(csv_text)

    assert sorted(df["date"]) == expected
