import datetime
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import FUTURES_MARGIN_ANNOUNCEMENT_METADATA_PATH, TW_FUTURES_DB_PATH
from core.dao.connection import DBConnection
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.cleaners.futures_margin_cleaner import FuturesMarginCleaner
from core.pipeline.tw.crawlers.futures_margin_crawler import FuturesMarginCrawler
from core.pipeline.tw.loaders.futures_margin_loader import FuturesMarginLoader
from core.pipeline.utils.data_utils import DataUtils
from core.pipeline.utils.exceptions import DataLoadError
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
台期貨保證金 Updater

**兩支來源、三條入庫路徑**：

| 來源 | 內容 | 入庫 |
|------|------|------|
| 指數類一覽表 | 指數期貨的每口金額 | `futures_margin_history` |
| 股票類一覽表 一(一) | 股票股期的**適用比例** | `stock_futures_margin_rate_history` |
| 股票類一覽表 一(二) | **ETF 股期的每口金額** | `futures_margin_history`（與指數期貨同表） |

**分表的依據是「金額 vs 比例」，不是「指數 vs 股票」**——ETF 股期給的是每口固定
金額，語意與臺股期貨相同。

**各段的生效日不同**（2026-09-01 實查：指數類 08/12、股票股期 08/28、
ETF 股期 08/12），故三條路徑各自帶自己的 `effective_date`，不共用。

**一次請求就結束，沒有逐日／逐商品迴圈**：來源是「現行一覽表」，整份一次回傳。
因此本 updater 不需要節流，也沒有續跑起點的問題。

「重跑冪等」在這裡的實現方式與其他 updater 不同：不是靠比對日期範圍，
而是靠**主鍵 `(effective_date, product)` ＋ `INSERT OR IGNORE`**——
保證金沒變就沒有新的 `effective_date`，整批被忽略，表內列數不變。

