from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import STOCK_INFO_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股股票清單（FinMind `taiwan_stock_info` 表，不含權證）的資料存取"""


class StockInfoDAO(BaseDAO):
    """
    - Description:
        `taiwan_stock_info` 表的查詢

        股票清單是多條 ETL 的「要爬哪些股票」來源（財報逐檔查詢、FinMind 回補）。
        **表不存在時回空清單、其他查詢錯誤一律往外拋**：舊版 `except Exception`
        回空清單，「DB 被鎖住」與「還沒跑過 stock_info」長得一模一樣，
        下游只會印一行「沒有目標股票」就結束，行程結束碼是 0。
    """

    TABLE_NAME: str = STOCK_INFO_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # === 查詢 ===
    def get_stock_ids(self) -> List[str]:
        """取得所有股票代號（去重、排序）；表不存在時為空清單"""

        if not self.table_exists():
            logger.warning(f"{self.TABLE_NAME} 不存在，股票清單為空")
            return []

        df: pd.DataFrame = self.query_df(
            f"SELECT DISTINCT stock_id FROM {self.TABLE_NAME} ORDER BY stock_id"
        )
        return df["stock_id"].astype(str).tolist()

    def get_listed_common_stock_ids(self) -> List[str]:
        """
        - Description:
            取得上市櫃普通股代號（排除 ETF、興櫃與非四碼代號，已排序）

            給財報逐檔查詢用：ETF 沒有財報，興櫃不在 MOPS 的上市櫃查詢範圍內。
        - Return:
            - List[str]
                股票代號；表不存在時為空清單
        """

        if not self.table_exists():
            logger.warning(f"{self.TABLE_NAME} 不存在，股票清單為空")
            return []

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT stock_id FROM {self.TABLE_NAME}
            WHERE type IN ('twse', 'tpex')
              AND industry_category NOT LIKE '%ETF%'
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
            ORDER BY stock_id
            """
        )
        return df["stock_id"].astype(str).tolist()
