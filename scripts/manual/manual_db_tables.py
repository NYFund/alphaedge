import argparse
import sys
from typing import List, Tuple, Type

import pandas as pd

from core.config import TW_STOCK_DB_PATH
from core.dao.base import BaseDAO
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO

"""
檢查 tw_stock.db 的 FinMind 參考表是否存在，並可抽樣查看券商分點資料

**以唯讀連線開啟**：人工檢查不該寫任何東西，也不該在檔案不存在時替它建出空 DB，
更不該在背景 ETL 寫入時與它搶寫入鎖。查詢一律走 DAO，本腳本不寫 SQL。

使用方法（從專案根目錄執行）：
    python -m scripts.manual.manual_db_tables
    python -m scripts.manual.manual_db_tables --broker-trading --limit 10
"""

# 要檢查的參考表：（顯示名稱, DAO 類別）
REFERENCE_TABLES: List[Tuple[str, Type[BaseDAO]]] = [
    ("stock_info", StockInfoDAO),
    ("stock_info_with_warrant", StockInfoWithWarrantDAO),
    ("broker_info", SecuritiesTraderInfoDAO),
]


def check_reference_tables(conn: DBConnection) -> bool:
    """
    - Description:
        逐一檢查參考表是否存在並列出筆數
    - Parameters:
        - conn: DBConnection
            唯讀連線
    - Return:
        - bool
            所有參考表都存在時為 True
    """

    all_exist: bool = True
    for display_name, dao_cls in REFERENCE_TABLES:
        dao: BaseDAO = dao_cls(conn=conn)
        exists: bool = dao.table_exists()
        all_exist &= exists

        print(f"{'[OK]' if exists else '[X]'} {display_name}")
        print(f"   表名: {dao.TABLE_NAME}")
        print(f"   存在: {'是' if exists else '否'}")
        if exists:
            print(f"   資料筆數: {dao.count_rows():,}")
        print()

    print("=" * 60)
    print("[OK] 所有資料表都存在！" if all_exist else "[X] 部分資料表不存在！")
    return all_exist


def show_broker_trading_sample(conn: DBConnection, limit: int) -> None:
    """
    - Description:
        列出券商分點表的筆數與最新幾列
    - Parameters:
        - conn: DBConnection
            唯讀連線
        - limit: int
            顯示筆數
    """

    dao: BrokerTradingDAO = BrokerTradingDAO(conn=conn)
    print(f"\n{'=' * 60}")
    print(f"券商分點資料表：{dao.TABLE_NAME}")
    print("=" * 60)

    if not dao.table_exists():
        print("[X] 資料表不存在")
        return

    row_count: int = dao.count_rows()
    print(f"[OK] 資料筆數: {row_count:,} 筆\n")
    if row_count == 0:
        print("[警告] 資料表中沒有資料")
        return

    sample: pd.DataFrame = dao.get_latest_rows(limit)
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(sample.to_string(index=False))
    print(f"\n[完成] 已顯示 {len(sample)} 筆資料（共 {row_count:,} 筆）")


def parse_args() -> argparse.Namespace:
    """解析命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="檢查 tw_stock.db 的 FinMind 參考表與券商分點抽樣"
    )
    parser.add_argument(
        "--broker-trading",
        action="store_true",
        help="額外列出券商分點資料表的抽樣資料",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="券商分點抽樣顯示筆數（預設 5）",
    )
    return parser.parse_args()


def main() -> int:
    """執行檢查；資料庫不存在或有參考表缺漏時回傳非零結束碼"""

    args: argparse.Namespace = parse_args()

    print(f"資料庫路徑: {TW_STOCK_DB_PATH}\n")
    if not TW_STOCK_DB_PATH.exists():
        print(f"[錯誤] 資料庫檔案不存在於 {TW_STOCK_DB_PATH}")
        return 1

    conn: DBConnection = connect_sqlite(TW_STOCK_DB_PATH, read_only=True)
    try:
        success: bool = check_reference_tables(conn)
        if args.broker_trading:
            show_broker_trading_sample(conn, args.limit)
    finally:
        conn.close()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
