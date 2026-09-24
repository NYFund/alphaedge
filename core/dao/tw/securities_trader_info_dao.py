from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import SECURITIES_TRADER_INFO_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""證券商資訊（FinMind `taiwan_securities_trader_info` 表）的資料存取"""


class SecuritiesTraderInfoDAO(BaseDAO):
    """`taiwan_securities_trader_info` 表的建表、寫入與查詢"""

    TABLE_NAME: str = SECURITIES_TRADER_INFO_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH

    # 單鍵的現況快照：一家券商分點一列
    KEY_COLUMN: str = "securities_trader_id"

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

    def create_table(self) -> None:
        """建立證券商資訊表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "securities_trader_id" TEXT NOT NULL,
                "securities_trader" TEXT,
                "date" TEXT,
                "address" TEXT,
                "phone" TEXT,
                PRIMARY KEY ("securities_trader_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
    def get_by_trader_id(self, securities_trader_id: str) -> pd.DataFrame:
        """依證券商代號取得單一證券商資訊；查無代號時為空表"""

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE securities_trader_id = ?
            """,
            (securities_trader_id,),
        )

    def get_all(self) -> pd.DataFrame:
        """取得全部證券商資訊"""

        return self.query_df(f"SELECT * FROM {self.TABLE_NAME}")

    def get_trader_ids(self) -> List[str]:
        """
        - Description:
            取得所有證券商代號（去重、排序）

            表不存在時回空清單；其他查詢錯誤往外拋——一律吞成空清單的話，
            「DB 被鎖住」會變成「沒有券商，略過」，整段券商分點回補一筆都沒跑。
        - Return:
            - List[str]
                證券商代號
        """

        if not self.table_exists():
            logger.warning(f"{self.TABLE_NAME} 不存在，券商清單為空")
            return []

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT securities_trader_id FROM {self.TABLE_NAME}
            ORDER BY securities_trader_id
            """
        )
        return df["securities_trader_id"].astype(str).tolist()
