import datetime
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from loguru import logger

from core.config import DOWNLOADS_METADATA_DIR_PATH
from core.dao.base import BaseDAO
from core.pipeline.shared.base_crawler import CrawlStatus

"""
「這次要向站方請求哪些日期」的共用決策

**舊做法是 `MAX(date) + 1`**，於是中間缺的日子永遠不會再被嘗試：
某天因為連線失敗沒抓到，隔天照樣從新的 `MAX(date)+1` 起跑，那個洞就留在資料庫裡，
而回測遇到缺日會當成休市靜默跳過。

改為**差集**：

    候選日期 ＝ 日曆 − 表內已有的日期 − 已確認沒有資料的日期 ＋ 上次沒跑完的日期

四個集合各自的來源：

| 集合 | 來源 | 為什麼 |
|------|------|--------|
| 日曆 | `calendar_dates`（例如 `price` 表的交易日）；沒有就用平日 | 週末不開市，送出請求只會換回「查無資料」 |
| 表內已有 | 目標資料表的 `DISTINCT date` | 已經有的不必重抓 |
| 已確認沒有 | `DateProgressStore.no_data` | 國定假日只需要問一次，之後不再浪費請求 |
| 上次沒跑完 | `DateProgressStore.incomplete` | **同一天有多個來源**，一邊成功一邊失敗時必須重來 |

最後一列是關鍵：price／chip／margin 每天都打**上市與上櫃兩次**請求。只要有一邊沒有完整
取得（連線失敗、清洗失敗、或只有一邊查無資料），updater 就整天不入庫（見
`BaseDataUpdater.record_market_day()`）——半個市場入庫不會有任何錯誤，回測只會安靜地少算。
但表內仍可能已有該日的部分資料（例如早期寫入的半份批次），這時差集會把它當成「已經有了」
而排除，缺的那半永遠補不回來。故失敗的日期要另外記下來，用它把「表內已有」的排除翻回去。

**只有站方明確回覆「查無資料」才會寫進 `no_data`**（見 `CrawlResult`）；
連線失敗、被擋、版面解析不出來都不會，所以那些日子下次還會再試。
這個分野就是整份工作的核心：**沒問到**與**問過了沒有**必須是兩件事。
"""


# 週六的 `weekday()` 值；用於判斷是否為週末
SATURDAY: int = 5