歷史（2020/03 起的調整公告）走 `update_history()`，不在 `update()` 內——
全量回補要逐則開公告明細頁，約數百次請求，不適合放進日常更新。
"""


class AnnouncementMetadataStore:
    """
    保證金調整公告的處理紀錄：哪些公告已經處理過、不必再下載

    **以公告連結為鍵，不以生效日**：同一個生效日常有多則公告（指數類與股票類
    分開公告，2026-09-16 實查正式 DB 有 15 個生效日同時出現在金額表與比例表），
    以生效日判斷會把「同日的另一則」誤判成已入庫而整則漏掉。生效日要下載附件
    才知道，資料表也沒有記公告身分，所以另存這份紀錄。

    結構為 `{link: {"announcement_date", "csv_url", "status", "effective_date", "tables"}}`：
    - `status="loaded"`：附件有期貨列且已寫入；`effective_date` 為其生效日，
      `tables` 為實際寫入的表（金額表、比例表或兩者）
    - `status="no_futures_rows"`：附件下載成功但沒有期貨列（選擇權、部位限制公告）

    **下載失敗、沒有附件的公告不記**，下次照常重試。`csv_url` 存的是解析出的原始
    網址（未經共用網址排除），跳過解析時仍能參與「共用同一個網址」的判斷。
    """

    STATUS_LOADED: str = "loaded"
    STATUS_NO_FUTURES_ROWS: str = "no_futures_rows"

    def __init__(self, metadata_path: Path) -> None:
        self.metadata_path: Path = metadata_path
        self.records: Dict[str, Dict[str, Any]] = self.load()

    def load(self) -> Dict[str, Dict[str, Any]]:
        """讀取紀錄；檔案不存在時為空"""

        if not self.metadata_path.exists():
            return {}
        records: Optional[Dict[str, Dict[str, Any]]] = DataUtils.load_json(
            self.metadata_path
        )
        return records or {}

    def save(self) -> None:
        """寫回紀錄檔"""

        DataUtils.save_json(self.records, self.metadata_path)

    def record(
        self,
        link: str,
        announcement_date: datetime.date,
        csv_url: str,
        status: str,
        effective_date: Optional[str] = None,
        tables: Optional[List[str]] = None,
    ) -> None:
        """記下一則已處理的公告並立即寫檔（回補中途中斷時，已處理的不必重抓）"""

        self.records[link] = {
            "announcement_date": announcement_date.isoformat(),
            "csv_url": csv_url,
            "status": status,
            "effective_date": effective_date,
            "tables": tables or [],
        }
        self.save()

    def is_processed(self, link: str, loaded_dates: Dict[str, Set[str]]) -> bool:
        """
        - Description:
            該則公告是否已處理過、可以跳過

            `loaded` 的公告還要**生效日確實在它寫過的每一張表內**才算：紀錄檔與
            資料庫是兩份檔案，資料庫被還原或重建時紀錄會過時，照紀錄跳過就會讓表內
            永遠缺那一則。**逐表核對而不看兩表聯集**：同日常有另一則公告寫進另一張表，
            聯集會把「這張表的列不見了」誤判成還在。
        - Parameters:
            - link: str
                公告連結
            - loaded_dates: Dict[str, Set[str]]
                資料表名稱 → 該表 `source='announcement'` 的生效日
        - Return:
            - bool
                可以跳過時為 True
        """

        record: Optional[Dict[str, Any]] = self.records.get(link)
        if record is None:
            return False
        if record["status"] == self.STATUS_NO_FUTURES_ROWS:
            return True
        return bool(record["tables"]) and all(
            record["effective_date"] in loaded_dates.get(table, set())
            for table in record["tables"]
        )


class FuturesMarginUpdater(BaseDataUpdater):
    """Futures Margin Updater"""

    # 歷史回補的節流：每則公告要開明細頁 ＋ 下載附件（2 次請求）
    ANNOUNCEMENT_DELAY_SECONDS: float = 1.5

    # 2020/03 起的公告才附 CSV；更早的只有掃描 PDF（無文字層），取不到數值
    ANNOUNCEMENT_START_DATE: datetime.date = datetime.date(2020, 1, 1)

    def __init__(self) -> None:
        super().__init__()

        # 歷史回補已處理過的公告（`update_history()` 用來跳過）
        self.announcement_metadata_path: Path = (
            FUTURES_MARGIN_ANNOUNCEMENT_METADATA_PATH
        )

        # **讀（摘要、公告一致性、鏈式驗證）與寫（loader）共用同一個 DAO**（tw_futures.db）
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.dao: FuturesMarginDAO = FuturesMarginDAO(db_path=TW_FUTURES_DB_PATH)
        self.conn: Optional[DBConnection] = self.dao.conn

        # ETL
        self.crawler: FuturesMarginCrawler = FuturesMarginCrawler()
        self.cleaner: FuturesMarginCleaner = FuturesMarginCleaner()
        self.loader: FuturesMarginLoader = FuturesMarginLoader(dao=self.dao)

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("update_futures_margin.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一個 DAO，一併結束）"""

        self.dao.close()
        self.conn = None

    def update(self) -> None:
        """
        - Description:
            更新台期貨保證金（指數類 ＋ 股票類）

            **兩支來源各自獨立**：一支失敗不影響另一支——它們的生效日與更新頻率
            本來就不同，把它們綁在一起只會讓一邊的站方問題連累另一邊。
        """

        failures: List[str] = []

        for name, step in (
            ("index_margin", self.update_index_margin),
            ("stock_margin", self.update_stock_margin),
        ):
            try:
                step()
            except Exception as error:
                # 兩段互不相干，一段失敗不該讓另一段完全不跑；但跑完要一起拋出。
                # **`DataLoadError` 也要攔**：兩個 step 自己就是用它表達失敗的
                # （取得一覽表失敗、清洗結果為空），漏掉它等於這個迴圈白寫——
                # 最常見的失敗會直接往外炸，`update_stock_margin()` 根本不會跑
                logger.error(
                    f"[Futures Margin] {name} 失敗：{type(error).__name__}: {error}"
                )
                failures.append(name)

        self.log_summary()

        if failures:
            raise DataLoadError("futures_margin", failures, succeeded=2 - len(failures))

    def update_index_margin(self) -> None:
        """
        更新指數類保證金（每口金額）

        任一層回傳 None 即中止，**不做部分入庫**：一覽表是一組相互一致的數字，
        解析出問題時只入一半比整批不入更難察覺。
        """

        logger.info("* Start Updating TAIFEX Futures Margin (Index)")

        text: Optional[str] = self.crawler.crawl_index_margin()
        if text is None:
            # **「跳過」不能是靜默的**：保證金一覽表沒抓到就代表這次沒有任何
            # 新資料，若只記 warning，行程仍以成功結束，缺漏要事後對帳才會發現
            raise DataLoadError("futures_margin", ["index: 取得一覽表失敗"])

        cleaned_df: Optional[pd.DataFrame] = self.cleaner.clean_index_margin(text)
        if cleaned_df is None or cleaned_df.empty:
            raise DataLoadError("futures_margin", ["index: 清洗結果為空"])

        effective_date: str = str(cleaned_df["effective_date"].iloc[0])
        inserted: int = self.loader.add_to_db(cleaned_df)
        logger.info(
            f"* 指數類 生效日 {effective_date}："
            f"抓到 {len(cleaned_df)} 個商品、新增 {inserted} 列"
        )

    def update_stock_margin(self) -> None:
        """
        - Description:
            更新股票類保證金

            **一份 CSV 拆成兩條入庫路徑**：股票股期的比例進比例表、
            ETF 股期的金額進 `futures_margin_history`（與指數期貨同表）。
            兩段的生效日不同，故各自帶自己的日期。
        """

        logger.info("* Start Updating TAIFEX Futures Margin (Stock)")

        text: Optional[str] = self.crawler.crawl_stock_margin()
        if text is None:
            raise DataLoadError("futures_margin", ["stock: 取得一覽表失敗"])

        cleaned: Optional[Dict[str, Optional[pd.DataFrame]]] = (
            self.cleaner.clean_stock_margin(text)
        )
        if cleaned is None:
            raise DataLoadError("futures_margin", ["stock: 清洗結果為空"])

        rate_df: Optional[pd.DataFrame] = cleaned.get("rate")
        if rate_df is not None and not rate_df.empty:
            inserted: int = self.loader.add_rates_to_db(rate_df)
            logger.info(
                f"* 股票股期（比例）生效日 {rate_df['effective_date'].iloc[0]}："
                f"抓到 {len(rate_df)} 檔、新增 {inserted} 列"
            )

        amount_df: Optional[pd.DataFrame] = cleaned.get("amount")
        if amount_df is not None and not amount_df.empty:
            # ETF 股期給的是每口金額，與指數期貨同一張表
            inserted: int = self.loader.add_to_db(amount_df)
            logger.info(
                f"* ETF 股期（金額）生效日 {amount_df['effective_date'].iloc[0]}："
                f"抓到 {len(amount_df)} 檔、新增 {inserted} 列"
            )

    def log_summary(self) -> None:
        """
        更新後逐表回報現況，讓「有沒有真的補到」一眼可見

        **新增 0 列是正常狀態不是失敗**：保證金沒調整時本來就不會有新列。
        """

        for table, total, products, earliest, latest in self.dao.get_table_summaries():
            logger.info(
                f"* {table}：{total} 列、{products} 個商品，"
                f"生效日範圍 {earliest} ~ {latest}"
            )

    # === 歷史回補 ===
    def update_history(
        self,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        force: bool = False,
    ) -> None:
        """
        - Description:
            以調整公告回補歷史保證金

            **只補得到 2020/03 起**：更早的公告附件是掃描影像（無文字層），
            取不到數值。沒有 CSV 附件的公告會被跳過
            並在收尾統計，**不可當成「那天沒有調整」**。

            **已處理過的公告不再開明細頁、也不再下載附件**（見
            `AnnouncementMetadataStore`），重跑只抓新公告與上次失敗的公告。
        - Parameters:
            - start_date / end_date: Optional[datetime.date]
                查詢區間；未指定時為 2020-01-01 ~ 今天
            - force: bool
                忽略處理紀錄、全部重抓（站方更正附件內容時用）
        """

        start: datetime.date = start_date or self.ANNOUNCEMENT_START_DATE
        end: datetime.date = end_date or datetime.date.today()

        logger.info(f"* Start Backfilling Futures Margin History: {start} ~ {end}")

        announcements: List[Dict[str, str]] = self.crawler.crawl_announcements(
            start, end
        )
        if not announcements:
            logger.warning("[Futures Margin] 查無公告，本次中止")
            return

        # **必須限定 source**：snapshot 恰好同一天時會讓該則公告被整則跳過
        loaded_dates: Dict[str, Set[str]] = {
            table: self.loader.get_effective_dates(table, source="announcement")
            for table in (FuturesMarginDAO.TABLE_NAME, FuturesMarginDAO.RATE_TABLE_NAME)
        }
        store: AnnouncementMetadataStore = AnnouncementMetadataStore(
            self.announcement_metadata_path
        )
        processed_links: Set[str] = (
            set()
            if force
            else {
                announcement["link"]
                for announcement in announcements
                if store.is_processed(announcement["link"], loaded_dates)
            }
        )
        resolved: List[Tuple[Dict[str, str], Optional[str]]] = self.resolve_csv_urls(
            announcements,
            known_urls={
                link: store.records[link]["csv_url"] for link in processed_links
            },
        )
        stats: Dict[str, int] = {
            "loaded": 0,
            "loaded_rates": 0,
            "skipped_existing": 0,
            "download_failed": 0,
            "no_csv": 0,
            "no_futures_rows": 0,
            "chain_gaps": 0,
        }
        gap_products: set = set()

        for announcement, csv_url in resolved:
            announcement_date: datetime.date = TimeUtils.to_date(
                announcement["date"].replace("/", "-")
            )

            if announcement["link"] in processed_links:
                stats["skipped_existing"] += 1
                continue

            if csv_url is None:
                # 2015~2019 的掃描 PDF、以及新商品上市的 SPAN 參數公告
                stats["no_csv"] += 1
                continue

            text: Optional[str] = self.crawler.crawl_announcement_csv(csv_url)
            cleaned: Optional[Dict[str, Optional[pd.DataFrame]]] = (
                None
                if text is None
                else self.cleaner.clean_margin_announcement(
                    text, announcement["title"], announcement_date
                )
            )
            if text is None:
                # 下載失敗不記進處理紀錄，下次回補重試
                stats["download_failed"] += 1
                time.sleep(self.ANNOUNCEMENT_DELAY_SECONDS)
                continue
            if cleaned is None:
                # 選擇權或部位限制公告：有 CSV 但沒有期貨列，不是解析失敗
                stats["no_futures_rows"] += 1
                store.record(
                    announcement["link"],
                    announcement_date,
                    csv_url,
                    AnnouncementMetadataStore.STATUS_NO_FUTURES_ROWS,
                )
                time.sleep(self.ANNOUNCEMENT_DELAY_SECONDS)
                continue

            margin_df: Optional[pd.DataFrame] = cleaned.get("margin")
            rate_df: Optional[pd.DataFrame] = cleaned.get("rate")
            reference_df: pd.DataFrame = margin_df if margin_df is not None else rate_df
            effective_date: str = str(reference_df["effective_date"].iloc[0])

            if margin_df is not None:
                _, mismatches = self.check_announcement_consistency(margin_df)
                if mismatches:
                    # **只記缺口不拒收**：公告載明的「調整後」本來就是對的，
                    # 「調整前」對不上代表**我們的歷史有缺口**（某則公告的附件被
                    # 站方覆寫而取不到），不代表這一則的值有問題。
                    # 拒收只會讓缺口往後連鎖擴大——2026-09-01 實測，
                    # 一個缺口造成後續 46 則全被拒收，表內只剩 186 列。
                    #
                    # 真正會寫錯歷史的「附件被覆寫」已由 `resolve_csv_urls()` 的
                    # 網址去重**結構性擋掉**，不需要再用數值猜。
                    stats["chain_gaps"] += 1
                    gap_products.update(m.split(":")[0] for m in mismatches)
                    logger.warning(
                        f"* {announcement_date} → 生效日 {effective_date}："
                        f"{len(mismatches)} 個商品的「調整前」與表內不符，"
                        f"代表前面有缺口（仍入庫）。前三筆：{mismatches[:3]}"
                    )

                inserted: int = self.loader.add_announcements_to_db(
                    margin_df[self.cleaner.margin_cleaned_cols]
                )
                stats["loaded"] += inserted
                logger.info(
                    f"* {announcement_date} → 生效日 {effective_date}（金額）："
                    f"{len(margin_df)} 個商品、新增 {inserted} 列"
                )

            if rate_df is not None:
                inserted: int = self.loader.add_rates_to_db(
                    rate_df[self.cleaner.rate_cleaned_cols], replace=True
                )
                stats["loaded_rates"] += inserted
                logger.info(
                    f"* {announcement_date} → 生效日 {effective_date}（比例）："
                    f"{len(rate_df)} 檔、新增 {inserted} 列"
                )

            # 寫入後才記：loader 每次寫入即 commit，紀錄不會領先資料庫
            store.record(
                announcement["link"],
                announcement_date,
                csv_url,
                AnnouncementMetadataStore.STATUS_LOADED,
                effective_date,
                [
                    table
                    for table, df in (
                        (FuturesMarginDAO.TABLE_NAME, margin_df),
                        (FuturesMarginDAO.RATE_TABLE_NAME, rate_df),
                    )
                    if df is not None
                ],
            )
            time.sleep(self.ANNOUNCEMENT_DELAY_SECONDS)

        logger.info(
            f"📊 回補統計：金額新增 {stats['loaded']} 列、比例新增 "
            f"{stats['loaded_rates']} 列、已存在跳過 {stats['skipped_existing']} 則、"
            f"無 CSV 附件 {stats['no_csv']} 則、無期貨列 {stats['no_futures_rows']} 則、"
            f"附件下載失敗 {stats['download_failed']} 則"
        )
        if stats["chain_gaps"]:
            logger.warning(
                f"⚠️ 有 {stats['chain_gaps']} 則公告的「調整前」與表內不符，"
                f"代表這些商品的歷史有缺口（多為附件被站方覆寫而取不到）："
                f"{sorted(gap_products)[:15]}"
            )
        self.log_summary()

    def resolve_csv_urls(
        self,
        announcements: List[Dict[str, str]],
        known_urls: Optional[Dict[str, Optional[str]]] = None,
    ) -> List[Tuple[Dict[str, str], Optional[str]]]:
        """
        - Description:
            先把每則公告的附件網址解析出來，並處理「多則共用同一個網址」

            **部分附件用固定檔名**（`保證金調整情形列表.csv`），站方會覆寫它——
            2026-09-01 實測，2022/04/14 與 2026/03/31 共用同一個網址，
            今天下載拿到的是 2026 的內容。**同一個網址被多則引用時，
            只有最新那則的內容可信**，較早的一律視為沒有附件。

            這是結構性的判斷，不依賴數值大小，也不必事先列舉檔名。

            **已處理過的公告不重開明細頁，但仍參與共用網址的判斷**：若只解析新公告，
            舊公告與新公告共用網址時就看不出來，新公告會被當成專屬檔名。
        - Parameters:
            - announcements: List[Dict[str, str]]
                依日期排序的公告清單
            - known_urls: Optional[Dict[str, Optional[str]]]
                已知附件網址的公告（連結 → 網址），不再開明細頁
        - Return:
            - List[Tuple[Dict[str, str], Optional[str]]]
                每則公告與其可信的附件網址（不可信或沒有時為 None）
        """

        logger.info(f"* 解析 {len(announcements)} 則公告的附件網址…")

        known: Dict[str, Optional[str]] = known_urls or {}
        urls: List[Optional[str]] = []
        for announcement in announcements:
            if announcement["link"] in known:
                urls.append(known[announcement["link"]])
                continue
            urls.append(self.crawler.resolve_announcement_csv(announcement["link"]))
            time.sleep(self.ANNOUNCEMENT_DELAY_SECONDS)

        # 同一網址只保留最後（最新）一則
        latest_index: Dict[str, int] = {}
        for i, url in enumerate(urls):
            if url is not None:
                latest_index[url] = i

        shared: int = 0
        resolved: List[Tuple[Dict[str, str], Optional[str]]] = []
        for i, (announcement, url) in enumerate(zip(announcements, urls)):
            if url is not None and latest_index[url] != i:
                shared += 1
                logger.warning(
                    f"⚠️ {announcement['date']} 的附件與 "
                    f"{announcements[latest_index[url]]['date']} 共用同一個網址"
                    f"（{url.rsplit('/', 1)[-1]}），站方已覆寫，視為無附件"
                )
                url = None
            resolved.append((announcement, url))

        logger.info(
            f"* 附件網址解析完成：{sum(1 for _, u in resolved if u)} 則可用、"
            f"{shared} 則因共用網址被排除"
        )
        return resolved

    def get_margin_in_effect(self, product: str, effective_date: str) -> Optional[int]:
        """
        取得該商品在 `effective_date` **之前**最後生效的原始保證金

        **用 `<` 而非 `<=` 是刻意的**（`inclusive=False`）：要問的是「這次調整之前是多少」。
        比對發生在本次公告入庫之前，但表內仍可能已有同一生效日的列——現行一覽表
        （`source='snapshot'`）與公告同日生效，或重跑回補時本次公告前一輪已經寫入。
        `<=` 會拿到那一列的**調整後**數值，與公告的「調整前」必然不符，缺口判斷整個失真。
        回測與試算問的是「這一天適用多少」，才用 `<=`（見 `FuturesMarginAPI.get_margin()`）。
        """

        row: Optional[Tuple[Any, ...]] = self.dao.get_margin_in_effect(
            product, effective_date, inclusive=False
        )
        return None if row is None else row[2]

    def check_announcement_consistency(
        self, df: pd.DataFrame
    ) -> Tuple[bool, List[str]]:
        """
        - Description:
            入庫前驗證：公告載明的「調整前」須等於表內當時生效的值

            **回報缺口，不判斷可信度**：對不上代表**我們的歷史有缺口**
            （某則公告的附件被站方覆寫而取不到），不代表這一則的值有問題——
            公告載明的「調整後」本來就是權威。因此呼叫端只記錄不拒收。

            表內查無前值的商品（第一次出現）不參與比對。
        - Parameters:
            - df: pd.DataFrame
                cleaner 產出的公告 DataFrame（含「調整前」欄）
        - Return:
            - Tuple[bool, List[str]]
                （是否可信, 對不上的明細）
        """

        effective_date: str = str(df["effective_date"].iloc[0])
        comparable: int = 0
        mismatches: List[str] = []

        for row in df.itertuples(index=False):
            before = getattr(row, "調整前原始保證金", None)
            if before is None or pd.isna(before):
                continue
            current: Optional[int] = self.get_margin_in_effect(
                row.product, effective_date
            )
            if current is None:
                continue
            comparable += 1
            if int(before) != int(current):
                mismatches.append(
                    f"{row.product}: 表內 {current:,} vs 公告載明的調整前 {int(before):,}"
                )

        return not mismatches, mismatches

    def check_margin_chain(self, product: str) -> List[Tuple[str, int, int]]:
        """
        - Description:
            鏈式驗證：同一商品的相鄰兩次調整必須首尾相接

            公告附件同時給「調整前」與「調整後」，因此第 N 筆的調整前理應等於
            第 N−1 筆的調整後。對不上代表**中間漏了一次調整**——春節的調高／回調
            成對出現，漏一筆後面整段都會錯位，而且不會有任何執行期徵兆。

            **相鄰比對只用 `source='announcement'` 的列**（`snapshot` 沒有
            「調整前」可比，混進來會產生假的斷點）；但**最後一筆公告與現行一覽表
            之間會另外比一次**——漏抓最新那幾則公告時，只有這一段看得出來。
        - Parameters:
            - product: str
                契約代碼（Ex: TX）
        - Return:
            - List[Tuple[str, int, int]]
                斷點清單 `[(生效日, 前一筆的調整後, 本筆的調整前), ...]`；
                完全接得上時為空 list
        """

        rows: List[Tuple[str, int]] = self.dao.get_announcement_margins(product)

        breaks: List[Tuple[str, int, int]] = []
        for i in range(1, len(rows)):
            # 表內只存「調整後」，故以前一筆的值當作本筆理應的「調整前」；
            # 真正的比對值由回補當下的附件提供，此處驗的是序列的連續性
            previous_after: int = rows[i - 1][1]
            current_before: Optional[int] = self.get_previous_margin(
                product, rows[i][0]
            )
            if current_before is not None and current_before != previous_after:
                breaks.append((rows[i][0], previous_after, current_before))

        # **最後一筆公告必須接得上現行一覽表**——這一段是偵測「關鍵字漏抓」的關鍵。
        # 2026-09-01 實測：TAIFEX 自 2026/04/21 起把標題措辭由「保證金金額」改為
        # 「保證金」，只查前者會從那天起靜默漏掉每一次調整，而漏掉的部分**不會**
        # 在上面的相鄰比對中出現（沒有下一筆可比），只有與 snapshot 對照才看得出來。
        snapshot: Optional[Tuple[str, int]] = self.dao.get_latest_snapshot_margin(
            product
        )
        if rows and snapshot is not None and snapshot[0] > rows[-1][0]:
            if snapshot[1] != rows[-1][1]:
                breaks.append((snapshot[0], rows[-1][1], snapshot[1]))

        return breaks

    def get_previous_margin(self, product: str, effective_date: str) -> Optional[int]:
        """自中繼檔取回該次公告記載的「調整前原始保證金」；查不到時為 None"""

        csv_path = (
            self.cleaner.margin_dir
            / f"futures_margin_announcement_{effective_date.replace('-', '')}.csv"
        )
        if not csv_path.exists():
            return None

        df: pd.DataFrame = pd.read_csv(csv_path)
        matched = df[df["product"] == product]
        if matched.empty:
            return None

        value = matched["調整前原始保證金"].iloc[0]
        return None if pd.isna(value) else int(value)
