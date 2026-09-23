import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    BALANCE_SHEET_TABLE_NAME,
    CASH_FLOW_TABLE_NAME,
    COMPREHENSIVE_INCOME_TABLE_NAME,
    FINANCIAL_STATEMENT_DOWNLOADS_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.tw.cleaners.financial_statement_cleaner import (
    FinancialStatementCleaner,
)
from core.pipeline.tw.crawlers.financial_statement_crawler import (
    FinancialStatementCrawler,
)
from core.pipeline.tw.loaders.financial_statement_loader import FinancialStatementLoader
from core.pipeline.tw.updaters.financial_statement import (
    EquityChangeMixin,
)
from core.pipeline.utils import FinancialStatementType
from core.utils import TimeUtils
from core.utils.log_manager import LogManager

"""
* Crawl Balance Sheet (資產負債表)
資料區間（但是只有 102 年以後才可以爬）
上市: 民國 78 (1989) 年 ~ present
上櫃: 民國 82 (1993) 年 ~ present

* Crawl Statement of Comprehensive Income (綜合損益表)
資料區間（但是只有 102 年以後才可以爬）
上市: 民國 77 (1988) 年 ~ present
上櫃: 民國 82 (1993) 年 ~ present

* Crawl Cash Flow Statement (現金流量表)
資料區間
上市: 民國 102 (2013) 年 ~ present
上櫃: 民國 102 (2013) 年 ~ present

* Crawl Statement of Changes in Equity (權益變動表)
資料區間
上市: 民國 102 (2013) 年 ~ present
上櫃: 民國 102 (2013) 年 ~ present
"""

"""
財報申報期限（依行業類型區分）：

1. 一般行業：
   - Q1：5月15日
   - Q2：8月14日
   - Q3：11月14日
   - 年報：3月31日

2. 金控業：
   - Q1：5月30日
   - Q2：8月31日
   - Q3：11月29日
   - 年報：3月31日

3. 銀行及票券業：
   - Q1：5月15日
   - Q2：8月31日
   - Q3：11月14日
   - 年報：3月31日

4. 保險業：
   - Q1：5月15日
   - Q2：8月31日
   - Q3：11月14日
   - 年報：3月31日

5. 證券業：
   - Q1：5月15日
   - Q2：8月31日
   - Q3：11月14日
   - 年報：3月31日
"""


@dataclass
class StatementSpec:
    """
    一張「全市場一次查完」的財報的更新規格

    `label` 只用於 log 措辭。三張報表的流程完全相同，差異就只有這裡的五個欄位——
    改動前它們是三份各 60 行的複本，正規化後 diff 只剩 docstring 與 log 標籤。
    """

    table_name: str  # 目標資料表
    crawl: Callable[[int, int], Optional[List[pd.DataFrame]]]  # crawler 的爬取函式
    clean: Callable[..., Optional[pd.DataFrame]]  # cleaner 的清洗函式
    dir_path: Path  # downloads 底下的落地目錄
    label: str  # log 用的人話名稱，Title Case（Ex: "Balance Sheet"）

    @property
    def log_label(self) -> str:
        """`log_latest_year_season()` 用的句首大寫形式（Ex: "Balance sheet"）"""

        return self.label[:1] + self.label[1:].lower()

    @property
    def lowercase_label(self) -> str:
        """訊息內嵌用的全小寫形式（Ex: "balance sheet"）"""

        return self.label.lower()


