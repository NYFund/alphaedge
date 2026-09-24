import datetime
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.shared.graceful_stop import GracefulStop
from core.utils.log_manager import LogManager

"""
所有 updater 的共同基底，以及**每批一行的結果統計**

updater 原本跑完只會印「Latest available date: ...」，那句話在「今天什麼都沒抓到」
時長得跟正常一模一樣。`UpdateStats` 把一批更新拆成四個數字，讓「請求了幾天、
其中幾天真的沒資料、幾天是連不上」變成看得到的東西——連不上的那幾天下次會重試，
沒資料的那幾天不會，兩者混在一起就是資料靜靜缺一天的成因。
"""


@dataclass
class UpdateStats:
    """一批更新的結果統計"""

    requested: int = 0  # 送出請求的日期數
    ok: int = 0  # 拿到資料
    no_data: int = 0  # 站方明確回覆沒有資料（休市或尚未公布）
    unreachable: int = 0  # 取不到且無法斷定站方有沒有資料，下次會重試
    clean_failed: int = 0  # 抓到了但清洗失敗（版面異常），同樣下次會重試
    partial_no_data: int = 0  # 一邊查無資料、另一邊有資料，同樣下次會重試

    def record(self, *results: CrawlResult) -> CrawlStatus:
        """
        - Description:
            記錄同一天的多個來源結果，並回報這一天整體算哪一種

            同一天的上市與上櫃各爬一次，三種結果的判準：

            - 任一來源失敗 → `FAILED`。**即使另一邊成功也算失敗**，
              因為這天只拿到一半的資料，下次必須重來。
            - 全部來源都說沒有 → `NO_DATA`，可以記進永久名單。
            - 其餘 → `OK`。
        - Parameters:
            - results: CrawlResult
                同一天各來源的結果
        - Return:
            - CrawlStatus
                這一天的整體結果
        """

        self.requested += 1

        if any(result.is_failed for result in results):
            self.unreachable += 1
            return CrawlStatus.FAILED

        if all(result.is_no_data for result in results):
            self.no_data += 1
            return CrawlStatus.NO_DATA

        self.ok += 1
        return CrawlStatus.OK

    def count_clean_failure(self) -> None:
        """清洗失敗：從 ok 移到 unreachable，並單獨計數"""

        self.ok = max(self.ok - 1, 0)
        self.unreachable += 1
        self.clean_failed += 1

    def count_partial_no_data(self) -> None:
        """單一市場查無資料：從 ok 移到 unreachable，並單獨計數"""

        self.ok = max(self.ok - 1, 0)
        self.unreachable += 1
        self.partial_no_data += 1

    def summary_line(self, source: str) -> str:
        """單行統計字串"""

        line: str = (
            f"[{source}] 本批統計："
            f"{self.requested} requested / {self.ok} ok / "
            f"{self.no_data} no data / {self.unreachable} unreachable"
        )
        if self.clean_failed:
            line += f"（其中 {self.clean_failed} 天是清洗失敗）"
        if self.partial_no_data:
            line += f"（其中 {self.partial_no_data} 天是單一市場查無資料）"
        return line

    def report(self, source: str) -> None:
        """
        - Description:
            輸出統計行；有 unreachable 時提升為 warning

            用 `logger.info` 印出「12 天連不上」跟印出「一切正常」在 log 裡
            同樣不起眼，所以連不上的時候要換一個層級。
        - Parameters:
            - source: str
                資料來源名稱
        """

        line: str = self.summary_line(source)
        if self.unreachable:
            logger.warning(f"{line}；unreachable 的日期下次執行會自動重試")
        else:
            logger.info(line)


