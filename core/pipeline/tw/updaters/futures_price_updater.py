import datetime
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.api.tw.futures_stock_universe_api import FuturesStockUniverseAPI
from core.config import (
    DEFAULT_FUTURES_START_DATE,
    FUTURES_PRODUCT_LISTING_DATES,
    FUTURES_PRODUCT_NIGHT_SESSION_START_DATES,
    FUTURES_TARGET_PRODUCTS,
    TW_FUTURES_DB_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection
from core.dao.tw.futures_price_dao import FuturesPriceDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.tw.cleaners.futures_price_cleaner import FuturesPriceCleaner
from core.pipeline.tw.crawlers.futures_price_crawler import FuturesPriceCrawler
from core.pipeline.tw.loaders.futures_price_loader import FuturesPriceLoader
from core.pipeline.utils.exceptions import ProductUpdateError
from core.utils import FuturesSession, TimeUtils
from core.utils.log_manager import LogManager

"""
台期貨每日行情 Updater

1. **逐商品迴圈，不是逐日期迴圈**
   各商品的上市日不同（TX 1998-07、MTX 2001、TMF 2022 之後），且會陸續加入
   `FUTURES_TARGET_PRODUCTS`。若以「全表最新日」當續跑起點，新加的商品會被
   既有商品的進度擋住而整段歷史都補不到，故 resume 一律以 (product) 為單位。

2. 每個交易日要打兩次（日盤、夜盤）
   兩者是獨立行情，見 `futures_price_crawler` 的說明。

3. 分批入庫
   TX 全段約 6,900 個交易日、13,800 次請求，數小時起跳。整段跑完才入庫的話
   中斷等於前功盡棄，故每 `LOAD_BATCH_SIZE` 天就入庫一次。
"""


# 週六的 weekday() 值；用於判斷是否為週末
SATURDAY: int = 5

# 一個「商品 × 日」固定送出日盤＋夜盤兩次請求（夜盤查無資料也照打），
# 用於開跑前估算請求量
SESSIONS_PER_DAY: int = 2


class FuturesPriceUpdater(BaseDataUpdater):
    """Futures Price Updater"""

    # 每爬幾天就入庫一次；中斷最多只損失最後一批
    LOAD_BATCH_SIZE: int = 100
    BATCH_SLEEP_EVERY_N_FILES: int = 50
    BATCH_SLEEP_DURATION_SECONDS: int = 120
    BATCH_RANDOM_DELAY_MIN: int = 3
    BATCH_RANDOM_DELAY_MAX: int = 6

    # 查無資料時的重試等待（秒）。
    #
    # **TAIFEX 擋流量時回的是 HTTP 200 ＋ 一張沒有行情表的頁面**，與「非交易日」
    # 在 crawler 眼中完全相同（皆為 `None`）。2026-09-01 的回補實測：連跑約 160 次
    # 請求後站方開始擋，20 個原本有資料的交易日被判定成「查無資料」，保險絲因此
    # 誤觸中止——事後逐日重查，那 20 天全部都有資料。
    #
    # 因此**空產出一律再試一次**：真的沒開盤的日子重試也是空的（只多一次請求），
    # 被擋的日子則在等待後恢復。否則就是「把暫時性失敗當成『沒有資料』」：
    # 被擋的日子會被當成沒開盤，之後永遠不會再補。
    #
    # 等待時間隨連續空產出**遞增**（base × 1、×2 … 至多 ×8）：孤立的一天多半真的是
    # 國定假日，等太久是純粹的浪費；連續多天才像被擋，此時才需要給站方足夠的冷卻。
    EMPTY_RETRY_DELAY_SECONDS: int = 15
    EMPTY_RETRY_MAX_BACKOFF_FACTOR: int = 8

    # 空產出保險絲：連續這麼多個候選日都沒有任何資料就中止該商品。
    #
    # 這才是「代碼拼錯」的真正防線——crawler 只擋格式，因為「哪些代碼合法」
    # 沒有可靠的靜態答案（見 `FuturesPriceCrawler.validate_product()`）。
    # 拼錯的代碼會安靜地每天都查無資料，看起來就像「這幾年一直都是假日」，
    # 而數千次請求跑完才發現整張表是空的。
    #
    # **與 equity_change 那個「連續 30 檔無資料就判定未申報」的 bug 不同**：
    # 那裡「連續無資料」是合法狀態（一段新上市公司），誤判會**靜默跳過**；
    # 這裡是從區間**開頭**起算、且一律 **raise 中止**，不會安靜地少資料。
    EMPTY_PRODUCT_ABORT_THRESHOLD: int = 20

    def __init__(self) -> None:
        super().__init__()

        # **讀（續跑起點、摘要）與寫（loader）共用同一個 DAO**（tw_futures.db）
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.dao: FuturesPriceDAO = FuturesPriceDAO(db_path=TW_FUTURES_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # 補行交易日取自 tw_stock.db 的 `price` 表；第一次用到才以唯讀開啟，由本 updater 關閉
        self.stock_price_dao: Optional[StockPriceDAO] = None

        # ETL
        self.crawler: FuturesPriceCrawler = FuturesPriceCrawler()
        self.cleaner: FuturesPriceCleaner = FuturesPriceCleaner()
        self.loader: FuturesPriceLoader = FuturesPriceLoader(dao=self.dao)

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_futures_price.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束；含唯讀的台股連線）"""

        self.dao.close()
        self.conn = None

        if self.stock_price_dao is not None:
            self.stock_price_dao.close()
            self.stock_price_dao = None

    def get_actual_update_start_date(
        self,
        product: str,
        default_date: datetime.date,
    ) -> datetime.date:
        """
        - Description:
            取得該商品實際要開始更新的日期（表內該商品最新日 +1）

            **以 product 為單位而非全表**：新加入的商品在表內沒有任何資料，
            用全表最新日當起點會讓它的歷史整段補不到，且不會有任何錯誤訊息。
        - Parameters:
            - product: str
                商品代碼
            - default_date: datetime.date
                表內無該商品資料時的起始日
        - Return:
            - datetime.date
        """

        # **查詢錯誤往外拋**：舊版吞掉 `sqlite3.Error` 後改用預設起日，「DB 被鎖住、
        # 欄名打錯」會讓該商品從預設起日靜默重跑整段回補（數千次請求）
        latest: Optional[str] = self.dao.get_latest_date_by_product(product)
        if not latest:
            return default_date

        return TimeUtils.to_date(latest) + datetime.timedelta(days=1)

    def get_traded_weekend_dates(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """
        - Description:
            取得區間內實際開市的週末（補行交易日）

            期貨與現貨共用同一份行事曆，故直接以 `tw_stock.db` 的 `price` 表判斷，
            不另建期貨日曆（回測用的期貨日曆見 `FuturesCalendar`）。

            **已知限制**：`price` 表自 2013 起才有資料，故 **2013 年之前的補行
            交易日無法偵測**，那幾天的期貨資料會缺。補救方式是日後以明確日期
            重跑；不影響絕大多數區間。

            `tw_stock.db` 或 `price` 表不存在（只跑期貨的環境）時一律跳過週末並警告；
            其他查詢錯誤往外拋。連線以唯讀開啟、由本 updater 持有並在 `close()` 關閉
            ——舊版每次呼叫都 `with sqlite3.connect(...)`，而 `with` 只 commit 不關閉。
        - Parameters:
            - start_date / end_date: datetime.date
                查詢區間
        - Return:
            - Set[datetime.date]
                區間內開市的週末日期
        """

        trading_days: Optional[List[datetime.date]] = self.get_stock_trading_days(
            start_date, end_date
        )
        if trading_days is None:
            logger.warning("無法取得現貨交易日曆，無法判斷補行交易日，本次一律跳過週末")
            return set()

        return {date for date in trading_days if date.weekday() >= SATURDAY}

    def get_stock_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Optional[List[datetime.date]]:
        """
        區間內的現貨交易日（期貨與現貨共用同一份行事曆）

        `tw_stock.db` 或 `price` 表不存在（只跑期貨的環境）時回 None，由呼叫端決定
        怎麼退化；其他查詢錯誤往外拋。連線以唯讀開啟，第一次用到才開。
        """

        if self.stock_price_dao is None:
            if not TW_STOCK_DB_PATH.exists():
                logger.warning(f"找不到 {TW_STOCK_DB_PATH}")
                return None
            self.stock_price_dao = StockPriceDAO(
                db_path=TW_STOCK_DB_PATH, read_only=True
            )

        if not self.stock_price_dao.table_exists():
            logger.warning("tw_stock.db 的 price 表不存在")
            return None

        return self.stock_price_dao.get_trading_days(start_date, end_date)

    def find_gap_dates(self, product: str) -> List[datetime.date]:
        """
        - Description:
            該商品在表內最早與最新日期之間、現貨有開市卻沒有期貨行情的日子

            **續跑起點是「表內最新 +1」，中間的缺口它看不到**：被站方擋掉的日子
            重試後仍拿不到時會被當成沒開盤，之後的日子照常入庫、`MAX(date)` 越過
            它，這一天從此不會再被請求；`FuturesCalendar` 的交易日又取自行情表，
            缺掉的日子在回測裡被當成休市。以現貨交易日曆比對才分得出「沒開盤」
            與「沒拿到」。

            **已知限制**：`price` 表自 2013 年起才有資料，更早的缺口偵測不到；
            取不到現貨日曆時不偵測（回空清單並警告），不猜。
        - Parameters:
            - product: str
                商品代碼
        - Return:
            - List[datetime.date]
                缺口日期（已排序）
        """

        summary: Optional[Tuple[int, str, str]] = self.dao.get_product_summary(product)
        if summary is None:
            return []

        first: datetime.date = TimeUtils.to_date(summary[1])
        last: datetime.date = TimeUtils.to_date(summary[2])
        if first >= last:
            return []

        calendar: Optional[List[datetime.date]] = self.get_stock_trading_days(
            first, last
        )
        if calendar is None:
            logger.warning(f"* {product} 取不到現貨交易日曆，本次不偵測中間缺口")
            return []

        existing: Set[datetime.date] = set(
            self.dao.get_trading_days(first, last, product=product)
        )
        return [date for date in calendar if date not in existing]

    def get_candidate_dates(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """
        - Description:
            決定要送出請求的日期清單

            平日一律照爬（國定假日無法純靠日曆判斷，交給回應判定）；
            週末原則上跳過，但補行交易日例外（見 `get_traded_weekend_dates`）。
        - Parameters:
            - start_date / end_date: datetime.date
                更新區間
        - Return:
            - List[datetime.date]
        """

        traded_weekends: Set[datetime.date] = self.get_traded_weekend_dates(
            start_date, end_date
        )
        if traded_weekends:
            logger.info(
                f"區間內有 {len(traded_weekends)} 個補行交易日，一併納入更新："
                f"{sorted(traded_weekends)}"
            )

        return [
            date
            for date in TimeUtils.generate_date_range(start_date, end_date)
            if date.weekday() < SATURDAY or date in traded_weekends
        ]

    def load_batch(self, batch_dates: List[str]) -> None:
        """入庫本批爬取的日期；只載入本批檔案，避免每批重掃整個 downloads 目錄"""

        logger.info(
            f"* Loading batch: {len(batch_dates)} 天"
            f"（{batch_dates[0]} ~ {batch_dates[-1]}）"
        )
        self.loader.add_to_db(remove_files=False, only_dates=set(batch_dates))

    def update(
        self,
        start_date: datetime.date = DEFAULT_FUTURES_START_DATE,
        end_date: Optional[datetime.date] = None,
        products: Optional[List[str]] = None,
        resume: bool = True,
    ) -> None:
        """
        - Description:
            更新台期貨每日行情

            未指定 `products` 時取用 `FUTURES_TARGET_PRODUCTS`。
        - Parameters:
            - start_date: datetime.date
                起日；`resume=True` 時會被該商品在表內的最新日覆蓋
            - end_date: Optional[datetime.date]
                迄日；None 取當日
            - products: Optional[List[str]]
                要更新的商品；None 表示取設定檔
            - resume: bool
                True（日常更新）：起點取「表內該商品最新日 +1」，只補新的。
                False（歷史回補）：一律照 `start_date` 跑。**把起點往前拉時
                必須用這個**——日常路徑會被表內既有資料擋住而整段補不到，
                且不會有任何錯誤訊息，只會顯示「已是最新」。
                重複的日期由 loader 的 `INSERT OR IGNORE` 吸收，不會產生重複列。
        - Raise:
            - ProductUpdateError
                任一商品更新失敗；其餘商品仍會跑完才拋出
        """

        # 預設值不可寫成 `datetime.date.today()`——那是在 import 時求值的，
        # 長時間執行的行程會一直用啟動那天的日期（ruff B008）
        end_date: datetime.date = end_date or datetime.date.today()

        target_products: List[str] = products or FUTURES_TARGET_PRODUCTS

        # 商品代碼拼錯會讓整段回補安靜地全部查無資料，看似「一直都是假日」，
        # 故在送出任何請求之前先全部檢查一遍
        for product in target_products:
            self.crawler.validate_product(product)

        logger.info(f"* Start Updating TAIFEX Futures Price: {target_products}")

        # 逐商品隔離：一個商品觸發保險絲（或任何例外）不擋排在後面的商品——
        # 股期一次更新前 N 檔，上市晚於起點的那檔就會觸發保險絲。
        # 但跑完之後統一拋出，讓 target 仍以失敗結束，不會安靜地少一檔
        failures: Dict[str, str] = {}
        for product in target_products:
            try:
                self.update_product(product, start_date, end_date, resume=resume)
            except Exception as error:
                # **這層盲捕不可收斂**：`update_product()` 是一整條子管線，
                # 上面那行註解寫的「或任何例外」就是它的契約。收斂成列舉的話，
                # 漏掉的那一種會讓後面的商品全部不跑，而 target 仍以成功結束
                logger.opt(exception=True).error(
                    f"* {product} 更新失敗，繼續下一個商品：{type(error).__name__}"
                )
                failures[product] = f"{type(error).__name__}: {error}"

        self.log_summary(target_products)

        if failures:
            raise ProductUpdateError(
                failures, succeeded=len(target_products) - len(failures)
            )

    def update_stock_futures(
        self,
        start_date: datetime.date = DEFAULT_FUTURES_START_DATE,
        end_date: Optional[datetime.date] = None,
        top_n: Optional[int] = None,
        products: Optional[List[str]] = None,
        resume: bool = True,
    ) -> None:
        """
        - Description:
            更新**股票期貨**行情

            與指數期貨走同一條 ETL——商品代碼只是查詢參數——差別只在**商品清單
            從哪裡來**：指數期貨是 `FUTURES_TARGET_PRODUCTS` 這份字面值清單，
            股期則有 320 檔且會隨掛牌／下市異動，故改由 `futures_stock_universe`
            提供。

            **請求量由 `top_n` 把關**：320 檔全爬是每天 640 次請求（日夜盤各一），
            13 年的回補要好幾個月。實務上有意義的只有流動性前段——尾端有整批
            一天成交個位數口的商品，回測賺到的錢實際上掛不到單。
            `top_n` 在所有路徑下都是上限，包含排不出流動性的冷啟動
            （見 `resolve_stock_futures_products()`）；只有 `top_n=None` 才會
            取整份標的池，那是呼叫端明確要的。開跑前會先把估算的請求量寫進 log。
        - Parameters:
            - start_date / end_date: datetime.date
                回補區間
            - top_n: Optional[int]
                只爬流動性前 N 檔（依既有行情的平均成交量排序）
            - products: Optional[List[str]]
                直接指定商品，優先於 `top_n`
            - resume: bool
                是否從各商品表內的最新日接續
        """

        end_date: datetime.date = end_date or datetime.date.today()

        targets: List[str] = products or self.resolve_stock_futures_products(
            top_n, end_date
        )
        if not targets:
            logger.warning(
                "[Futures Price] 沒有可爬的股期商品——標的池是空的，"
                "請先執行 `--target futures_stock_universe`"
            )
            return

        # 量級要在送出第一個請求之前就看得見：`* Start updating: 320 檔` 這種訊息
        # 看不出那是 100 天還是 3 小時。用平日數當交易日上限估算（國定假日照樣
        # 送出請求才知道休市），`resume=True` 時實際量會更少
        weekdays: int = sum(
            1
            for date in TimeUtils.generate_date_range(start_date, end_date)
            if date.weekday() < SATURDAY
        )
        estimated_requests: int = len(targets) * weekdays * SESSIONS_PER_DAY
        logger.info(
            f"* Start updating stock futures price: {len(targets)} 檔"
            f"（{start_date} ~ {end_date}，估算上限約 {estimated_requests:,} 次請求）"
        )
        self.update(
            start_date=start_date,
            end_date=end_date,
            products=targets,
            resume=resume,
        )

    def resolve_stock_futures_products(
        self, top_n: Optional[int], date: datetime.date
    ) -> List[str]:
        """
        決定要爬哪些股期：有 `top_n` 就依流動性取前 N 檔，否則取整份標的池

        **流動性排序取自已入庫的行情**，故第一次跑（表內還沒有股期行情）時
        排不出來——那是雞生蛋，不是錯誤。此時退回**標的池的前 `top_n` 檔**
        當暖身樣本，暖身完再跑一次就有排序依據。

        ⚠️ **退回時不可改取整份標的池**：`top_n` 是請求量的上限，在失敗路徑上
        把它拿掉，等於呼叫端要 20 檔、實際送出 320 檔的請求量。曾經如此，
        代價是一次 100 天以上的回補擋住了排在後面的所有 target，而過程中
        只有一行警告。失敗路徑一律收緊，不放大。

        暖身樣本的順序是標的池順序（依商品代碼），**不是流動性順序**。

        標的池與行情同在 `tw_futures.db`，故共用本 updater 的連線，不另開一條。
        """

        universe_api: FuturesStockUniverseAPI = FuturesStockUniverseAPI(conn=self.conn)
        universe: List[str] = universe_api.get_products(date)
        if not top_n:
            return universe

        liquid: List[str] = universe_api.get_top_liquid_products(top_n, end_date=date)
        if liquid:
            return liquid

        logger.warning(
            f"[Futures Price] 表內還沒有足夠的股期行情，排不出流動性；"
            f"本次改取標的池前 {top_n} 檔當暖身樣本"
            f"（順序依商品代碼，非流動性）。暖身後再跑一次，"
            f"才會依實際成交量選出前 {top_n} 檔"
        )
        return universe[:top_n]

    def crawl_and_clean_date(
        self, product: str, date: datetime.date
    ) -> Set[FuturesSession]:
        """
        - Description:
            單日、雙時段（日盤 ＋ 夜盤）的爬取與清洗，回報**各時段**的結果

            **不可只回一個 bool**：「只拿到夜盤」與「兩個時段都拿到」在 bool 下
            長得一樣，於是缺日盤的那天照樣入庫、續跑起點照樣推進，缺的那一段
            永遠不會再被請求（MTX 2026-09-02 即如此——寫入時間換算台北是當天
            11:15，日盤都還沒收盤）。
        - Parameters:
            - product: str
                商品代碼
            - date: datetime.date
                查詢日
        - Return:
            - Set[FuturesSession]
                本日確實取得並清洗成功的時段
        """

        crawled: Set[FuturesSession] = set()

        # **不可寫 `for session in FuturesSession`**：那會連整併用的
        # `COMBINED` 也一起爬，而來源根本沒有那個時段（見 `data_sessions()`）
        for session in FuturesSession.data_sessions():
            if not self.has_night_session(product, date, session):
                continue

            raw_df: Optional[pd.DataFrame] = self.crawler.crawl_futures_price(
                date, product, session
            )
            if raw_df is None or raw_df.empty:
                continue

            cleaned_df: Optional[pd.DataFrame] = self.cleaner.clean_futures_price(
                raw_df, date, product, session
            )
            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned dataframe empty on {date} {product} {session.value}"
                )
                continue
            crawled.add(session)

        return crawled

    @staticmethod
    def has_night_session(
        product: str, date: datetime.date, session: FuturesSession
    ) -> bool:
        """
        - Description:
            判斷該商品在該日是否已經有夜盤可查

            盤後交易 2017-05-15 晚上才上線，且各商品是**逐批**納入的
            （TX／MTX 2017-05、TE 2018-11、ZEF 2021-06、TMF 2024-07、
            TF／ZFF 2025-06）。在那之前查夜盤等於白打一半的請求，並產生大量
            `No valid futures price rows` warning——資料是對的，雜訊是多的，
            而雜訊會淹掉真正該看的那幾行。

            **沒登錄起始日的商品一律回 True**（例如股期）：行為與本檢查加入前
            相同。填錯一個過晚的日期會讓回補靜默跳過開頭幾天，比多打請求嚴重得多，
            故寧可不登錄，見 `FUTURES_PRODUCT_NIGHT_SESSION_START_DATES`。
        - Parameters:
            - product: str
                商品代碼
            - date: datetime.date
                查詢日
            - session: FuturesSession
                要查的時段；日盤一律回 True
        - Return:
            - bool
                False 表示該日不必查這個時段
        """

        if session != FuturesSession.NIGHT:
            return True

        start: Optional[datetime.date] = FUTURES_PRODUCT_NIGHT_SESSION_START_DATES.get(
            product
        )
        return start is None or date >= start

    @staticmethod
    def is_day_complete(crawled: Set[FuturesSession]) -> bool:
        """
        - Description:
            判斷該日是否足以入庫並推進續跑起點

            判準是**日盤有沒有拿到**，不是「兩個時段都有」：TF、TE、ZFF 這類
            商品的夜盤本來就常常整天沒有成交（全表 2017-05-15 後，TF 有 1,977 天、
            TE 有 381 天只有日盤），要求兩段齊全會讓它們每次執行都重問一次，
            且永遠無法記為完成。反過來「只有夜盤沒有日盤」則一定是異常——
            日盤是主要時段，缺它代表當天的日盤尚未收盤或站方正在擋。
        - Parameters:
            - crawled: Set[FuturesSession]
                本日確實取得的時段
        - Return:
            - bool
                是否可入庫
        """

        return FuturesSession.DAY in crawled

    @staticmethod
    def clamp_to_listing_date(product: str, start_date: datetime.date) -> datetime.date:
        """
        - Description:
            把起點往後夾到該商品的上市日

            上市前的每一天都查無資料，累積 `EMPTY_PRODUCT_ABORT_THRESHOLD` 天就會
            觸發保險絲中止整檔回補（2026-09-01 的回補即因此停在 TMF）。呼叫端常常
            對所有商品傳同一個 `start_date`，故在此統一夾住，而不是要求每個呼叫端
            自己查表。

            **未登錄的商品不夾**（例如股期）：那類商品仍由保險絲擋代碼拼錯。
        - Parameters:
            - product: str
                商品代碼
            - start_date: datetime.date
                原本要開始的日期
        - Return:
            - datetime.date
                夾住後的起始日
        """

        listing_date: Optional[datetime.date] = FUTURES_PRODUCT_LISTING_DATES.get(
            product
        )
        if listing_date is None or start_date >= listing_date:
            return start_date

        logger.info(
            f"* {product} 於 {listing_date} 才上市，起點由 {start_date} "
            f"後移至該日（否則上市前的空白日會觸發保險絲）"
        )
        return listing_date

    def update_product(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        resume: bool = True,
    ) -> None:
        """單一商品的爬取 → 清洗 → 分批入庫；`resume=False` 時不查表內進度"""

        actual_start: datetime.date = (
            self.get_actual_update_start_date(product, default_date=start_date)
            if resume
            else start_date
        )
        actual_start = self.clamp_to_listing_date(product, actual_start)

        # 續跑時先補表內中間的缺口：起點是「最新 +1」，看不到被擋掉的那幾天
        gaps: List[datetime.date] = self.find_gap_dates(product) if resume else []
        if gaps:
            logger.warning(
                f"* {product} 表內有 {len(gaps)} 個現貨有開市卻沒有行情的日子，"
                f"本次一併回補：{[str(date) for date in gaps[:10]]}"
            )

        if actual_start > end_date and not gaps:
            logger.info(f"* {product} 已是最新（起點 {actual_start} 晚於 {end_date}）")
            return

        dates: List[datetime.date] = sorted(
            set(gaps) | set(self.get_candidate_dates(actual_start, end_date))
        )
        logger.info(f"* {product}: {actual_start} ~ {end_date}，共 {len(dates)} 天")

        file_cnt: int = 0
        batch_dates: List[str] = []
        consecutive_empty: int = 0

        with GracefulStop(label=f"futures_price:{product}") as stop:
            for date in dates:
                crawled: Set[FuturesSession] = self.crawl_and_clean_date(product, date)

                # 空產出可能是「非交易日」，也可能是「站方正在擋」——兩者在 crawler
                # 眼中相同，故一律等待後再試一次，只有第二次仍為空才算真的沒有資料。
                # **只拿到夜盤同樣要重試**：日盤尚未收盤時來源就是這個樣子
                if not self.is_day_complete(crawled):
                    backoff_seconds: int = self.EMPTY_RETRY_DELAY_SECONDS * min(
                        consecutive_empty + 1, self.EMPTY_RETRY_MAX_BACKOFF_FACTOR
                    )
                    logger.info(
                        f"{date} {product} 未取得日盤（本次時段："
                        f"{sorted(session.value for session in crawled) or '無'}），"
                        f"{backoff_seconds} 秒後重試一次"
                    )
                    self.sleep(backoff_seconds, stop)
                    crawled = self.crawl_and_clean_date(product, date)
                    if self.is_day_complete(crawled):
                        logger.warning(
                            f"{date} {product} 重試後取得資料——前一次為暫時性失敗（站方擋流量），"
                            f"不是非交易日"
                        )

                if self.is_day_complete(crawled):
                    batch_dates.append(TimeUtils.format_date(date))
                    consecutive_empty = 0
                else:
                    # **只有夜盤時整天不入庫**：入庫會讓 `MAX(date)` 推進過這一天，
                    # 缺的日盤永遠不會再被請求（續跑起點是 `MAX(date)+1`）
                    if crawled:
                        logger.warning(
                            f"{date} {product} 只取得 "
                            f"{sorted(session.value for session in crawled)}、缺日盤，"
                            f"整天不入庫，下次執行會重試"
                        )
                    consecutive_empty += 1
                    if consecutive_empty >= self.EMPTY_PRODUCT_ABORT_THRESHOLD:
                        # 先把已爬到的入庫再中止，不浪費前面的成果
                        if batch_dates:
                            self.load_batch(batch_dates)
                        raise ValueError(
                            f"{product} 自 {actual_start} 起連續 "
                            f"{consecutive_empty} 個候選日皆無資料，已中止。"
                            f"可能原因：① 代碼拼錯；② 該商品在此期間尚未上市"
                            f"（請調整 start_date）；③ 來源異常。"
                        )

                file_cnt += 1

                if len(batch_dates) >= self.LOAD_BATCH_SIZE:
                    self.load_batch(batch_dates)
                    batch_dates = []

                if stop.requested:
                    logger.warning(
                        f"[{product}] 收到中止要求，停在 {date}；"
                        f"手上這批先入庫再離開，未爬的日期下次執行會接續"
                    )
                    break

                file_cnt = self.throttle_per_file(file_cnt, stop)

        # 收尾：載入最後一批未達批量的日期
        if batch_dates:
            self.load_batch(batch_dates)

    def log_summary(self, products: List[str]) -> None:
        """更新後逐商品回報最新日期與列數，讓「有沒有真的補到」一眼可見"""

        for product in products:
            summary: Optional[Tuple[int, str, str]] = self.dao.get_product_summary(
                product
            )

            if summary is None:
                logger.warning(f"{product}: 表內仍無資料")
                continue

            logger.info(f"{product}: {summary[0]} 列，{summary[1]} ~ {summary[2]}")
