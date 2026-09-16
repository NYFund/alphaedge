from pathlib import Path
from typing import Optional

from loguru import logger

from core.config import FUTURES_CONTINUOUS_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO

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
