import sqlite3
from pathlib import Path
from typing import List

import pandas as pd
import pytest
from loguru import logger

from core.config import (
    SECURITIES_TRADER_INFO_TABLE_NAME,
    STOCK_INFO_TABLE_NAME,
    STOCK_INFO_WITH_WARRANT_TABLE_NAME,
)
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO
from core.pipeline.tw.loaders.finmind import (
    broker_info_loader,
    stock_info_loader,
)
from core.pipeline.utils import FinMindDataType

"""
三張「單鍵參考資料表」的入庫行為護欄

台股總覽、台股總覽（含權證）、證券商資訊原本是三份逐字複製的實作，合併成
`load_reference_table()` 之後由本檔釘住合併前後必須相同的四件事：

1. 三張表各自寫進正確的資料表，**欄位順序**與 schema 一致；
2. 重跑不會產生重複列（已存在的主鍵以最新快照覆蓋，不新增列）；
3. 同一個 CSV 內的重複列先被去掉；
4. **log 措辭逐字不變**——回補時那幾行是判斷「跑到哪張表」的唯一依據。

不連網路、不碰正式的 tw_stock.db。
"""

STOCK_INFO_ROWS: List[dict] = [
    {
        "industry_category": "半導體業",
        "stock_id": "2330",
        "stock_name": "台積電",
        "type": "twse",
        "date": "2026-01-01",
    },
    {
        "industry_category": "半導體業",
        "stock_id": "2317",
        "stock_name": "鴻海",
        "type": "twse",
        "date": "2026-01-01",
    },
]

BROKER_INFO_ROWS: List[dict] = [
    {
        "securities_trader_id": "1020",
        "securities_trader": "合庫",
        "date": "2026-01-01",
        "address": "台北市",
        "phone": "02-1234-5678",
    },
    {
        "securities_trader_id": "1021",
        "securities_trader": "合庫台中",
        "date": "2026-01-01",
        "address": "台中市",
        "phone": "04-1234-5678",
    },
]


