import datetime
import functools
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
from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.shared.base_updater import BaseDataUpdater, UpdateStats
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.shared.request_utils import RateLimitPolicy
from core.pipeline.shared.step_runner import retry_if_blocked, run_isolated
from core.pipeline.tw.cleaners.futures_price_cleaner import FuturesPriceCleaner
from core.pipeline.tw.crawlers.futures_price_crawler import FuturesPriceCrawler
from core.pipeline.tw.loaders.futures_price_loader import FuturesPriceLoader
from core.pipeline.utils.exceptions import ProductUpdateError
from core.utils import FuturesSession, TimeUtils

"""
台期貨每日行情 Updater

1. **逐商品迴圈，不是逐日期迴圈；續跑是逐商品的差集**
   各商品的上市日不同（TX 1998-07、MTX 2001、TMF 2022 之後），且會陸續加入
   `FUTURES_TARGET_PRODUCTS`。若以「全表最新日」當續跑起點，新加的商品會被
   既有商品的進度擋住而整段歷史都補不到，故候選日期一律以商品（product）為單位算
   「平日 ＋ 補行交易日 − 該商品已有 − 已確認無資料 ＋ 上次沒跑完」（`DatePlanner`），
   中間被擋掉的日子下次仍會被請求；進度檔逐商品一份。

2. 每個交易日要打兩次（日盤、夜盤）
   兩者是獨立行情，見 `futures_price_crawler` 的說明。

3. 分批入庫
   TX 全段約 6,900 個交易日、13,800 次請求，數小時起跳。整段跑完才入庫的話
   中斷等於前功盡棄，故每 `LOAD_BATCH_SIZE` 天就入庫一次。
"""


# 週六的 `weekday()` 值；用於判斷是否為週末
SATURDAY: int = 5

# 一個「商品 × 日」固定送出日盤＋夜盤兩次請求（夜盤查無資料也照打），
# 用於開跑前估算請求量
SESSIONS_PER_DAY: int = 2


