import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.config import CORPORATE_ACTION_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股非除權息公司行動（`corporate_action` 表：減資、分割、面額變更）的資料存取"""


class CorporateActionDAO(BaseDAO):
    """
    - Description:
        `corporate_action` 表的建表、寫入與查詢

        調整倍率 ＝ 恢復買賣參考價 ÷ 停止買賣前收盤價。
        **減資時 > 1（價格上調）、分割時 < 1**，與 `dividend.還原係數`（恆 < 1）方向不同。
    """

    TABLE_NAME: str = CORPORATE_ACTION_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
    def create_table(self) -> None:
        """建立 `corporate_action` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT,
                "停止買賣前收盤價" REAL NOT NULL,
                "恢復買賣參考價" REAL NOT NULL,
                "調整倍率" REAL NOT NULL,
                "事件類型" TEXT NOT NULL,
                "原因" TEXT,
                "資料來源" TEXT NOT NULL,
                PRIMARY KEY ("date", "stock_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
    def get_adjust_ratios(self) -> pd.DataFrame:
        """取得全期間的調整倍率（`date`／`stock_id`／`調整倍率` 三欄，未排序）"""

        return self.query_df(f"SELECT date, stock_id, 調整倍率 FROM {self.TABLE_NAME}")

    def get_event_keys(self) -> pd.DataFrame:
        """取得全期間的公司行動事件鍵（`date`／`stock_id` 兩欄，未排序）"""

        return self.query_df(f"SELECT date, stock_id FROM {self.TABLE_NAME}")

    def get_adjust_ratios_by_date(self, date: datetime.date) -> pd.DataFrame:
        """取得單日的調整倍率（`date`／`stock_id`／`調整倍率` 三欄）；表不存在時為空表"""

        if not self.table_exists():
            return pd.DataFrame(columns=["date", "stock_id", "調整倍率"])

        return self.query_df(
            f"SELECT date, stock_id, 調整倍率 FROM {self.TABLE_NAME} WHERE date = ?",
            (date,),
        )