class DateProgressStore:
    """
    - Description:
        記錄每個日期「問到什麼程度」的持久化狀態

        兩個集合互斥：

        - `no_data`：**所有來源**都明確回覆沒有資料，之後不必再問。
        - `incomplete`：至少有一個來源沒問到（連線失敗、被擋、版面異常），
          下次必須重來，即使該日已有部分資料入庫。

        存成 JSON 而不是資料表，理由與 `BrokerTradingMetadataStore` 相同：
        這是執行期的爬取進度，不是資料本身，重建成本低且不該進版控。
        檔案不存在或損毀時一律當成空集合——最壞的結果只是多問幾次，
        比誤把「沒問到」當成「問過了沒有」安全得多。
    """

    def __init__(self, source: str, path: Optional[Path] = None) -> None:
        self.source: str = source
        self.path: Path = path or (
            DOWNLOADS_METADATA_DIR_PATH / "no_data" / f"{source}_date_progress.json"
        )
        self.no_data: Set[datetime.date] = set()
        self.incomplete: Set[datetime.date] = set()
        self.load()

    def load(self) -> None:
        """讀取進度檔；不存在或損毀時視為空"""

        if not self.path.exists():
            return

        try:
            payload: Dict[str, List[str]] = json.loads(
                self.path.read_text(encoding="utf-8")
            )
            self.no_data = {
                datetime.date.fromisoformat(value)
                for value in payload.get("no_data", [])
            }
            self.incomplete = {
                datetime.date.fromisoformat(value)
                for value in payload.get("incomplete", [])
            }
        except (OSError, ValueError, TypeError) as error:
            # 讀檔（OSError）、JSON 壞掉或日期格式不對（ValueError）、
            # 進度檔結構被改成別的形狀（TypeError）——三者都只該讓進度視為空，
            # 其餘例外代表這支函式自己寫錯了，不該被「最壞只是多問幾次」蓋掉
            logger.warning(
                f"[{self.source}] 讀取日期進度檔失敗（{type(error).__name__}: {error}），"
                f"視為空；最壞只是多問幾次"
            )
            self.no_data = set()
            self.incomplete = set()

    def record_no_data(
        self, date: datetime.date, today: Optional[datetime.date] = None
    ) -> None:
        """
        - Description:
            記下「所有來源都說沒有資料」的日期

            **當天（含未來）不寫入**：`CrawlResult.NO_DATA` 同時代表「休市」與
            「盤後尚未公布」，兩者在回應上無法區分。盤中跑一次更新就把今天寫進
            永久名單的話，收盤後那天的資料**再也不會被抓**——而這正是每晚排程
            之外、手動跑一次就會踩到的情境。
        - Parameters:
            - date: datetime.date
                確認沒有資料的日期
            - today: Optional[datetime.date]
                今天；None 取系統日期（測試可覆寫）
        """

        self.incomplete.discard(date)

        if date >= (today or datetime.date.today()):
            logger.debug(
                f"[{self.source}] {date} 尚未過完，「查無資料」可能只是還沒公布，不寫入永久名單"
            )
            return

        self.no_data.add(date)

    def record_incomplete(self, date: datetime.date) -> None:
        """記下「至少有一個來源沒問到」的日期，下次必須重來"""

        self.no_data.discard(date)
        self.incomplete.add(date)

    def record_complete(self, date: datetime.date) -> None:
        """記下「所有來源都問到了」的日期：清掉先前的重試標記"""

        self.no_data.discard(date)
        self.incomplete.discard(date)

    def record(
        self,
        date: datetime.date,
        status: CrawlStatus,
        today: Optional[datetime.date] = None,
    ) -> None:
        """
        - Description:
            依當日整體結果更新進度（`UpdateStats.record()` 的回傳值直接餵進來）
        - Parameters:
            - date: datetime.date
                該日
            - status: CrawlStatus
                當日整體結果
            - today: Optional[datetime.date]
                今天；None 取系統日期（測試可覆寫）
        """

        if status is CrawlStatus.FAILED:
            self.record_incomplete(date)
        elif status is CrawlStatus.NO_DATA:
            self.record_no_data(date, today=today)
        else:
            self.record_complete(date)

    def save(self) -> None:
        """把目前的狀態寫回檔案"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "no_data": sorted(date.isoformat() for date in self.no_data),
                    "incomplete": sorted(date.isoformat() for date in self.incomplete),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.debug(
            f"[{self.source}] 已記錄 {len(self.no_data)} 個確認無資料、"
            f"{len(self.incomplete)} 個待重試的日期"
        )


class DatePlanner:
    """決定一次更新要向站方請求哪些日期"""

    @staticmethod
    def get_existing_dates(
        dao: BaseDAO,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Set[datetime.date]:
        """
        - Description:
            取得目標資料表在區間內已有的日期
        - Parameters:
            - dao: BaseDAO
                目標資料表的 DAO
            - start_date / end_date: datetime.date
                查詢區間
        - Return:
            - Set[datetime.date]
                表不存在時為空集合（初次更新的正常狀態）
        """

        if not dao.table_exists():
            return set()

        return set(dao.get_distinct_dates(start_date, end_date))

    @staticmethod
    def get_trading_dates(
        dao: BaseDAO,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Set[datetime.date]:
        """
        - Description:
            以某張表（實務上是 `price`）在區間內有資料的日期作為交易日曆

            比「非週末」精確：涵蓋國定假日與**補行交易日**（補班的週六照常開市，
            2013 起有 11 天，用「非週末」近似會整天漏抓）。

            **日曆尾端會補上平日**（見 `plan()` 的 `calendar_dates` 說明）：
            `price` 通常比 chip／margin 早一步更新，若完全以它為準，
            最新的那幾天會等到下一次執行才被請求。
        - Parameters:
            - dao: BaseDAO
                作為日曆來源的資料表 DAO
            - start_date / end_date: datetime.date
                查詢區間
        - Return:
            - Set[datetime.date]
                有資料的日期；表不存在時為空集合
        """

        return DatePlanner.get_existing_dates(dao, start_date, end_date)

    @staticmethod
    def get_weekend_dates(
        daos: Iterable[BaseDAO],
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Set[datetime.date]:
        """
        - Description:
            取得幾張表在區間內有資料的週末日期（實務上就是補行交易日）

            給 `price` 當 `plan()` 的 `extra_dates`：它自己就是交易日曆，候選母集合
            只能是平日，被刪掉的補行交易日永遠不會再被請求。`chip`／`margin` 的日曆
            雖然也取自 `price`，但它們手上已有的補行交易日可以反過來補給 `price`。

            **已知限制**：尚未出現在任何一張表的新補行交易日仍然補不到（日曆尾端只補
            平日）。台股自 2019 年起已無補行交易日，實務影響低。
        - Parameters:
            - daos: Iterable[BaseDAO]
                作為來源的資料表 DAO；不存在的表視為沒有日期
            - start_date / end_date: datetime.date
                查詢區間
        - Return:
            - Set[datetime.date]
                區間內有資料的週六、週日
        """

        weekends: Set[datetime.date] = set()
        for dao in daos:
            weekends |= {
                date
                for date in DatePlanner.get_existing_dates(dao, start_date, end_date)
                if date.weekday() >= SATURDAY
            }
        return weekends

    @staticmethod
    def generate_weekdays(
        start_date: datetime.date, end_date: datetime.date
    ) -> Set[datetime.date]:
        """區間內的所有平日（週一到週五）"""

        days: Set[datetime.date] = set()
        date: datetime.date = start_date
        while date <= end_date:
            if date.weekday() < SATURDAY:
                days.add(date)
            date += datetime.timedelta(days=1)
        return days

    @staticmethod
    def extend_calendar_tail(
        calendar_dates: Set[datetime.date], end_date: datetime.date
    ) -> Set[datetime.date]:
        """
        - Description:
            把日曆尾端補到 `end_date`（以平日填補）

            **日曆來源（`price`）通常比使用它的目標（chip／margin）早一步更新**，
            而 `update_db` 的執行順序又不保證 price 先跑完。少了這一段，
            chip／margin 會**永遠落後 price 一天**：今天的日期不在日曆裡，
            於是今天不被請求，要等明天那一輪才補上。
        - Parameters:
            - calendar_dates: Set[datetime.date]
                日曆來源給出的交易日
            - end_date: datetime.date
                更新迄日
        - Return:
            - Set[datetime.date]
                補上尾端平日後的日曆
        """

        if not calendar_dates:
            return calendar_dates

        latest: datetime.date = max(calendar_dates)
        if latest >= end_date:
            return calendar_dates

        return calendar_dates | DatePlanner.generate_weekdays(
            latest + datetime.timedelta(days=1), end_date
        )

    @staticmethod
    def plan(
        dao: BaseDAO,
        start_date: datetime.date,
        end_date: datetime.date,
        no_data_dates: Optional[Iterable[datetime.date]] = None,
        incomplete_dates: Optional[Iterable[datetime.date]] = None,
        calendar_dates: Optional[Iterable[datetime.date]] = None,
        extra_dates: Optional[Iterable[datetime.date]] = None,
    ) -> List[datetime.date]:
        """
        - Description:
            算出這次要請求的日期清單（見模組說明的差集公式）
        - Parameters:
            - dao: BaseDAO
                目標資料表的 DAO
            - start_date / end_date: datetime.date
                更新區間
            - no_data_dates: Optional[Iterable[datetime.date]]
                已確認沒有資料的日期，會被排除
            - incomplete_dates: Optional[Iterable[datetime.date]]
                上次沒跑完的日期，**即使表內已有資料也要重來**
            - calendar_dates: Optional[Iterable[datetime.date]]
                交易日曆；None 表示改用「區間內所有平日」。
                非 None 時會先以 `extend_calendar_tail()` 補到 `end_date`
            - extra_dates: Optional[Iterable[datetime.date]]
                無論如何都要納入候選的日期
        - Return:
            - List[datetime.date]
                由早到晚排序的候選日期
        """

        if start_date > end_date:
            return []

        universe: Set[datetime.date] = (
            DatePlanner.extend_calendar_tail(set(calendar_dates), end_date)
            if calendar_dates is not None
            else DatePlanner.generate_weekdays(start_date, end_date)
        )
        if extra_dates:
            universe |= set(extra_dates)

        universe = {date for date in universe if start_date <= date <= end_date}

        existing: Set[datetime.date] = DatePlanner.get_existing_dates(
            dao, start_date, end_date
        )
        table_name: str = dao.TABLE_NAME
        retry: Set[datetime.date] = {
            date for date in (incomplete_dates or ()) if start_date <= date <= end_date
        }

        # `retry` 蓋過「表內已有」：同一天的多個來源只成功了一部分時，
        # 表內已有的那半不能把另一半擋掉
        candidates: Set[datetime.date] = (
            universe - existing - set(no_data_dates or ())
        ) | (universe & retry)

        if retry:
            logger.info(
                f"[{table_name}] 有 {len(retry)} 天上次沒跑完（部分來源失敗），本次重來："
                f"{sorted(retry)[:10]}"
                + ("…（僅列前 10 筆）" if len(retry) > 10 else "")
            )

        # 缺口與新日期分開報告：前者代表過去有一天沒補到，值得看一眼
        latest_existing: Optional[datetime.date] = max(existing) if existing else None
        gaps: List[datetime.date] = sorted(
            date
            for date in candidates
            if latest_existing is not None and date < latest_existing
        )
        if gaps:
            logger.warning(
                f"[{table_name}] 偵測到 {len(gaps)} 天缺口（表內最新為 {latest_existing}），"
                f"本次一併回補：{gaps[:10]}"
                + ("…（僅列前 10 筆）" if len(gaps) > 10 else "")
            )

        return sorted(candidates)
