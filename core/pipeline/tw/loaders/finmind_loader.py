import shutil
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.config import FINMIND_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.dao.tw.securities_trader_info_dao import SecuritiesTraderInfoDAO
from core.dao.tw.stock_info_dao import StockInfoDAO, StockInfoWithWarrantDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.tw.loaders.finmind import (
    broker_info_loader,
    broker_trading_loader,
    stock_info_loader,
)

"""
FinMind Loader

本檔是**門面（facade）**：對外介面與呼叫方式維持不變，四張表的 schema 與各自的
入庫流程拆在 `core/pipeline/tw/loaders/finmind/` 底下（見該套件的說明）。
"""


class FinMindLoader(BaseDataLoader):
    """FinMind Loader - 將 FinMind 資料存入資料庫"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        """
        - Description:
            建立 FinMind loader
        - Parameters:
            - conn: Optional[DBConnection]
                共用連線（通常由 updater 傳入，讓讀寫走同一條連線）。
                四張表共用一條連線，故收連線而不是單一 DAO；指定時 loader 不擁有它，
                `disconnect()` 不會關閉；未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # Downloads directory Path
        self.finmind_dir: Path = FINMIND_DOWNLOADS_PATH

        self.setup()

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Loader"""
        self.connect()

        # Ensure Database Tables Exist
        self.create_missing_tables()

        self.finmind_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.conn is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
            self.owns_conn = True

    def disconnect(self) -> None:
        """Disconnect the Database；共用連線由建立者關閉"""

        if not self.owns_conn:
            return

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def commit(self) -> None:
        """提交目前的交易（批次更新時由 updater 定期呼叫）"""

        if self.conn is not None:
            self.conn.commit()

    def create_db(self, *args, **kwargs) -> None:
        """Create New Database Tables"""

        self.create_missing_tables()

    def create_missing_tables(self) -> None:
        """確保所有 FinMind 資料表存在；券商分點表的索引每次都補（`IF NOT EXISTS`）"""

        StockInfoDAO(conn=self.conn).ensure_table()
        StockInfoWithWarrantDAO(conn=self.conn).ensure_table()
        SecuritiesTraderInfoDAO(conn=self.conn).ensure_table()
        BrokerTradingDAO(conn=self.conn).ensure_table()

    def add_to_db(self, remove_files: bool = False) -> None:
        """Add Data into Database from CSV files"""

        if self.conn is None:
            self.connect()

        # Ensure Database Tables Exist
        self.create_missing_tables()

        # 處理四個 CSV 檔案
        self.load_stock_info()
        self.load_stock_info_with_warrant()
        self.load_broker_info()
        self.load_broker_trading_daily_report()  # 不傳入 df，從 CSV 檔案載入

        self.conn.commit()
        self.disconnect()

        if remove_files:
            shutil.rmtree(self.finmind_dir)
            logger.info(f"Removed directory: {self.finmind_dir}")

    def load_stock_info(self) -> None:
        """載入台股總覽資料到資料庫"""

        stock_info_loader.load_stock_info(self.conn, self.finmind_dir)

    def load_stock_info_with_warrant(self) -> None:
        """載入台股總覽(含權證)資料到資料庫"""

        stock_info_loader.load_stock_info_with_warrant(self.conn, self.finmind_dir)

    def load_broker_info(self) -> None:
        """載入證券商資訊表資料到資料庫"""

        broker_info_loader.load_broker_info(self.conn, self.finmind_dir)

    def load_broker_trading_daily_report(
        self,
        df: Optional[pd.DataFrame] = None,
        commit: bool = True,
    ) -> Optional[int]:
        """
        - Description:
            載入當日券商分點統計表資料到資料庫

            如果傳入 df 參數，則直接從 DataFrame 載入；否則從 CSV 檔案載入
            （檔案結構：`broker_trading/{broker_id}/{stock_id}.csv`；批量更新不寫這些 CSV，
            故此路徑不含批量更新抓進 DB 的資料）
        - Parameters:
            - df: Optional[pd.DataFrame]
                必須包含 `stock_id`／`date`／`securities_trader_id`；
                `buy_volume`／`sell_volume`／`buy_price`／`sell_price`／`securities_trader` 可選
            - commit: bool
                DataFrame 路徑是否在寫入後立即 commit；批次更新時由 updater 傳 False 並定期 commit
        - Return:
            - Optional[int]
                DataFrame 路徑回傳新寫入的列數；CSV 路徑回傳 None
        """
        if self.conn is None:
            self.connect()

        # 確保資料表存在
        self.create_missing_tables()

        # 如果提供了 DataFrame，直接載入
        if df is not None:
            return broker_trading_loader.load_from_dataframe(
                self.conn, df, commit=commit
            )
        else:
            # 從 CSV 檔案載入
            broker_trading_loader.load_from_files(self.conn, self.finmind_dir)
            return None
