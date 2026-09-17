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
1. 當日卷商分點統計表 (TaiwanStockTradingDailyReportSecIdAgg) - 資料起始日期：2021/6/30
2. 台股總覽 (TaiwanStockInfo)
3. 台股總覽(含權證) (TaiwanStockInfoWithWarrant)
4. 證券商資訊表 (TaiwanSecuritiesTraderInfo)
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

        # 從環境變數取得 FinMind API Token
        api_token: Optional[str] = os.getenv("FINMIND_API_TOKEN")
        if not api_token:
            raise ValueError(
                "FINMIND_API_TOKEN is missing. Please set it in your .env file."
            )

        self.api: DataLoader = DataLoader()
        self.api.login_by_token(api_token=api_token)
        logger.info("FinMind API initialized successfully")

    def crawl(self, *args, **kwargs) -> None:
        pass

    def crawl_stock_info(self) -> Optional[pd.DataFrame]:
        """爬取台股總覽 (TaiwanStockInfo)
        資料欄位說明：
            - industry_category: str         # 產業別
            - stock_id: str                  # 股票代碼
            - stock_name: str                # 股票名稱
            - type: str                      # 掛牌板別
            - date: str                      # 更新日期

        回傳值：
            pd.DataFrame；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Taiwan Stock Info")

        try:
            # 直接使用 API 專用方法
            df: pd.DataFrame = self.api.taiwan_stock_info()

            if df is None or df.empty:
                logger.warning("No data available for Taiwan Stock Info")
                return None

            logger.info(f"Successfully crawled {len(df)} records")
            return df

        except Exception as e:
            raise self.to_request_error(e, "Taiwan Stock Info") from e

    def crawl_stock_info_with_warrant(self) -> Optional[pd.DataFrame]:
        """爬取台股總覽(含權證) (TaiwanStockInfoWithWarrant)
        資料欄位說明：
            - industry_category: str         # 產業別
            - stock_id: str                  # 股票代碼
            - stock_name: str                # 股票名稱
            - type: str                      # 掛牌板別
            - date: str                      # 更新日期

        回傳值：
            pd.DataFrame；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Taiwan Stock Info With Warrant")

        try:
            # 直接使用 API 專用方法
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
        爬取證券商資訊表 (TaiwanSecuritiesTraderInfo)
        資料欄位說明：
            - securities_trader_id: str      # 券商代碼 (FinMind API 原始欄位名稱)
            - securities_trader: str         # 券商名稱 (FinMind API 原始欄位名稱)
            - date: str                      # 開業日
            - address: str                   # 地址
            - phone: str                     # 電話

        回傳值：
            pd.DataFrame；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info("* Start crawling Broker Info")

        try:
            # 直接使用 API 專用方法
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
        爬取「當日券商分點統計表」（TaiwanStockTradingDailyReportSecIdAgg）

        參數：
            - stock_id: Optional[str]                # 股票代碼（可選，不提供則返回所有股票）
            - securities_trader_id: Optional[str]    # 券商代碼（可選，不提供則返回所有券商）
            - start_date: Optional[datetime.date | str]    # 起始日期（可以是 datetime.date 或 "YYYY-MM-DD" 格式的字符串）
            - end_date: Optional[datetime.date | str]      # 結束日期（可以是 datetime.date 或 "YYYY-MM-DD" 格式的字符串）

        資料日期範圍：
            FinMind 資料的起始日期是 2021/6/30 ~ now

        API 調用方式：
            使用 self.api.taiwan_stock_trading_daily_report_secid_agg() 方法，
            直接傳遞參數：stock_id, securities_trader_id, start_date, end_date
            注意：API 需要所有參數都有值才能取得資料

        資料欄位說明：
            - securities_trader: str         # 券商名稱 (FinMind API 原始欄位名稱)
            - securities_trader_id: str      # 券商代碼 (FinMind API 原始欄位名稱)
            - stock_id: str                  # 股票代碼
            - date: str                      # 日期（YYYY-MM-DD）
            - buy_volume: int                # 買進總股數
            - sell_volume: int               # 賣出總股數
            - buy_price: float               # 買進均價
            - sell_price: float              # 賣出均價

        回傳值：
            pd.DataFrame；API 正常回傳空表時為 None（呼叫失敗一律拋 FinMindError）
        """

        logger.info(
            f"* Start crawling Broker Trading Daily Report: {start_date} to {end_date}"
        )

        # 處理 start_date：如果是字符串則直接使用，如果是 datetime.date 則轉換為字符串
        if isinstance(start_date, str):
            start_date_str: str = start_date
        elif isinstance(start_date, datetime.date):
            start_date_str: str = start_date.strftime("%Y-%m-%d")
        else:
            raise ValueError(
                f"start_date must be str or datetime.date, got {type(start_date)}"
            )

        # 處理 end_date：如果是字符串則直接使用，如果是 datetime.date 則轉換為字符串
        if isinstance(end_date, str):
            end_date_str: str = end_date
        elif isinstance(end_date, datetime.date):
            end_date_str: str = end_date.strftime("%Y-%m-%d")
        else:
            raise ValueError(
                f"end_date must be str or datetime.date, got {type(end_date)}"
            )

        try:
            # 直接使用 API 方法，傳遞所有參數
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

            **呼叫失敗一律往外拋，只有 API 正常回傳空表才回 None**：舊版除配額用盡外
            全部 `return None`，updater 把帳號等級不足、連線失敗都記成「沒有資料」，
            整批以結束碼 0 成功結束。
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