class BaseDataUpdater(ABC):
    """Base Class of Data Updater"""

    # 節流：每處理 N 個單位休息一次，其餘時候隨機短暫延遲。
    # 放在基底是因為每一支長跑 updater 都需要同一組數字；各自寫一份的話，
    # 調整節流要記得改好幾個檔案，而漏改的那支會在站方限流時先被擋下來
    BATCH_SLEEP_EVERY_N_FILES: int = 100
    BATCH_SLEEP_DURATION_SECONDS: int = 120
    BATCH_RANDOM_DELAY_MIN: int = 1
    BATCH_RANDOM_DELAY_MAX: int = 5

    def __init__(self) -> None:
        pass

    def throttle_per_file(
        self, file_cnt: int, stop: Optional[GracefulStop] = None
    ) -> int:
        """
        - Description:
            以**檔案／日期**為單位的節流，回傳更新後的計數

            **名字帶單位是刻意的**：權益變動表那條逐檔迴圈另有一份以「請求」
            為單位的節流（`throttle_per_request()`），兩者的判準不同
            （這裡達門檻後計數歸零，那裡取模、計數從不歸零）。
            都叫 `throttle` 的話子類會把基底這份遮蔽掉，而簽名不同，
            一旦有人呼叫基底那份就是參數綁錯位置。

            達到 `BATCH_SLEEP_EVERY_N_FILES` 就長睡一次並把計數歸零，
            其餘時候隨機短睡，避免固定間隔的請求樣態。

            **傳入 `stop` 時用的是可中斷的 sleep**：`time.sleep()` 被訊號打斷會
            自動續睡（PEP 475），於是「每 100 天睡 2 分鐘」那一段按下 Ctrl+C
            得等滿 2 分鐘才有反應。數小時～數十小時的回補按了沒反應，
            實際上就是逼人用 `kill -9`，而那會讓手上未入庫的那批直接消失。
        - Parameters:
            - file_cnt: int
                自上次長睡以來已處理的單位數
            - stop: Optional[GracefulStop]
                中止旗標；None 表示退化成不可中斷的 `time.sleep()`
        - Return:
            - int
                更新後的計數；剛長睡過為 0
        """

        if file_cnt >= self.BATCH_SLEEP_EVERY_N_FILES:
            # 訊息帶出實際秒數：原本三支寫死「Sleep 2 minutes」，而月營收那支的
            # 間隔其實是 30 秒，log 與行為對不上
            logger.info(f"Sleep {self.BATCH_SLEEP_DURATION_SECONDS} seconds...")
            self.sleep(self.BATCH_SLEEP_DURATION_SECONDS, stop)
            return 0

        delay: int = random.randint(
            self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
        )
        self.sleep(delay, stop)
        return file_cnt

    @staticmethod
    def sleep(seconds: float, stop: Optional[GracefulStop] = None) -> None:
        """節流用的 sleep；有 `stop` 時可被中止打斷，否則退化成 `time.sleep()`"""

        if stop is not None:
            stop.sleep(seconds)
            return

        # 沒有旗標可問時只能照睡；這條路徑保留給尚未接上 GracefulStop 的呼叫端
        time.sleep(seconds)

    @staticmethod
    def clean_one(
        clean: Callable[..., Optional[pd.DataFrame]],
        raw: pd.DataFrame,
        date: datetime.date,
        label: str,
    ) -> bool:
        """
        - Description:
            清洗單一來源的單日資料，**把失敗隔離在這一天之內**

            `BaseDataCleaner.check_column_count()` 會在版面不符時拋
            `ColumnLayoutError`。若讓它一路往上炸，一個異常的歷史日期就會中止
            整段回補——而且是在最壞的時間點：本批已爬好、尚未入庫的日期全部作廢
            （`load_batch()` 每 100 天才呼叫一次）。

            爬取層的失敗已經是逐日隔離的（見 `CrawlResult`），清洗層沒有理由不是。

            **回 `None`／空表同樣算失敗**：走到這裡代表 crawler 已判定站方有資料，
            清洗後卻一列都不剩，只可能是版面異常。若照常回 True，當天會被記為完成、
            只入庫另一個市場，之後差集判定表內已有這天，永遠不會回頭補。
        - Parameters:
            - clean: Callable
                cleaner 的清洗函式
            - raw: pd.DataFrame
                原始表格
            - date: datetime.date
                該日
            - label: str
                來源名稱，只用於訊息
        - Return:
            - bool
                清洗成功且有資料為 True；拋例外、回 None 或空表皆為 False
        """

        try:
            cleaned: Optional[pd.DataFrame] = clean(raw, date)
        except Exception as error:
            # **這裡的盲捕是刻意的隔離邊界**：`clean` 是各來源自己實作的清洗函式，
            # 拋得出什麼完全由那一側決定。收斂成具名例外等於要求每個清洗器
            # 都只能拋我們列得出來的那幾種——漏一種就會讓單日失敗升級成整批中止，
            # 而本方法的契約本來就是「拋例外一律計為失敗、下次重試」
            logger.error(
                f"[{label}] {date} 清洗失敗（{type(error).__name__}: {error}），"
                f"本日計為失敗、下次執行會重試"
            )
            return False

        if cleaned is None or cleaned.empty:
            logger.error(
                f"[{label}] {date} 清洗後無資料（站方有回應但無有效列），"
                f"本日計為失敗、下次執行會重試"
            )
            return False

        return True

    @staticmethod
    def record_market_day(
        stats: UpdateStats, twse: CrawlResult, tpex: CrawlResult
    ) -> CrawlStatus:
        """
        - Description:
            記錄上市＋上櫃拼成的一天，並回報這一天整體算哪一種

            在 `UpdateStats.record()` 之上多擋一種組合：一邊 `NO_DATA`、另一邊 `OK`。
            站方的「查無資料」涵蓋「尚未公布」，而兩個市場的公布時間不同——收盤後
            先公布的那一邊若照常入庫，當日就只有半個市場。故這種組合視為 `FAILED`，
            整天不入庫、下次重試。代價是若真有「一個市場開市、另一個休市」的日子，
            它會每輪被重試；那只是多幾次請求，半個市場入庫則不會有任何錯誤。

            不改 `UpdateStats.record()` 本身：除權息、減資等區間查詢也共用它，
            一個市場在整段區間內查無資料是正常的。
        - Parameters:
            - stats: UpdateStats
                本批統計
            - twse / tpex: CrawlResult
                兩個市場的爬取結果
        - Return:
            - CrawlStatus
                這一天的整體結果
        """

        day_status: CrawlStatus = stats.record(twse, tpex)
        if day_status is CrawlStatus.OK and not (twse.is_ok and tpex.is_ok):
            stats.count_partial_no_data()
            return CrawlStatus.FAILED
        return day_status

    @staticmethod
    def report_partial_day(
        source: str,
        date: datetime.date,
        twse: CrawlResult,
        tpex: CrawlResult,
    ) -> None:
        """
        - Description:
            記下「沒有完整取得」而整天不入庫的日期

            同一天由上市、上櫃兩份拼成，只入庫問到的那一邊的話，重試成功之前
            回測讀到的就是半個市場，而且不會有任何錯誤。故整天不入庫，並列出
            兩個市場各自的狀態，讓 log 看得出是哪一邊沒問到。
        - Parameters:
            - source: str
                資料來源名稱（例如 `"price"`）
            - date: datetime.date
                該日
            - twse / tpex: CrawlResult
                兩個市場的爬取結果；兩者皆 `OK` 代表是清洗失敗，
                一邊 `NO_DATA` 代表該市場休市或尚未公布
        """

        logger.warning(
            f"[{source}] {date} 未完整取得（TWSE: {twse.status.value}、"
            f"TPEX: {tpex.status.value}；兩者皆 ok 代表清洗失敗，"
            f"一邊 no_data 代表該市場休市或尚未公布），整天不入庫、下次執行會重試"
        )

    @staticmethod
    def report_cleaner_failures(dates: List[datetime.date]) -> None:
        """清洗失敗的日期列一次，讓「哪幾天要重跑」不必翻整份 log"""

        if not dates:
            return

        logger.error(
            f"* 有 {len(dates)} 天清洗失敗、已標記為待重試：{dates[:10]}"
            + ("…（僅列前 10 筆）" if len(dates) > 10 else "")
        )

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Updater"""
        pass

    @abstractmethod
    def update(self, *args, **kwargs) -> None:
        """Update the Database"""
        pass


class DailyTwoMarketUpdater(BaseDataUpdater):
    """
    - Description:
        「逐日爬上市＋上櫃、清洗、分批入庫」這條流程的骨架

        `price`／`chip`／`margin` 三支走的是同一條路：規劃日期 → 逐日雙市場爬 →
        清洗 → 記進度 → 每 N 天入庫一次 → 節流 → 收尾報表。原本三支各抄一份
        100 行的迴圈，`chip` 與 `margin` 正規化後 diff 只有 21 行差異，
        連 `load_batch()` 的註解都一字不差。

        **抄一份的代價不是行數而是漂移**：`price` 的「原始列數門檻」是後來才加的，
        另外兩支沒有；哪天在 `chip` 修了一個判斷，`margin` 不會跟著修，
        而兩邊的症狀都是「資料靜靜少一天」，不會有任何錯誤。

        子類要填的東西：`SOURCE`（同時決定進度檔名、log 訊息與 crawler／cleaner
        的方法名）、`SOURCE_LABEL`、`LOG_FILE_NAME`，以及 `plan_dates()` 這個 hook。

        **爬取與清洗一律以 `SOURCE` 組方法名取用**（`crawl_twse_{SOURCE}`、
        `clean_twse_{SOURCE}`），子類不必各自繫結。
    """

    # 資料來源代號；同時是進度檔名與 crawler／cleaner 的方法名後綴
    SOURCE: str = ""
    # log 訊息中的顯示名稱
    SOURCE_LABEL: str = ""
    # 落地的 log 檔名
    LOG_FILE_NAME: str = ""

    # 每爬幾天就入庫一次。整段爬完才入庫的話，中斷等於前功盡棄——
    # 2013 起的回補有 3,300 個交易日、數小時，中途失敗要全部重來。
    # 分批之後最多只損失最後一批（未入庫的部分），重跑會自動接續。
    LOAD_BATCH_SIZE: int = 100

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger(self.LOG_FILE_NAME)

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def load_batch(self, batch_dates: List[str]) -> None:
        """
        - Description:
            入庫本批爬取的日期

            **只載入本批的檔案**：loader 預設會掃整個 downloads 目錄，若每批都全掃，
            13 年的回補會變成「數十批 × 數千檔」的重複讀取。
        - Parameters:
            - batch_dates: List[str]
                本批的日期字串（`YYYYMMDD`），對應 downloads 內的檔名後綴
        """

        logger.info(
            f"* Loading batch: {len(batch_dates)} 天（{batch_dates[0]} ~ {batch_dates[-1]}）"
        )
        self.loader.add_to_db(remove_files=False, only_dates=set(batch_dates))

    @abstractmethod
    def plan_dates(
        self,
        progress: DateProgressStore,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        - Description:
            規劃本次要爬哪些日期

            **刻意留成抽象方法而不給預設**：三支的日曆來源不同（`chip`／`margin`
            以 `price` 表為日曆，`price` 自己就是日曆來源故只能以平日為母集合，
            另外從 `chip`／`margin` 把補行交易日補回來）。給一個「常見」的預設值，
            等於讓漏填的新來源靜靜用錯日曆——症狀是整天漏抓，且不會報錯。
        - Parameters:
            - progress: DateProgressStore
                本來源的進度檔（已確認無資料／上次沒跑完的日期）
            - start_date / end_date: datetime.date
                回補區間（含頭含尾）
        - Return:
            - List[datetime.date]
                本次待爬日期，已排序
        """

    def crawl_day(self, date: datetime.date) -> Tuple[CrawlResult, CrawlResult]:
        """爬取單日的上市與上櫃資料"""

        twse: CrawlResult = getattr(self.crawler, f"crawl_twse_{self.SOURCE}")(date)
        tpex: CrawlResult = getattr(self.crawler, f"crawl_tpex_{self.SOURCE}")(date)
        return twse, tpex

    def clean_day(
        self, date: datetime.date, twse: CrawlResult, tpex: CrawlResult
    ) -> bool:
        """
        - Description:
            清洗單日兩個市場的資料

            **任一市場沒問到（含一邊查無資料）時兩邊都不清洗**：這天反正不入庫，
            清洗只會在 downloads 留下半份 CSV。呼叫端已先判斷過整天的狀態，
            故本方法只在「兩邊都問到」時被呼叫。
        - Parameters:
            - date: datetime.date
                該日
            - twse / tpex: CrawlResult
                兩個市場的爬取結果
        - Return:
            - bool
                兩邊都清洗成功為 True
        """

        cleaned: bool = True
        for result, label in ((twse, "TWSE"), (tpex, "TPEX")):
            if not result.is_ok:
                continue
            clean: Callable[..., Optional[pd.DataFrame]] = getattr(
                self.cleaner, f"clean_{label.lower()}_{self.SOURCE}"
            )
            cleaned &= self.clean_one(clean, result.data, date, label)
        return cleaned

    def report_latest_date(self) -> None:
        """收尾印出表內最新日期；沒有任何資料時降級為 warning"""

        table_latest_date: Optional[str] = self.dao.get_latest_date()
        if table_latest_date:
            logger.info(
                f"Stock {self.SOURCE} data updated. "
                f"Latest available date: {table_latest_date}"
            )
        else:
            logger.warning(f"No new stock {self.SOURCE} data was updated")

    def update(
        self,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
    ) -> None:
        """
        - Description:
            逐日爬取兩個市場、清洗、分批入庫

            **候選日期是差集而不是 `MAX(date)+1`**：後者讓中間缺的日子永遠不會
            再被嘗試。日曆來源由 `plan_dates()` 決定。

            **只有兩個市場都問到的日子才入庫**：只入庫一邊的話，重試成功之前
            回測讀到的是半個市場，且不會有任何錯誤。清洗失敗同樣擋下——
            另一邊的 CSV 可能已經寫出。

            **中止訊號收在安全點**：按下 Ctrl+C 後先把手上這批入庫、進度存檔、
            印完統計行才離開，未入庫的那批不會憑空消失；要立刻中止再按一次。
        - Parameters:
            - start_date: datetime.date
                回補起日
            - end_date: Optional[datetime.date]
                回補迄日；None 取當日（**預設值不可寫在 def 行**，那是在 import
                時求值的，長時間執行的行程會一直用啟動那天的日期）
        """

        logger.info(f"* Start Updating TWSE & TPEX {self.SOURCE_LABEL} Data...")

        end_date: datetime.date = end_date or datetime.date.today()

        progress: DateProgressStore = DateProgressStore(self.SOURCE)
        dates: List[datetime.date] = self.plan_dates(progress, start_date, end_date)
        logger.info(f"本次待更新日期：{len(dates)} 天（{start_date} ~ {end_date}）")

        file_cnt: int = 0
        batch_dates: List[str] = []
        stats: UpdateStats = UpdateStats()
        cleaner_failures: List[datetime.date] = []

        with GracefulStop(label=self.SOURCE) as stop:
            for date in dates:
                logger.info(date.strftime("%Y/%m/%d"))
                twse, tpex = self.crawl_day(date)
                day_status: CrawlStatus = self.record_market_day(stats, twse, tpex)

                cleaned: bool = True
                if day_status is not CrawlStatus.FAILED:
                    cleaned = self.clean_day(date, twse, tpex)

                if not cleaned:
                    cleaner_failures.append(date)
                    day_status = CrawlStatus.FAILED
                    stats.count_clean_failure()

                progress.record(date, day_status)

                file_cnt += 1
                if day_status is CrawlStatus.FAILED:
                    self.report_partial_day(self.SOURCE, date, twse, tpex)
                else:
                    batch_dates.append(date.strftime("%Y%m%d"))

                if len(batch_dates) >= self.LOAD_BATCH_SIZE:
                    self.load_batch(batch_dates)
                    batch_dates = []
                    # 與入庫同步落盤：中斷時已確認過的休市日不必再問一次
                    progress.save()

                if stop.requested:
                    logger.warning(
                        f"[{self.SOURCE}] 收到中止要求，停在 {date}；"
                        f"手上這批先入庫再離開，未爬的日期下次執行會接續"
                    )
                    break

                file_cnt = self.throttle_per_file(file_cnt, stop)

        # 收尾：載入最後一批未達批量的日期
        if batch_dates:
            self.load_batch(batch_dates)

        progress.save()
        stats.report(self.SOURCE)
        self.report_cleaner_failures(cleaner_failures)
        self.report_latest_date()
