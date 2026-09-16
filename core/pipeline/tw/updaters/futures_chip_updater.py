import datetime
import random
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.config import (
    FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
    FUTURES_LARGE_TRADER_TABLE_NAME,
    FUTURES_PUT_CALL_RATIO_TABLE_NAME,
    TW_FUTURES_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.futures_chip_cleaner import FuturesChipCleaner
from core.pipeline.tw.crawlers.futures_chip_crawler import FuturesChipCrawler
from core.pipeline.tw.loaders.futures_chip_loader import FuturesChipLoader
from core.pipeline.utils.exceptions import DataLoadError
from core.utils.log_manager import LogManager

"""
台期貨籌碼 Updater：三大法人、大額交易人、選擇權 PCR

**改用月批次的區間查詢**（2026-09-02）：三個端點都支援日期區間，一次請求可以拿
一整個月。逐日打的話 2015 年以來是 4,262 次請求，月批次只要 140 次——而這不只是
快慢的問題：

> **TAIFEX 擋流量時回的是 HTTP 200 ＋ 一整頁 HTML，與非交易日的回應一模一樣。**
> 第一次歷史回補就是這樣：逐日打了 4,000 多次之後被擋，2024-08 ~ 2025-10 約
> 250 個交易日**全部被記成「查無資料」**，事後單獨重查每一天都有資料。

故本 updater 做兩件事來確保「沒抓到」是真的沒有：

1. **月批次**，把請求數壓到三十分之一。
2. **交易日對照**：某批次沒拿到 CSV 時，用 `futures_price_daily` 的交易日確認
   那段期間是不是真的沒有交易日；**若有交易日就是被擋**，等待後重試，
   重試仍失敗才記 warning（不是安靜的 info）。

---

**三大法人的歷史只回溯到 2023-09-04**（2026-09-05 實測）：更早的日期無論換哪個
端點都拿不到，查詢頁甚至會**靜靜回傳最新一天**而不是報錯。故本 updater 會把該
資料集的起始日**夾到那一天之後**並說明原因，不浪費請求去撈拿不到的東西。

⚠️ **這個下限不要憑印象改。** 2026-09-02 曾實測出「切點 2024-08-17~19」而把夾擠
寫成兩年，2026-09-05 以「每次最多重試 8 回」重測才發現那是**網路不穩被誤讀成查無
資料**——TAIFEX 被擋與非交易日的回應都是 HTTP 200 ＋ 一頁 HTML，單次請求分不出
「站方沒有」與「這次沒問到」。實測結果：2023-08-28~09-01 無資料、2023-09-04 起
每一週都拿得到，切點落在這兩者之間。

⚠️ **籌碼是盤後公布**：當天盤中跑只會拿到「無資料」，那是正常狀態。
回測要用的本來就是前一交易日的籌碼（見 `FuturesChipAPI` 的前視偏差說明）。
"""


class FuturesChipUpdater(BaseDataUpdater):
    """更新 tw_futures.db 的三張籌碼表"""

    # 節流：月批次之後請求數只剩三十分之一，但仍要留間隔——
    # TAIFEX 擋流量時回的是 HTTP 200 ＋ 一頁 HTML，被擋了不會有任何錯誤
    MIN_DELAY_SECONDS: float = 2.0
    MAX_DELAY_SECONDS: float = 4.0

    # 疑似被擋時的重試（沿用 `futures_price_updater` 的作法：等待隨次數遞增）
    BLOCKED_RETRY_ATTEMPTS: int = 3
    BLOCKED_RETRY_DELAY_SECONDS: int = 30

    # 沒有指定起點時的預設回補起點：與行情一致（2015-01-01 起）
    DEFAULT_START_DATE: datetime.date = datetime.date(2015, 1, 1)

    # **三大法人拿得到的最早一天**（2026-09-05 實測，見模組說明）。
    #
    # 寫成固定日期而不是「今天往回推 N 天」：來源保留的長度看起來接近三年
    # （2023-09-04 距實測日 2026-09-05 正好三年多一天），但**單靠一次觀測分不出
    # 「固定起點」還是「滾動視窗」**。兩種寫法猜錯的代價不對稱——
    # 猜成滾動而實際是固定，夾擠點會逐年往後跑、把真的拿得到的資料擋在外面且無聲；
    # 猜成固定而實際是滾動，最多只是每次回補多問幾個已經滑出視窗的月份，
    # 而月批次一個月只有一次請求，且日常續跑本來就從表內最新日接續、根本走不到這裡。
    INSTITUTIONAL_EARLIEST_DATE: datetime.date = datetime.date(2023, 9, 4)

    def __init__(self) -> None:
        super().__init__()

        self.crawler: Optional[FuturesChipCrawler] = None
        self.cleaner: Optional[FuturesChipCleaner] = None
        self.loader: Optional[FuturesChipLoader] = None
        # 判斷「被擋」還是「真的沒資料」要靠交易日，來源是同庫的行情表
        self.price_api: Optional[FuturesPriceAPI] = None
        # 籌碼寫入與行情讀取同在 tw_futures.db，共用一條連線，由本 updater 關閉
        self.conn: Optional[DBConnection] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("futures_chip_updater.log")
        self.crawler = FuturesChipCrawler()
        self.cleaner = FuturesChipCleaner()
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
        self.loader = FuturesChipLoader(conn=self.conn)
        self.price_api = FuturesPriceAPI(conn=self.conn)

    def close(self) -> None:
        """關閉資料連線（loader 與行情 API 共用同一條，一併結束）"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_datasets(self) -> List[Tuple[str, str, Callable, Callable]]:
        """三個資料集的 (表名, 標籤, crawl, clean)"""

        return [
            (
                FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME,
                "institutional",
                self.crawler.crawl_institutional,
                self.cleaner.clean_institutional,
            ),
            (
                FUTURES_LARGE_TRADER_TABLE_NAME,
                "large_trader",
                self.crawler.crawl_large_trader,
                self.cleaner.clean_large_trader,
            ),
            (
                FUTURES_PUT_CALL_RATIO_TABLE_NAME,
                "pcr",
                self.crawler.crawl_put_call_ratio,
                self.cleaner.clean_put_call_ratio,
            ),
        ]

    def update(
        self,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        resume: bool = True,
        tables: Optional[List[str]] = None,
    ) -> None:
        """
        - Description:
            以**月批次**更新三個籌碼資料集

            **每個資料集各自續跑**：以自己表內的最新日為起點，避免已補到的那一張
            重爬一整段。`resume=False` 則一律使用傳入的 `start_date`（歷史回補用）。
        - Parameters:
            - start_date / end_date: Optional[datetime.date]
                回補區間；None 分別取 `DEFAULT_START_DATE` 與今天
            - resume: bool
                是否從各表的最新日接續
            - tables: Optional[List[str]]
                只更新這幾張表；None 表示三張全做。

                **這個開關是給歷史回補用的。** 三張表的涵蓋範圍差很多——
                `futures_large_trader` 與 `futures_put_call_ratio` 從 2015 就完整，
                `futures_institutional_chip` 因為來源只留三年而起點晚得多。
                要補後者就得 `resume=False` ＋ 指定 `start_date`，但那個組合會讓
                另外兩張已完整的表**整段重抓**（上百次請求、上百萬列 INSERT OR IGNORE）。
                指定 `tables` 才能只動要補的那一張。
        - Raise:
            - DataLoadError
                有月份「該有交易日卻沒拿到資料」時拋出，見下方說明
        """

        end: datetime.date = end_date or datetime.date.today()
        blocked: List[str] = []

        datasets: List[Tuple[str, str, Callable, Callable]] = [
            dataset
            for dataset in self.get_datasets()
            if tables is None or dataset[0] in tables
        ]
        if not datasets:
            logger.warning(f"[Futures Chip] `tables` 沒有對應到任何資料集：{tables}")
            return

        for table, label, crawl, clean in datasets:
            start: datetime.date = self.resolve_start_date(
                table, start_date, resume=resume
            )
            start = self.clamp_start_date(table, label, start)
            _, blocked_windows = self.update_dataset(
                table, label, crawl, clean, start, end
            )
            blocked.extend(
                f"{table} {window_start}~{window_end}"
                for window_start, window_end in blocked_windows
            )

        # **「該有資料卻沒拿到」必須讓行程非零結束**：TAIFEX 擋流量時回的是
        # HTTP 200 ＋ 一整頁 HTML，與非交易日的回應一模一樣。舊版只記 warning，
        # 於是被擋的月份會被當成「那幾個月沒有籌碼」而永遠不再補。
        if blocked:
            raise DataLoadError("futures_chip", blocked)

    def resolve_start_date(
        self,
        table: str,
        start_date: Optional[datetime.date],
        resume: bool,
    ) -> datetime.date:
        """決定該資料集的起點：續跑時取表內最新日的次日"""

        if not resume:
            return start_date or self.DEFAULT_START_DATE

        latest: Optional[str] = self.loader.get_latest_date(table)
        if latest is None:
            return start_date or self.DEFAULT_START_DATE

        return datetime.date.fromisoformat(latest) + datetime.timedelta(days=1)

    def clamp_start_date(
        self, table: str, label: str, start_date: datetime.date
    ) -> datetime.date:
        """
        把三大法人的起始日夾到來源真的給得出來的範圍內

        **不夾的話會白打上千次請求**（而且每一次都被記成「查無資料」，
        看起來像是那幾年真的沒有籌碼）。其餘兩個資料集有完整歷史，不受此限。
        """

        if table != FUTURES_INSTITUTIONAL_CHIP_TABLE_NAME:
            return start_date

        if start_date >= self.INSTITUTIONAL_EARLIEST_DATE:
            return start_date

        logger.warning(
            f"[Futures Chip] {label} 的來源最早只到 "
            f"{self.INSTITUTIONAL_EARLIEST_DATE}，起始日由 {start_date} 調整為該日"
            f"（更早的資料換哪個端點都拿不到）"
        )
        return self.INSTITUTIONAL_EARLIEST_DATE

    def update_dataset(
        self,
        table: str,
        label: str,
        crawl: Callable,
        clean: Callable,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Tuple[int, List[Tuple[datetime.date, datetime.date]]]:
        """
        - Description:
            逐月爬取、清洗並入庫單一資料集
        - Parameters:
            - table / label: str
                目標資料表與 log 用名稱
            - crawl / clean: Callable
                該資料集的爬取與清洗函式
            - start_date / end_date: datetime.date
                回補區間
        - Return:
            - Tuple[int, List[Tuple[datetime.date, datetime.date]]]
                （新增列數, 該有資料卻沒拿到的月份區間）
        """

        if start_date > end_date:
            logger.info(f"[Futures Chip] {table} 已是最新（{start_date} > {end_date}）")
            return 0, []

        logger.info(f"* Updating {table}: {start_date} ~ {end_date}（月批次）")

        inserted: int = 0
        blocked_windows: List[Tuple[datetime.date, datetime.date]] = []

        for window_start, window_end in self.split_months(start_date, end_date):
            raw: Optional[str] = self.crawl_window(
                crawl, label, window_start, window_end
            )
            if raw is None:
                if self.has_trading_days(window_start, window_end):
                    blocked_windows.append((window_start, window_end))
                self.throttle()
                continue

            df: Optional[pd.DataFrame] = clean(raw)
            if df is None or df.empty:
                self.throttle()
                continue

            self.loader.save_csv(df, f"{label}_{window_start.strftime('%Y%m')}.csv")
            inserted += self.loader.add_to_db(table, df)
            # 每個月批次寫完就落地：中斷時已入庫的月份不必重抓
            self.loader.commit()
            self.throttle()

        if blocked_windows:
            logger.error(
                f"[Futures Chip] {table} 有 {len(blocked_windows)} 個月份「該有資料卻沒拿到」，"
                f"多半是被擋流量，請稍後重跑：{blocked_windows[:5]}"
            )

        logger.info(f"[Futures Chip] {table}：本次新增 {inserted} 列")
        return inserted, blocked_windows

    def crawl_window(
        self,
        crawl: Callable,
        label: str,
        window_start: datetime.date,
        window_end: datetime.date,
    ) -> Optional[str]:
        """
        - Description:
            取得單一月份的資料；**疑似被擋就重試**

            判準是「這個月有沒有交易日」——有交易日卻拿不到 CSV，就不可能是
            「非交易日」，只會是被擋或來源異常。等待時間隨次數遞增，
            與 `futures_price_updater` 同一種作法。
        - Parameters:
            - crawl: Callable
                該資料集的爬取方法
            - label: str
                log 用的資料集名稱
            - window_start / window_end: datetime.date
                月批次區間
        - Return:
            - Optional[str]
                CSV 原文；重試後仍拿不到時為 None
        """

        raw: Optional[str] = crawl(window_start, window_end)
        if raw is not None or not self.has_trading_days(window_start, window_end):
            return raw

        for attempt in range(1, self.BLOCKED_RETRY_ATTEMPTS):
            wait: int = self.BLOCKED_RETRY_DELAY_SECONDS * attempt
            logger.warning(
                f"[Futures Chip] {label} {window_start}~{window_end} 該有交易日卻沒拿到 CSV，"
                f"{wait} 秒後重試（第 {attempt} 次）"
            )
            time.sleep(wait)

            raw = crawl(window_start, window_end)
            if raw is not None:
                return raw

        return None

    def has_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> bool:
        """
        該區間內是否有交易日（依 `futures_price_daily`）

        **這是「被擋」與「真的沒資料」的唯一判準**。行情表本身還沒建立時
        一律回 True（寧可多重試幾次，也不要把被擋當成沒資料）。

        舊版以 `except Exception: return True` 表達「表不存在」，連 `database is locked`
        也一起吞；改為只判斷表存不存在，其他錯誤往外拋。
        """

        if not self.price_api.dao.table_exists():
            return True
        return bool(self.price_api.get_trading_days(start_date, end_date))

    @staticmethod
    def split_months(
        start_date: datetime.date, end_date: datetime.date
    ) -> List[Tuple[datetime.date, datetime.date]]:
        """把區間切成逐月的 `(起, 迄)`；頭尾保留原始的起訖日"""

        windows: List[Tuple[datetime.date, datetime.date]] = []
        cursor: datetime.date = start_date

        while cursor <= end_date:
            if cursor.month == 12:
                next_month: datetime.date = datetime.date(cursor.year + 1, 1, 1)
            else:
                next_month = datetime.date(cursor.year, cursor.month + 1, 1)

            window_end: datetime.date = min(
                next_month - datetime.timedelta(days=1), end_date
            )
            windows.append((cursor, window_end))
            cursor = next_month

        return windows

    def throttle(self) -> None:
        """批次之間的間隔；月批次之後請求數已經很少，但仍不要連續打"""

        time.sleep(random.uniform(self.MIN_DELAY_SECONDS, self.MAX_DELAY_SECONDS))

    def get_coverage(self) -> Dict[str, Optional[str]]:
        """三張表各自補到哪一天（供人工確認進度）"""

        return {
            table: self.loader.get_latest_date(table)
            for table, _, _, _ in self.get_datasets()
        }
