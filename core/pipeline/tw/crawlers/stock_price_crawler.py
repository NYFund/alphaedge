import datetime

from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler, CrawlResult
from core.pipeline.shared.request_utils import FetchResult, RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import TimeUtils

"""
TWSE 網站提供資料日期：
1. 2004/2/11 ~ present

TPEX 網站提供資料日期：
1. 上櫃資料從 96/7/2 以後才提供
2. 從 109/4/30 開始後 csv 檔的 column 不一樣
"""


class StockPriceCrawler(BaseDataCrawler):
    """爬取上市、上櫃公司的股票收盤行情（OHLC、成交量）"""

    def __init__(self) -> None:
        super().__init__()

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, date: datetime.date) -> None:
        """Crawl Price Data"""

        self.crawl_twse_price(date)
        self.crawl_tpex_price(date)

    def crawl_twse_price(self, date: datetime.date) -> CrawlResult:
        """
        - Description:
            爬取上市公司股票收盤行情（TWSE 提供 2004/2/11 起）

            回傳 `CrawlResult` 而非 `Optional[DataFrame]`：連線失敗與休市必須
            分開，否則 updater 會把「這天沒抓到」記成休市而永遠不再重試。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - CrawlResult
        """

        logger.info(f"* Start crawling TWSE Price: {date}")

        date_str: str = TimeUtils.format_date(date, sep="")
        url: str = URLManager.get_url("TWSE_CLOSING_QUOTE_URL", date=date_str)
        result: FetchResult = RequestUtils.fetch(url)

        # 個股明細固定在最後一張表。
        # **證券代號要以 converters 保留原始字串**：某些日期的代號全是數字，
        # pandas 會推斷成整數而讓 `0050` 變成 `50`（與 margin 同一個做法）
        return self.parse_html_table(
            result, f"TWSE price {date}", index=-1, converters={0: str}
        )

    def crawl_tpex_price(self, date: datetime.date) -> CrawlResult:
        """
        - Description:
            爬取上櫃公司股票收盤行情

            上櫃資料自 96/7/2 起提供，且 109/4/30 之後欄位不同（由 cleaner 處理）。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - CrawlResult
        """

        logger.info(f"* Start crawling TPEX Price: {date}")

        date_str: str = TimeUtils.format_date(date, sep="/")
        url: str = URLManager.get_url("TPEX_CLOSING_QUOTE_URL", date=date_str)
        result: FetchResult = RequestUtils.fetch(url)

        return self.parse_html_table(result, f"TPEX price {date}", index=0)
