from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.pipeline.shared.base_updater import YearlyRangeTwoMarketUpdater
from core.pipeline.tw.cleaners.corporate_action_cleaner import (
    OUTPUT_COLUMNS,
    CorporateActionCleaner,
)
from core.pipeline.tw.crawlers.corporate_action_crawler import CorporateActionCrawler
from core.pipeline.tw.loaders.corporate_action_loader import CorporateActionLoader

"""
非除權息公司行動（減資／面額變更）更新器

兩個來源都支援日期區間，故一律**以「年」為單位請求**：2013~今日各只要十餘次請求，
不退化成逐日爬取。實測全期間上市 360 筆、上櫃 283 筆，量很小。
這類事件是「事後公告」，每次重掃整個區間；流程見 `YearlyRangeTwoMarketUpdater`。
"""


class CorporateActionUpdater(YearlyRangeTwoMarketUpdater):
    """公司行動（減資、面額變更）updater"""

    SOURCE: str = "corporate_action"
    SOURCE_LABEL: str = "Corporate Action"
    TABLE_LABEL: str = "Corporate action"
    LOG_FILE_NAME: str = "update_corporate_action.log"
    # 公司行動的清洗檔名要自帶 `.csv`（cleaner 照原樣落地），與除權息不同
    FILE_SUFFIX: str = ".csv"

    def __init__(self) -> None:
        super().__init__()

        # 讀（最新日期）與寫（loader）共用同一個 DAO，一次更新只開一條連線
        self.dao: CorporateActionDAO = CorporateActionDAO(db_path=TW_STOCK_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        self.crawler: CorporateActionCrawler = CorporateActionCrawler()
        self.cleaner: CorporateActionCleaner = CorporateActionCleaner()
        self.loader: CorporateActionLoader = CorporateActionLoader(dao=self.dao)

        self.setup()

    def load_detected_events(
        self, events: List[Dict[str, object]], file_name: str = "detected.csv"
    ) -> None:
        """
        - Description:
            把**人工確認過**的偵測事件以 `detected` 來源寫入

            ETF 的受益權單位分割不在任何結構化端點裡（`TWTB8U` 與 OpenAPI 的
            143 個端點都查過），只能由 `corporate_action_detector` 產出候選、
            人工確認後從這裡進來。0050 的 2025-06-18 一拆四就是這一類。

            **倍率由前收盤與參考價相除得出，與端點來源同一個定義**，
            下游算還原係數時才不必分辨資料是哪裡來的。
        - Parameters:
            - events: List[Dict[str, object]]
                每筆需含 `date`／`stock_id`／`證券名稱`／`停止買賣前收盤價`／
                `恢復買賣參考價`／`原因`
            - file_name: str
                落地的 CSV 檔名
        """

        if not events:
            logger.info("[corporate_action] 沒有要寫入的偵測事件")
            return

        df: pd.DataFrame = pd.DataFrame(events)
        required: List[str] = [
            "date",
            "stock_id",
            "證券名稱",
            "停止買賣前收盤價",
            "恢復買賣參考價",
            "原因",
        ]
        missing: List[str] = [column for column in required if column not in df.columns]
        if missing:
            raise ValueError(f"偵測事件缺欄位：{missing}")

        df["stock_id"] = df["stock_id"].astype(str)
        df["調整倍率"] = (df["恢復買賣參考價"] / df["停止買賣前收盤價"]).round(6)
        df["資料來源"] = "detected"
        df["事件類型"] = df.apply(
            lambda row: self.cleaner.classify_event(
                reason=row["原因"],
                ratio=row["調整倍率"],
                source="detected",
            ),
            axis=1,
        )

        self.cleaner.save(df[OUTPUT_COLUMNS], file_name)
        self.loader.add_to_db(remove_files=False)
        logger.info(f"[corporate_action] 已寫入 {len(df)} 筆人工確認的偵測事件")
