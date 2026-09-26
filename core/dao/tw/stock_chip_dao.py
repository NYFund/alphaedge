from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from core.config import CHIP_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.base import BaseDAO

"""台股三大法人盤後籌碼（`chip` 表）的資料存取"""


class StockChipDAO(BaseDAO):
    """`chip` 表的建表、寫入與查詢（數量單位：股）"""

    TABLE_NAME: str = CHIP_TABLE_NAME

    # 與建表 DDL 的 PRIMARY KEY 一致；`load_csv_directory()` 以它做檔內去重
    PRIMARY_KEY_COLUMNS: Tuple[str, ...] = ("date", "stock_id", "證券名稱")
    DEFAULT_DB_PATH: Optional[Path] = TW_STOCK_DB_PATH
    NEEDS_SYMBOL_DATE_INDEX: bool = True

    # === 建表 ===
    def create_table(self) -> None:
        """建立 `chip` 表並 commit"""

        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "stock_id" TEXT NOT NULL,
                "證券名稱" TEXT NOT NULL,
                "外資買進股數" INT NOT NULL,
                "外資賣出股數" INT NOT NULL,
                "外資買賣超股數" INT NOT NULL,
                "投信買進股數" INT NOT NULL,
                "投信賣出股數" INT NOT NULL,
                "投信買賣超股數" INT NOT NULL,
                "自營商買進股數(自行買賣)" INT,
                "自營商賣出股數(自行買賣)" INT,
                "自營商買賣超股數(自行買賣)" INT,
                "自營商買進股數(避險)" INT,
                "自營商賣出股數(避險)" INT,
                "自營商買賣超股數(避險)" INT,
                "自營商買進股數" INT,
                "自營商賣出股數" INT,
                "自營商買賣超股數" INT NOT NULL,
                "三大法人買賣超股數" INT NOT NULL,
                PRIMARY KEY ("date", "stock_id", "證券名稱")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢 ===
