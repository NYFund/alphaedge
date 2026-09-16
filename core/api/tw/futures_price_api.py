import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.utils.constant import FuturesSession
from core.utils.log_manager import LogManager

"""
Futures Price API: query futures_price_daily table through FuturesPriceDAO

**只讀 `tw_futures.db`，不讀 `downloads/` 下的中繼檔**：中繼檔只是 crawler 到
loader 之間的暫存，唯一鍵與去重要入庫後才成立，資料庫才是唯一的真相來源。

與 `StockPriceAPI` 的三個結構性差異，用之前務必先看懂：

1. **一天不只一列**。股票是 `(date, stock_id)`；期貨是
   `(date, product, expiry, session)`——同一天同一商品有多個到期月的合約在交易，
   日盤與夜盤又是兩筆獨立行情。任何「一天一列」的假設都會錯。
2. **本 API 不做換月，也不挑近月**。查詢一律回傳當日**所有**掛牌中的合約，
   由呼叫端自行決定要哪一個。連續合約與換月規則另有實作（`futures_continuous`、`FuturesRollPlanner`），
   把「近月」的定義藏進 API 只會讓兩處各有一套換月邏輯。
3. **夜盤沒有結算價與未沖銷契約量**（來源就沒有這兩項，欄位為 NULL），
   且 **2017-05-15 之前根本沒有夜盤**。
"""


