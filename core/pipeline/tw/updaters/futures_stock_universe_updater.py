import datetime
import sqlite3
from typing import List, Optional, Set

import pandas as pd
from loguru import logger

from core.config import PRICE_TABLE_NAME, TW_FUTURES_DB_PATH, TW_STOCK_DB_PATH
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.futures_stock_universe_cleaner import (
    FuturesStockUniverseCleaner,
)
from core.pipeline.tw.crawlers.futures_stock_universe_crawler import (
    FuturesStockUniverseCrawler,
)
from core.pipeline.tw.loaders.futures_stock_universe_loader import (
    FuturesStockUniverseLoader,
)
from core.utils import StockFuturesType, TimeUtils
from core.utils.log_manager import LogManager

"""
股票期貨標的池 Updater

**存在的理由：股期不能像指數期貨那樣把商品清單寫死在 `FUTURES_TARGET_PRODUCTS`**。
指數期貨 5~15 檔、幾年才動一次，字面值清單完全夠用；股期 320 檔且會隨掛牌／下市
異動，手寫清單必然過期，也沒有地方放契約單位的歷史序列。故清單改由本表提供，
下游（股期行情 ETL）以 `FuturesStockUniverseAPI.get_products()` 取得要爬的商品，
不必為每一檔手動指定。

1. **一次請求就結束，沒有回補區間**
    來源是一張完整的清單頁，不分日期也不分商品，與 `futures_price_updater`
    「逐商品 × 逐時段 × 逐日」的形態完全不同，故本類沒有節流與分批入庫。

2. 冪等：同一天重跑不會產生第二份快照
    快照日就是執行日，主鍵為 (snapshot_date, product_id)，重跑會被
    `INSERT OR IGNORE` 擋下。爬之前先查表可以連請求都省下來。

3. 更新頻率建議「每日」而不是「每月」
    掛牌／下市日在本表只能由快照差分推得，快照愈稀疏，推出來的日期誤差愈大。
    整份清單一天只有一次請求，成本可以忽略。
"""


