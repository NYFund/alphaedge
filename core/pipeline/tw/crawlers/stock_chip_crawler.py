import datetime

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import TimeUtils

"""
三大法人爬蟲資料時間表：
1. TWSE
    - TWSE: 2012 (ROC: 101)/5/2 開始提供
    - TWSE 改制時間: 2014/12/1, 2017/12/18
2. TPEX
    - TPEX: 2007 (ROC: 96)/4/20 開始提供
        - URL1: 2007/4/21 ~ 2014 (ROC: 103)/11/30
        - URL2: 2014/12/1 ~ present
    - TPEX 改制時間: 2018/1/15
"""


class StockChipCrawler(BaseDataCrawler):
    """爬取上市、上櫃股票三大法人盤後籌碼"""

    # TPEX URL 格式變更日（2014/12/1 起）
    TPEX_URL_CHANGE_DATE: datetime.date = datetime.date(2014, 12, 1)

    def __init__(self) -> None:
        super().__init__()

        self.tpex_url_change_date: datetime.date = self.TPEX_URL_CHANGE_DATE

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, date: datetime.date) -> None:
        """Crawl TWSE & TPEX Chip Data"""

        self.crawl_twse_chip(date)
        self.crawl_tpex_chip(date)

    def crawl_twse_chip(self, date: datetime.date) -> CrawlResult:
        """
        - Description:
            TWSE 三大法人單日爬蟲
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - CrawlResult
        """

        logger.info(f"* Start crawling TWSE chip: {date}")

        date_str: str = TimeUtils.format_date(date, sep="")
        twse_url: str = URLManager.get_url("TWSE_CHIP_URL", date=date_str)
        result: FetchResult = RequestUtils.fetch(twse_url)

        # 證券代號以 converters 保留原始字串，否則全數字的代號會被推斷成整數，
        # `0050` 入庫變成 `50`（與 margin 同一個做法）
        return self.parse_html_table(
            result, f"TWSE chip {date}", index=0, converters={0: str}
        )

    def crawl_tpex_chip(self, date: datetime.date) -> CrawlResult:
        """
        - Description:
            TPEX 三大法人單日爬蟲

            回傳的表格首列是合計列、末欄是空欄，兩者都在這裡去掉；
            **去不掉代表版面改了**，那是 FAILED 而不是休市。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - CrawlResult
        """

        logger.info(f"* Start crawling TPEX chip: {date}")

        # 根據 TPEX URL 改制時間取得對應的 URL
        date_str: str = TimeUtils.format_date(date, sep="/")
        if date < self.tpex_url_change_date:
            tpex_url: str = URLManager.get_url("TPEX_CHIP_URL_1", date=date_str)
        else:
            tpex_url: str = URLManager.get_url("TPEX_CHIP_URL_2", date=date_str)

        result: FetchResult = RequestUtils.fetch(tpex_url)
        parsed: CrawlResult = self.parse_html_table(
            result, f"TPEX chip {date}", index=0
        )
        if not parsed.is_ok:
            return parsed

        tpex_df: pd.DataFrame = parsed.data
        try:
            tpex_df.drop(
                index=tpex_df.index[0], columns=tpex_df.columns[-1], inplace=True
            )
        except (IndexError, KeyError) as error:
            # 表格是空的時 `index[0]` 拋 `IndexError`，欄位數與預期不符時
            # `drop(columns=...)` 拋 `KeyError`——兩者都代表版面變了
            logger.warning(
                f"TPEX chip {date}: 版面與預期不符（{type(error).__name__}: {error}）"
            )
            return CrawlResult.failed(f"unexpected_layout: {type(error).__name__}")

        # 去掉合計列後沒有任何個股，代表當天沒有資料
        if tpex_df.empty:
            logger.info(f"TPEX chip {date}: 去除合計列後無資料（休市或尚未公布）")
            return CrawlResult.no_data("去除合計列後無資料")

        return CrawlResult.ok(tpex_df)
