import datetime
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_CONTINUOUS_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO, to_sql_params

"""台期貨連續合約（`tw_futures.db` 的 `futures_continuous` 衍生表）的資料存取"""


class FuturesContinuousDAO(BaseDAO):
    """
    - Description:
        連續合約表的建表與寫入

        **主鍵含 `method` 與 `roll_rule`**（`(date, product, session, method, roll_rule)`）：
        同一天可以有多條合法的序列，三種調整方式與三種換月規則並存於同一張表。
        本表是衍生表，重建時同主鍵的值**應該**被新結果覆蓋，寫入走 `insert_or_replace()`。
    """

    TABLE_NAME: str = FUTURES_CONTINUOUS_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH

    def ensure_table(self) -> None:
        """確保資料表與序列查詢索引存在（`IF NOT EXISTS`）並 commit；可重複呼叫"""

        # `expiry` 是**當天實際採用的契約**，不是主鍵的一部分——同一天在同一組
        # （method, roll_rule）之下只會有一個當家契約。把它存下來是為了讓
        # 換月接點可被稽核：`roll_flag = 1` 的那幾天，`expiry` 必定與前一天不同。
        #
        # `adj_factor` 是**已套用的調整量**，存下來才能還原回真實價格：
        # BACKWARD 為加減量（原始價 ＝ 調整價 − adj_factor），
        # RATIO 為乘數（原始價 ＝ 調整價 ÷ adj_factor），NONE 恆為 0。
        # 最新一段的 adj_factor 為 0／1——逆向調整以最新為基準，那一段就是真實價。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "product" TEXT NOT NULL,
                "session" TEXT NOT NULL,
                "method" TEXT NOT NULL,
                "roll_rule" TEXT NOT NULL,
                "expiry" TEXT NOT NULL,
                "開盤價" REAL,
                "最高價" REAL,
                "最低價" REAL,
                "收盤價" REAL,
                "成交量" INT,
                "結算價" REAL,
                "未沖銷契約量" INT,
                "roll_flag" INT NOT NULL,
                "roll_gap" REAL,
                "adj_factor" REAL,
                PRIMARY KEY ("date", "product", "session", "method", "roll_rule")
            );
            """
        )

        # 最常見的查詢是「某商品某組設定的整段序列」，主鍵前綴是 date，幫不上忙
        self.conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_futures_continuous_series
            ON {self.TABLE_NAME}
            ("product", "session", "method", "roll_rule", "date");
            """
        )
        self.conn.commit()

        if not self.table_exists():
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    def delete_series(
        self, product: str, session: str, method: str, roll_rule: str
    ) -> int:
        """
        - Description:
            刪除某一組（商品, 時段, 調整方式, 換月規則）的所有列；**不 commit**

            重建前一定要先刪：`INSERT OR REPLACE` 只覆寫主鍵相同的列，
            以較晚的起日重建時，起日之前的舊列會原封不動留著，帶著上一代的
            `adj_factor`——兩代交界那次換月的價差因此沒有被調整，
            而表面上序列仍然連續。刪掉來源行情的壞日時同理，對應的衍生列
            也只能靠這裡清掉。
        - Parameters:
            - product / session / method / roll_rule: str
                要清掉的那一組序列
        - Return:
            - int
                刪除的列數
        """

        cursor: sqlite3.Cursor = self.conn.execute(
            f"""
            DELETE FROM {self.TABLE_NAME}
            WHERE product = ? AND session = ? AND method = ? AND roll_rule = ?
            """,
            to_sql_params(product, session, method, roll_rule),
        )
        return cursor.rowcount

    def get_series(
        self,
        product: str,
        session: str,
        method: str,
        roll_rule: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """
        - Description:
            取某一組（商品, 時段, 調整方式, 換月規則）在指定區間的序列

            四個條件缺一不可：少了任何一個，同一天會回多列（三種調整方式 ×
            三種換月規則並存於同一張表），而那些列的價格是不同口徑的。
        - Parameters:
            - product / session / method / roll_rule: str
                要取的那一組序列
            - start_date / end_date: datetime.date
                日期範圍（含兩端）
        - Return:
            - pd.DataFrame
                依日期排序的序列；`start_date > end_date` 或查無資料時為空表
        """

        if start_date > end_date:
            return pd.DataFrame()

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE product = ? AND session = ? AND method = ? AND roll_rule = ?
              AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            to_sql_params(product, session, method, roll_rule, start_date, end_date),
        )
