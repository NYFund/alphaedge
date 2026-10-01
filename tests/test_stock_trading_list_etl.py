import datetime
import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest
import requests

import core.pipeline.tw.crawlers.stock_trading_list_crawler as crawler_module
from core.config import DAY_TRADE_LIST_START_DATE, SHORT_SALE_LIST_START_DATE
from core.pipeline.shared.base_cleaner import ColumnLayoutError
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.request_utils import FetchResult
from core.pipeline.tw.cleaners.stock_trading_list_cleaner import (
    StockTradingListCleaner,
)
from core.pipeline.tw.crawlers.stock_trading_list_crawler import (
    StockTradingListCrawler,
)
from core.pipeline.tw.updaters.stock_day_trade_list_updater import (
    StockDayTradeListUpdater,
)
from core.pipeline.tw.updaters.stock_short_sale_list_updater import (
    StockShortSaleListUpdater,
)

"""
交易所兩份資格名單的 ETL：爬蟲的判斷、清洗的欄位對應、updater 的起點

**完全不連網路**。回應的形狀照 2026-10-01 實測：上市平盤下名單的欄位與資料在最外層、
上市當沖名單在第二張表、上櫃兩份都在第一張表，四個端點都會回 `date`（`YYYYMMDD`）。
"""

DATE: datetime.date = datetime.date(2024, 1, 2)

SHORT_SALE_FIELDS: List[str] = [
    "證券代號",
    "證券名稱",
    "暫停融券賣出",
    "暫停借券賣出",
    "前一交易日收盤價跌停本日禁止平盤下融券、借券賣出",
]


