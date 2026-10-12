import datetime
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import FUTURES_MARGIN_HISTORY_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO
from core.dao.tw.stock_futures_margin_rate_dao import (
    effective_dates_of,
    insert_change_series,
    summarize_change_series,
)

"""
台期貨保證金金額表（`futures_margin_history`，`tw_futures.db`）的資料存取

| 表 | 涵蓋 | 內容 | DAO |
|----|------|------|-----|
| `futures_margin_history` | 指數期貨 ＋ ETF 股期 | 每口固定金額 | 本類 |
| `stock_futures_margin_rate_history` | 股票股期 | 適用比例 ＋ 級距 | `StockFuturesMarginRateDAO` |

兩張表都是**變動序列**（只有保證金真的變動時才有列），「某日生效的保證金」一律取
`effective_date` 不晚於（或早於）該日的最大者。同一支 ETL 寫兩張表，但**一張表一個 DAO**：
鍵欄、欄位與每口保證金的算法都不同，塞同一個 DAO 靠表名參數切換，每個方法都得先驗表名。
"""


class FuturesMarginDAO(BaseDAO):
    """期貨保證金金額表的建表、寫入與查詢"""

    TABLE_NAME: str = FUTURES_MARGIN_HISTORY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH
    # 變動序列：生效日 ＋ 契約代碼
    DATE_COLUMN: str = "effective_date"
    KEY_COLUMNS: Tuple[str, ...] = ("product",)

    # === 建表 ===
    def create_table(self) -> None:
        """建立金額表與 `(product, effective_date)` 索引（`IF NOT EXISTS`）並 commit"""

        # 主鍵為 (effective_date, product)：本表是變動序列。
        #
        # 金額欄為 INT：TAIFEX 的保證金一律是整數元，沒有小數。
        #
        # `source` 區分資料來源：`snapshot` 來自現行一覽表、`announcement` 來自
        # 調整公告。兩者可能給出同一個 (effective_date, product)，
        # 一覽表先寫入者留存、公告則覆蓋（見 loader 的 `add_announcements_to_db()`）。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "effective_date" TEXT NOT NULL,
                "product" TEXT NOT NULL,
                "product_name" TEXT,
                "結算保證金" INT,
                "維持保證金" INT,
                "原始保證金" INT NOT NULL,
                "source" TEXT NOT NULL,
                PRIMARY KEY ("effective_date", "product")
            );
            """
        )

        # 下游最常見的查詢是「某商品在某日生效的保證金」，走
        # `WHERE product = ? AND effective_date <= ? ORDER BY effective_date DESC`，
        # 主鍵的前綴是 effective_date，幫不上這種查詢，故另建索引
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_futures_margin_product
            ON {self.TABLE_NAME} ("product", "effective_date");
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 寫入 ===
    def insert_rows(self, df: pd.DataFrame, replace: bool = False) -> int:
        """
        - Description:
            寫入金額表，回傳**實際新增**的列數；不 commit

            同一組保證金重複抓到時整批被忽略，這正是「變動序列」的實現方式。
            回傳的是前後列數差而非 `rowcount`——後者會把被忽略／被覆蓋的也算進去。
        - Parameters:
            - df: pd.DataFrame
                要寫入的資料；第一欄是生效日（`datetime.date`），寫入前轉成 ISO 字串
            - replace: bool
                True 時同主鍵覆蓋（`INSERT OR REPLACE`，公告用），False 時忽略
        - Return:
            - int
                實際新增的列數（覆蓋既有列不計入）
        """

        return insert_change_series(self, df, replace)

    # === 查詢 ===
    def get_effective_dates(self, source: Optional[str] = None) -> Set[str]:
        """表內已有的所有生效日；`source` 為 None 表示不分來源"""

        return effective_dates_of(self, source)

    def get_margin_in_effect(
        self,
        product: str,
        date: datetime.date,
        inclusive: bool = True,
        fallback_to_earliest: bool = False,
    ) -> Optional[Tuple[Any, ...]]:
        """
        - Description:
            取得該商品在某日**生效中**的（結算, 維持, 原始）保證金

            「生效日」有 `<=` 與 `<` 兩種語意，以 `inclusive` 區分，
            **兩種都是刻意的**，呼叫端須明確傳入：

            | `inclusive` | 條件 | 使用者 | 回答的問題 |
            |---|---|---|---|
            | True | `effective_date <= date` | `FuturesMarginAPI`（回測、試算） | 這一天**適用**多少——當天調整的新值當天就生效 |
            | False | `effective_date < date` | `FuturesMarginUpdater`（公告一致性驗證） | 這次調整**之前**是多少——同一生效日已有列（一覽表、或重跑時的本次公告）時，`<=` 會拿到調整後的值，與公告的「調整前」必然不符 |

            表不存在（尚未跑過保證金 ETL）時回 None；被鎖住或 schema 壞掉一律往外拋。
        - Parameters:
            - product: str
                契約代碼（Ex: TX、NYF）
            - date: datetime.date
                查詢日（或本次公告的生效日）
            - inclusive: bool
                見上表
            - fallback_to_earliest: bool
                查不到時是否退回該商品最早的一列
        - Return:
            - Optional[Tuple[Any, ...]]
                （結算保證金, 維持保證金, 原始保證金）；查無資料時為 None
        """

        if not self.table_exists():
            return None

        operator: str = "<=" if inclusive else "<"
        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT 結算保證金, 維持保證金, 原始保證金 "
            f"FROM {self.TABLE_NAME} "
            f"WHERE product = ? AND effective_date {operator} ? "
            f"ORDER BY effective_date DESC LIMIT 1",
            (product, date),
        )

        if row is None and fallback_to_earliest:
            row = self.fetch_one(
                f"SELECT 結算保證金, 維持保證金, 原始保證金 "
                f"FROM {self.TABLE_NAME} "
                f"WHERE product = ? ORDER BY effective_date LIMIT 1",
                (product,),
            )

        return row

    def get_covered_date_range(self, product: str) -> Optional[Tuple[str, str]]:
        """金額表中該商品的（最早, 最晚）生效日；表不存在或無資料時為 None"""

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MIN(effective_date), MAX(effective_date) "
            f"FROM {self.TABLE_NAME} WHERE product = ?",
            (product,),
        )
        if row is None or row[0] is None:
            return None
        return row[0], row[1]

    def get_announcement_margins(self, product: str) -> List[Tuple[str, int]]:
        """金額表中該商品來自調整公告的（生效日, 原始保證金），依生效日排序"""

        return self.conn.execute(
            f"SELECT effective_date, 原始保證金 "
            f"FROM {self.TABLE_NAME} "
            f"WHERE product = ? AND source = 'announcement' "
            f"ORDER BY effective_date",
            (product,),
        ).fetchall()

    def get_latest_snapshot_margin(self, product: str) -> Optional[Tuple[str, int]]:
        """金額表中該商品最新一筆來自現行一覽表的（生效日, 原始保證金）"""

        return self.fetch_one(
            f"SELECT effective_date, 原始保證金 "
            f"FROM {self.TABLE_NAME} "
            f"WHERE product = ? AND source = 'snapshot' "
            f"ORDER BY effective_date DESC LIMIT 1",
            (product,),
        )

    def get_announcement_products(self) -> List[str]:
        """金額表中有調整公告紀錄的商品代碼，依代碼排序"""

        return [
            row[0]
            for row in self.conn.execute(
                f"SELECT DISTINCT product FROM {self.TABLE_NAME} "
                f"WHERE source = 'announcement' ORDER BY product"
            ).fetchall()
        ]

    def get_summary(self) -> Tuple[str, int, int, Optional[str], Optional[str]]:
        """（表名, 列數, 商品數, 最早生效日, 最晚生效日）"""

        return summarize_change_series(self, "product")
