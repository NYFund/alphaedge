import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from loguru import logger

from core.config import DOWNLOADS_METADATA_DIR_PATH

"""
「這個年季還有哪些個股要向站方請求」的共用決策

`date_planner.py` 處理的是**逐日**來源（price／chip／margin），一次請求換回
全市場一天的資料；本檔處理的是**逐檔**來源——目前只有權益變動表
（MOPS `ajax_t164sb06` 一次只回一家公司一季），一個年季就要打兩千多次請求，
整段回補以十萬次計。兩者的差集公式相同，只是單位從「日期」換成「年季 × 個股」：

    候選 ＝ 目標清單 − 表內已有的個股 − 已確認沒有資料的個股 ＋ 上次沒問完的個股

四個集合各自的來源：

| 集合 | 來源 | 為什麼 |
|------|------|--------|
| 目標清單 | `taiwan_stock_info` 的上市櫃普通股 | 逐檔查詢沒有「全市場」端點，清單得自己給 |
| 表內已有 | `equity_change` 該年季的 `DISTINCT stock_id` | 已入庫的不必重打 |
| 已確認沒有 | `SeasonProgressStore.no_data` | **這是本檔存在的主因**，見下 |
| 上次沒問完 | `SeasonProgressStore.incomplete` | 站方過載造成的失敗要重試，且要看得見 |

**第三列是逐檔來源特有的坑。** 逐日來源的「查無資料」只有假日那幾天；逐檔來源
每個年季都有大量公司當時還沒上市——2020Q1 全市場 2,086 檔裡 343 檔查無資料，
佔 16%。這些公司在資料表裡不會留下任何列，於是「查無資料」與「還沒爬」在
資料庫層完全無法區分，**每次重跑都會重打那 343 次無效請求**。
而中斷重跑正是這條回補的常態（數十小時），代價因此會一再累積。

**`no_data` 只有在該年季的申報期已經關閉時才寫入**（由呼叫端以
`record_no_data(settled=...)` 決定，理由見該方法）。這與
`DateProgressStore.record_no_data()` 不寫入「今天」是同一道防線：
還沒申報完的年季，此刻的「查無資料」可能只是那家公司還沒送件。
"""


