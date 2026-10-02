import datetime
from typing import List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    SHORT_SALE_LIST_CORRUPTED_DAYS,
    SHORT_SALE_LIST_START_DATE,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.dao.tw.stock_short_sale_list_dao import StockShortSaleListDAO
from core.pipeline.shared.base_crawler import CrawlResult
from core.pipeline.shared.base_updater import DailyTwoMarketUpdater
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.pipeline.tw.cleaners.stock_trading_list_cleaner import (
    StockTradingListCleaner,
)
from core.pipeline.tw.crawlers.stock_trading_list_crawler import (
    StockTradingListCrawler,
)
from core.pipeline.tw.loaders.stock_short_sale_list_loader import (
    StockShortSaleListLoader,
)

"""台股平盤下得融（借）券賣出名單：逐日爬上市＋上櫃、清洗、分批入庫（端點與陷阱見 `StockTradingListCrawler`）"""


class StockShortSaleListUpdater(DailyTwoMarketUpdater):
    """Stock ShortSale List Updater"""

    SOURCE: str = "short_sale_list"
    SOURCE_LABEL: str = "Short Sale List"
    LOG_FILE_NAME: str = "update_short_sale_list.log"

    def __init__(self) -> None:
        super().__init__()

        # 讀（日期規劃）與寫（loader）共用同一個 DAO，理由同 `StockMarginUpdater`
        self.dao: StockShortSaleListDAO = StockShortSaleListDAO(
            db_path=TW_STOCK_DB_PATH
        )
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: StockTradingListCrawler = StockTradingListCrawler()
        self.cleaner: StockTradingListCleaner = StockTradingListCleaner()
        self.loader: StockShortSaleListLoader = StockShortSaleListLoader(dao=self.dao)

        self.setup()

    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        以 `price` 表的交易日為日曆，起日不早於名單的起點

        **起點之前一律不請求**：TPEX 在起點之前回 `ok`、0 列，爬了只會被記成失敗、
        每次執行都重試一輪，而那些日子永遠不會有資料。
        """

        if start_date < SHORT_SALE_LIST_START_DATE:
            logger.info(
                f"Short Sale List 自 {SHORT_SALE_LIST_START_DATE} 起提供，起日 {start_date} 調整為該日"
            )
            start_date = SHORT_SALE_LIST_START_DATE

        return self.plan_dates_by_price_calendar(progress, start_date, end_date)

    def crawl_day(self, date: datetime.date) -> Tuple[CrawlResult, CrawlResult]:
        """
        - Description:
            爬取單日的上市與上櫃名單；來源已損毀的市場日改以前後交易日推估

            **先照常爬，拿不到才推估**：站方哪天修好了，直接用真資料，不必改設定。
            只有 `SHORT_SALE_LIST_CORRUPTED_DAYS` 列出的市場日會推估，其餘失敗照舊
            記為未完成、下次重試。
        """

        twse, tpex = super().crawl_day(date)
        if not twse.is_ok and (date, "TWSE") in SHORT_SALE_LIST_CORRUPTED_DAYS:
            twse = self.estimate_from_neighbors(date, "TWSE")
        if not tpex.is_ok and (date, "TPEX") in SHORT_SALE_LIST_CORRUPTED_DAYS:
            tpex = self.estimate_from_neighbors(date, "TPEX")
        return twse, tpex

    def estimate_from_neighbors(self, date: datetime.date, market: str) -> CrawlResult:
        """
        - Description:
            以前一與後一交易日的名單推估來源已損毀的那一天

            前後交易日取自 `price` 表（含補行交易日）。任一鄰日拿不到就放棄推估、
            記為失敗，不退而只用單邊——單邊等於少了一半的保守性，而且沒人會發現。
        - Parameters:
            - date: datetime.date
                損毀的那一天
            - market: str
                `"TWSE"` 或 `"TPEX"`
        - Return:
            - CrawlResult
                推估成功為 `OK`（原始表格形狀，照常清洗入庫）；否則 `FAILED`
        """

        calendar: Set[datetime.date] = DatePlanner.get_trading_dates(
            StockPriceDAO(conn=self.dao.conn),
            date - datetime.timedelta(days=30),
            date + datetime.timedelta(days=30),
        )
        prev_dates: List[datetime.date] = sorted(d for d in calendar if d < date)
        next_dates: List[datetime.date] = sorted(d for d in calendar if d > date)
        if not prev_dates or not next_dates:
            logger.warning(
                f"[{self.SOURCE}] {market} {date} 找不到前後交易日，無法推估"
            )
            return CrawlResult.failed("neighbor_day_not_found")

        crawl = getattr(self.crawler, f"crawl_{market.lower()}_{self.SOURCE}")
        before: CrawlResult = crawl(prev_dates[-1])
        after: CrawlResult = crawl(next_dates[0])
        if not (before.is_ok and after.is_ok):
            logger.warning(
                f"[{self.SOURCE}] {market} {date} 的鄰日名單沒有完整取得"
                f"（{prev_dates[-1]}: {before.status.value}、"
                f"{next_dates[0]}: {after.status.value}），本次不推估"
            )
            return CrawlResult.failed("neighbor_day_unavailable")

        # 欄數不同時 `merge_strictest()` 會拋例外，在這裡先擋下，只讓這一天失敗而不中止整批
        if len(before.data.columns) != len(after.data.columns):
            logger.warning(
                f"[{self.SOURCE}] {market} {date} 的前後兩天欄數不同，本次不推估"
            )
            return CrawlResult.failed("neighbor_layout_mismatch")

        merged: pd.DataFrame = self.merge_strictest(before.data, after.data)
        logger.warning(
            f"[{self.SOURCE}] {market} {date} 來源已損毀"
            f"（{SHORT_SALE_LIST_CORRUPTED_DAYS[(date, market)]}），"
            f"改以 {prev_dates[-1]} 與 {next_dates[0]} 的名單推估：{len(merged)} 檔"
        )
        return CrawlResult.ok(merged)

    @staticmethod
    def merge_strictest(before: pd.DataFrame, after: pd.DataFrame) -> pd.DataFrame:
        """
        - Description:
            合併前後兩天的原始名單，每一處都取對放空較嚴格的一邊

            - 成員取交集：兩天都在名單上，才當作那天可平盤下融（借）券賣出
            - 註記取聯集：暫停融券、暫停借券、禁止平盤下任一天有標，就當作有標

            推估錯的代價只能是「回測少放空幾筆」，不能是「回測放空了實際不能放空的標的」。
            欄位依位置對應（代號、名稱、其後皆為註記），與清洗器一致。
        - Parameters:
            - before / after: pd.DataFrame
                前一與後一交易日的原始表格（欄數須相同）
        - Return:
            - pd.DataFrame
                與原始表格同形狀的推估名單，名稱沿用前一日
        """

        if len(before.columns) != len(after.columns):
            raise ValueError(
                f"前後兩天的欄數不同（{len(before.columns)} vs {len(after.columns)}），無法逐欄合併"
            )

        before = before.copy()
        after = after.copy()
        after.columns = before.columns
        code_col: str = before.columns[0]
        before[code_col] = before[code_col].astype(str).str.strip()
        after[code_col] = after[code_col].astype(str).str.strip()

        later: pd.DataFrame = after.drop_duplicates(code_col).set_index(code_col)
        merged: pd.DataFrame = before[before[code_col].isin(later.index)].copy()
        for col in before.columns[2:]:
            flagged: pd.Series = merged[col].fillna("").astype(str).str.strip().ne("")
            merged[col] = merged[col].where(flagged, merged[code_col].map(later[col]))
        return merged.reset_index(drop=True)
