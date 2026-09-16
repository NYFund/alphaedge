from pathlib import Path
from typing import Optional, Set

import pandas as pd
from loguru import logger

from core.config import (
    FUTURES_MARGIN_DOWNLOADS_PATH,
    FUTURES_MARGIN_HISTORY_TABLE_NAME,
    STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.connection import DBConnection
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
Futures Margin Loader

**負責兩張表**，分表依據是「金額 vs 比例」而不是「指數 vs 股票」：

| 表 | 內容 | 寫入方法 |
|----|------|----------|
| `futures_margin_history` | 每口固定金額：指數期貨 ＋ **ETF 股期** | `add_to_db()` |
| `stock_futures_margin_rate_history` | 適用比例 ＋ 級距：股票股期 | `add_rates_to_db()` |

ETF 股期給的是每口固定金額，語意與臺股期貨相同，故與指數期貨同表；
硬要因為它掛在「股票類」來源就塞進比例表，比例欄會永遠是 NULL。

**兩張表都是「變動序列」，不是每日快照**——只有保證金真的變動時才會新增列。
主鍵中的 `effective_date` 取自來源該段落的「更新日期」，即該組保證金開始適用的
日子；同一組連抓 30 天只會產生 1 列，其餘 29 次被 `INSERT OR IGNORE` 擋掉。

因此「某日生效的保證金」的查法是**取 `effective_date <= 該日` 的最大者**，
不是 `effective_date = 該日`——後者只有在剛好調整的那天才查得到（見 S5 的 API）。

**比例表的每口保證金要自己算**：`標的股價 × 契約單位 × 比例`，其中契約單位取自
`futures_stock_universe.contract_size`（2000 股／100 股）、股價取自 `tw_stock.db`
的 `price` 表。這是兩張表最大的使用差異。
"""


class FuturesMarginLoader(BaseDataLoader):
    """Futures Margin Loader（金額表 ＋ 比例表）"""

    def __init__(self, dao: Optional[FuturesMarginDAO] = None) -> None:
        """
        - Description:
            建立期貨保證金 loader
        - Parameters:
            - dao: Optional[FuturesMarginDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；未指定時 loader 自行建立
        """

        super().__init__()

        self.dao: Optional[FuturesMarginDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態（指向 tw_futures.db）
        self.conn: Optional[DBConnection] = dao.conn if dao else None

        # Downloads directory Path
        self.margin_dir: Path = FUTURES_MARGIN_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.margin_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
            TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.dao = FuturesMarginDAO(db_path=TW_FUTURES_DB_PATH)
            self.owns_dao = True
        self.conn = self.dao.conn

    def disconnect(self) -> None:
        """Disconnect the Database；共用的 DAO 由建立者關閉"""

        if not self.owns_dao:
            return

        if self.dao is not None:
            self.dao.close()
            self.dao = None
        self.conn = None

    def create_db(self) -> None:
        """Create New Database Table"""

        self.dao.create_tables()

    def create_missing_tables(self) -> None:
        """確保兩張保證金資料表都存在"""

        self.dao.ensure_tables()

    def add_to_db(self, df: pd.DataFrame) -> int:
        """
        - Description:
            將金額型保證金寫入 `futures_margin_history`

            指數期貨與 ETF 股期皆走這裡——兩者的欄位完全相同。
        - Parameters:
            - df: pd.DataFrame
                cleaner 產出的金額 DataFrame
        - Return:
            - int
                實際新增的列數
        """

        return self.insert_rows(df, FUTURES_MARGIN_HISTORY_TABLE_NAME, "保證金")

    def add_rates_to_db(self, df: pd.DataFrame, replace: bool = False) -> int:
        """
        - Description:
            將比例型保證金寫入 `stock_futures_margin_rate_history`（股票股期）
        - Parameters:
            - df: pd.DataFrame
                cleaner 產出的比例 DataFrame
        - Return:
            - int
                實際新增的列數
        """

        return self.insert_rows(
            df,
            STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
            "股期保證金比例",
            replace=replace,
        )

    def add_announcements_to_db(self, df: pd.DataFrame) -> int:
        """
        - Description:
            將調整公告的保證金寫入金額表，**同一主鍵時覆蓋既有列**

            **公告比現行一覽表權威**：它明載生效日與「調整前／調整後」兩組數字，
            而一覽表只說「現在是多少」。若同一個 `(生效日, 商品)` 兩者都有，
            應以公告為準——否則先寫入的 snapshot 會擋住公告，
            讓該次調整在表中查無 `source='announcement'` 的列，
            鏈式驗證因此出現假斷點（2026-09-01 實測踩到）。
        - Parameters:
            - df: pd.DataFrame
                cleaner 產出的公告 DataFrame（僅 `margin_cleaned_cols`）
        - Return:
            - int
                實際新增的列數（覆蓋既有列時不計入）
        """

        return self.insert_rows(
            df, FUTURES_MARGIN_HISTORY_TABLE_NAME, "保證金（公告）", replace=True
        )

    def insert_rows(
        self, df: pd.DataFrame, table: str, label: str, replace: bool = False
    ) -> int:
        """
        - Description:
            兩張表共用的寫入：`INSERT OR IGNORE`／`INSERT OR REPLACE`，寫完即 commit

            同一組保證金重複抓到時整批被忽略，這正是「變動序列」的實現方式，
            不需要另外判斷有沒有變。寫入包在 savepoint 內，失敗時整批回滾。
        - Parameters:
            - df: pd.DataFrame
                要寫入的資料
            - table: str
                目標資料表
            - label: str
                log 用的人話名稱
            - replace: bool
                True 時同主鍵覆蓋（公告用），False 時同主鍵忽略（一覽表用）
        - Return:
            - int
                實際新增的列數
        """

        if df is None or df.empty:
            logger.warning(f"[Futures Margin] 無{label}資料可入庫")
            return 0

        if self.dao is None:
            self.connect()
        self.create_missing_tables()

        inserted: int
        with self.dao.savepoint():
            inserted = self.dao.insert_rows(table, df, replace=replace)
        self.dao.commit()

        if inserted:
            logger.info(f"* 新增 {inserted} 列{label}（共 {len(df)} 列，其餘已存在）")
        else:
            logger.info(f"* {label}無變動，{len(df)} 列皆已存在")

        return inserted

    def count_rows(self, table: str = FUTURES_MARGIN_HISTORY_TABLE_NAME) -> int:
        """指定資料表目前的列數；預設為金額表"""

        return self.dao.count_rows(table)

    def get_effective_dates(
        self,
        table: str = FUTURES_MARGIN_HISTORY_TABLE_NAME,
        source: Optional[str] = None,
    ) -> Set[str]:
        """
        指定資料表已有的所有生效日；預設為金額表

        **`source` 不可省略地當成「全部」用**：回補時要問的是「這則公告入庫了嗎」，
        若把 snapshot 的日期也算進來，恰好同一天的公告會被誤判為已處理而整則跳過
        （2026-09-01 實測踩到，該則的其餘 25 個商品因此全部沒進表）。
        """

        if self.dao is None:
            self.connect()
        self.create_missing_tables()

        return self.dao.get_effective_dates(table, source=source)
