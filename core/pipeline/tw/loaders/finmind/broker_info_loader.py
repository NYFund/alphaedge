import sqlite3
from pathlib import Path

from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.pipeline.tw.loaders.finmind.reference_table_loader import (
    ReferenceTableSpec,
    load_reference_table,
)
from core.pipeline.utils import FinMindDataType

"""證券商資訊表的 CSV 入庫；流程與台股總覽相同，見 `reference_table_loader.py`"""

BROKER_INFO_SPEC: ReferenceTableSpec = ReferenceTableSpec(
    data_type=FinMindDataType.BROKER_INFO,
    csv_name="taiwan_securities_trader_info.csv",
    dao_class=SecuritiesTraderInfoDAO,
    # 欄位順序須與 crawler schema 註解一致
    column_order=[
        "securities_trader_id",
        "securities_trader",
        "date",
        "address",
        "phone",
    ],
    label="broker info",
)


def load_broker_info(conn: sqlite3.Connection, finmind_dir: Path) -> None:
    """載入證券商資訊表資料到資料庫"""

    load_reference_table(conn, finmind_dir, BROKER_INFO_SPEC)
