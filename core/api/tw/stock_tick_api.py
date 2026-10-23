import datetime
from typing import Optional

import pandas as pd
from loguru import logger

from core.api.base import BaseDataAPI
from core.dao.tw.stock_tick_dao import StockTickDAO

"""
台股 tick API：查詢 TimescaleDB 的 `stock_tick`

查詢一律經 `StockTickDAO`（讀取走 ConnectorX）；回傳欄位、dtype 與排序是固定契約，
`StockQuoteAdapter` 依賴它們。`seq` 只用來讓同一時間戳記的多筆成交有固定順序，不回傳。
"""


class StockTickAPI(BaseDataAPI):
    """
    - Description:
        台股逐筆成交查詢

        回傳欄位固定為 `stock_id, time, close, volume, bid_price, bid_volume,
        ask_price, ask_volume, tick_type`；`time` 是不帶時區的台北當地時間。
        日期區間兩端都包含（以「迄日隔天 00:00 之前」的半開區間查詢）。
    """

    LOG_FILE_NAME: str = "stock_tick_api.log"

    def __init__(self, dao: Optional[StockTickDAO] = None) -> None:
        """
        - Description:
            建立 API；連不上資料庫或還沒建表時當場拋出，不讓回測跑完才發現整段沒有報價
        - Parameters:
            - dao: Optional[StockTickDAO]
                共用的 tick DAO（整合測試用來指向暫存 schema）；
                未指定時自行建立並在 `close()` 關閉
        """

        self.dao: Optional[StockTickDAO] = dao
        self.owns_dao: bool = dao is None
        # 不傳 SQLite 連線：`DEFAULT_DB_PATH` 為 None，基底不會開 SQLite
        super().__init__()

    def setup(self) -> None:
        """建立 tick DAO 並確認資料表存在"""

        super().setup()

        if self.dao is None:
            self.dao = StockTickDAO()
        if not self.dao.table_exists():
            message: str = (
                f"TimescaleDB 裡沒有 {self.dao.schema}.{StockTickDAO.TABLE_NAME}："
                "tick 尚未匯入，無法做 tick 級回測"
            )
            logger.error(message)
            raise RuntimeError(message)

    def close(self) -> None:
        """
        關閉自己建立的 DAO

        讀取走 ConnectorX，每次查詢自行開關連線；常駐的只有 DAO 用來確認建表的 psycopg 連線
        """

        if self.owns_dao and self.dao is not None:
            self.dao.close()
            self.dao = None
        super().close()

    def get(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得區間內全市場的 tick，依股票、時間排序（同一檔的 tick 連在一起）"""

        if start_date > end_date:
            return pd.DataFrame()
        return self.dao.query_ticks(start_date, end_date, ("stock_id", "time", "seq"))

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """
        取得區間內全市場的 tick，所有股票混在一起依時間排序（模擬盤中的成交順序）

        同一時間戳記跨股票的順序以代號固定下來，回測才可重現。
        """

        if start_date > end_date:
            return pd.DataFrame()
        return self.dao.query_ticks(start_date, end_date, ("time", "stock_id", "seq"))

    def get_stock_ticks(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得個股區間內的 tick，依時間排序"""

        if start_date > end_date:
            return pd.DataFrame()
        return self.dao.query_ticks(
            start_date, end_date, ("time", "seq"), stock_id=stock_id
        )

    def get_last_tick(
        self,
        stock_id: str,
        date: datetime.date,
    ) -> pd.DataFrame:
        """取得當日最後一筆 tick"""

        tick: pd.DataFrame = self.get_stock_ticks(stock_id, date, date)

        if tick.empty:
            return pd.DataFrame()
        return tick.iloc[-1:]