class FuturesPriceAPI(BaseDataAPI):
    """Futures Price API"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.dao: Optional[FuturesPriceDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
        self.dao = FuturesPriceDAO(conn=self.conn)
        LogManager.setup_logger(
            "futures_price_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    @staticmethod
    def to_session_value(session: Optional[FuturesSession]) -> Optional[str]:
        """
        - Description:
            把交易時段 Enum 轉成 DAO 收的字串值

            `session=None` 代表**不過濾**，日盤與夜盤都回傳——那是「我知道自己
            要處理兩筆」的明確表態，不是預設值。
        - Parameters:
            - session: Optional[FuturesSession]
                交易時段；None 表示不過濾
        - Return:
            - Optional[str]
                `day`／`night`；None 表示不過濾
        """

        return None if session is None else session.value

    def get(
        self,
        date: datetime.date,
        product: Optional[str] = None,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> pd.DataFrame:
        """
        - Description:
            取得指定日期的行情，**當日所有到期月的合約都會回傳**

            預設只取日盤：日盤是一般交易時段，「這一天的行情」在絕大多數語境下
            指的是它；夜盤要另外指定。要兩者都拿就傳 `session=None`。
        - Parameters:
            - date: datetime.date
                查詢日期
            - product: Optional[str]
                商品代碼（Ex: TX）；None 表示所有商品
            - session: Optional[FuturesSession]
                交易時段；None 表示日夜盤都取
        - Return:
            - pd.DataFrame
                依 `expiry` 排序的行情；查無資料時為空 DataFrame
        """

        return self.dao.get_by_date(
            date, product=product, session=self.to_session_value(session)
        )

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        product: Optional[str] = None,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> pd.DataFrame:
        """取得日期範圍內的行情（所有到期月）；參數語意同 `get()`"""

        return self.dao.get_range(
            start_date,
            end_date,
            product=product,
            session=self.to_session_value(session),
        )

    def get_contract_price(
        self,
        product: str,
        expiry: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> pd.DataFrame:
        """
        - Description:
            取得**單一合約**的時間序列（一個到期月從掛牌到最後交易日）

            這是本 API 唯一能安全當成「一天一列」使用的方法：固定
            `(product, expiry, session)` 之後，主鍵只剩 `date`。
        - Parameters:
            - product: str
                商品代碼（Ex: TX）
            - expiry: str
                到期月份（Ex: `202601`；週契約帶 `W` 尾碼如 `202601W1`）
            - start_date / end_date: datetime.date
                查詢區間（含頭含尾）
            - session: Optional[FuturesSession]
                交易時段；None 表示日夜盤都取
        - Return:
            - pd.DataFrame
                依日期排序的行情；查無資料時為空 DataFrame
        """

        return self.dao.get_contract_price(
            product,
            expiry,
            start_date,
            end_date,
            session=self.to_session_value(session),
        )

    def get_trading_days(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        product: Optional[str] = None,
    ) -> List[datetime.date]:
        """
        - Description:
            取得日期範圍內的期貨交易日（已排序、去重）

            判準與 `StockPriceAPI.get_trading_days()` 相同——當日表內有資料即為
            開盤日。**但兩者不可互相替代**：期貨有夜盤、結算日與台股不完全一致，
            期貨交易日曆見 `FuturesCalendar`，本方法只是「表內有哪些日期」的直接回答。

            **不過濾 session**：夜盤成交的那一天同樣是交易日。

            表不存在時回空清單（全新環境的正常狀態），其他查詢錯誤往外拋。
        - Parameters:
            - start_date / end_date: datetime.date
                查詢區間（含頭含尾）
            - product: Optional[str]
                商品代碼；None 表示任一商品有資料即算開盤日
        - Return:
            - List[datetime.date]
                區間內的交易日；無資料時為空 list
        """

        return self.dao.get_trading_days(start_date, end_date, product=product)

    def get_expiries(
        self,
        date: datetime.date,
        product: str,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> List[str]:
        """
        - Description:
            取得指定日期該商品**掛牌中的所有到期月**（已排序）

            回傳的順序即到期先後（字串 `YYYYMM` 的字典序等同時間序），
            但**本方法不指定哪一個是「近月」**——最近的到期月在最後交易日
            當天仍在清單裡，是否該換月是呼叫端的政策，見本檔說明第 2 點。
        - Parameters:
            - date: datetime.date
                查詢日期
            - product: str
                商品代碼（Ex: TX）
            - session: Optional[FuturesSession]
                交易時段；None 表示日夜盤都算
        - Return:
            - List[str]
                到期月清單；查無資料時為空 list
        """

        return self.dao.get_expiries(
            date, product, session=self.to_session_value(session)
        )

    def get_products(self) -> List[str]:
        """取得表內有資料的所有商品代碼（已排序）"""

        return self.dao.get_products()

    # === 具名查詢：下游一律走這一組，不要自行操作 DataFrame 欄位 ===
    @staticmethod
    def build_expiry_map(df: pd.DataFrame, column: str) -> Dict[str, Any]:
        """
        - Description:
            由單日單商品的 DataFrame 建立 `{expiry: 欄位值}` 對照表

            期貨版的 `BaseDataAPI.build_column_map()`——後者的鍵寫死為
            `stock_id`，期貨在固定商品之後的自然鍵是到期月。

            **值維持資料庫原樣不做轉型**（含 `NaN`）：夜盤的 `結算價` 本來就是
            NULL，把它補成 0 會讓「沒有結算價」與「結算價為 0」混為一談。
        - Parameters:
            - df: pd.DataFrame
                單日單商品的行情
            - column: str
                要取的欄位名
        - Return:
            - Dict[str, Any]
                對照表；`df` 為空或無該欄位時回傳空 dict
        """

        if df.empty or column not in df.columns or "expiry" not in df.columns:
            return {}

        deduped: pd.DataFrame = df.drop_duplicates(subset="expiry", keep="first")
        return dict(zip(deduped["expiry"].astype(str), deduped[column]))

    def get_close_map(
        self,
        date: datetime.date,
        product: str,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> Dict[str, Any]:
        """取得單日單商品的收盤價對照表 `{expiry: 收盤價}`"""

        df: pd.DataFrame = self.get(date, product=product, session=session)
        return self.build_expiry_map(df, FuturesPriceColumn.CLOSE.value)

    def get_settlement_map(
        self,
        date: datetime.date,
        product: str,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> Dict[str, Any]:
        """
        取得單日單商品的結算價對照表 `{expiry: 結算價}`

        **夜盤沒有結算價**，`session=FuturesSession.NIGHT` 時每個值都會是 `NaN`。
        """

        df: pd.DataFrame = self.get(date, product=product, session=session)
        return self.build_expiry_map(df, FuturesPriceColumn.SETTLEMENT.value)

    def get_volume_map(
        self,
        date: datetime.date,
        product: str,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> Dict[str, Any]:
        """
        取得單日單商品的成交量對照表 `{expiry: 成交量}`

        **單位是「口」不是「張」**，且不需要像股票那樣除以 `Units.LOT`。
        """

        df: pd.DataFrame = self.get(date, product=product, session=session)
        return self.build_expiry_map(df, FuturesPriceColumn.VOLUME.value)

    def get_open_interest_map(
        self,
        date: datetime.date,
        product: str,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> Dict[str, Any]:
        """
        取得單日單商品的未沖銷契約量對照表 `{expiry: 未沖銷契約量}`

        與結算價一樣，**夜盤沒有這一項**。
        """

        df: pd.DataFrame = self.get(date, product=product, session=session)
        return self.build_expiry_map(df, FuturesPriceColumn.OPEN_INTEREST.value)

    def get_close_series(
        self,
        product: str,
        expiry: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: Optional[FuturesSession] = FuturesSession.DAY,
    ) -> pd.Series:
        """
        - Description:
            取得單一合約的收盤價序列（index 為日期字串）

            技術指標的共通輸入。固定合約之後才有「序列」可言——跨合約直接接起來
            會在換月接點產生假跳空，那要走 `futures_continuous` 連續合約。
        - Parameters:
            - product / expiry: str
                商品代碼與到期月
            - start_date / end_date: datetime.date
                查詢區間（含頭含尾）
            - session: Optional[FuturesSession]
                交易時段
        - Return:
            - pd.Series
                收盤價序列；查無資料時為空 Series
        """

        df: pd.DataFrame = self.get_contract_price(
            product=product,
            expiry=expiry,
            start_date=start_date,
            end_date=end_date,
            session=session,
        )
        close_col: str = FuturesPriceColumn.CLOSE.value

        if df.empty or close_col not in df.columns:
            return pd.Series(dtype=float)
        return df.set_index("date")[close_col]
