import sqlite3
from pathlib import Path
from typing import List

from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO
from core.pipeline.tw.loaders.finmind.reference_table_loader import (
    ReferenceTableSpec,
    load_reference_table,
)
from core.pipeline.utils import FinMindDataType

"""
台股總覽（不含權證／含權證）的 CSV 入庫

兩張表的差別只有資料表與檔名——欄位、主鍵、流程完全相同，故共用
`load_reference_table()`，各自只保留一份 spec。
"""

# 欄位順序須與 crawler schema 註解一致
STOCK_INFO_COLUMN_ORDER: List[str] = [
    "industry_category",
    "stock_id",
    "stock_name",
    "type",
    "date",
]

STOCK_INFO_SPEC: ReferenceTableSpec = ReferenceTableSpec(
    data_type=FinMindDataType.STOCK_INFO,
    csv_name="taiwan_stock_info.csv",
    dao_class=StockInfoDAO,
    column_order=STOCK_INFO_COLUMN_ORDER,
    label="stock info",
)

STOCK_INFO_WITH_WARRANT_SPEC: ReferenceTableSpec = ReferenceTableSpec(
    data_type=FinMindDataType.STOCK_INFO_WITH_WARRANT,
    csv_name="taiwan_stock_info_with_warrant.csv",
    dao_class=StockInfoWithWarrantDAO,
    column_order=STOCK_INFO_COLUMN_ORDER,
    label="stock info with warrant",
)


def load_stock_info(conn: sqlite3.Connection, finmind_dir: Path) -> None:
    """載入台股總覽資料到資料庫"""

    load_reference_table(conn, finmind_dir, STOCK_INFO_SPEC)


def load_stock_info_with_warrant(conn: sqlite3.Connection, finmind_dir: Path) -> None:
    """載入台股總覽(含權證)資料到資料庫"""

    load_reference_table(conn, finmind_dir, STOCK_INFO_WITH_WARRANT_SPEC)