class FinancialStatementUpdater(EquityChangeMixin, BaseDataUpdater):
    """Financial Statement Updater"""

    BATCH_SLEEP_EVERY_N_FILES: int = 10
    BATCH_SLEEP_DURATION_SECONDS: int = 30
    BATCH_RANDOM_DELAY_MIN: int = 1
    BATCH_RANDOM_DELAY_MAX: int = 5

    # 各季申報期限取「各行業中最晚」的那一天（見本檔開頭的申報期限表）：
    # 金控 Q1 是 5/30、Q3 是 11/29，Q2 各業別皆 8/31，年報一律隔年 3/31。
    # 值為 (跨年數, 月, 日)——年報的期限落在次年，故 Q4 的跨年數是 1。
    #
    # **四張表共用這一份**（名稱刻意不帶 equity_change）：申報期限是制度，
    # 不分報表；資產負債表、綜合損益表、現金流量表與權益變動表都以它判斷
    # 「這一季的資料算不算收齊了」
    FILING_DEADLINES: Dict[int, Tuple[int, int, int]] = {
        1: (0, 5, 30),
        2: (0, 8, 31),
        3: (0, 11, 29),
        4: (1, 3, 31),
    }
    # 申報期限之後再留的寬限天數：逾期申報、申請延期都會落在期限之後，
    # 而「把還沒送件的公司寫進永久無資料名單」的代價是它送件後再也不會被抓
    FILING_GRACE_DAYS: int = 30

    def __init__(self) -> None:
        super().__init__()

        # **讀（年季規劃、逐檔 resume）與寫（loader）共用同一條連線**：舊版 updater 與
        # loader 各開一條連線到同一個 DB，updater 那條從不關閉。四張表各有一個 DAO，
        # 故由 updater 持有連線、需要時以 `get_dao()` 就地建 DAO
        self.conn: Optional[DBConnection] = connect_sqlite(TW_STOCK_DB_PATH)

        # ETL
        self.crawler: FinancialStatementCrawler = FinancialStatementCrawler()
        self.cleaner: FinancialStatementCleaner = FinancialStatementCleaner()
        self.loader: FinancialStatementLoader = FinancialStatementLoader(conn=self.conn)

        # Data directories for each report
        self.fs_dir: Path = FINANCIAL_STATEMENT_DOWNLOADS_PATH
        self.balance_sheet_dir: Path = (
            self.fs_dir / FinancialStatementType.BALANCE_SHEET.lower()
        )
        self.comprehensive_income_dir: Path = (
            self.fs_dir / FinancialStatementType.COMPREHENSIVE_INCOME.lower()
        )
        self.cash_flow_dir: Path = (
            self.fs_dir / FinancialStatementType.CASH_FLOW.lower()
        )
        self.equity_change_dir: Path = (
            self.fs_dir / FinancialStatementType.EQUITY_CHANGE.lower()
        )

        # 權益變動表的爬取進度檔（哪些「年季 × 個股」確認沒資料／沒問到）；
        # None 表示用 SeasonProgressStore 的預設位置，測試會指到 tmp 目錄
        self.equity_change_progress_path: Optional[Path] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        # 設定 log 檔案儲存路徑
        LogManager.setup_logger("update_financial_statement.log")

    def close(self) -> None:
        """關閉資料連線（loader 共用同一條連線，一併結束）"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_dao(self, table_name: str) -> FinancialStatementDAO:
        """取得指定財報表的 DAO（共用本 updater 的連線）"""

        return FinancialStatementDAO(table_name, conn=self.conn)

    def log_latest_year_season(self, table_name: str, label: str) -> None:
        """
        - Description:
            入庫後回報表內最新的年季
        - Parameters:
            - table_name: str
                財報表名稱
            - label: str
                日誌用的報表名稱
        """

        latest: Optional[Tuple[int, int]] = self.get_dao(
            table_name
        ).get_latest_year_season()
        if latest is None:
            logger.warning(f"No {label} data in database after update")
            return

        logger.info(
            f"{label} data updated. Latest available date: {latest[0]}Q{latest[1]}"
        )

    def statement_specs(self) -> List[StatementSpec]:
        """
        三張全市場報表的規格；順序即更新順序

        **每次呼叫都重建**：`crawl`／`clean` 綁的是 `self.crawler`／`self.cleaner`
        的繫結方法，存成類別常數的話測試換掉 crawler 之後會綁到舊的那一個。
        """

        return [
            StatementSpec(
                table_name=BALANCE_SHEET_TABLE_NAME,
                crawl=self.crawler.crawl_balance_sheet,
                clean=self.cleaner.clean_balance_sheet,
                dir_path=self.balance_sheet_dir,
                label="Balance Sheet",
            ),
            StatementSpec(
                table_name=COMPREHENSIVE_INCOME_TABLE_NAME,
                crawl=self.crawler.crawl_comprehensive_income,
                clean=self.cleaner.clean_comprehensive_income,
                dir_path=self.comprehensive_income_dir,
                label="Comprehensive Income",
            ),
            StatementSpec(
                table_name=CASH_FLOW_TABLE_NAME,
                crawl=self.crawler.crawl_cash_flow,
                clean=self.cleaner.clean_cash_flow,
                dir_path=self.cash_flow_dir,
                label="Cash Flow",
            ),
        ]

    def update(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """Update the Database"""

        # 三張「全市場一次查完」的報表走同一份流程，差異全在 spec 裡
        for spec in self.statement_specs():
            self.update_statement(spec, start_year, end_year, start_season, end_season)

        # Update Equity Changes
        # 逐檔查詢，量級與前三張報表差三個數量級（一個年季約兩千次請求），
        # 故放在最後：即使這裡耗時或中斷，前三張報表已經入庫完成
        self.update_equity_changes(start_year, end_year, start_season, end_season)

    def update_statement(
        self,
        spec: StatementSpec,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """
        - Description:
            更新單一張「全市場一次查完」的財報

            資產負債表、綜合損益表、現金流量表三者的流程完全相同，改動前是三份
            各 60 行的複本，正規化後 diff 只剩 docstring 與 log 標籤。
            差異全部收進 `StatementSpec`，這裡只留一份流程。

            **權益變動表不走這裡**：它是逐檔查，量級差三個數量級，
            另有 `update_equity_changes()`。
        - Parameters:
            - spec: StatementSpec
                這張報表的資料表名、爬取／清洗函式、落地目錄與 log 標籤
            - start_year / end_year / start_season / end_season: int
                回補的年季區間
        """

        logger.info(f"* Start Updating {spec.label} Data...")

        # Step 1: Crawl
        # 候選年季是差集而不是 `MAX + 1`（理由見 `plan_pending_year_seasons()`）
        year_seasons: List[Tuple[int, int]] = self.plan_pending_year_seasons(
            table_name=spec.table_name,
            start_year=start_year,
            start_season=start_season,
            end_year=end_year,
            end_season=end_season,
        )
        file_cnt: int = 0

        for year, season in year_seasons:
            logger.info(f"* {year}Q{season}")
            df_list: Optional[List[pd.DataFrame]] = spec.crawl(year, season)

            # Step 2: Clean
            if df_list is None or not df_list:
                continue

            cleaned_df: pd.DataFrame = spec.clean(df_list, year, season)

            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned {spec.lowercase_label} dataframe empty on {year}Q{season}"
                )
                continue

            file_cnt = self.throttle_per_file(file_cnt + 1)

        # Step 3: Load
        self.loader.add_to_db(
            dir_path=spec.dir_path,
            table_name=spec.table_name,
            remove_files=False,
        )

        # 重新取得更新後的最新年度跟季度
        self.log_latest_year_season(spec.table_name, spec.log_label)

    @classmethod
    def is_season_settled(
        cls, year: int, season: int, today: Optional[datetime.date] = None
    ) -> bool:
        """
        - Description:
            該年季的申報期是否已關閉（含寬限期）

            **兩處用它，解的是同一個問題**：

            1. 逐檔的權益變動表：只有已關閉的年季，「查無資料」才能寫進永久名單。
               申報期間查某一檔沒有資料，多半只代表那家公司還沒送件；
               若這時就寫進永久名單，它送件之後**再也不會被抓到**。
            2. 逐季的三張報表（`plan_pending_year_seasons()`）：申報期內入庫的
               年季只有「已送件的那批公司」，不算收齊。當成收齊的話，年季差集
               從此跳過它，後送件的公司永遠補不進來。

            兩者都是「來源當下只有這麼多」與「來源真的只有這麼多」的區分，
            與逐日來源「當天不寫入 `no_data`」（`DateProgressStore.record_no_data()`）
            是同一道防線。

            期限取各行業中最晚的那一天再加寬限天數，寧可晚一個月才停止重問，
            也不要把已申報的公司誤鎖在名單裡。
        - Parameters:
            - year / season: int
                年季
            - today: Optional[datetime.date]
                今天；None 取系統日期（測試可覆寫）
        - Return:
            - bool
                申報期已關閉為 True
        """

        year_offset: int
        month: int
        day: int
        year_offset, month, day = cls.FILING_DEADLINES[season]
        deadline: datetime.date = datetime.date(
            year + year_offset, month, day
        ) + datetime.timedelta(days=cls.FILING_GRACE_DAYS)

        return (today or datetime.date.today()) > deadline

    def is_season_filed(
        self,
        year: int,
        season: int,
        stop: Optional[GracefulStop] = None,
    ) -> bool:
        """
        - Description:
            判斷該年季是否已申報，用來略過「還沒到申報期」的年季

            尚未申報的年季，每一檔都會是「查無資料」；不擋掉的話，每次日常更新都要
            為當季白打兩千次請求。

            **判斷依據是幾檔長期上市的權值股，不是「連續 N 檔查無資料」。**
            後者曾實際造成資料遺失：2026-08-22 的 2020Q1 回補跑到代號 6874 附近時，
            撞上一段「2020 年後才上市」的連續新股，被誤判成整季未申報而中止，
            **323 檔（含 9933 中鼎、9945 潤泰新等確定有資料的公司）從未被嘗試**。
            股票代號是排序過的，某個號段連續都是新股完全正常，拿它當全季的證據是錯的。
        - Parameters:
            - year / season: int
                要判斷的年季
            - stop: Optional[GracefulStop]
                中止旗標；試探期間收到訊號就不再打後續請求
        - Return:
            - bool
                是否已申報；暫時性失敗一律回 True（寧可多打請求，不可略過已申報的年季）
        """

        # 該年季已經有資料就是已申報的鐵證，連請求都不必打
        if self.get_crawled_stock_ids(year, season):
            return True

        for stock_id in self.EQUITY_CHANGE_PROBE_STOCK_IDS:
            if stop is not None and stop.requested:
                # 收工途中不必再判定：回 False 只是略過本季，重跑會重新試探
                return False

            df_list: Optional[List[pd.DataFrame]] = self.crawl_equity_changes_one(
                year, season, stock_id
            )

            # None 是站方過載或非預期例外，不是「沒有資料」，
            # 不能拿來證明整季未申報
            if df_list is None or df_list:
                return True

            self.throttle_per_request(stop)

        return False

    def plan_pending_year_seasons(
        self,
        table_name: str,
        start_year: int,
        start_season: int,
        end_year: int,
        end_season: int,
    ) -> List[Tuple[int, int]]:
        """
        - Description:
            算出該報表這次要請求的年季：區間內所有年季 − 表內**已收齊**的年季

            **不可用 `MAX(year, season) + 1` 起跑**：某一季失敗被跳過之後，只要下一季
            成功入庫，`MAX` 就越過它，那一季從此不會再被請求——資產負債表 2021Q1
            整季缺就是這樣來的。逐日來源的 `DatePlanner.plan()` 改用差集是同一個理由。

            代價是來源本就沒有的年季（2013Q1）與尚未公布的年季每輪都會再問一次，
            每張報表每季只是上市、上櫃各一次請求。
        - Parameters:
            - table_name: str
                目標資料表
            - start_year / start_season: int
                區間起點
            - end_year / end_season: int
                區間終點
        - Return:
            - List[Tuple[int, int]]
                由早到晚排序的 (year, season)
        """

        # **不可用 years × seasons 的笛卡兒積**：起點 2024Q3、終點 2026Q4 時
        # `seasons` 只會是 [3, 4]，2025Q1／Q2 與 2026Q1／Q2 整整四季不會被爬，
        # 且不會有任何錯誤——它們只是從來沒出現在迴圈裡
        year_seasons: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
            start_year, start_season, end_year, end_season, periods_per_year=4
        )
        existing: Set[Tuple[int, int]] = self.get_existing_year_seasons(table_name)

        # **申報期還沒關閉的年季不算完成**：財報是逐家公司申報的，申報期內入庫的
        # 只是「當下已送件的那批公司」。把它當成完成，年季差集從此跳過它，
        # 後送件的公司永遠補不進來——而每季申報期間跑一次日常更新就會踩到。
        # 重問的代價很小（每張報表每季只有上市、上櫃各一次請求），且寫入走
        # `INSERT OR IGNORE`，已入庫的列不會重複，只有新送件的公司會被加進來
        incomplete: Set[Tuple[int, int]] = {
            year_season
            for year_season in existing
            if not self.is_season_settled(*year_season)
        }
        if incomplete:
            logger.info(
                f"[{table_name}] {len(incomplete)} 個年季仍在申報期內"
                f"（{sorted(f'{y}Q{s}' for y, s in incomplete)}），"
                f"本次一併重問以補進後送件的公司"
            )

        settled: Set[Tuple[int, int]] = existing - incomplete
        pending: List[Tuple[int, int]] = [
            year_season for year_season in year_seasons if year_season not in settled
        ]

        # 只有夾在表內最早與最新之間的才算缺口：早於最早的是來源本就沒有的年季
        # （2013Q1），晚於最新的是新年季，兩者每輪都會出現，報出來只是噪音
        if existing:
            earliest: Tuple[int, int] = min(existing)
            latest: Tuple[int, int] = max(existing)
            gaps: List[str] = [
                f"{year}Q{season}"
                for year, season in pending
                if earliest < (year, season) < latest
            ]
            if gaps:
                logger.warning(
                    f"[{table_name}] 偵測到 {len(gaps)} 個年季缺口"
                    f"（表內最新為 {latest[0]}Q{latest[1]}），本次一併回補：{gaps[:10]}"
                )

        logger.info(f"[{table_name}] 本次待更新年季：{len(pending)} 季")
        return pending

    def get_existing_year_seasons(self, table_name: str) -> Set[Tuple[int, int]]:
        """表內已有的 (year, season)；表不存在時為空集合（初次更新的正常狀態）"""

        return self.get_dao(table_name).get_existing_year_seasons()