def fake_fetch(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> None:
    """讓爬蟲拿到指定的 JSON 回應"""

    response: requests.Response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response.encoding = "utf-8"
    monkeypatch.setattr(
        crawler_module.RequestUtils,
        "fetch",
        lambda url: FetchResult.succeeded(response),
    )


def twse_short_sale_payload(date: str = "20240102", rows: Any = None) -> Dict[str, Any]:
    """上市平盤下名單的回應（欄位與資料在最外層）"""

    return {
        "stat": "OK",
        "date": date,
        "fields": SHORT_SALE_FIELDS,
        "data": rows if rows is not None else [["2330", "台積電", "", "", ""]],
    }


# === 爬蟲 ===
def test_twse_short_sale_list_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """上市平盤下名單沒有 `tables`，欄位與資料直接在最外層"""

    fake_fetch(monkeypatch, twse_short_sale_payload())

    result: CrawlResult = StockTradingListCrawler().crawl_twse_short_sale_list(DATE)

    assert result.is_ok
    assert result.data["證券代號"].tolist() == ["2330"]


def test_twse_day_trade_list_reads_the_second_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上市當沖名單的第一張表是全市場統計，挑錯表不會報錯、只會入庫錯的東西"""

    fake_fetch(
        monkeypatch,
        {
            "stat": "OK",
            "date": "20240102",
            "tables": [
                {"fields": ["當日沖銷交易總成交股數"], "data": [["1,248,050,000"]]},
                {
                    "fields": [
                        "證券代號",
                        "證券名稱",
                        "暫停現股賣出後現款買進當沖註記",
                    ],
                    "data": [["2330", "台積電", ""]],
                },
            ],
        },
    )

    result: CrawlResult = StockTradingListCrawler().crawl_twse_day_trade_list(DATE)

    assert result.is_ok
    assert result.data["證券代號"].tolist() == ["2330"]


def test_mismatched_date_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    站方回別天的名單時記為失敗

    上櫃端點收到非斜線日期時不報錯，而是回近幾日的資料；入庫的話擋單會整段錯位。
    """

    fake_fetch(monkeypatch, twse_short_sale_payload(date="20231229"))

    result: CrawlResult = StockTradingListCrawler().crawl_twse_short_sale_list(DATE)

    assert result.is_failed


def test_empty_list_on_a_trading_day_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    交易日的名單 0 列是站方異常，不是「沒有任何證券可放空」

    記成查無資料的話那天永遠不會再補，回測又會把它讀成全部不得放空。
    """

    fake_fetch(monkeypatch, twse_short_sale_payload(rows=[]))

    result: CrawlResult = StockTradingListCrawler().crawl_twse_short_sale_list(DATE)

    assert result.is_failed


def test_holiday_message_is_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """站方明確回「查無資料」才算休市"""

    fake_fetch(monkeypatch, {"stat": "很抱歉，沒有符合條件的資料!"})

    result: CrawlResult = StockTradingListCrawler().crawl_twse_short_sale_list(DATE)

    assert result.is_no_data


# === 清洗 ===
@pytest.fixture
def cleaner(tmp_path: Path) -> StockTradingListCleaner:
    """輸出寫到暫存目錄的清洗器"""

    cleaner: StockTradingListCleaner = StockTradingListCleaner()
    cleaner.short_sale_dir = tmp_path / "short_sale_list"
    cleaner.day_trade_dir = tmp_path / "day_trade_list"
    cleaner.setup()
    return cleaner


def test_flags_become_zero_or_one(cleaner: StockTradingListCleaner) -> None:
    """`*`、`Y`、全形 `＊` 都是「有」，空白是「沒有」；非個股列被濾掉"""

    raw: pd.DataFrame = pd.DataFrame(
        [
            ["2330", "台積電 ", "*", "", " "],
            ["2317", "鴻海", "", "＊", "Y"],
            ["合計", "", "", "", ""],
        ],
        columns=SHORT_SALE_FIELDS,
    )

    cleaned: pd.DataFrame = cleaner.clean_twse_short_sale_list(raw, DATE)

    assert cleaned["stock_id"].tolist() == ["2330", "2317"]
    assert cleaned["證券名稱"].tolist() == ["台積電", "鴻海"]
    assert cleaned["暫停融券賣出"].tolist() == [1, 0]
    assert cleaned["暫停借券賣出"].tolist() == [0, 1]
    assert cleaned["禁止平盤下融借券賣出"].tolist() == [0, 1]
    assert (cleaner.short_sale_dir / "twse_20240102.csv").exists()


def test_early_tpex_day_trade_list_without_flag_column(
    cleaner: StockTradingListCleaner,
) -> None:
    """2014 年的上櫃當沖名單只有兩欄（當時尚未開放先賣後買），註記補 0"""

    raw: pd.DataFrame = pd.DataFrame([["006201", "元大富櫃50"]], columns=["a", "b"])

    cleaned: pd.DataFrame = cleaner.clean_tpex_day_trade_list(raw, DATE)

    assert cleaned["暫停先賣後買當沖"].tolist() == [0]


def test_unexpected_layout_raises(cleaner: StockTradingListCleaner) -> None:
    """欄位數不符代表版面改制，拋例外讓那天記為失敗，而不是錯位入庫"""

    raw: pd.DataFrame = pd.DataFrame([["2330", "台積電", ""]], columns=["a", "b", "c"])

    with pytest.raises(ColumnLayoutError):
        cleaner.clean_twse_short_sale_list(raw, DATE)


# === Updater 的起點 ===
@pytest.mark.parametrize(
    ("updater_cls", "list_start"),
    [
        (StockShortSaleListUpdater, SHORT_SALE_LIST_START_DATE),
        (StockDayTradeListUpdater, DAY_TRADE_LIST_START_DATE),
    ],
    ids=["short-sale", "day-trade"],
)
def test_plan_never_requests_days_before_the_list_start(
    monkeypatch: pytest.MonkeyPatch, updater_cls: type, list_start: datetime.date
) -> None:
    """
    起點之前一律不請求

    上櫃在起點之前回 `ok`、0 列，會被記成失敗並在每次執行時重試，而那些日子永遠不會有資料。
    """

    captured: List[datetime.date] = []

    def capture(
        self: Any, progress: Any, start: datetime.date, end: datetime.date
    ) -> List[datetime.date]:
        captured.append(start)
        return []

    monkeypatch.setattr(updater_cls, "plan_dates_by_price_calendar", capture)
    updater = updater_cls.__new__(updater_cls)

    updater.plan_dates(None, datetime.date(2013, 1, 1), datetime.date(2026, 1, 1))

    assert captured == [list_start]
