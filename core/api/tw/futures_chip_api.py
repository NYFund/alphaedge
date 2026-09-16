import datetime
from typing import Any, Dict, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import (
    API_LOG_FILE_LEVEL,
    API_LOGS_DIR_PATH,
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_LARGE_TRADER_TABLE_NAME,
    FUTURES_PUT_CALL_RATIO_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.utils.log_manager import LogManager

"""
Futures Chip API: 三大法人、大額交易人與選擇權 PCR

**本 API 的存在理由是前視偏差，不是查詢便利**。

三個籌碼資料集**全部是盤後公布**：8/28 的三大法人要等 8/28 收盤後才看得到。
回測若在 8/28 的訊號裡讀到 8/28 的籌碼，等於用當天收盤後才知道的資訊去下
當天的單——那是最典型、也最不容易被發現的前視偏差，因為程式完全不會報錯，
只會讓回測績效好得不合理。

故本 API 的主要入口是 `get_available()`：**只回傳「查詢日之前」已公布的資料**，
語意是「站在這一天早上，我能知道什麼」。要看某一天實際公布了什麼，
走 `get_on_date()`——那是研究用途，**不可用於產生訊號**。

| 方法 | 語意 | 回測可用 |
|------|------|:--------:|
| `get_available(date)` | 該日**之前**最近一次公布的籌碼 | ✅ |
| `get_on_date(date)` | 該日當天公布的籌碼 | ❌（前視） |

`table=` 參數只接受三張籌碼表（白名單由 `FuturesChipDAO` 把關），其他值一律 `ValueError`。
"""


class FuturesChipAPI(BaseDataAPI):
    """Futures Chip API（三張籌碼表）"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.daos: Dict[str, FuturesChipDAO] = {}

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
        LogManager.setup_logger(
            "futures_chip_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get_dao(self, table: str) -> FuturesChipDAO:
        """
        - Description:
            取得指定籌碼表的 DAO（共用本 API 的連線，依表名快取）
        - Parameters:
            - table: str
                籌碼表名稱
        - Return:
            - FuturesChipDAO
                該表的 DAO
        - Raise:
            - ValueError
                表名不是三張籌碼表之一（舊版沒有檢查就組進 SQL）
        """

        if table not in self.daos:
            self.daos[table] = FuturesChipDAO(table, conn=self.conn)
        return self.daos[table]

    # === 前視偏差對齊：回測一律走這一組 ===
    def get_available(
        self,
        date: datetime.date,
        table: str = FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    ) -> pd.DataFrame:
        """
        - Description:
            取得「站在 `date` 這一天早上」能知道的籌碼

            實作即「取 `資料日 < date` 的最大者」——籌碼盤後才公布，
            當天的資料當天不可能知道。**不是 `<=`**：那一個等號就是前視偏差。
        - Parameters:
            - date: datetime.date
                回測當前日
            - table: str
                要查的籌碼表
        - Return:
            - pd.DataFrame
                最近一次已公布的籌碼；查無資料時為空 DataFrame
        """

        latest: Optional[str] = self.get_latest_available_date(date, table)
        if latest is None:
            return pd.DataFrame()

        return self.get_dao(table).get_by_date(latest)

    def get_latest_available_date(
        self,
        date: datetime.date,
        table: str = FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    ) -> Optional[str]:
        """該日之前最近一個有籌碼的日期（**嚴格小於**，見 `get_available()`）"""

        # 表還沒建（尚未跑過籌碼 ETL）才回 None；其餘錯誤一律上拋，
        # 否則「資料庫被鎖住」會被當成「這天之前沒有籌碼」而靜默少開倉
        return self.get_dao(table).get_latest_date_before(date)

    # === 研究用：看某一天實際公布了什麼 ===
    def get_on_date(
        self,
        date: datetime.date,
        table: str = FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    ) -> pd.DataFrame:
        """
        取得該日**當天公布**的籌碼

        ⚠️ **不可用於產生訊號**：當日籌碼要收盤後才知道，拿來下當日的單即為
        前視偏差。回測請走 `get_available()`。
        """

        # 舊版 `except pd.errors.DatabaseError` 一律回空表，連「被鎖住」也吞；
        # 改為只有表不存在時回空表
        return self.get_dao(table).get_by_date(date)

    # === 具名查詢 ===
    def get_institutional_net(
        self, date: datetime.date, product_name: str, investor: str
    ) -> Optional[float]:
        """
        取得某法人在某商品的**未平倉口數淨額**（多 − 空），已做前視對齊

        這是籌碼策略最常用的一個數字：外資的台指期未平倉淨額。
        """

        df: pd.DataFrame = self.get_available(
            date, FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
        )
        if df.empty:
            return None

        matched: pd.DataFrame = df[
            (df["product_name"] == product_name) & (df["investor"] == investor)
        ]
        if matched.empty or "多空未平倉口數淨額" not in matched.columns:
            return None

        return float(matched.iloc[0]["多空未平倉口數淨額"])

    def get_put_call_ratio(self, date: datetime.date) -> Optional[Dict[str, Any]]:
        """取得已公布的選擇權 PCR（成交量比與未平倉量比），已做前視對齊"""

        df: pd.DataFrame = self.get_available(date, FUTURES_PUT_CALL_RATIO_TABLE_NAME)
        return None if df.empty else df.iloc[0].to_dict()

    def get_large_trader(
        self,
        date: datetime.date,
        product: str,
        expiry: str = "999999",
        trader_type: str = "0",
    ) -> Optional[Dict[str, Any]]:
        """
        - Description:
            取得某商品的大額交易人部位，已做前視對齊

            **預設取 `999999` ＋ 類別 `0`**：前者是「所有契約合計」、後者是
            「前五／十大交易人」。類別 `1` 是其中的**特定法人**，是 `0` 的子集，
            兩者相加沒有意義（見 `FuturesChipCleaner`）。
        - Parameters:
            - date: datetime.date
                回測當前日
            - product: str
                契約代碼（Ex: TX）
            - expiry: str
                到期月份；`999999` 為所有契約合計、`666666` 為所有週契約合計
            - trader_type: str
                交易人類別；`0` 為前五／十大、`1` 為其中的特定法人
        - Return:
            - Optional[Dict[str, Any]]
                該列資料；查無資料時為 None
        """

        df: pd.DataFrame = self.get_available(date, FUTURES_LARGE_TRADER_TABLE_NAME)
        if df.empty:
            return None

        matched: pd.DataFrame = df[
            (df["product"] == product)
            & (df["expiry"] == expiry)
            & (df["trader_type"] == trader_type)
        ]
        return None if matched.empty else matched.iloc[0].to_dict()

    def get_covered_date_range(
        self, table: str = FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME
    ) -> Optional[Dict[str, str]]:
        """該表的資料涵蓋範圍（供人工確認回補進度）"""

        covered = self.get_dao(table).get_covered_date_range()

        if covered is None:
            return None
        return {"earliest": covered[0], "latest": covered[1]}
