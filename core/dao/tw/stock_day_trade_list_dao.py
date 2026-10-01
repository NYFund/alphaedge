from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from core.config import DAY_TRADE_LIST_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股現股當沖標的名單（`day_trade_list` 表）的資料存取"""


class StockDayTradeListDAO(BaseDAO):
    """
    `day_trade_list` 表的建表、寫入與查詢

    一列＝某日某檔**可現股當沖**的證券；`暫停先賣後買當沖` 為 0／1。
    **某日有列＝該日上市與上櫃都已入庫**（理由同 `StockShortSaleListDAO`）。
    """

    TABLE_NAME: str = DAY_TRADE_LIST_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
    def create_table(self) -> None:
        """建立 `day_trade_list` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "暫停先賣後買當沖" INT NOT NULL,
                PRIMARY KEY ("date", "stock_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")