class ProductDateSource:
    """
    `DatePlanner` 用的逐商品視圖：`futures_price_daily` 一張表放所有商品，差集要以商品為單位算

    用全表的日期算差集，新加入的商品會被既有商品的日期擋住、整段歷史補不到，
    而且不會有任何錯誤訊息。
    """

    def __init__(self, dao: FuturesPriceDAO, product: str) -> None:
        self.dao: FuturesPriceDAO = dao
        self.product: str = product
        # 進度訊息裡要看得出是哪個商品
        self.TABLE_NAME: str = f"{dao.TABLE_NAME}:{product}"

    def table_exists(self) -> bool:
        """行情表在不在（表不存在是全新環境的正常狀態）"""

        return self.dao.table_exists()

    def get_distinct_dates(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """該商品在區間內已有行情的日期"""

        return self.dao.get_trading_days(start_date, end_date, product=self.product)


class FuturesPriceUpdater(BaseDataUpdater):
    """期貨行情 updater"""

    LOG_FILE_NAME: str = "update_futures_price.log"

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
    # **這個判準只在這裡成立，不可搬去逐檔查詢的流程**（例如
    # `EquityChangeMixin`）：那邊「連續無資料」是合法狀態（一整段新上市公司），
    # 誤判會**靜默跳過**整季；這裡是從區間**開頭**起算、且一律 **raise 中止**，
    # 不會安靜地少資料。
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
        """初始化 updater 的設定"""

        super().setup()

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束；含唯讀的台股連線）"""

        self.dao.close()
        self.conn = None

        if self.stock_price_dao is not None:
            self.stock_price_dao.close()
            self.stock_price_dao = None

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
            ——**不可改成每次呼叫都 `with sqlite3.connect(...)`**，`with` 只 commit
            不關閉連線。
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

    def get_candidate_dates(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        """
        - Description:
            區間內所有平日 ＋ 補行交易日：差集的母集合，也是 `resume=False` 整段重抓的候選

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
                起日（會先夾到該商品的上市日）；`resume=True` 時不會早於該商品在表內的第一筆
            - end_date: Optional[datetime.date]
                迄日；None 取當日
            - products: Optional[List[str]]
                要更新的商品；None 表示取設定檔
            - resume: bool
                True（日常更新）：差集——從該商品在表內的第一筆起，補區間內缺的日子
                （含中間被擋掉的），已確認無資料的不再問（見 `plan_dates()`）。
                False（歷史回補）：一律照 `start_date` 整段重抓。**把起點往前拉到表內第一筆之前
                必須用這個**——日常路徑不回頭看第一筆之前，整段補不到，
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
        failures: Dict[str, str] = dict(
            run_isolated(
                [
                    (
                        product,
                        functools.partial(
                            self.update_product,
                            product,
                            start_date,
                            end_date,
                            resume=resume,
                        ),
                    )
                    for product in target_products
                ],
                "Futures Price",
            )
        )

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
                True 各商品走差集續跑（不回頭看表內第一筆之前）；False 照 `start_date` 整段重抓
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
        把它拿掉，等於呼叫端要 20 檔、實際送出 320 檔的請求量——一次 100 天以上的
        回補會擋住排在後面的所有 target，而過程中只有一行警告。
        失敗路徑一律收緊，不放大。

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
    ) -> Tuple[Set[FuturesSession], bool]:
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
            - Tuple[Set[FuturesSession], bool]
                （本日確實取得並清洗成功的時段, 是否有時段取不到）。後者涵蓋被擋、
                連不上、解析器出錯與清洗後為空，**不含**站方明確查無資料
        """

        crawled: Set[FuturesSession] = set()
        unreachable: bool = False

        # **不可寫 `for session in FuturesSession`**：那會連整併用的
        # `COMBINED` 也一起爬，而來源根本沒有那個時段（見 `data_sessions()`）
        for session in FuturesSession.data_sessions():
            if not self.has_night_session(product, date, session):
                continue

            result: CrawlResult = self.crawler.crawl_futures_price(
                date, product, session
            )
            if result.is_failed:
                logger.warning(
                    f"{date} {product} {session.value} 取不到：{result.reason}"
                )
                unreachable = True
                continue
            if not result.is_ok:
                continue

            cleaned_df: Optional[pd.DataFrame] = self.cleaner.clean_futures_price(
                result.data, date, product, session
            )
            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned dataframe empty on {date} {product} {session.value}"
                )
                unreachable = True
                continue
            crawled.add(session)

        return crawled, unreachable

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

    def progress_store(self, product: str) -> DateProgressStore:
        """該商品的日期進度檔（已確認無資料／上次沒跑完）；逐商品一份，休市日每個商品都要各自問過一次"""

        return DateProgressStore(f"futures_price_{product}")

    def plan_dates(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        progress: DateProgressStore,
        resume: bool = True,
    ) -> List[datetime.date]:
        """
        - Description:
            決定該商品這次要請求的日期

            **續跑是差集，不是 `MAX(date)+1`**：候選 ＝ 平日 ＋ 補行交易日 − 該商品表內已有
            − 已確認無資料 ＋ 上次沒跑完。中間被擋掉的日子因此下次仍會被請求；`MAX(date)+1`
            會越過它，而 `FuturesCalendar` 的交易日取自行情表，那一天在回測裡就成了休市。

            母集合用平日而不是現貨日曆：`price` 表自 2013 年起才有資料，拿它當日曆會讓
            更早的區間一天都不請求。但**現貨日曆涵蓋的區間內、現貨沒開市的平日就是期貨的
            休市日**（兩個市場共用同一份行事曆），直接當成已確認無資料，不浪費請求去問站方；
            日曆尾端現貨還沒更新到的日子不算——那是「還不知道」，不是「沒開市」。
            日曆之外的休市日由站方的回應判定後記進進度檔，之後不再問。

            **續跑不回頭看該商品第一筆之前**：那段對沒登錄上市日的商品（股期）就是上市前的
            空白日，每次都重問會一直撞保險絲。往前擴張區間要用 `resume=False`。
        - Parameters:
            - product: str
                商品代碼
            - start_date / end_date: datetime.date
                更新區間；起點會先夾到該商品的上市日
            - progress: DateProgressStore
                該商品的進度檔
            - resume: bool
                True 走差集；False 不看表內與進度，整段重抓（重複列由 `INSERT OR IGNORE` 吸收）
        - Return:
            - List[datetime.date]
                由早到晚的候選日期；已是最新時為空
        """

        start: datetime.date = self.clamp_to_listing_date(product, start_date)
        if resume:
            # **查詢錯誤往外拋**：吞掉改用預設起點的話，「DB 被鎖住、欄名打錯」
            # 會讓該商品從預設起日靜默重跑整段回補（數千次請求）
            summary: Optional[Tuple[int, str, str]] = self.dao.get_product_summary(
                product
            )
            if summary is not None:
                start = max(start, TimeUtils.to_date(summary[1]))
        if start > end_date:
            return []

        if not resume:
            return self.get_candidate_dates(start, end_date)

        calendar: Optional[List[datetime.date]] = self.get_stock_trading_days(
            start, end_date
        )
        if calendar is None:
            logger.warning(
                "無法取得現貨交易日曆，無法判斷補行交易日與休市日，本次一律跳過週末"
            )
        calendar_days: Set[datetime.date] = set(calendar or [])
        calendar_tail: Optional[datetime.date] = max(calendar_days, default=None)
        known_closed: Set[datetime.date] = {
            date
            for date in DatePlanner.generate_weekdays(start, end_date)
            if calendar_tail is not None
            and date <= calendar_tail
            and date not in calendar_days
        }

        return DatePlanner.plan(
            dao=ProductDateSource(self.dao, product),
            start_date=start,
            end_date=end_date,
            no_data_dates=progress.no_data | known_closed,
            incomplete_dates=progress.incomplete,
            extra_dates={date for date in calendar_days if date.weekday() >= SATURDAY},
        )

    def update_product(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        resume: bool = True,
    ) -> None:
        """單一商品的爬取 → 清洗 → 分批入庫；候選日期由 `plan_dates()` 決定"""

        progress: DateProgressStore = self.progress_store(product)
        dates: List[datetime.date] = self.plan_dates(
            product, start_date, end_date, progress, resume=resume
        )
        if not dates:
            logger.info(
                f"* {product} 已是最新（{start_date} ~ {end_date} 沒有待請求的日期）"
            )
            return

        # 表內最新日之後的才是「新日期」，沒拿到時商品要記為失敗；更早的是中間缺口，
        # 只是順便回補——沒有新日期可抓時（週末），補不到的舊缺口會讓 target 天天失敗
        latest: Optional[str] = self.dao.get_latest_date_by_product(product)
        latest_date: Optional[datetime.date] = (
            TimeUtils.to_date(latest) if latest else None
        )
        new_dates: Set[datetime.date] = {
            date for date in dates if latest_date is None or date > latest_date
        }
        logger.info(
            f"* {product}: {dates[0]} ~ {dates[-1]}，共 {len(dates)} 天"
            f"（新日期 {len(new_dates)} 天、缺口 {len(dates) - len(new_dates)} 天）"
        )

        throttle: RateLimitPolicy = self.build_rate_limit_policy()
        batch_dates: List[str] = []
        consecutive_empty: int = 0
        stats: UpdateStats = UpdateStats()
        # 擋流量時站方回 HTTP 200 ＋ 沒有行情表的頁面，與非交易日長得一樣；
        # 現貨有開市的日子期貨也有行情，以此分辨「沒開盤」與「沒拿到」。
        # 取不到現貨日曆時（只跑期貨的環境）退回只認明確的請求失敗
        stock_trading_days: Set[datetime.date] = set(
            self.get_stock_trading_days(dates[0], dates[-1]) or []
        )

        with GracefulStop(label=f"futures_price:{product}") as stop:
            for date in dates:
                crawled: Set[FuturesSession]
                unreachable: bool
                # 沒拿到日盤時一律等待後再試一次：站方擋流量多半是暫時的，
                # 第二次仍取不到才記為失敗（或兩個時段都查無資料，才是非交易日）。
                # **只拿到夜盤同樣要重試**：日盤尚未收盤時來源就是這個樣子。
                # 等待隨連續空產出遞增（base × 1、×2 … 至多 ×8）
                (crawled, unreachable), retries = retry_if_blocked(
                    fetch=functools.partial(self.crawl_and_clean_date, product, date),
                    is_blocked=lambda outcome: not self.is_day_complete(outcome[0]),
                    attempts=2,
                    delay_seconds=self.EMPTY_RETRY_DELAY_SECONDS
                    * min(consecutive_empty + 1, self.EMPTY_RETRY_MAX_BACKOFF_FACTOR),
                    label=f"Futures Price {product} {date}",
                    stop=stop,
                    sleep=lambda seconds: self.sleep(seconds, stop),
                )
                if retries and self.is_day_complete(crawled):
                    logger.warning(
                        f"{date} {product} 重試後取得資料——前一次為暫時性失敗（站方擋流量），"
                        f"不是非交易日"
                    )

                status: CrawlStatus = self.classify_day(
                    crawled, unreachable, date in stock_trading_days
                )
                # 休市日記下來之後不再問；沒拿到的記成待重試，下次一定再來
                progress.record(date, status)
                if date in new_dates:
                    stats.mark_day(date, status)

                if self.is_day_complete(crawled):
                    batch_dates.append(TimeUtils.format_date(date))
                    consecutive_empty = 0
                else:
                    # **只有夜盤時整天不入庫**：缺的日盤要再被請求——表內沒有這一天、
                    # 進度檔又記成待重試，下次執行一定會再來
                    if crawled:
                        logger.warning(
                            f"{date} {product} 只取得 "
                            f"{sorted(session.value for session in crawled)}、缺日盤，"
                            f"整天不入庫，下次執行會重試"
                        )
                    consecutive_empty += 1
                    if consecutive_empty >= self.EMPTY_PRODUCT_ABORT_THRESHOLD:
                        # 先把已爬到的入庫、進度存檔再中止，不浪費前面的成果
                        if batch_dates:
                            self.load_batch(batch_dates)
                        progress.save()
                        raise ValueError(
                            f"{product} 自 {dates[0]} 起連續 "
                            f"{consecutive_empty} 個候選日皆無資料，已中止。"
                            f"可能原因：① 代碼拼錯；② 該商品在此期間尚未上市"
                            f"（請調整 start_date）；③ 來源異常。"
                        )

                if len(batch_dates) >= self.LOAD_BATCH_SIZE:
                    self.load_batch(batch_dates)
                    batch_dates = []
                    # 與入庫同步落盤：中斷時已確認過的休市日不必再問一次
                    progress.save()

                if stop.requested:
                    logger.warning(
                        f"[{product}] 收到中止要求，停在 {date}；"
                        f"手上這批先入庫再離開，未爬的日期下次執行會接續"
                    )
                    break

                throttle.tick(stop)

        # 收尾：載入最後一批未達批量的日期，進度一併落盤
        if batch_dates:
            self.load_batch(batch_dates)
        progress.save()

        # 重試後仍為空就當成非交易日、不入庫也不報錯——站方整批回 HTTP 403 時
        # 每一天都會這樣，表停在舊資料卻列為成功。故最近一個應有資料的日子取不到時，
        # 讓這個商品失敗（`update()` 收齊所有商品後統一拋出）
        self.raise_if_unreachable(f"futures_price:{product}", stats)

    def classify_day(
        self,
        crawled: Set[FuturesSession],
        unreachable: bool,
        is_stock_trading_day: bool,
    ) -> CrawlStatus:
        """
        - Description:
            把一天（重試後）的結果歸成 `OK`／`NO_DATA`／`FAILED`

            - 拿到日盤 → `OK`。
            - 有時段請求失敗、清洗後為空，或**只拿到夜盤** → `FAILED`：日盤已收盤的
              過去日期缺日盤，表就停在前一天。
            - 兩個時段都沒有行情表，但現貨當天有開市 → `FAILED`：擋流量時站方回的
              正是這種頁面。
            - 其餘 → `NO_DATA`（非交易日）。
        - Parameters:
            - crawled: Set[FuturesSession]
                本日確實取得的時段
            - unreachable: bool
                是否有時段取不到
            - is_stock_trading_day: bool
                現貨當天是否開市
        - Return:
            - CrawlStatus
                這一天的結果
        """

        if self.is_day_complete(crawled):
            return CrawlStatus.OK
        if unreachable or crawled or is_stock_trading_day:
            return CrawlStatus.FAILED
        return CrawlStatus.NO_DATA

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
