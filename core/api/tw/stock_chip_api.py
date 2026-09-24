import datetime
from pathlib import Path
from typing import Any, Dict, Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import (
    TW_STOCK_DB_PATH,
)
from core.config.schema import ChipColumn
from core.dao.base import BaseDAO
from core.dao.tw.stock_chip_dao import StockChipDAO

"""Institutional investors chip API: query chip table through StockChipDAO"""


class StockChipAPI(BaseDataAPI):
    """Institutional investors chip API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = StockChipDAO
    LOG_FILE_NAME: str = "stock_chip_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢

    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的三大法人籌碼"""

        return self.dao.get_by_date(date)

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票日期範圍內的三大法人籌碼"""

        return self.dao.get_range(start_date, end_date)

    def get_stock_chip(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的三大法人籌碼"""

        return self.dao.get_by_stock(stock_id, start_date, end_date)

    # === 具名查詢：策略層一律走這一組，不要自行操作 DataFrame 欄位 ===
    def get_foreign_net_shares_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的外資買賣超股數對照表

            **值為買賣超，賣超是負數**；單位是「股」不是「張」，呼叫端要比較
            張數門檻時須自行乘 `Units.LOT`，不要直接拿張數與本值相比。

            取值細節見 `BaseDataAPI.build_column_map()`；值維持資料庫原樣，
            由呼叫端決定如何轉型與判斷。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - Dict[str, Any]
                {stock_id: 外資買賣超股數}
        """

        df: pd.DataFrame = self.get(date)
        return self.build_column_map(df, ChipColumn.FOREIGN_NET_SHARES.value)

    def get_trust_net_shares_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的投信買賣超股數對照表

            範圍僅限現有策略實際用到的欄位，不預先補齊整張表——沒有呼叫端的
            方法只會變成下一批死碼。

            取值細節見 `BaseDataAPI.build_column_map()`；值維持資料庫原樣，
            由呼叫端決定如何轉型與判斷。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - Dict[str, Any]
                {stock_id: 投信買賣超股數}
        """

        df: pd.DataFrame = self.get(date)
        return self.build_column_map(df, ChipColumn.TRUST_NET_SHARES.value)

    def get_net_chip(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票的三大法人淨買賣超"""

        if start_date > end_date:
            return pd.DataFrame()

        # 區間查詢走 `get_range()`；`get()` 只收單一 `date`，傳兩個參數會 `TypeError`
        df: pd.DataFrame = self.get_range(start_date, end_date)
        if df.empty:
            return df

        df = df.loc[
            :,
            (
                "date",
                "stock_id",
                "證券名稱",
                "外資買賣超股數",
                "投信買賣超股數",
                "自營商買賣超股數",
            ),
        ]
        return df

    def get_stock_net_chip(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的三大法人淨買賣超"""

        if start_date > end_date:
            return pd.DataFrame()

        df: pd.DataFrame = self.get_stock_chip(stock_id, start_date, end_date)
        df = df.loc[
            :,
            (
                "date",
                "stock_id",
                "證券名稱",
                "外資買賣超股數",
                "投信買賣超股數",
                "自營商買賣超股數",
            ),
        ]
        return df