@pytest.fixture
def env(tmp_path: Path) -> tuple:
    """建好三張表與三份 CSV，回傳 (conn, finmind_dir)"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    StockInfoDAO(conn=conn).ensure_table()
    StockInfoWithWarrantDAO(conn=conn).ensure_table()
    SecuritiesTraderInfoDAO(conn=conn).ensure_table()

    finmind_dir: Path = tmp_path / "finmind"
    for data_type, csv_name, rows in [
        (FinMindDataType.STOCK_INFO, "taiwan_stock_info.csv", STOCK_INFO_ROWS),
        (
            FinMindDataType.STOCK_INFO_WITH_WARRANT,
            "taiwan_stock_info_with_warrant.csv",
            STOCK_INFO_ROWS,
        ),
        (
            FinMindDataType.BROKER_INFO,
            "taiwan_securities_trader_info.csv",
            BROKER_INFO_ROWS,
        ),
    ]:
        directory: Path = finmind_dir / data_type.value.lower()
        directory.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(directory / csv_name, index=False)

    return conn, finmind_dir


@pytest.fixture
def captured_logs() -> List[str]:
    """收集 loguru 訊息，供 log 措辭的斷言使用"""

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]))
    yield messages
    logger.remove(sink_id)


def row_count(conn: sqlite3.Connection, table: str) -> int:
    """資料表列數"""

    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# === 三張表各自入庫 ===
def test_each_table_gets_its_own_rows(env) -> None:
    """三支 loader 各寫各的表，不會互相污染"""

    conn, finmind_dir = env

    stock_info_loader.load_stock_info(conn, finmind_dir)
    stock_info_loader.load_stock_info_with_warrant(conn, finmind_dir)
    broker_info_loader.load_broker_info(conn, finmind_dir)

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 2
    assert row_count(conn, STOCK_INFO_WITH_WARRANT_TABLE_NAME) == 2
    assert row_count(conn, SECURITIES_TRADER_INFO_TABLE_NAME) == 2


def test_column_values_land_in_the_right_columns(env) -> None:
    """
    欄位順序錯位不會報錯，只會讓值互換

    寫入依 DataFrame 的欄位名對應，但這裡先以 `column_order` 重排過；
    順序若被改動，讀回來的值就會對不上。
    """

    conn, finmind_dir = env
    stock_info_loader.load_stock_info(conn, finmind_dir)

    row = conn.execute(
        f"SELECT industry_category, stock_id, stock_name, type, date "
        f"FROM {STOCK_INFO_TABLE_NAME} WHERE stock_id = '2330'"
    ).fetchone()

    assert row == ("半導體業", "2330", "台積電", "twse", "2026-01-01")


def test_broker_info_uses_its_own_key_and_column_order(env) -> None:
    """證券商表的主鍵是 `securities_trader_id`，欄位順序也與台股總覽不同"""

    conn, finmind_dir = env
    broker_info_loader.load_broker_info(conn, finmind_dir)

    row = conn.execute(
        f"SELECT securities_trader_id, securities_trader, date, address, phone "
        f"FROM {SECURITIES_TRADER_INFO_TABLE_NAME} WHERE securities_trader_id = '1020'"
    ).fetchone()

    assert row == ("1020", "合庫", "2026-01-01", "台北市", "02-1234-5678")


# === 重跑與去重 ===
def test_rerun_does_not_duplicate(env) -> None:
    """同鍵覆蓋而不是新增；重跑列數不變"""

    conn, finmind_dir = env

    stock_info_loader.load_stock_info(conn, finmind_dir)
    stock_info_loader.load_stock_info(conn, finmind_dir)
    broker_info_loader.load_broker_info(conn, finmind_dir)
    broker_info_loader.load_broker_info(conn, finmind_dir)

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 2
    assert row_count(conn, SECURITIES_TRADER_INFO_TABLE_NAME) == 2


def test_only_new_keys_are_appended(env) -> None:
    """CSV 新增一檔後，只會補進那一檔"""

    conn, finmind_dir = env
    stock_info_loader.load_stock_info(conn, finmind_dir)

    csv_path: Path = (
        finmind_dir / FinMindDataType.STOCK_INFO.value.lower() / "taiwan_stock_info.csv"
    )
    pd.DataFrame(
        STOCK_INFO_ROWS
        + [
            {
                "industry_category": "金融保險業",
                "stock_id": "2891",
                "stock_name": "中信金",
                "type": "twse",
                "date": "2026-01-02",
            }
        ]
    ).to_csv(csv_path, index=False)

    stock_info_loader.load_stock_info(conn, finmind_dir)

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 3


def test_existing_rows_are_refreshed_with_the_latest_snapshot(env) -> None:
    """
    已存在的標的以最新快照覆蓋：興櫃轉上市櫃後 `type` 要跟著更新

    以前只補新增，新股第一次入庫的 `emerging` 永遠不會變，以 `type IN ('twse',
    'tpex')` 取清單的財報權益變動表就永遠排除它，而且不會有任何 warning。
    快照裡已經沒有的標的不刪：下市的仍要查得到。
    """

    conn, finmind_dir = env
    csv_path: Path = (
        finmind_dir / FinMindDataType.STOCK_INFO.value.lower() / "taiwan_stock_info.csv"
    )
    pd.DataFrame([dict(row, type="emerging") for row in STOCK_INFO_ROWS]).to_csv(
        csv_path, index=False
    )
    stock_info_loader.load_stock_info(conn, finmind_dir)

    pd.DataFrame(STOCK_INFO_ROWS[:1]).to_csv(csv_path, index=False)
    stock_info_loader.load_stock_info(conn, finmind_dir)

    types = dict(
        conn.execute(f"SELECT stock_id, type FROM {STOCK_INFO_TABLE_NAME}").fetchall()
    )
    assert types[STOCK_INFO_ROWS[0]["stock_id"]] == STOCK_INFO_ROWS[0]["type"]
    assert types[STOCK_INFO_ROWS[1]["stock_id"]] == "emerging"


def test_duplicates_within_the_csv_are_removed(env) -> None:
    """同一份 CSV 內重複的主鍵先被去掉，保留第一筆"""

    conn, finmind_dir = env
    csv_path: Path = (
        finmind_dir / FinMindDataType.STOCK_INFO.value.lower() / "taiwan_stock_info.csv"
    )
    duplicated_row: dict = dict(STOCK_INFO_ROWS[0])
    duplicated_row["stock_name"] = "台積電（重複列）"
    pd.DataFrame(STOCK_INFO_ROWS + [duplicated_row]).to_csv(csv_path, index=False)

    stock_info_loader.load_stock_info(conn, finmind_dir)

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 2
    name = conn.execute(
        f"SELECT stock_name FROM {STOCK_INFO_TABLE_NAME} WHERE stock_id = '2330'"
    ).fetchone()[0]
    assert name == "台積電"


# === 缺檔與空檔 ===
def test_missing_csv_warns_and_returns(env, tmp_path: Path) -> None:
    """CSV 不存在時只警告不拋錯——日常更新可能只跑其中一張表"""

    conn, _ = env

    stock_info_loader.load_stock_info(conn, tmp_path / "not_exist")

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 0


def test_empty_csv_is_skipped(env) -> None:
    """空檔案跳過，不會寫入任何列"""

    conn, finmind_dir = env
    csv_path: Path = (
        finmind_dir / FinMindDataType.STOCK_INFO.value.lower() / "taiwan_stock_info.csv"
    )
    pd.DataFrame(columns=list(STOCK_INFO_ROWS[0].keys())).to_csv(csv_path, index=False)

    stock_info_loader.load_stock_info(conn, finmind_dir)

    assert row_count(conn, STOCK_INFO_TABLE_NAME) == 0


# === log 措辭 ===
def test_log_wording_is_preserved_per_table(env, captured_logs: List[str]) -> None:
    """
    三張表的 log 措辭必須逐字保留

    回補時 `Loading X from ...` 那一行是判斷「跑到哪張表」的唯一依據；
    合併實作時若讓三者共用同一句話，log 就再也分不出是哪一張表。
    """

    conn, finmind_dir = env

    stock_info_loader.load_stock_info(conn, finmind_dir)
    stock_info_loader.load_stock_info_with_warrant(conn, finmind_dir)
    broker_info_loader.load_broker_info(conn, finmind_dir)

    joined: str = "\n".join(captured_logs)

    assert "Loading stock info from taiwan_stock_info.csv..." in joined
    assert (
        "Loading stock info with warrant from taiwan_stock_info_with_warrant.csv..."
        in joined
    )
    assert "Loading broker info from taiwan_securities_trader_info.csv..." in joined


def test_saved_message_reports_row_count(env, captured_logs: List[str]) -> None:
    """入庫訊息帶列數，是回補時確認「有沒有真的寫進去」的依據"""

    conn, finmind_dir = env
    stock_info_loader.load_stock_info(conn, finmind_dir)

    assert "Saved taiwan_stock_info.csv into database (2 rows)" in "\n".join(
        captured_logs
    )


def test_rerun_logs_refreshed_rows(env, captured_logs: List[str]) -> None:
    """重跑時分開報新增與刷新的列數，看得出「有跑到、只是沒有新標的」"""

    conn, finmind_dir = env
    stock_info_loader.load_stock_info(conn, finmind_dir)
    captured_logs.clear()
    stock_info_loader.load_stock_info(conn, finmind_dir)

    assert (
        "Saved taiwan_stock_info.csv into database (0 new rows, 2 refreshed)"
        in "\n".join(captured_logs)
    )
