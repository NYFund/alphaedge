import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import FUTURES_PRICE_DAILY_TABLE_NAME, TW_FUTURES_DB_PATH
from core.dao.base import BaseDAO

"""台期貨每日行情（`tw_futures.db` 的 `futures_price_daily` 表）的資料存取"""


class FuturesPriceDAO(BaseDAO):
    """
    - Description:
        期貨每日行情表的建表、寫入與查詢

        主鍵是 `(date, product, expiry, session)`：同一天同一商品有多個到期月，
        日盤與夜盤又是兩筆獨立行情。`session` 參數收 `FuturesSession` 的**字串值**
        （`day`／`night`），None 表示不過濾——DAO 不 import `core.utils`，
        Enum 由 API 轉成字串再傳進來。
    """

    TABLE_NAME: str = FUTURES_PRICE_DAILY_TABLE_NAME
    DEFAULT_DB_PATH: Optional[Path] = TW_FUTURES_DB_PATH

    # === 建表 ===
    def ensure_table(self) -> None:
        """確保資料表存在；可重複呼叫"""

        if not self.table_exists():
            self.create_table()

    def create_table(self) -> None:
        """建立期貨每日行情表並 commit"""

        # 價格單位為指數點；成交量與未沖銷契約量單位為口。
        #
        # **價格欄位一律允許 NULL**，這與 stock 各表刻意不同：
        # - 夜盤沒有結算價與未沖銷契約量（那是日結數字，日盤時段才產出）
        # - 某個契約整個時段沒有成交時，OHLC 本來就不存在
        # 這些欄位若宣告 NOT NULL，cleaner 就得填 0 才寫得進來，
        # 而結算價 0 會讓損益與維持率整段歸零且無任何徵兆。
        #
        # 主鍵含 session：同一天同一契約的日盤與夜盤是兩筆獨立行情。
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME}(
                "date" TEXT NOT NULL,
                "product" TEXT NOT NULL,
                "expiry" TEXT NOT NULL,
                "session" TEXT NOT NULL,
                "開盤價" REAL,
                "最高價" REAL,
                "最低價" REAL,
                "收盤價" REAL,
                "成交量" INT NOT NULL,
                "結算價" REAL,
                "未沖銷契約量" INT,
                "最後最佳買價" REAL,
                "最後最佳賣價" REAL,
                PRIMARY KEY ("date", "product", "expiry", "session")
            );
            """
        )
        self.conn.commit()

        if self.table_exists():
            logger.info(f"Table {self.TABLE_NAME} create successfully!")
        else:
            logger.warning(f"Table {self.TABLE_NAME} create unsuccessfully!")

    # === 查詢條件 ===
    @staticmethod
    def build_optional_filter(
        column: str, value: Optional[str]
    ) -> Tuple[str, List[Any]]:
        """
        - Description:
            組出「值為 None 就不過濾」的等值條件

            欄名只接受本類別內部寫死的字串（`product`／`session`），不接受外部輸入。
        - Parameters:
            - column: str
                欄位名
            - value: Optional[str]
                條件值；None 表示不過濾
        - Return:
            - Tuple[str, List[Any]]
                （附加在 WHERE 之後的條件片段, 對應的參數）
        """

        if value is None:
            return "", []
        return f" AND {column} = ?", [value]

    # === 查詢 ===
    def get_by_date(
        self,
        date: datetime.date,
        product: Optional[str] = None,
        session: Optional[str] = None,
    ) -> pd.DataFrame:
        """取得指定日期的行情（所有到期月），依 `product, expiry` 排序"""

        product_clause, product_params = self.build_optional_filter("product", product)
        session_clause, session_params = self.build_optional_filter("session", session)

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE date = ?{product_clause}{session_clause}
            ORDER BY product, expiry
            """,
            (date, *product_params, *session_params),
        )

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        product: Optional[str] = None,
        session: Optional[str] = None,
    ) -> pd.DataFrame:
        """取得日期範圍內的行情（所有到期月）；`start_date > end_date` 時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        product_clause, product_params = self.build_optional_filter("product", product)
        session_clause, session_params = self.build_optional_filter("session", session)

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE date BETWEEN ? AND ?{product_clause}{session_clause}
            ORDER BY date, product, expiry
            """,
            (start_date, end_date, *product_params, *session_params),
        )

    def get_contract_price(
        self,
        product: str,
        expiry: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: Optional[str] = None,
    ) -> pd.DataFrame:
        """取得單一合約在區間內的行情（依日期排序）；`start_date > end_date` 時回傳空表"""

        if start_date > end_date:
            return pd.DataFrame()

        session_clause, session_params = self.build_optional_filter("session", session)

        return self.query_df(
            f"""
            SELECT * FROM {self.TABLE_NAME}
            WHERE product = ?
            AND expiry = ?
            AND date BETWEEN ? AND ?{session_clause}
            ORDER BY date
            """,
            (product, expiry, start_date, end_date, *session_params),
        )

    def get_trading_days(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        product: Optional[str] = None,
    ) -> List[datetime.date]:
        """
        - Description:
            取得區間內表內有資料的日期（已排序、去重；不過濾 session）

            **表不存在時回空清單並警告**（尚未跑過 `--target futures_price` 是全新環境的
            正常狀態）；其他查詢錯誤往外拋。舊版以 `except pd.errors.DatabaseError`
            一併吞掉，「欄名打錯、DB 損毀」也會變成「沒有交易日」。
        - Parameters:
            - start_date / end_date: datetime.date
                查詢區間（含頭含尾）
            - product: Optional[str]
                商品代碼；None 表示任一商品有資料即算
        - Return:
            - List[datetime.date]
                區間內的交易日；無資料時為空 list
        """

        if start_date > end_date:
            return []

        if not self.table_exists():
            logger.warning(
                f"[Futures Price] {self.TABLE_NAME} 不存在，"
                f"回傳空交易日清單（請先執行 --target futures_price）"
            )
            return []

        product_clause, product_params = self.build_optional_filter("product", product)

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT date FROM {self.TABLE_NAME}
            WHERE date BETWEEN ? AND ?{product_clause}
            ORDER BY date
            """,
            (start_date, end_date, *product_params),
        )

        if df.empty:
            return []
        return pd.to_datetime(df["date"]).dt.date.tolist()

    def get_expiries(
        self,
        date: datetime.date,
        product: str,
        session: Optional[str] = None,
    ) -> List[str]:
        """取得指定日期該商品掛牌中的所有到期月（已排序）；查無資料時為空 list"""

        session_clause, session_params = self.build_optional_filter("session", session)

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT expiry FROM {self.TABLE_NAME}
            WHERE date = ?
            AND product = ?{session_clause}
            ORDER BY expiry
            """,
            (date, product, *session_params),
        )

        if df.empty:
            return []
        return df["expiry"].astype(str).tolist()

    def get_products(self) -> List[str]:
        """取得表內有資料的所有商品代碼（已排序）"""

        df: pd.DataFrame = self.query_df(
            f"""
            SELECT DISTINCT product FROM {self.TABLE_NAME}
            ORDER BY product
            """
        )

        if df.empty:
            return []
        return df["product"].astype(str).tolist()

    def get_day_session_volume_stats(
        self,
        products: List[str],
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
    ) -> pd.DataFrame:
        """
        - Description:
            各商品日盤的交易日數與總成交量（流動性排序用）
        - Parameters:
            - products: List[str]
                要統計的商品；空清單回空表
            - start_date / end_date: Optional[datetime.date]
                統計區間（含頭含尾）；None 表示不限
        - Return:
            - pd.DataFrame
                欄位 `product`／`trading_days`／`total_volume`；無資料的商品不出現
        """

        if not products:
            return pd.DataFrame(columns=["product", "trading_days", "total_volume"])

        conditions: List[str] = ["session = 'day'"]
        params: List[Any] = []
        if start_date is not None:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date is not None:
            conditions.append("date <= ?")
            params.append(end_date)

        placeholders: str = ",".join("?" * len(products))
        params.extend(products)

        return self.query_df(
            f"""
            SELECT product, COUNT(DISTINCT date) AS trading_days, SUM(成交量) AS total_volume
            FROM {self.TABLE_NAME}
            WHERE {" AND ".join(conditions)} AND product IN ({placeholders})
            GROUP BY product
            """,
            tuple(params),
        )

    def get_latest_date_by_product(self, product: str) -> Optional[str]:
        """
        - Description:
            該商品在表內的最新日期；表不存在或無該商品資料時為 None

            **以 product 為單位而非全表**：新加入的商品在表內沒有任何資料，
            用全表最新日當起點會讓它的歷史整段補不到。查詢錯誤往外拋——
            吞掉會讓 updater 從預設起日靜默重跑整段回補。
        - Parameters:
            - product: str
                商品代碼
        - Return:
            - Optional[str]
                最新日期（`YYYY-MM-DD`）
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"SELECT MAX(date) FROM {self.TABLE_NAME} WHERE product = ?",
            (product,),
        )
        return row[0] if row and row[0] else None

    def get_product_summary(self, product: str) -> Optional[Tuple[int, str, str]]:
        """
        - Description:
            該商品的列數與日期範圍；表不存在或無資料時為 None
        - Parameters:
            - product: str
                商品代碼
        - Return:
            - Optional[Tuple[int, str, str]]
                （列數, 最早日期, 最晚日期）
        """

        if not self.table_exists():
            return None

        row: Optional[Tuple[Any, ...]] = self.fetch_one(
            f"""
            SELECT COUNT(*), MIN(date), MAX(date)
            FROM {self.TABLE_NAME} WHERE product = ?
            """,
            (product,),
        )
        if not row or not row[0]:
            return None
        return int(row[0]), str(row[1]), str(row[2])
