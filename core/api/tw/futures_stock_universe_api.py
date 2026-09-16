import datetime
import sqlite3
from typing import Dict, List, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_FUTURES_DB_PATH
from core.dao.connection import connect_sqlite
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.utils.log_manager import LogManager

"""
Futures Stock Universe API: 股票期貨標的池的讀取層

**股期與指數期貨最根本的差異：乘數不是常數**。

指數期貨的契約乘數寫在 `FUTURES_MULTIPLIER`（TX 200、MTX 50），幾十年不變；
股票期貨的「契約單位」則是**標準型 2,000 股**，但**除權息之後會被交易所調整**
（配股、現金增資都會改變它），調整後的契約甚至會換一個代碼（`EE1`、`EE2`…）。
因此股期的乘數必須查**當時**的快照，寫死或用今天的值回測歷史一定錯。

---

**與「台股還原價」的關係——這是最容易雙重調整的地方**：

台股的除權息用**還原價**處理（價格往回調整）；股期則是**調整契約單位**
（價格不動、每口的股數變）。兩者是同一件事的兩種處理方式，
**擇一即可，同時套用就是雙重調整**。

故本專案的規則是：**股期行情一律用原始價，不套任何還原係數**
（`FuturesQuote.adj_close` 恆為 None），除權息的影響由「當時的契約單位」承接。
"""


class FuturesStockUniverseAPI(BaseDataAPI):
    """Futures Stock Universe API（股期商品清單、契約單位歷史、流動性排序）"""

    # 標準型契約單位（股）；`futures_stock_universe.contract_size` 的預設值
    STANDARD_CONTRACT_SIZE: int = 2000

    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[sqlite3.Connection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.dao: Optional[FuturesStockUniverseDAO] = None
        self.price_dao: Optional[FuturesPriceDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
        self.dao = FuturesStockUniverseDAO(conn=self.conn)
        self.price_dao = FuturesPriceDAO(conn=self.conn)
        LogManager.setup_logger(
            "futures_stock_universe_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    # === 快照查詢 ===
    def get_snapshot_date(self, date: Optional[datetime.date] = None) -> Optional[str]:
        """
        取得不晚於 `date` 的最近一份快照日；`date` 為 None 時取最新一份

        **本表是快照序列**（每次更新新增一份），故任何「某日的狀態」都要先解出
        該日適用的快照日，再以它為條件查詢。查詢日早於第一份快照時退回最早的一份；
        表還沒建時回 None，被鎖住或 schema 壞掉一律上拋。
        """

        return self.dao.resolve_snapshot_date(date)

    def get_products(
        self,
        date: Optional[datetime.date] = None,
        product_types: Optional[List[str]] = None,
    ) -> List[str]:
        """
        - Description:
            取得該日在列的股期商品代碼

            **這是股期行情 ETL 的商品清單來源**：股期 320 檔且會隨掛牌／下市異動，
            寫死清單必然過期。
        - Parameters:
            - date: Optional[datetime.date]
                查詢日；None 表示取最新快照
            - product_types: Optional[List[str]]
                只取這些類型（見 `StockFuturesType`）；None 表示全部
        - Return:
            - List[str]
                商品代碼；查無快照時為空 list
        """

        snapshot: Optional[str] = self.get_snapshot_date(date)
        if snapshot is None:
            return []

        return self.dao.get_product_ids(snapshot, product_types)

    def get_contract_size(
        self, product_id: str, date: Optional[datetime.date] = None
    ) -> Optional[int]:
        """
        - Description:
            取得該股期在指定日期的**契約單位（股）＝ 乘數**

            **不可用 `STANDARD_CONTRACT_SIZE` 當預設值**：小型股期是 100 股、
            ETF 期貨是 10,000 股，而除權息調整後的契約更是任意數字。
            查不到就回 None，讓呼叫端決定要中止還是跳過。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: Optional[datetime.date]
                查詢日；None 表示最新快照；**該日適用的快照中沒有這個商品就回 None**
                （那天不在列，回測不該拿到乘數），與保證金試算的查法不同，見
                `FuturesStockUniverseDAO.get_contract_size()`
        - Return:
            - Optional[int]
                契約單位；查無資料時為 None
        """

        return self.dao.get_contract_size(product_id, date, per_product=False)

    def get_contract_size_history(self, product_id: str) -> pd.DataFrame:
        """
        - Description:
            取得契約單位的**變動序列**（只列出真的變動的那幾份快照）

            除權息造成的乘數調整只能由快照差分推得——來源沒有「調整生效日」
            這個欄位。快照愈密集，推出來的生效日愈準；本表建於 2026-08-29，
            更早的調整一律看不到。
        - Parameters:
            - product_id: str
                股期代碼
        - Return:
            - pd.DataFrame
                `snapshot_date` 與 `contract_size`；無變動時只有一列
        """

        df: pd.DataFrame = self.dao.get_contract_size_series(product_id)
        if df.empty:
            return df

        # 只留下與前一份不同的列（含第一列）
        changed: pd.Series = df["contract_size"] != df["contract_size"].shift()
        return df[changed].reset_index(drop=True)

    def get_underlying(
        self, product_id: str, date: Optional[datetime.date] = None
    ) -> Optional[Dict[str, str]]:
        """取得標的證券（代號與名稱），供與 `tw_stock.db` 對照"""

        snapshot: Optional[str] = self.get_snapshot_date(date)
        if snapshot is None:
            return None

        row = self.dao.get_underlying(snapshot, product_id)
        if row is None:
            return None
        return {
            "underlying_stock_id": row[0],
            "underlying_name": row[1],
            "product_type": row[2],
        }

    # === 流動性 ===
    def get_top_liquid_products(
        self,
        top_n: int = 20,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        min_days: int = 5,
    ) -> List[str]:
        """
        - Description:
            依**成交量**取流動性前 N 檔股期

            **流動性差距是數量級的**：股期 320 檔裡，前 20 檔的成交量佔了絕大多數，
            尾端有整批一天成交個位數口的商品——把它們納入回測只會製造
            「回測賺錢、實際掛不到單」的假訊號。

            **成交量取自行情表而不是標的池**：標的池沒有量的欄位，這也是本篩選
            不放在標的池 ETL 的原因。故**必須先有股期行情才能篩**。
        - Parameters:
            - top_n: int
                取前幾檔
            - start_date / end_date: Optional[datetime.date]
                統計區間；None 表示不限
            - min_days: int
                至少要有幾個交易日的資料才納入排序——只上市兩天就爆量的商品
                不能代表長期流動性
        - Return:
            - List[str]
                商品代碼，依平均日成交量由大到小；無資料時為空 list
        """

        universe: List[str] = self.get_products(end_date)
        if not universe:
            return []

        df: pd.DataFrame = self.price_dao.get_day_session_volume_stats(
            universe, start_date, end_date
        )

        if df.empty:
            return []

        df = df[df["trading_days"] >= min_days].copy()
        if df.empty:
            return []

        df["avg_volume"] = df["total_volume"] / df["trading_days"]
        df = df.sort_values("avg_volume", ascending=False)

        return df["product"].head(top_n).tolist()