class FuturesStockUniverseUpdater(BaseDataUpdater):
    """Futures Stock Universe Updater"""

    def __init__(self) -> None:
        super().__init__()

        # **讀（快照是否已入庫、前一份快照、差分）與寫（loader）共用同一個 DAO**：
        # 舊版每個查詢方法各自 `sqlite3.connect()` 一次
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
            db_path=TW_FUTURES_DB_PATH
        )
        self.conn: Optional[sqlite3.Connection] = self.dao.conn

        self.crawler: FuturesStockUniverseCrawler = FuturesStockUniverseCrawler()
        self.cleaner: FuturesStockUniverseCleaner = FuturesStockUniverseCleaner()
        self.loader: FuturesStockUniverseLoader = FuturesStockUniverseLoader(
            dao=self.dao
        )

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("futures_stock_universe_updater.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def update(
        self,
        snapshot_date: Optional[datetime.date] = None,
        force: bool = False,
    ) -> None:
        """
        - Description:
            抓取一份股票期貨標的池快照並入庫

            **快照日預設為今天而不是「最新交易日」**：本表記錄的是「這一天在
            TAIFEX 看到的清單」，不是某個交易日的行情，假日抓到的清單一樣有效。
        - Parameters:
            - snapshot_date: Optional[datetime.date]
                快照日；None 表示今天
            - force: bool
                當日快照已存在時是否仍重抓。預設不重抓，連請求都省下來
        """

        snapshot_date = snapshot_date or datetime.date.today()

        if not force and self.is_snapshot_loaded(snapshot_date):
            logger.info(
                f"* {snapshot_date} 的標的池快照已存在，略過（force=True 可重抓）"
            )
            return

        logger.info(f"* Start Updating TAIFEX Stock Futures Universe: {snapshot_date}")

        raw_df: Optional[pd.DataFrame] = self.crawler.crawl_stock_universe()
        if raw_df is None or raw_df.empty:
            logger.warning("[Futures Universe] 未取得標的清單，本次不入庫")
            return

        cleaned_df: Optional[pd.DataFrame] = self.cleaner.clean_stock_universe(
            raw_df, snapshot_date
        )
        if cleaned_df is None or cleaned_df.empty:
            logger.warning("[Futures Universe] 清洗後無有效資料，本次不入庫")
            return

        # 差分要拿「入庫前」的最近一份快照比，入庫後就比不出差異了
        previous_date: Optional[str] = self.get_latest_snapshot_date(
            before=snapshot_date
        )

        self.loader.add_to_db(
            remove_files=False,
            only_dates={TimeUtils.format_date(snapshot_date)},
        )

        self.log_summary(cleaned_df)
        self.log_snapshot_diff(cleaned_df, previous_date)
        self.log_underlying_match(cleaned_df)

    def is_snapshot_loaded(self, snapshot_date: datetime.date) -> bool:
        """檢查該日快照是否已入庫"""

        return self.dao.is_snapshot_loaded(snapshot_date)

    def get_latest_snapshot_date(
        self, before: Optional[datetime.date] = None
    ) -> Optional[str]:
        """
        - Description:
            取得最新一份快照的日期
        - Parameters:
            - before: Optional[datetime.date]
                只看早於此日的快照；None 表示不設限
        - Return:
            - Optional[str]
                快照日；表不存在或尚無資料時為 None
        """

        return self.dao.get_latest_snapshot_date(before, inclusive=False)

    @staticmethod
    def log_summary(df: pd.DataFrame) -> None:
        """彙報本次快照的組成"""

        logger.info(f"* 標的池快照共 {len(df)} 檔")

        for product_type in StockFuturesType:
            count: int = int((df["product_type"] == product_type.value).sum())
            logger.info(f"  - {product_type.value}: {count} 檔")

        night_count: int = int(df["night_session_time"].notna().sum())
        logger.info(f"  - 有盤後交易時段（夜盤）: {night_count} 檔")

    def log_snapshot_diff(self, df: pd.DataFrame, previous_date: Optional[str]) -> None:
        """
        - Description:
            與前一份快照比對，列出新增、消失與契約單位異動的商品

            這三者就是本表要回答的問題（掛牌／下市／乘數調整），但**都是觀測值**：
            第一份快照沒有可比的對象，之後每一次差異也只代表「兩次觀測之間發生了
            變化」，不是官方生效日。要精確的日期須另抓 TAIFEX 商品異動公告。
        - Parameters:
            - df: pd.DataFrame
                本次快照
            - previous_date: Optional[str]
                前一份快照的日期；None 表示這是第一份
        """

        if previous_date is None:
            logger.info("* 這是第一份標的池快照，尚無可比對的前一份")
            return

        previous_df: pd.DataFrame = self.dao.get_snapshot_contracts(previous_date)

        current: Set[str] = set(df["product_id"])
        previous: Set[str] = set(previous_df["product_id"])

        added: List[str] = sorted(current - previous)
        removed: List[str] = sorted(previous - current)

        logger.info(
            f"* 與 {previous_date} 的快照比對：新增 {len(added)}、消失 {len(removed)}"
        )
        if added:
            logger.info(f"  - 新增（疑似掛牌）: {added}")
        if removed:
            logger.info(f"  - 消失（疑似下市）: {removed}")

        merged: pd.DataFrame = df.merge(
            previous_df, on="product_id", suffixes=("", "_prev")
        )
        changed: pd.DataFrame = merged[
            merged["contract_size"] != merged["contract_size_prev"]
        ]
        if not changed.empty:
            # 契約單位變動多半來自標的除權息後的契約調整，**是 PnL 會算錯的直接原因**，
            # 故不與上面的新增／消失混在同一行，單獨以 warning 列出
            logger.warning(
                f"[Futures Universe] {len(changed)} 檔的契約單位有異動，"
                f"請確認是否為除權息契約調整："
                + ", ".join(
                    f"{row.product_id} {row.underlying_name} "
                    f"{row.contract_size_prev} → {row.contract_size}"
                    for row in changed.itertuples()
                )
            )

    @staticmethod
    def log_underlying_match(df: pd.DataFrame) -> None:
        """
        - Description:
            檢查標的證券代號能否對回 tw_stock.db 的現股行情

            **這是本表最重要的驗收點**：股期回測要對照現股的除權息與籌碼，
            對不上的標的即使行情爬得回來也接不進下游。對不上通常有兩種成因：
            標的是 ETF（現股行情本來就不在 `price` 表的涵蓋範圍內），或是
            上櫃標的尚未回補；兩者都不是錯誤，故記 info 不記 warning。

            `tw_stock.db` 或 `price` 表不存在（只跑期貨的環境）時略過並警告；以唯讀開啟、
            用完即關，不會替缺檔環境建出空的 `tw_stock.db`。其他查詢錯誤往外拋。
        - Parameters:
            - df: pd.DataFrame
                本次快照
        """

        stock_ids: List[str] = sorted(set(df["underlying_stock_id"]))
        if not stock_ids:
            return

        if not TW_STOCK_DB_PATH.exists():
            logger.warning(
                f"[Futures Universe] 找不到 {TW_STOCK_DB_PATH}，略過現股代號比對"
            )
            return

        with StockPriceDAO(db_path=TW_STOCK_DB_PATH, read_only=True) as price_dao:
            if not price_dao.table_exists():
                logger.warning(
                    f"[Futures Universe] {PRICE_TABLE_NAME} 表不存在，略過現股代號比對"
                )
                return
            matched: Set[str] = price_dao.get_existing_stock_ids(stock_ids)

        unmatched: List[str] = [sid for sid in stock_ids if sid not in matched]
        logger.info(
            f"* 標的代號比對現股 {PRICE_TABLE_NAME} 表："
            f"{len(matched)}/{len(stock_ids)} 檔對得上"
        )
        if unmatched:
            logger.info(f"  - 對不上的標的（多為 ETF 或尚未回補的上櫃股）: {unmatched}")
