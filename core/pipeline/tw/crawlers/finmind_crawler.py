import datetime
import os
from typing import Optional, Union

import pandas as pd
from FinMind.data import DataLoader
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.utils import (
    FinMindError,
    FinMindPermissionError,
    FinMindQuotaExhaustedError,
    FinMindRequestError,
)
from core.utils.log_manager import LogManager

"""
FinMind 資料爬蟲

負責爬取以下資料：
1. 當日券商分點統計表（TaiwanStockTradingDailyReportSecIdAgg）——資料起始日 2021/6/30
2. 台股總覽（TaiwanStockInfo）
3. 台股總覽含權證（TaiwanStockInfoWithWarrant）
4. 證券商資訊表（TaiwanSecuritiesTraderInfo）

所有爬取方法的失敗都會轉成 `FinMindError` 家族往外拋（見 `to_request_error()`），
只有 API 正常回傳空表才回 None，避免權限或連線問題被誤判成「當日無資料」。
"""


class FinMindCrawler(BaseDataCrawler):
    """爬取 FinMind 提供的台股相關資料"""

    def __init__(self) -> None:
        super().__init__()
        self.api: Optional[DataLoader] = None
        self.setup()

    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Crawler"""

        LogManager.setup_logger("crawl_finmind.log")

        api_token: Optional[str] = os.getenv("FINMIND_API_TOKEN")
        if not api_token:
            raise ValueError(
                "FINMIND_API_TOKEN is missing. Please set it in your .env file."
            )

        self.api: DataLoader = DataLoader()
        self.api.login_by_token(api_token=api_token)
        logger.info("FinMind API initialized successfully")

    def crawl(self, *args, **kwargs) -> None:
        """本爬蟲沒有單一入口，各資料集請改呼叫對應的 `crawl_*` 方法"""
        pass

    def crawl_stock_info(self) -> Optional[pd.DataFrame]:
        """
        - Description:
            爬取台股總覽（TaiwanStockInfo）

            欄位語意：
            - industry_category: str  產業別
            - stock_id: str  股票代碼
            - stock_name: str  股票名稱
            - type: str  掛牌板別
            - date: str  更新日期
        - Return:
            - Optional[pd.DataFrame]
                總覽表；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Taiwan Stock Info")

        try:
            df: pd.DataFrame = self.api.taiwan_stock_info()

            if df is None or df.empty:
                logger.warning("No data available for Taiwan Stock Info")
                return None

            logger.info(f"Successfully crawled {len(df)} records")
            return df

        except Exception as e:
            raise self.to_request_error(e, "Taiwan Stock Info") from e

    def crawl_stock_info_with_warrant(self) -> Optional[pd.DataFrame]:
        """
        - Description:
            爬取台股總覽（含權證）（TaiwanStockInfoWithWarrant）

            欄位語意：
            - industry_category: str  產業別
            - stock_id: str  股票代碼
            - stock_name: str  股票名稱
            - type: str  掛牌板別
            - date: str  更新日期
        - Return:
            - Optional[pd.DataFrame]
                總覽表；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Taiwan Stock Info With Warrant")

        try:
            df: pd.DataFrame = self.api.taiwan_stock_info_with_warrant()

            if df is None or df.empty:
                logger.warning("No data available for Taiwan Stock Info With Warrant")
                return None

            logger.info(f"Successfully crawled {len(df)} records")
            return df

        except Exception as e:
            raise self.to_request_error(e, "Taiwan Stock Info With Warrant") from e

    def crawl_broker_info(self) -> Optional[pd.DataFrame]:
        """
        - Description:
            爬取證券商資訊表（TaiwanSecuritiesTraderInfo）

            欄位語意（`securities_trader_id`／`securities_trader` 為 FinMind API
            原始欄位名稱，不可自行改名）：
            - securities_trader_id: str  券商代碼
            - securities_trader: str  券商名稱
            - date: str  開業日
            - address: str  地址
            - phone: str  電話
        - Return:
            - Optional[pd.DataFrame]
                券商資訊表；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Broker Info")

        try:
            df: pd.DataFrame = self.api.taiwan_securities_trader_info()

            if df is None or df.empty:
                logger.warning("No data available for Broker Info")
                return None

            logger.info(f"Successfully crawled {len(df)} records")
            return df

        except Exception as e:
            raise self.to_request_error(e, "Broker Info") from e

    def crawl_broker_trading_daily_report(
        self,
        stock_id: Optional[str] = None,
        securities_trader_id: Optional[str] = None,
        start_date: Optional[Union[datetime.date, str]] = None,
        end_date: Optional[Union[datetime.date, str]] = None,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            爬取「當日券商分點統計表」（TaiwanStockTradingDailyReportSecIdAgg）

            來源資料自 2021/6/30 起才有，查更早的日期一律查無資料。
            FinMind 端要求四個參數都有值才會回資料，少帶任一個都會拿到空表。

            欄位語意（`securities_trader`／`securities_trader_id` 為 FinMind API
            原始欄位名稱）：
            - securities_trader: str  券商名稱
            - securities_trader_id: str  券商代碼
            - stock_id: str  股票代碼
            - date: str  日期（YYYY-MM-DD）
            - buy_volume: int  買進總股數（Unit: 股）
            - sell_volume: int  賣出總股數（Unit: 股）
            - buy_price: float  買進均價
            - sell_price: float  賣出均價
        - Parameters:
            - stock_id: Optional[str]
                股票代碼
            - securities_trader_id: Optional[str]
                券商代碼
            - start_date: Optional[Union[datetime.date, str]]
                起始日期，可為 datetime.date 或 "YYYY-MM-DD" 字串
            - end_date: Optional[Union[datetime.date, str]]
                結束日期，格式同 start_date
        - Return:
            - Optional[pd.DataFrame]
                分點統計表；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info(
            f"* Start crawling Broker Trading Daily Report: {start_date} to {end_date}"
        )

        if isinstance(start_date, str):
            start_date_str: str = start_date
        elif isinstance(start_date, datetime.date):
            start_date_str: str = start_date.strftime("%Y-%m-%d")
        else:
            raise ValueError(
                f"start_date must be str or datetime.date, got {type(start_date)}"
            )

        if isinstance(end_date, str):
            end_date_str: str = end_date
        elif isinstance(end_date, datetime.date):
            end_date_str: str = end_date.strftime("%Y-%m-%d")
        else:
            raise ValueError(
                f"end_date must be str or datetime.date, got {type(end_date)}"
            )

        try:
            df: pd.DataFrame = self.api.taiwan_stock_trading_daily_report_secid_agg(
                stock_id=stock_id,
                securities_trader_id=securities_trader_id,
                start_date=start_date_str,
                end_date=end_date_str,
            )

            if df is None or df.empty:
                logger.warning(f"No data available for {start_date} to {end_date}")
                return None

            logger.info(f"Successfully crawled {len(df)} records")
            return df

        except Exception as e:
            raise self.to_request_error(
                e,
                f"broker trading daily report "
                f"(trader={securities_trader_id}, stock={stock_id}, {start_date} to {end_date})",
            ) from e

    @staticmethod
    def to_request_error(error: Exception, label: str) -> FinMindError:
        """
        - Description:
            把 FinMind API 呼叫拋出的例外歸類成 pipeline 的 FinMind 例外

            **呼叫失敗一律往外拋，只有 API 正常回傳空表才回 None**：失敗若吞成
            `None`，updater 會把帳號等級不足、連線失敗都記成「沒有資料」，
            整批仍以結束碼 0 成功結束，資料缺漏就此無聲通過。
        - Parameters:
            - error: Exception
                FinMind 套件拋出的原始例外
            - label: str
                log 與訊息用的資料集描述
        - Return:
            - FinMindError
                配額用盡為 `FinMindQuotaExhaustedError`、帳號等級不足為
                `FinMindPermissionError`，其餘為 `FinMindRequestError`
        """

        if FinMindError.is_quota_error(error):
            logger.warning(
                f"FinMind API quota exhausted while crawling {label}: {error}"
            )
            return FinMindQuotaExhaustedError("FinMind API quota exhausted")
        if FinMindPermissionError.is_permission_error(error):
            logger.error(f"FinMind 帳號等級不足，無法取得 {label}：{error}")
            return FinMindPermissionError(
                f"FinMind 帳號等級不足，無法取得 {label}：{error}"
            )
        logger.error(f"Error crawling {label}: {error}")
        return FinMindRequestError(f"Error crawling {label}: {error}")
