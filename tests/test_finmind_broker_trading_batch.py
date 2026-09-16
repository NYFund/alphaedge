import datetime
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytest

from core.config import (
    SECURITIES_TRADER_INFO_TABLE_NAME,
    STOCK_INFO_TABLE_NAME,
    STOCK_TRADING_DAILY_REPORT_TABLE_NAME,
)
from core.pipeline.tw.crawlers.finmind_crawler import FinMindCrawler

"""
券商分點批量更新的行為護欄（真的會斷言，不是腳本）

**為什麼要新增這一檔**：既有的 `tests/test_broker_trading_updater.py` 把整段測試包在
`try/except` 裡、失敗時 `return False`，pytest 只會發出 `PytestReturnNotNoneWarning`
並判定 **passed**——換句話說那一檔永遠不會紅，拿它當重構的驗收等於沒有驗收。

本檔完全離線（crawler 的 `setup()` 與 API 呼叫皆被替換），釘住三件事：
1. 批量更新會逐 (券商 × 股票) 組合送出請求，且資料確實入庫。
2. metadata 記錄每個組合的 `earliest_date`／`latest_date`。
3. **中斷後重跑不會重複爬取已存在的組合**——任何效能優化都不得改變這個 resume 語意。
4. 批量更新不寫 CSV，且寫不寫 CSV 不影響入庫內容。
"""

START_DATE: datetime.date = datetime.date(2024, 1, 2)
END_DATE: datetime.date = datetime.date(2024, 1, 4)

TRADER_IDS: List[str] = ["1020", "1021"]
STOCK_IDS: List[str] = ["2330", "2317"]


def seed_reference_tables(conn: sqlite3.Connection) -> None:
    """塞入 `FinMindContext.get_stock_list()`／`get_securities_trader_list()` 會讀到的兩張表"""

    conn.executemany(
        f"INSERT INTO {STOCK_INFO_TABLE_NAME} "
        f"(industry_category, stock_id, stock_name, type, date) VALUES (?, ?, ?, ?, ?)",
        [
            ("半導體業", stock_id, stock_id, "twse", "2024-01-01")
            for stock_id in STOCK_IDS
        ],
    )
    conn.executemany(
        f"INSERT INTO {SECURITIES_TRADER_INFO_TABLE_NAME} "
        f"(securities_trader_id, securities_trader, date, address, phone) "
        f"VALUES (?, ?, ?, ?, ?)",
        [
            (trader_id, f"券商{trader_id}", "2024-01-01", "", "")
            for trader_id in TRADER_IDS
        ],
    )
    conn.commit()


def make_crawler_stub(calls: List[Tuple[str, str, str, str]]):
    """回傳固定三日資料的 crawler 替身，並記錄每次呼叫的參數"""

    def stub(
        stock_id: str,
        securities_trader_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Optional[pd.DataFrame]:
        calls.append((stock_id, securities_trader_id, str(start_date), str(end_date)))
        dates: List[datetime.date] = [
            d
            for d in (
                START_DATE + datetime.timedelta(days=offset)
                for offset in range((END_DATE - START_DATE).days + 1)
            )
            if start_date <= d <= end_date
        ]
        if not dates:
            return None
        return pd.DataFrame(
            [
                {
                    "securities_trader": f"券商{securities_trader_id}",
                    "securities_trader_id": securities_trader_id,
                    "stock_id": stock_id,
                    "date": date.strftime("%Y-%m-%d"),
                    "buy_volume": 1000,
                    "sell_volume": 500,
                    "buy_price": 100.0,
                    "sell_price": 101.0,
                }
                for date in dates
            ]
        )

    return stub


@pytest.fixture
def updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """DB、downloads 與 metadata 全部導向暫存區，且不連 FinMind API"""

    db_path: Path = tmp_path / "tw_stock.db"
    downloads_path: Path = tmp_path / "finmind"
    metadata_path: Path = tmp_path / "meta" / "broker_trading_metadata.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    # crawler 的 setup() 會讀 FINMIND_API_TOKEN 並實際登入，離線測試一律略過
    monkeypatch.setattr(FinMindCrawler, "setup", lambda self, *a, **k: None)

    monkeypatch.setattr(
        "core.pipeline.tw.updaters.finmind_updater.TW_STOCK_DB_PATH", db_path
    )
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.finmind_updater.BROKER_TRADING_METADATA_PATH",
        metadata_path,
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.finmind_loader.TW_STOCK_DB_PATH", db_path
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.finmind_loader.FINMIND_DOWNLOADS_PATH", downloads_path
    )
    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.finmind_cleaner.FINMIND_DOWNLOADS_PATH",
        downloads_path,
    )

    from core.pipeline.tw.updaters.finmind_updater import FinMindUpdater

    finmind_updater = FinMindUpdater()
    seed_reference_tables(finmind_updater.conn)
    return finmind_updater