class SeasonProgressStore:
    """
    - Description:
        記錄每個「年季 × 個股」問到什麼程度的持久化狀態

        兩個集合互斥，語意與 `DateProgressStore` 一致：

        - `no_data`：站方明確回覆查無資料，**且該年季的申報期已關閉**，
          之後不必再問。
        - `incomplete`：沒問到（站方過載、連線失敗、版面解析不出來），
          下次必須重來。

        存成 JSON 而不是資料表，理由與 `DateProgressStore` 相同：這是執行期的
        爬取進度，不是資料本身，重建成本低且不該進版控。檔案不存在或損毀時
        一律當成空集合——最壞的結果只是多問幾次，比誤把「沒問到」當成
        「問過了沒有」安全得多。
    """

    def __init__(self, source: str, path: Optional[Path] = None) -> None:
        self.source: str = source
        self.path: Path = path or (
            DOWNLOADS_METADATA_DIR_PATH / "no_data" / f"{source}_season_progress.json"
        )
        # key 為 `period_key()` 產生的「2020Q1」字串，value 為股票代號集合
        self.no_data: Dict[str, Set[str]] = {}
        self.incomplete: Dict[str, Set[str]] = {}
        self.load()

    @staticmethod
    def period_key(year: int, season: int) -> str:
        """年季的字串鍵（JSON 的 key 只能是字串，且這個形式在 log 裡讀得懂）"""

        return f"{year}Q{season}"

    def load(self) -> None:
        """讀取進度檔；不存在或損毀時視為空"""

        if not self.path.exists():
            return

        try:
            payload: Dict[str, Dict[str, List[str]]] = json.loads(
                self.path.read_text(encoding="utf-8")
            )
            self.no_data = self._parse_section(payload.get("no_data", {}))
            self.incomplete = self._parse_section(payload.get("incomplete", {}))
        except (OSError, ValueError, TypeError) as error:
            # 與日期進度檔同一條規則：讀檔、JSON／年季格式、結構走樣三類視為空
            logger.warning(
                f"[{self.source}] 讀取年季進度檔失敗（{type(error).__name__}: {error}），"
                f"視為空；最壞只是多問幾次"
            )
            self.no_data = {}
            self.incomplete = {}

    def get_no_data(self, year: int, season: int) -> Set[str]:
        """該年季已確認沒有資料的個股"""

        return self.no_data.get(self.period_key(year, season), set())

    def get_incomplete(self, year: int, season: int) -> Set[str]:
        """該年季上次沒問完、下次要重試的個股"""

        return self.incomplete.get(self.period_key(year, season), set())

    def record_no_data(
        self, year: int, season: int, stock_id: str, settled: bool
    ) -> None:
        """
        - Description:
            記下「站方明確回覆查無資料」的個股

            **`settled` 為 False 時不寫入永久名單。** 財報是逐家公司申報的，
            申報期未關閉的年季，此刻的「查無資料」可能只是那家公司還沒送件；
            寫進永久名單的話，它送件之後**再也不會被抓**——而這正是每季申報期間
            跑一次日常更新就會踩到的情境。判斷申報期是否關閉屬於資料來源的領域
            知識，故由呼叫端傳入（見 `FinancialStatementUpdater.is_season_settled()`）。

            同一道防線在逐日來源是「當天（含未來）一律不寫入」
            （`DateProgressStore.record_no_data()`）。
        - Parameters:
            - year / season: int
                年季
            - stock_id: str
                查無資料的股票代號
            - settled: bool
                該年季的申報期是否已關閉；False 時只清掉重試標記，不寫入
        """

        key: str = self.period_key(year, season)
        self.incomplete.get(key, set()).discard(stock_id)

        if not settled:
            logger.debug(
                f"[{self.source}] {key} 申報期未關閉，{stock_id} 的「查無資料」"
                f"可能只是還沒送件，不寫入永久名單"
            )
            return

        self.no_data.setdefault(key, set()).add(stock_id)

    def record_incomplete(self, year: int, season: int, stock_id: str) -> None:
        """記下「沒問到」的個股，下次必須重來"""

        key: str = self.period_key(year, season)
        self.no_data.get(key, set()).discard(stock_id)
        self.incomplete.setdefault(key, set()).add(stock_id)

    def record_complete(self, year: int, season: int, stock_id: str) -> None:
        """
        - Description:
            記下「問到了」的個股：只清掉先前的標記，**不另存「已完成」集合**

            「哪些公司已經有資料」的唯一真相是資料表本身。若另存一份已完成名單，
            兩者就會有不一致的窗口：批次落地是每 100 檔一次，行程在
            「進度存檔了、CSV 還沒入庫」之間被強制中止時，那份名單會宣稱
            完成而資料庫裡沒有列，那些公司從此不會再被爬。
        - Parameters:
            - year / season: int
                年季
            - stock_id: str
                已取得資料的股票代號
        """

        key: str = self.period_key(year, season)
        self.no_data.get(key, set()).discard(stock_id)
        self.incomplete.get(key, set()).discard(stock_id)

    def save(self) -> None:
        """把目前的狀態寫回檔案"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "no_data": self._dump_section(self.no_data),
                    "incomplete": self._dump_section(self.incomplete),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.debug(
            f"[{self.source}] 已記錄 {self.count(self.no_data)} 筆確認無資料、"
            f"{self.count(self.incomplete)} 筆待重試的（年季 × 個股）"
        )

    @staticmethod
    def count(section: Dict[str, Set[str]]) -> int:
        """整份（跨年季）的筆數"""

        return sum(len(stock_ids) for stock_ids in section.values())

    @staticmethod
    def _parse_section(raw: Dict[str, List[str]]) -> Dict[str, Set[str]]:
        """把 JSON 的 `{年季: [代號]}` 轉成集合，並丟掉空年季"""

        return {
            str(key): {str(stock_id) for stock_id in stock_ids}
            for key, stock_ids in raw.items()
            if stock_ids
        }

    @staticmethod
    def _dump_section(section: Dict[str, Set[str]]) -> Dict[str, List[str]]:
        """轉回可序列化的 `{年季: [代號]}`；排序讓進度檔的 diff 讀得懂"""

        return {
            key: sorted(stock_ids)
            for key, stock_ids in sorted(section.items())
            if stock_ids
        }


class SeasonPlanner:
    """決定一個年季還要向站方請求哪些個股"""

    @staticmethod
    def plan(
        target_stock_ids: Iterable[str],
        crawled_stock_ids: Optional[Iterable[str]] = None,
        no_data_stock_ids: Optional[Iterable[str]] = None,
        retry_stock_ids: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """
        - Description:
            算出這次要請求的個股清單（見模組說明的差集公式）

            **回傳順序沿用 `target_stock_ids` 的順序**（實務上是代號排序）：
            順序穩定，中斷後重跑的 log 才能與前一次對照。
        - Parameters:
            - target_stock_ids: Iterable[str]
                目標清單
            - crawled_stock_ids: Optional[Iterable[str]]
                該年季已入庫的個股，會被排除
            - no_data_stock_ids: Optional[Iterable[str]]
                已確認沒有資料的個股，會被排除
            - retry_stock_ids: Optional[Iterable[str]]
                上次沒問完的個股，**即使被上面兩個集合排除也要重來**
        - Return:
            - List[str]
                依 `target_stock_ids` 順序排列的候選個股
        """

        crawled: Set[str] = set(crawled_stock_ids or ())
        no_data: Set[str] = set(no_data_stock_ids or ())
        retry: Set[str] = set(retry_stock_ids or ())

        # `retry` 蓋過另外兩個集合，理由與 `DatePlanner.plan()` 相同：
        # 沒問到就是沒問到，不能被「表內已有」或「確認沒有」擋掉
        return [
            stock_id
            for stock_id in target_stock_ids
            if stock_id in retry
            or (stock_id not in crawled and stock_id not in no_data)
        ]
