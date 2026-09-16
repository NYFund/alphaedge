import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import FUTURES_STOCK_UNIVERSE_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO

"""股票期貨標的池（`tw_futures.db` 的 `futures_stock_universe` 表）的資料存取"""


class FuturesStockUniverseDAO(BaseDAO):
    """
    - Description:
        股期標的池快照序列的建表、寫入與查詢

        **本表是快照序列**（每次更新新增一份，主鍵 `(snapshot_date, product_id)`），
        任何「某日的狀態」都要先解出該日適用的快照日。快照日的查詢只有
        `get_latest_snapshot_date()` 一份實作——舊版 API、updater 與無呼叫端的
        `get_active_products()` 各寫了一份 `MAX(snapshot_date)`。

        表不存在（尚未跑過標的池 ETL）時查詢回 None／空清單；被鎖住或 schema 壞掉一律往外拋。
    """

    TABLE_NAME: str = FUTURES_STOCK_UNIVERSE_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表與索引存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

    def create_table(self) -> None:
        """建立股期標的池表與 `(product_id, snapshot_date)` 索引並 commit"""

        # `underlying_stock_id` 為 TEXT 且不可改成整數：ETF 標的有 `0050`
        # （前導 0）與 `00679B`（含英文字母），轉成數字就對不回 tw_stock.db。
        #
        # `contract_size` 是**掛牌時的標準契約單位，不是契約乘數**。標的除權息後
        # TAIFEX 會調整乘數或另掛新契約（代碼帶數字尾碼，如 `EE1`），實際乘數會
        # 偏離本欄。算 PnL 一律走 `TwFuturesDataFeed.resolve_multiplier()`，不可直接拿本欄當乘數。
        #
        # 兩個交易時段欄位允許 NULL：`-` 代表沒有該時段，2026-08-29 實查僅 6 檔
        # 有盤後交易時段。填空字串會讓「沒有夜盤」與「未知」混為一談。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "snapshot_date" TEXT NOT NULL,
                "product_id" TEXT NOT NULL,
                "base_code" TEXT NOT NULL,
                "product_type" TEXT NOT NULL,
                "underlying_stock_id" TEXT NOT NULL,
                "underlying_name" TEXT NOT NULL,
                "underlying_listing_board" TEXT,
                "contract_size" INT NOT NULL,
                "day_session_time" TEXT,
                "night_session_time" TEXT,
                PRIMARY KEY ("snapshot_date", "product_id")
            );
            """
        )

        # 下游最常見的查詢是「某商品的快照歷史」（差分出掛牌／下市與乘數異動），
        # 主鍵的前綴是 snapshot_date，幫不上這種查詢，故另建索引
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_futures_stock_universe_product
            ON {self.TABLE_NAME} ("product_id", "snapshot_date");
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 快照日 ===
    def get_latest_snapshot_date(
        self,
        date: Optional[datetime.date] = None,
        inclusive: bool = True,
    ) -> Optional[str]:
        """
        - Description:
            取得不晚於（或早於）`date` 的最近一份快照日
        - Parameters:
            - date: Optional[datetime.date]
                基準日；None 表示不設限（取最新一份）
            - inclusive: bool
                True 取 `<= date`（查某日適用的快照）；False 取 `< date`
                （updater 入庫前找「前一份」來做差分）
        - Return:
            - Optional[str]
                快照日；表不存在或沒有符合的快照時為 None
        """

        if not self.table_exists():
            return None

        if date is None:
            row: Optional[Tuple[Any, ...]] = self.fetch_one(
                f"SELECT MAX(snapshot_date) FROM {self.TABLE_NAME}"
            )
        else:
            operator: str = "<=" if inclusive else "<"
            row = self.fetch_one(
                f"SELECT MAX(snapshot_date) FROM {self.TABLE_NAME} "
                f"WHERE snapshot_date {operator} ?",
                (date,),
            )

        return row[0] if row and row[0] else None

    def get_earliest_snapshot_date(self) -> Optional[str]:
        """取得最早一份快照日；表不存在或為空時為 None"""

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MIN(snapshot_date) FROM {self.TABLE_NAME}"
        )
        return row[0] if row and row[0] else None

    def resolve_snapshot_date(
        self, date: Optional[datetime.date] = None
    ) -> Optional[str]:
        """
        - Description:
            解出 `date` 適用的快照日：不晚於 `date` 的最近一份，`date` 為 None 時取最新一份

            查詢日早於第一份快照時**退回最早的一份**。這是近似不是事實——本表只回溯到
            建表之日（2026-08-29），更早的掛牌狀態與契約單位無從得知。
        - Parameters:
            - date: Optional[datetime.date]
                查詢日
        - Return:
            - Optional[str]
                快照日；表不存在或為空時為 None
        """

        return (
            self.get_latest_snapshot_date(date, inclusive=True)
            or self.get_earliest_snapshot_date()
        )

    def is_snapshot_loaded(self, snapshot_date: datetime.date) -> bool:
        """檢查該日快照是否已入庫；表不存在時為 False"""

        if not self.table_exists():
            return False

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE snapshot_date = ?",
            (snapshot_date,),
        )
        return bool(row and row[0])

    # === 快照內容 ===
    def get_product_ids(
        self,
        snapshot_date: str,
        product_types: Optional[List[str]] = None,
    ) -> List[str]:
        """
        - Description:
            取得某份快照中的商品代碼（依代碼排序）
        - Parameters:
            - snapshot_date: str
                快照日
            - product_types: Optional[List[str]]
                只取這些商品類型；None 表示全部
        - Return:
            - List[str]
                商品代碼
        """

        query: str = f"SELECT product_id FROM {self.TABLE_NAME} WHERE snapshot_date = ?"
        params: Tuple[Any, ...] = (snapshot_date,)

        if product_types:
            placeholders: str = ",".join("?" * len(product_types))
            query += f" AND product_type IN ({placeholders})"
            params += tuple(product_types)

        return [
            row[0] for row in self.conn.execute(query + " ORDER BY product_id", params)
        ]

    def get_snapshot_contracts(self, snapshot_date: str) -> pd.DataFrame:
        """取得某份快照的 `product_id`／`underlying_name`／`contract_size`（快照差分用）"""

        return self.query_df(
            f"""
            SELECT product_id, underlying_name, contract_size
            FROM {self.TABLE_NAME}
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        )

    def get_underlying(
        self, snapshot_date: str, product_id: str
    ) -> Optional[Tuple[Any, ...]]:
        """取得某份快照中該商品的（標的代號, 標的名稱, 商品類型）；查無時為 None"""

        return self.fetch_one(
            f"SELECT underlying_stock_id, underlying_name, product_type "
            f"FROM {self.TABLE_NAME} "
            f"WHERE snapshot_date = ? AND product_id = ?",
            (snapshot_date, product_id),
        )

    def get_contract_size(
        self,
        product_id: str,
        date: Optional[datetime.date] = None,
        per_product: bool = False,
    ) -> Optional[int]:
        """
        - Description:
            取得股期在指定日期的契約單位（股）

            原本兩份實作的查法不同，收斂後以 `per_product` 區分，兩種語意都是刻意的：

            | `per_product` | 查法 | 使用者 | 為什麼 |
            |---|---|---|---|
            | False | 先解出**全表**該日適用的快照，再查該商品 | 回測乘數（`FuturesStockUniverseAPI`） | 快照中沒有該商品＝那天不在列，回測不該拿到乘數；回 None 讓 DataFeed 中斷 |
            | True | 該商品**自己**不晚於 `date` 的最近一份，沒有就退回它最早的一份 | 保證金試算（`FuturesMarginAPI`） | 試算只需要合理的股數；已下市或晚掛牌的商品仍取它自己最接近的值 |

            **不可用標準型 2,000 股當預設值**：小型股期是 100 股、ETF 期貨是 10,000 股，
            除權息調整後的契約更是任意數字。查不到就回 None，由呼叫端決定中止或跳過。
        - Parameters:
            - product_id: str
                股期代碼（Ex: CDF）
            - date: Optional[datetime.date]
                查詢日；None 表示最新快照（`per_product=True` 時為該商品最新一份）
            - per_product: bool
                是否以該商品自己的快照序列為準（見上表）
        - Return:
            - Optional[int]
                契約單位；查無資料時為 None
        """

        if not self.table_exists():
            return None

        if not per_product:
            snapshot: Optional[str] = self.resolve_snapshot_date(date)
            if snapshot is None:
                return None

            row: Optional[Tuple[Any, ...]] = self.fetch_one(
                f"SELECT contract_size FROM {self.TABLE_NAME} "
                f"WHERE snapshot_date = ? AND product_id = ?",
                (snapshot, product_id),
            )
            return None if row is None else row[0]

        if date is None:
            row = self.fetch_one(
                f"SELECT contract_size FROM {self.TABLE_NAME} "
                f"WHERE product_id = ? ORDER BY snapshot_date DESC LIMIT 1",
                (product_id,),
            )
            return None if row is None else row[0]

        row = self.fetch_one(
            f"SELECT contract_size FROM {self.TABLE_NAME} "
            f"WHERE product_id = ? AND snapshot_date <= ? "
            f"ORDER BY snapshot_date DESC LIMIT 1",
            (product_id, date),
        )
        if row is not None:
            return row[0]

        # 查詢日早於該商品第一份快照時退回最早的一份，並非「沒有這個商品」
        row = self.fetch_one(
            f"SELECT contract_size FROM {self.TABLE_NAME} "
            f"WHERE product_id = ? ORDER BY snapshot_date LIMIT 1",
            (product_id,),
        )
        return None if row is None else row[0]

    def get_contract_size_series(self, product_id: str) -> pd.DataFrame:
        """取得該商品每一份快照的契約單位（`snapshot_date`／`contract_size`，依快照日排序）"""

        return self.query_df(
            f"SELECT snapshot_date, contract_size FROM {self.TABLE_NAME} "
            f"WHERE product_id = ? ORDER BY snapshot_date",
            (product_id,),
        )
