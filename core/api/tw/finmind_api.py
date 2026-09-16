import datetime
from typing import Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO
from core.utils.log_manager import LogManager

"""取得 FinMind 資料的 API

Usage:
    from core.api.tw.finmind_api import FinMindAPI

    api = FinMindAPI()

    # 由 DataFeed 傳入共用連線時，`close()` 不會關掉別人的連線
    api = FinMindAPI(conn=shared_conn)

    # 台股總覽（不含權證）
    df = api.get_all_stock_info()
    row = api.get_stock_info("2330")

    # 台股總覽（含權證）
    df = api.get_all_stock_info_with_warrant()
    row = api.get_stock_info_with_warrant("2330")

    # 證券商資訊
    df = api.get_all_broker_info()
    row = api.get_broker_info("9A00")

    # 當日券商分點統計（依股票、日期、券商查詢）
    df = api.get_broker_trading_for_stock_on_date("2330", date)
    df = api.get_broker_trading_for_stock_in_range("2330", start_date, end_date)
    df = api.get_broker_trading_by_date(date)
    df = api.get_broker_trading_range(start_date, end_date)
    df = api.get_broker_trading_by_broker_and_date("永豐金證券", date)
"""


class FinMindAPI(BaseDataAPI):
    """FinMind 資料 API：台股總覽、證券商資訊、券商分點日報"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立（與其他 core/api/tw/ 一致）
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.stock_info_dao: Optional[StockInfoDAO] = None
        self.stock_info_with_warrant_dao: Optional[StockInfoWithWarrantDAO] = None
        self.securities_trader_info_dao: Optional[SecuritiesTraderInfoDAO] = None
        self.broker_trading_dao: Optional[BrokerTradingDAO] = None

        self.setup()

    def setup(self) -> None:
        """設定連線與 log"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
        self.stock_info_dao = StockInfoDAO(conn=self.conn)
        self.stock_info_with_warrant_dao = StockInfoWithWarrantDAO(conn=self.conn)
        self.securities_trader_info_dao = SecuritiesTraderInfoDAO(conn=self.conn)
        self.broker_trading_dao = BrokerTradingDAO(conn=self.conn)
        LogManager.setup_logger(
            "finmind_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    # -----------------------------------------------------------------------
    # 台股總覽 (taiwan_stock_info)
    # -----------------------------------------------------------------------
    # 欄位: industry_category, stock_id, stock_name, type, date
    # PK: stock_id
    #

    def get_stock_info(self, stock_id: str) -> pd.DataFrame:
        """取得單一股票的台股總覽（不含權證）"""

        return self.stock_info_dao.get_by_stock(stock_id)

    def get_all_stock_info(self) -> pd.DataFrame:
        """取得全部台股總覽（不含權證）"""

        return self.stock_info_dao.get_all()

    # -----------------------------------------------------------------------
    # 台股總覽含權證 (taiwan_stock_info_with_warrant)
    # -----------------------------------------------------------------------
    # 欄位同上，PK: stock_id
    #

    def get_stock_info_with_warrant(self, stock_id: str) -> pd.DataFrame:
        """取得單一股票的台股總覽（含權證）"""

        return self.stock_info_with_warrant_dao.get_by_stock(stock_id)

    def get_all_stock_info_with_warrant(self) -> pd.DataFrame:
        """取得全部台股總覽（含權證）"""

        return self.stock_info_with_warrant_dao.get_all()

    # -----------------------------------------------------------------------
    # 證券商資訊 (taiwan_securities_trader_info)
    # -----------------------------------------------------------------------
    # 欄位: securities_trader_id, securities_trader, date, address, phone
    # PK: securities_trader_id
    #

    def get_broker_info(self, securities_trader_id: str) -> pd.DataFrame:
        """依證券商代號取得單一證券商資訊"""

        return self.securities_trader_info_dao.get_by_trader_id(securities_trader_id)

    def get_all_broker_info(self) -> pd.DataFrame:
        """取得全部證券商資訊"""

        return self.securities_trader_info_dao.get_all()

    # -----------------------------------------------------------------------
    # 當日券商分點統計 (taiwan_stock_trading_daily_report_secid_agg)
    # -----------------------------------------------------------------------
    # 欄位: securities_trader, securities_trader_id, stock_id, date,
    #       buy_volume, sell_volume, buy_price, sell_price
    # PK: (stock_id, date, securities_trader_id)
    #

    def get_broker_trading_by_date(self, date: datetime.date) -> pd.DataFrame:
        """取得指定日期的全部券商分點日報"""

        return self.broker_trading_dao.get_by_date(date)

    def get_broker_trading_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得日期區間內的全部券商分點日報"""

        return self.broker_trading_dao.get_range(start_date, end_date)

    def get_broker_trading_for_stock_on_date(
        self,
        stock_id: str,
        date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定股票在指定日期的券商分點日報（單日）"""

        return self.broker_trading_dao.get_by_stock_and_date(stock_id, date)

    def get_broker_trading_for_stock_in_range(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定股票在日期區間內的券商分點日報（多日）"""

        return self.broker_trading_dao.get_by_stock_in_range(
            stock_id, start_date, end_date
        )

    def get_broker_trading_by_broker_and_date(
        self,
        securities_trader: str,
        date: datetime.date,
    ) -> pd.DataFrame:
        """依券商中文名稱與日期取得該券商當日所有股票的分點日報"""

        return self.broker_trading_dao.get_by_trader_name_and_date(
            securities_trader, date
        )
