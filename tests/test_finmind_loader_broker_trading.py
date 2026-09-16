"""
測試 `finmind/broker_trading_loader.load_from_dataframe()` 的去重語意：
重複的 key 不重插、新日期可插入、其他組合不受影響。去重交給資料庫主鍵
（`INSERT OR IGNORE`），不再先把已存在的 key 查回記憶體。

**資料庫走 pytest 的 `tmp_path`**：本測試原本在 `tests/temp/` 建帶時間戳的 `.db`
且從不刪除，跑一次留一個 12 MB 的檔——2026-09-13 清理時已累積 560 個、275 MB
（健檢第四輪 S6）。`tmp_path` 由 pytest 負責回收，且每個測試各自隔離。
"""

import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import patch

import pandas as pd

from core.config import STOCK_TRADING_DAILY_REPORT_TABLE_NAME


def _make_broker_trading_df(
    stock_id: str,
    securities_trader_id: str,
    securities_trader: str,
    dates: List[str],
) -> pd.DataFrame:
    """組出符合 loader 需求的 broker trading DataFrame。"""
    rows = []
    for d in dates:
        rows.append(
            {
                "securities_trader": securities_trader,
                "securities_trader_id": securities_trader_id,
                "stock_id": stock_id,
                "date": d,
                "buy_volume": 100,
                "sell_volume": 50,
                "buy_price": 500.0,
                "sell_price": 501.0,
            }
        )
    return pd.DataFrame(rows)


def test_load_broker_trading_from_dataframe_optimization(tmp_path: Path) -> None:
    """重複不重插、新日期可插入，且不影響其他 (stock_id, securities_trader_id) 組合"""

    temp_db_path: str = str(tmp_path / "tw_stock.db")

    with (
        patch("core.config.TW_STOCK_DB_PATH", temp_db_path),
        patch("core.pipeline.tw.loaders.finmind_loader.TW_STOCK_DB_PATH", temp_db_path),
    ):
        from core.pipeline.tw.loaders.finmind_loader import FinMindLoader

        loader: FinMindLoader = FinMindLoader()
        loader.connect()
        loader.create_missing_tables()

        try:
            # 1) 第一次載入：一組 (2330, 1020) 兩筆日期
            df1 = _make_broker_trading_df(
                stock_id="2330",
                securities_trader_id="1020",
                securities_trader="兆豐",
                dates=["2024-07-01", "2024-07-02"],
            )
            n1 = loader.load_broker_trading_daily_report(df=df1)
            assert n1 == 2, f"第一次應插入 2 筆，實際 {n1}"

            conn = sqlite3.connect(temp_db_path)
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME}")
            total = cur.fetchone()[0]
            conn.close()
            assert total == 2, f"表應為 2 筆，實際 {total}"

            # 2) 再載入相同資料：應全部視為重複，插入 0 筆
            n2 = loader.load_broker_trading_daily_report(df=df1)
            assert n2 == 0, f"重複載入應插入 0 筆，實際 {n2}"

            cur = sqlite3.connect(temp_db_path).cursor()
            cur.execute(f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME}")
            total = cur.fetchone()[0]
            assert total == 2, f"表仍應為 2 筆，實際 {total}"

            # 3) 同一組合多一筆新日期：應只插入 1 筆
            df3 = _make_broker_trading_df(
                stock_id="2330",
                securities_trader_id="1020",
                securities_trader="兆豐",
                dates=["2024-07-01", "2024-07-02", "2024-07-03"],
            )
            n3 = loader.load_broker_trading_daily_report(df=df3)
            assert n3 == 1, f"應只插入 1 筆新日期，實際 {n3}"

            cur = sqlite3.connect(temp_db_path).cursor()
            cur.execute(f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME}")
            total = cur.fetchone()[0]
            assert total == 3, f"表應為 3 筆，實際 {total}"

            # 4) 先手動插入另一組合 (2317, 1020)，再載入僅 (2330, 1020) 的 df，
            #    另一組合筆數不應被影響
            conn = sqlite3.connect(temp_db_path)
            conn.execute(
                f"""
                INSERT INTO {STOCK_TRADING_DAILY_REPORT_TABLE_NAME}
                (securities_trader, securities_trader_id, stock_id, date, buy_volume, sell_volume, buy_price, sell_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("兆豐", "1020", "2317", "2024-07-05", 200, 100, 100.0, 101.0),
            )
            conn.commit()
            conn.close()

            cur = sqlite3.connect(temp_db_path).cursor()
            cur.execute(
                f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME} WHERE stock_id = ? AND securities_trader_id = ?",
                ("2317", "1020"),
            )
            count_2317_before = cur.fetchone()[0]
            assert count_2317_before == 1

            # 只載入 (2330, 1020) 的資料（一筆已存在、一筆新日期）
            df4 = _make_broker_trading_df(
                stock_id="2330",
                securities_trader_id="1020",
                securities_trader="兆豐",
                dates=["2024-07-02", "2024-07-04"],
            )
            n4 = loader.load_broker_trading_daily_report(df=df4)
            assert n4 == 1, f"應只插入 1 筆 (2024-07-04)，實際 {n4}"

            cur = sqlite3.connect(temp_db_path).cursor()
            cur.execute(
                f"SELECT COUNT(*) FROM {STOCK_TRADING_DAILY_REPORT_TABLE_NAME} WHERE stock_id = ? AND securities_trader_id = ?",
                ("2317", "1020"),
            )
            count_2317_after = cur.fetchone()[0]
            assert count_2317_after == 1, "另一組合 (2317, 1020) 筆數不應被改動"

        finally:
            loader.disconnect()
