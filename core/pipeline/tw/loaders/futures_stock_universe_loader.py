from pathlib import Path
from typing import List, Optional, Set

import pandas as pd
from loguru import logger

from core.config import FUTURES_UNIVERSE_DOWNLOADS_PATH, TW_FUTURES_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
Futures Stock Universe Loader

**這張表是「快照序列」，不是「現況表」**——每次執行都新增一整份當日快照，
不覆蓋也不刪除舊的。理由是來源（TAIFEX 標的一覽表）只給當下有哪些商品，
沒有掛牌日與下市日；唯一能得到這兩個日期的方式就是留下每次看到的樣子再差分：

- 掛牌日 ≈ `MIN(snapshot_date)`（該商品第一次出現的快照日）
- 下市   ≈ 該商品的 `MAX(snapshot_date)` 早於全表最新快照日
- 契約單位異動 ≈ 同一 `product_id` 的 `contract_size` 在快照之間改變

⚠️ **這三者都是「觀測值」而非官方日期**：本表建立之前就已掛牌的商品，
其 `MIN(snapshot_date)` 只會是本表的第一天，不是真正的掛牌日。要精確的掛牌／
下市日必須另抓 TAIFEX 契約調整與商品異動公告。

每份快照約 320 列，即使每日執行，一年也只有約 8 萬列，不需要為了省空間改成
覆蓋式現況表——那會讓上面三個問題全部無解。
"""


class FuturesStockUniverseLoader(BaseDataLoader):
    """Futures Stock Universe Loader"""

    def __init__(self, dao: Optional[FuturesStockUniverseDAO] = None) -> None:
        """
        - Description:
            建立股期標的池 loader
        - Parameters:
            - dao: Optional[FuturesStockUniverseDAO]
                共用的 DAO（通常由 updater 傳入，讓讀寫走同一條連線）。
                指定時 loader 不擁有它，`disconnect()` 不會關閉；
                未指定時 loader 自行建立，入庫完成即關閉
        """

        super().__init__()

        self.dao: Optional[FuturesStockUniverseDAO] = dao
        self.owns_dao: bool = dao is None

        # 保留 `conn` 屬性：既有呼叫端與測試仍以它判斷連線狀態（指向 tw_futures.db）
        self.conn: Optional[DBConnection] = dao.conn if dao else None

        # Downloads directory Path
        self.universe_dir: Path = FUTURES_UNIVERSE_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Loader"""

        self.connect()

        # Ensure Database Table Exists
        self.create_missing_tables()

        self.universe_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        """Connect to the Database"""

        if self.dao is None:
            # 期貨與股票分庫，故不是 TW_STOCK_DB_PATH；路徑在呼叫當下從本模組讀取，
            # 測試才能以 monkeypatch 改寫
            TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.dao = FuturesStockUniverseDAO(db_path=TW_FUTURES_DB_PATH)
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
        """創建股票期貨標的池 db"""

        self.dao.create_table()

    def create_missing_tables(self) -> None:
        """確保股票期貨標的池資料表存在"""

        self.dao.ensure_table()

    def add_to_db(
        self,
        remove_files: bool = False,
        only_dates: Optional[Set[str]] = None,
    ) -> None:
        """
        - Description:
            將資料夾中的所有 CSV 檔存入 tw_futures.db 的股票期貨標的池表；有任何檔案失敗
            就拋 `DataLoadError`。每個檔案包在 savepoint 內，寫到一半出錯時整檔回滾
        - Parameters:
            - remove_files: bool
                全部成功後是否刪除 downloads 目錄
            - only_dates: Optional[Set[str]]
                只處理這些日期（`YYYYMMDD`）的檔案；None 表示整個目錄
        """

        if self.dao is None:
            self.connect()

        self.create_missing_tables()

        file_cnt: int = 0
        failed_files: List[str] = []
        partial_files: List[str] = []
        skipped_cnt: int = 0

        for file_path in self.select_csv_files(self.universe_dir, only_dates):
            try:
                # 商品代碼與證券代號一律當字串：`0050` 的前導 0 會被吃掉，
                # 而 `00679B` 根本不是數字。
                #
                # **`keep_default_na=False` 不可省，且 `dtype=str` 擋不住它**：
                # 穩懋的商品代碼就是 `NA`，落在 pandas 預設的 NA 字面值裡，回讀時
                # 會變成 NaN 而觸發 base_code 的 NOT NULL，再被 `INSERT OR IGNORE`
                # 靜靜吞掉——2026-08-29 實測就是這樣少了 1 檔（319/320），
                # 只有 `finish_load` 的「部分列寫入」警告會提到。
                # 同一個坑在 crawler 解析 HTML 時已經踩過一次，CSV 回讀是第二次。
                #
                # `na_values=[""]` 則是為了保住真正的空值：關掉預設 NA 之後，
                # 沒有夜盤的空欄位會變成空字串而不是 NULL。
                df: pd.DataFrame = pd.read_csv(
                    file_path,
                    dtype={
                        "product_id": str,
                        "base_code": str,
                        "underlying_stock_id": str,
                    },
                    keep_default_na=False,
                    na_values=[""],
                )
                inserted: int
                skipped: int
                with self.dao.savepoint():
                    inserted, skipped = self.dao.insert_or_ignore(df)
                if inserted == 0 and skipped > 0:
                    # 整檔已在資料庫中：同一天重跑必然走到這裡
                    skipped_cnt += 1
                    continue
                if skipped > 0:
                    partial_files.append(str(file_path))
                logger.info(f"Save {file_path} into database")
                file_cnt += 1
            except Exception as e:
                logger.warning(f"Error saving {file_path}: {e}")
                failed_files.append(str(file_path))

        self.dao.commit()
        self.disconnect()

        self.finish_load(
            source="futures_stock_universe",
            succeeded=file_cnt,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=FUTURES_UNIVERSE_DOWNLOADS_PATH,
            skipped_files=skipped_cnt,
            partial_files=partial_files,
        )
