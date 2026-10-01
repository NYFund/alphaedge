import datetime
from pathlib import Path
from typing import List, Optional, Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.stock_short_sale_list_dao import StockShortSaleListDAO
from core.models import ShortSaleListSnapshot

"""Stock short sale list API: 平盤下得融（借）券賣出名單（`short_sale_list` 表）"""


class StockShortSaleListAPI(BaseDataAPI):
    """平盤下得融（借）券賣出名單 API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = StockShortSaleListDAO
    LOG_FILE_NAME: str = "stock_short_sale_list_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢
    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得指定日期的整份名單"""

        return self.dao.get_by_date(date)

    def get_covered_dates(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """區間內已入庫的日期；**表不存在時回傳空 list**（等同全部未入庫）"""

        if not self.dao.table_exists():
            return []
        return self.dao.get_distinct_dates(start_date, end_date)

    def get_snapshot(self, date: datetime.date) -> Optional[ShortSaleListSnapshot]:
        """
        - Description:
            把單日名單轉成回測用的快照
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Optional[ShortSaleListSnapshot]
                **該日未入庫（或表不存在）時為 None**——不可以回空快照，
                空快照的意思是「當天沒有任何證券可融券」
        """

        if not self.dao.table_exists():
            return None

        df: pd.DataFrame = self.get(date)
        if df.empty:
            return None

        return ShortSaleListSnapshot(
            listed=frozenset(df["stock_id"]),
            margin_halted=frozenset(df.loc[df["暫停融券賣出"] == 1, "stock_id"]),
            sbl_halted=frozenset(df.loc[df["暫停借券賣出"] == 1, "stock_id"]),
            below_reference_banned=frozenset(
                df.loc[df["禁止平盤下融借券賣出"] == 1, "stock_id"]
            ),
        )
