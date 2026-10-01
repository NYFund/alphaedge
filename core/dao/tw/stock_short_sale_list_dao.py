from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from core.config import SHORT_SALE_LIST_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股平盤下得融（借）券賣出名單（`short_sale_list` 表）的資料存取"""


class StockShortSaleListDAO(BaseDAO):
    """
    `short_sale_list` 表的建表、寫入與查詢

    一列＝某日某檔**可融資融券**的證券；三個註記欄為 0／1。
    **某日有列＝該日上市與上櫃都已入庫**：updater 只在兩個市場都取得時才入庫，
    故「該日不在表內」只代表沒爬到，不代表當天沒有任何可融券的證券。
    """

    TABLE_NAME: str = SHORT_SALE_LIST_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
    def create_table(self) -> None:
        """建立 `short_sale_list` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "暫停融券賣出" INT NOT NULL,
                "暫停借券賣出" INT NOT NULL,
                "禁止平盤下融借券賣出" INT NOT NULL,
                PRIMARY KEY ("date", "stock_id")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")
