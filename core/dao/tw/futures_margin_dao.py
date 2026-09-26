import datetime
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    FUTURES_MARGIN_HISTORY_TABLE_NAME,
    STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.base import BaseDAO, table_exists

"""
台期貨保證金兩張表（`tw_futures.db`）的資料存取

| 表 | 涵蓋 | 內容 |
|----|------|------|
| `futures_margin_history`（`TABLE_NAME`） | 指數期貨 ＋ ETF 股期 | 每口固定金額 |
| `stock_futures_margin_rate_history`（`RATE_TABLE_NAME`） | 股票股期 | 適用比例 ＋ 級距 |

兩張表都是**變動序列**（只有保證金真的變動時才有列），「某日生效的保證金」一律取
`effective_date` 不晚於（或早於）該日的最大者。兩表緊密相關（同一支 ETL、同一套查法），
故放在同一個 DAO。
"""


class FuturesMarginDAO(BaseDAO):
    """期貨保證金金額表與比例表的建表、寫入與查詢"""

    TABLE_NAME: str = FUTURES_MARGIN_HISTORY_TABLE_NAME
    RATE_TABLE_NAME: str = STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH

    # 表名只能是這兩張；以參數指定表的方法一律先過白名單
    TABLES: Tuple[str, ...] = (TABLE_NAME, RATE_TABLE_NAME)

    # 各表的商品鍵欄（金額表是契約代碼，比例表是股期代碼）
    PRODUCT_COLUMNS: Tuple[Tuple[str, str], ...] = (
        (TABLE_NAME, "product"),
        (RATE_TABLE_NAME, "product_id"),
    )

    @classmethod
    def check_table_name(cls, table: str) -> str:
        """確認表名是兩張保證金表之一；不是就當場拋出 `ValueError`"""

        if table not in cls.TABLES:
            raise ValueError(
                f"不支援的保證金資料表：{table!r}；可用的有 {list(cls.TABLES)}"
            )
        return table

    def rate_table_exists(self) -> bool:
        """比例表是否存在"""

        return table_exists(self.conn, self.RATE_TABLE_NAME)

    # === 建表 ===
    def ensure_tables(self) -> None:
        """確保兩張表與各自的 `(商品, effective_date)` 索引都存在；可重複呼叫"""

        if not (self.table_exists() and self.rate_table_exists()):
            self.create_tables()

    def create_tables(self) -> None:
        """建立金額表與比例表（`IF NOT EXISTS`）並 commit"""

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

        # 比例表（股票股期）。
        #
        # **比例欄存的是小數**（`0.1350` 而非 `13.50`）：下游直接乘不必再除以 100，
        # 而「忘記除 100」會讓保證金差 100 倍卻不會報錯。
        #
        # `保證金所屬級距` **可以是 NULL**：處置／注意股票沒有級距但仍有（更高的）
        # 比例，2026-09-01 實查 296 檔中有 15 檔如此。不可因為級距為空就丟掉該檔。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.RATE_TABLE_NAME}(
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
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_stock_futures_margin_product
            ON {self.RATE_TABLE_NAME}
            ("product_id", "effective_date");
            """
        )
        self.conn.commit()

        for table in self.TABLES:
            if table_exists(self.conn, table):
                logger.info(f"Table {table} create successfully!")
            else:
                logger.warning(f"Table {table} create unsuccessfully!")

    # === 寫入 ===
    def insert_rows(self, table: str, df: pd.DataFrame, replace: bool = False) -> int:
        """
        - Description:
            寫入兩張表之一，回傳**實際新增**的列數；不 commit

            同一組保證金重複抓到時整批被忽略，這正是「變動序列」的實現方式。
            回傳的是前後列數差而非 `rowcount`——後者會把被忽略／被覆蓋的也算進去。
        - Parameters:
            - table: str
                `TABLE_NAME` 或 `RATE_TABLE_NAME`
            - df: pd.DataFrame
                要寫入的資料；第一欄是生效日（`datetime.date`），寫入前轉成 ISO 字串
            - replace: bool
                True 時同主鍵覆蓋（`INSERT OR REPLACE`，公告用），False 時忽略
        - Return:
            - int
                實際新增的列數（覆蓋既有列不計入）
        """

        table = self.check_table_name(table)
        if df is None or df.empty:
            return 0

        columns: List[str] = list(df.columns)
        placeholders: str = ", ".join(["?"] * len(columns))
        quoted: str = ", ".join(f'"{c}"' for c in columns)
        conflict: str = "REPLACE" if replace else "IGNORE"

        before: int = self.count_rows(table)
        self.conn.executemany(
            f"INSERT OR {conflict} INTO {table} ({quoted}) VALUES ({placeholders})",
            [
                tuple(str(v) if i == 0 else v for i, v in enumerate(row))
                for row in df.values
            ],
        )
        return self.count_rows(table) - before

    # === 查詢 ===
    def count_rows(self, table: str = TABLE_NAME) -> int:
        """指定保證金表目前的列數；預設為金額表"""

        table = self.check_table_name(table)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def get_effective_dates(
        self, table: str = TABLE_NAME, source: Optional[str] = None
    ) -> Set[str]:
        """指定保證金表已有的所有生效日；`source` 為 None 表示不分來源"""

        table = self.check_table_name(table)
        query: str = f"SELECT DISTINCT effective_date FROM {table}"
        params: Tuple[Any, ...] = ()
        if source is not None:
            query += " WHERE source = ?"
            params = (source,)

        return {row[0] for row in self.conn.execute(query, params)}

    def get_margin_in_effect(
        self,
        product: str,
        date: datetime.date,
        inclusive: bool = True,
        fallback_to_earliest: bool = False,
    ) -> Optional[Tuple[Any, ...]]:
        """
        - Description:
            取得金額表中該商品在某日**生效中**的（結算, 維持, 原始）保證金

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

    def get_rates_in_effect(
        self,
        product_id: str,
        date: datetime.date,
        fallback_to_earliest: bool = False,
    ) -> Optional[Tuple[Any, ...]]:
        """
        - Description:
            取得比例表中該股期在某日生效中的（結算, 維持, 原始）適用比例

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

        if not self.rate_table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT 結算保證金適用比例, 維持保證金適用比例, 原始保證金適用比例 "
            f"FROM {self.RATE_TABLE_NAME} "
            f"WHERE product_id = ? AND effective_date <= ? "
            f"ORDER BY effective_date DESC LIMIT 1",
            (product_id, date),
        )

        if row is None and fallback_to_earliest:
            row = self.fetch_one(
                f"SELECT 結算保證金適用比例, 維持保證金適用比例, 原始保證金適用比例 "
                f"FROM {self.RATE_TABLE_NAME} "
                f"WHERE product_id = ? ORDER BY effective_date LIMIT 1",
                (product_id,),
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

    def get_table_summaries(
        self,
    ) -> List[Tuple[str, int, int, Optional[str], Optional[str]]]:
        """兩張表各自的（表名, 列數, 商品數, 最早生效日, 最晚生效日）"""

        summaries: List[Tuple[str, int, int, Optional[str], Optional[str]]] = []
        for table, key in self.PRODUCT_COLUMNS:
            row: Tuple[Any, ...] = self.conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {key}), "
                f"MIN(effective_date), MAX(effective_date) FROM {table}"
            ).fetchone()
            summaries.append((table, row[0], row[1], row[2], row[3]))
        return summaries
