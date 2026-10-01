import datetime
from pathlib import Path
from typing import List, Optional, Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.stock_day_trade_list_dao import StockDayTradeListDAO
from core.models import DayTradeListSnapshot

"""Stock day trade list API: 現股當沖標的名單（`day_trade_list` 表）"""


class StockDayTradeListAPI(BaseDataAPI):
    """現股當沖標的名單 API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = StockDayTradeListDAO
    LOG_FILE_NAME: str = "stock_day_trade_list_api.log"

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

    def get_snapshot(self, date: datetime.date) -> Optional[DayTradeListSnapshot]:
        """
        - Description:
            把單日名單轉成回測用的快照
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Optional[DayTradeListSnapshot]
                **該日未入庫（或表不存在）時為 None**，理由同
                `StockShortSaleListAPI.get_snapshot()`
        """

        if not self.dao.table_exists():
            return None

        df: pd.DataFrame = self.get(date)
        if df.empty:
            return None

        return DayTradeListSnapshot(
            day_tradable=frozenset(df["stock_id"]),
            sell_first_halted=frozenset(
                df.loc[df["暫停先賣後買當沖"] == 1, "stock_id"]
            ),
        )
