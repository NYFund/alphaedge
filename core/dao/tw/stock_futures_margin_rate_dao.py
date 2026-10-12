import datetime
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO

"""
股票股期保證金比例表（`stock_futures_margin_rate_history`，`tw_futures.db`）的資料存取

與金額表（`FuturesMarginDAO`）同一支 ETL、同一套「生效日取不晚於該日的最大者」查法，
但**一張表一個 DAO**：兩張表的鍵欄、欄位與每口保證金的算法都不同
（比例表要自己算 `標的股價 × 契約單位 × 比例`），塞在同一個 DAO 裡靠表名參數切換，
每個方法都得先驗表名、呼叫端也得記得傳哪一張。
"""


class StockFuturesMarginRateDAO(BaseDAO):
    """股期保證金比例表的建表、寫入與查詢"""

    TABLE_NAME: str = STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH
    # 變動序列：生效日 ＋ 股期代碼
    DATE_COLUMN: str = "effective_date"
    KEY_COLUMNS: Tuple[str, ...] = ("product_id",)

    # === 建表 ===
    def create_table(self) -> None:
        """建立比例表與 `(product_id, effective_date)` 索引（`IF NOT EXISTS`）並 commit"""

        # **比例欄存的是小數**（`0.1350` 而非 `13.50`）：下游直接乘不必再除以 100，
        # 而「忘記除 100」會讓保證金差 100 倍卻不會報錯。
        #
        # `保證金所屬級距` **可以是 NULL**：處置／注意股票沒有級距但仍有（更高的）
        # 比例，2026-09-01 實查 296 檔中有 15 檔如此。不可因為級距為空就丟掉該檔。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "effective_date" TEXT NOT NULL,
                "product_id" TEXT NOT NULL,
                -- 公告來源不提供標的證券代號，故可為 NULL；
                -- 一覽表（snapshot）來源一定有值
                "underlying_stock_id" TEXT,
                "product_name" TEXT,
                "保證金所屬級距" TEXT,
                "結算保證金適用比例" REAL,
                "維持保證金適用比例" REAL,
                "原始保證金適用比例" REAL NOT NULL,
                "source" TEXT NOT NULL,
                PRIMARY KEY ("effective_date", "product_id")
            );
            """
        )
        # 下游最常見的查詢是「某股期在某日生效的比例」，主鍵的前綴是 effective_date，
        # 幫不上這種查詢，故另建索引
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_stock_futures_margin_product
            ON {self.TABLE_NAME}
            ("product_id", "effective_date");
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
            寫入比例表，回傳**實際新增**的列數；不 commit

            同一組比例重複抓到時整批被忽略，這正是「變動序列」的實現方式。
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

    def get_rates_in_effect(
        self,
        product_id: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[Tuple[Any, ...]]:
        """
        - Description:
            取得該股期在某日生效中的（結算, 維持, 原始）適用比例

            級距穩定的商品（Ex: CDF 一直是級距 1）從不出現在調整公告裡，表內只有
            現行一覽表那一列——`fallback_to_earliest=True` 時對它們取最早一列就是正確答案。
            表不存在時回 None；其他錯誤往外拋。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: datetime.date
                查詢日（`effective_date <= date`）
            - fallback_to_earliest: bool
                查不到時是否退回該商品最早的一列
        - Return:
            - Optional[Tuple[Any, ...]]
                三種比例；查無資料時為 None
        """

        if not self.table_exists():
            return None

        columns: str = "結算保證金適用比例, 維持保證金適用比例, 原始保證金適用比例"
        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT {columns} FROM {self.TABLE_NAME} "
            f"WHERE product_id = ? AND effective_date <= ? "
            f"ORDER BY effective_date DESC LIMIT 1",
            (product_id, date),
        )

        if row is None and fallback_to_earliest:
            row = self.fetch_one(
                f"SELECT {columns} FROM {self.TABLE_NAME} "
                f"WHERE product_id = ? ORDER BY effective_date LIMIT 1",
                (product_id,),
            )

        return row

    def get_summary(self) -> Tuple[str, int, int, Optional[str], Optional[str]]:
        """（表名, 列數, 商品數, 最早生效日, 最晚生效日）"""

        return summarize_change_series(self, "product_id")


# === 兩張保證金表共用的實作本體（金額表的 DAO 也用）===
def insert_change_series(dao: BaseDAO, df: pd.DataFrame, replace: bool) -> int:
    """以 `INSERT OR IGNORE`／`REPLACE` 寫入變動序列，回傳前後列數差；第一欄的生效日轉 ISO 字串"""

    if df is None or df.empty:
        return 0

    columns: List[str] = list(df.columns)
    placeholders: str = ", ".join(["?"] * len(columns))
    quoted: str = ", ".join(f'"{c}"' for c in columns)
    conflict: str = "REPLACE" if replace else "IGNORE"

    before: int = dao.count_rows()
    dao.conn.executemany(
        f"INSERT OR {conflict} INTO {dao.TABLE_NAME} ({quoted}) VALUES ({placeholders})",
        [
            tuple(str(v) if i == 0 else v for i, v in enumerate(row))
            for row in df.values
        ],
    )
    return dao.count_rows() - before


def effective_dates_of(dao: BaseDAO, source: Optional[str]) -> Set[str]:
    """表內已有的所有生效日；`source` 為 None 表示不分來源"""

    query: str = f"SELECT DISTINCT effective_date FROM {dao.TABLE_NAME}"
    params: Tuple[Any, ...] = ()
    if source is not None:
        query += " WHERE source = ?"
        params = (source,)

    return {row[0] for row in dao.conn.execute(query, params)}


def summarize_change_series(
    dao: BaseDAO, product_column: str
) -> Tuple[str, int, int, Optional[str], Optional[str]]:
    """（表名, 列數, 商品數, 最早生效日, 最晚生效日）"""

    row: Tuple[Any, ...] = dao.conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT {product_column}), "
        f"MIN(effective_date), MAX(effective_date) FROM {dao.TABLE_NAME}"
    ).fetchone()
    return (dao.TABLE_NAME, int(row[0]), int(row[1]), row[2], row[3])
