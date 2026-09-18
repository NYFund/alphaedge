import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus

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

    def __init__(self) -> None:
        pass

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
