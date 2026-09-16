import datetime
import sqlite3
from pathlib import Path
from typing import Dict, Optional, Union

from loguru import logger

from core.config import BROKER_TRADING_METADATA_PATH, TW_STOCK_DB_PATH
from core.dao.connection import connect_sqlite
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.finmind_cleaner import FinMindCleaner
from core.pipeline.tw.crawlers.finmind_crawler import FinMindCrawler
from core.pipeline.tw.loaders.finmind_loader import FinMindLoader
from core.pipeline.tw.updaters.finmind import (
    BrokerInfoUpdater,
    BrokerTradingMetadataStore,
    BrokerTradingUpdater,
    FinMindContext,
    StockInfoUpdater,
)
from core.pipeline.utils import FinMindDataType
from core.utils.log_manager import LogManager

"""
FinMind data updater: stock info with warrant, broker info, broker trading daily report

本檔是**門面（facade）**：對外介面與 `tasks/update_db.py` 的呼叫方式維持不變，
實作按資料集拆在 `core/pipeline/tw/updaters/finmind/` 底下（見該套件的說明）。
"""


class FinMindUpdater(BaseDataUpdater):
    """FinMind Updater"""

    # 預設日期（update_all 時 broker_trading 若未給 start_date）
    DEFAULT_BROKER_TRADING_START_DATE: datetime.date = datetime.date(2021, 6, 30)

    def __init__(self) -> None:
        super().__init__()

        # **讀（股票／券商清單、metadata 重建）與寫（loader）共用同一條連線**：舊版各開
        # 一條連線到同一個 DB，券商分點批次更新前還得先 commit loader 那條，
        # 否則 updater 的 SELECT 會被寫入鎖卡住
        self.conn: Optional[sqlite3.Connection] = connect_sqlite(TW_STOCK_DB_PATH)

        # ETL
        self.crawler: FinMindCrawler = FinMindCrawler()
        self.cleaner: FinMindCleaner = FinMindCleaner()
        self.loader: FinMindLoader = FinMindLoader(conn=self.conn)

        # Broker trading metadata 文件路徑（記錄每個 broker_id 和 stock_id 的日期範圍）
        self.broker_trading_metadata_path: Path = BROKER_TRADING_METADATA_PATH

        self.setup()

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_finmind.log")

        # 共用執行環境：四個資料集打的是同一把 token，quota 狀態掛在這裡
        self.context: FinMindContext = FinMindContext(
            crawler=self.crawler,
            cleaner=self.cleaner,
            loader=self.loader,
            conn=self.conn,
        )
        self.metadata: BrokerTradingMetadataStore = BrokerTradingMetadataStore(
            metadata_path=self.broker_trading_metadata_path,
            conn=self.conn,
        )

        # 各資料集的更新流程
        self.stock_info: StockInfoUpdater = StockInfoUpdater(self.context)
        self.broker_info: BrokerInfoUpdater = BrokerInfoUpdater(self.context)
        self.broker_trading: BrokerTradingUpdater = BrokerTradingUpdater(
            self.context, self.metadata
        )

        # 動態獲取 API quota 限制
        self.context.refresh_api_quota_limit()

    def close(self) -> None:
        """關閉資料連線（loader 共用同一條連線，一併結束）"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @property
    def api_quota_limit(self) -> int:
        """FinMind 每小時配額上限（狀態實際存放在 `context`）"""

        return self.context.api_quota_limit

    @api_quota_limit.setter
    def api_quota_limit(self, value: int) -> None:
        self.context.api_quota_limit = value

    def update(
        self,
        data_type: Optional[Union[str, FinMindDataType]] = None,
        start_date: Optional[Union[datetime.date, str]] = None,
        end_date: Optional[Union[datetime.date, str]] = None,
        **kwargs,
    ) -> None:
        """
        通用更新方法

        Args:
            data_type: 資料類型，可選值：
                - FinMindDataType.STOCK_INFO 或 "stock_info": 更新台股總覽（不含權證）
                - FinMindDataType.STOCK_INFO_WITH_WARRANT 或 "stock_info_with_warrant": 更新台股總覽（含權證）
                - FinMindDataType.BROKER_INFO 或 "broker_info": 更新證券商資訊
                - FinMindDataType.BROKER_TRADING 或 "broker_trading": 更新券商分點統計
                - "all" 或 None: 更新所有資料
            start_date: 起始日期（僅用於 BROKER_TRADING）
            end_date: 結束日期（僅用於 BROKER_TRADING）
        """
        # 處理 "all" 或 None 的情況
        if data_type is None or (
            isinstance(data_type, str) and data_type.lower() == "all"
        ):
            self.update_all(
                start_date=start_date,
                end_date=end_date,
            )
            return

        # 將字串轉換為 Enum（向後兼容）
        if isinstance(data_type, str):
            data_type_str: str = data_type.upper()
            try:
                data_type = FinMindDataType(data_type_str)
            except ValueError:
                # 嘗試小寫形式（更友好的用戶輸入）
                data_type_str_lower: str = data_type.lower()
                type_mapping: Dict[str, FinMindDataType] = {
                    dt.value.lower(): dt for dt in FinMindDataType
                }
                if data_type_str_lower in type_mapping:
                    data_type = type_mapping[data_type_str_lower]
                else:
                    raise ValueError(
                        f"Unknown data_type string: {data_type}. "
                        f"Supported strings: {[dt.value.lower() for dt in FinMindDataType]}, 'all'"
                    )

        if data_type == FinMindDataType.STOCK_INFO:
            self.update_stock_info()
        elif data_type == FinMindDataType.STOCK_INFO_WITH_WARRANT:
            self.update_stock_info_with_warrant()
        elif data_type == FinMindDataType.BROKER_INFO:
            self.update_broker_info()
        elif data_type == FinMindDataType.BROKER_TRADING:
            if start_date is None or end_date is None:
                raise ValueError(
                    "start_date and end_date are required for BROKER_TRADING"
                )
            self.update_broker_trading_daily_report(
                start_date=start_date,
                end_date=end_date,
            )
        else:
            raise ValueError(
                f"Unknown data_type: {data_type}. "
                f"Supported types: {[dt.name for dt in FinMindDataType]}, 'all'"
            )

    def update_stock_info(self) -> None:
        """更新台股總覽資料（不含權證）"""

        self.stock_info.update_stock_info()

    def update_stock_info_with_warrant(self) -> None:
        """更新台股總覽(含權證)資料"""

        self.stock_info.update_stock_info_with_warrant()

    def update_broker_info(self) -> None:
        """更新證券商資訊表資料"""

        self.broker_info.update_broker_info()

    def update_broker_trading_daily_report(
        self,
        start_date: Union[datetime.date, str],
        end_date: Union[datetime.date, str],
    ) -> None:
        """批量更新當日券商分點統計表資料（逐 (券商 × 股票) 組合）"""

        self.broker_trading.update(start_date=start_date, end_date=end_date)

    def update_all(
        self,
        start_date: Optional[Union[datetime.date, str]] = None,
        end_date: Optional[Union[datetime.date, str]] = None,
    ) -> None:
        """
        更新所有 FinMind 資料

        Args:
            start_date: 起始日期（僅用於 broker_trading_daily_report）
            end_date: 結束日期（僅用於 broker_trading_daily_report）
        """

        logger.info("* Start Updating All FinMind Data...")

        # 更新台股總覽（不含權證）
        self.update_stock_info()

        # 更新台股總覽（含權證）
        self.update_stock_info_with_warrant()

        # 更新證券商資訊
        self.update_broker_info()

        # 更新券商分點統計（需要日期範圍）
        if start_date is None:
            start_date = self.DEFAULT_BROKER_TRADING_START_DATE
        if end_date is None:
            end_date = datetime.date.today()

        # 批量更新所有券商和股票組合
        self.update_broker_trading_daily_report(
            start_date=start_date,
            end_date=end_date,
        )

        logger.info("✅ All FinMind Data updated successfully")