def row_count(conn: sqlite3.Connection) -> int:
    """券商分點表的總列數"""

    return conn.execute(
        f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME}"
    ).fetchone()[0]


def test_batch_update_writes_every_combination(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 券商 × 2 股票 × 3 日 = 12 列，且每個組合各送出一次請求"""

    calls: List[Tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub(calls)
    )

    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)

    assert len(calls) == len(TRADER_IDS) * len(STOCK_IDS)
    assert {(stock_id, trader_id) for stock_id, trader_id, _, _ in calls} == {
        (stock_id, trader_id) for trader_id in TRADER_IDS for stock_id in STOCK_IDS
    }
    assert row_count(updater.loader.conn) == 12


def test_metadata_records_date_range_per_combination(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """metadata 逐組合記錄 earliest／latest，resume 就是靠這兩個欄位"""

    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub([])
    )

    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)

    metadata: Dict = json.loads(
        updater.broker_trading_metadata_path.read_text(encoding="utf-8")
    )

    assert set(metadata.keys()) == set(TRADER_IDS)
    for trader_id in TRADER_IDS:
        assert set(metadata[trader_id].keys()) == set(STOCK_IDS)
        for stock_id in STOCK_IDS:
            assert metadata[trader_id][stock_id] == {
                "earliest_date": str(START_DATE),
                "latest_date": str(END_DATE),
            }


def test_rerun_does_not_recrawl_existing_combinations(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    中斷後重跑不重複爬取已存在的 `(broker_id, stock_id, date)`

    這是本份工作的共通驗收標準：任何優化都不得改變 resume 語意。
    """

    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub([])
    )
    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)
    rows_after_first_run: int = row_count(updater.loader.conn)

    second_run_calls: List[Tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        updater.crawler,
        "crawl_broker_trading_daily_report",
        make_crawler_stub(second_run_calls),
    )
    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)

    assert second_run_calls == []
    assert row_count(updater.loader.conn) == rows_after_first_run


def test_extending_end_date_only_crawls_the_new_days(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """區間往後延伸時，起點取 metadata 的 latest_date + 1，不重爬舊日期"""

    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub([])
    )
    updater.update_broker_trading_daily_report(
        start_date=START_DATE, end_date=START_DATE
    )

    calls: List[Tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub(calls)
    )
    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)

    assert calls
    assert all(
        request_start == str(START_DATE + datetime.timedelta(days=1))
        for _, _, request_start, _ in calls
    )
    assert row_count(updater.loader.conn) == 12


def test_batch_update_does_not_write_csv(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批量更新清洗後直接入庫，不在 downloads 留下 `{broker_id}/{stock_id}.csv`"""

    monkeypatch.setattr(
        updater.crawler, "crawl_broker_trading_daily_report", make_crawler_stub([])
    )

    updater.update_broker_trading_daily_report(start_date=START_DATE, end_date=END_DATE)

    broker_trading_dir: Path = updater.cleaner.finmind_dir / "broker_trading"
    assert row_count(updater.loader.conn) == 12
    assert list(broker_trading_dir.rglob("*.csv")) == []


def test_write_csv_flag_does_not_change_loaded_rows(updater) -> None:
    """
    `write_csv=True`／`False` 對同一批原始資料，入庫內容逐筆相同

    原始資料刻意重複一份，確認兩種模式都有做檔內去重——略過寫檔時若連去重
    一起略過，loader 雖然也會去重，但 cleaner 回傳的列數就會對不上。
    """

    from core.dao.tw.broker_trading_dao import BrokerTradingDAO
    from core.pipeline.tw.loaders.finmind import broker_trading_loader

    stub = make_crawler_stub([])
    raw_df: pd.DataFrame = pd.concat(
        [
            stub(stock_id, trader_id, START_DATE, END_DATE)
            for trader_id in TRADER_IDS
            for stock_id in STOCK_IDS
        ]
        * 2,
        ignore_index=True,
    )

    loaded: Dict[bool, pd.DataFrame] = {}
    for write_csv in (True, False):
        cleaned_df: pd.DataFrame = updater.cleaner.clean_broker_trading_daily_report(
            raw_df.copy(), write_csv=write_csv
        )
        conn: sqlite3.Connection = sqlite3.connect(":memory:")
        BrokerTradingDAO(conn=conn).ensure_table()
        broker_trading_loader.load_from_dataframe(conn, cleaned_df.copy())
        loaded[write_csv] = pd.read_sql_query(
            f"SELECT * FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME} "
            f"ORDER BY securities_trader_id, stock_id, date",
            conn,
        )
        conn.close()

    broker_trading_dir: Path = updater.cleaner.finmind_dir / "broker_trading"
    assert len(loaded[False]) == 12
    pd.testing.assert_frame_equal(loaded[True], loaded[False])
    # 只有 True 那一輪會寫檔：2 券商 × 2 股票
    assert len(list(broker_trading_dir.rglob("*.csv"))) == 4
