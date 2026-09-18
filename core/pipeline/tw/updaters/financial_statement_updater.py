import datetime
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    BALANCE_SHEET_TABLE_NAME,
    CASH_FLOW_TABLE_NAME,
    COMPREHENSIVE_INCOME_TABLE_NAME,
    EQUITY_CHANGE_TABLE_NAME,
    FINANCIAL_STATEMENT_DOWNLOADS_PATH,
    TW_STOCK_DB_PATH,
)
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.dao.tw.stock_info_dao import StockInfoDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.shared.season_planner import SeasonPlanner, SeasonProgressStore
from core.pipeline.tw.cleaners.financial_statement_cleaner import (
    FinancialStatementCleaner,
)
from core.pipeline.tw.crawlers.financial_statement_crawler import (
    FinancialStatementCrawler,
)
from core.pipeline.tw.loaders.financial_statement_loader import FinancialStatementLoader
from core.pipeline.utils import FinancialStatementType
from core.pipeline.utils.exceptions import DataLoadError
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
class EquityChangeSeasonStats:
    """
    - Description:
        單一年季逐檔爬取的結果統計

        欄位刻意與 `UpdateStats` 對齊（requested／ok／no_data／unreachable／
        clean_failed），但不共用那個類別：`UpdateStats.record()` 收的是同一天
        多個來源的 `CrawlResult`，而權益變動表是「一檔一次請求」，
        且 crawler 的回傳值還是舊的三態（`None`／`[]`／非空 list）。

        **這行統計是本步驟唯一的異常偵測手段**：2026-08-22 的 2020Q1 回補少抓了
        323 檔、行程仍以結束碼 0 結束，就是因為收尾沒有一行可對照的數字。
    """

    requested: int = 0  # 實際送出請求的檔數
    ok: int = 0  # 取得並清洗成功
    no_data: int = 0  # 站方明確回覆查無資料（多半是當時尚未上市）
    unreachable: int = 0  # 站方過載或連線失敗，**下次會重試**
    clean_failed: int = 0  # 清洗拋出例外（版面異常），同樣下次會重試
    cleaned_empty: int = 0  # 抓到報表卻清出空表，見 `report()`
    stopped: bool = False  # 是否因收到中止訊號而提早收工
    # 連續拋例外的檔數（非累計）；用於斷路器，見
    # `FinancialStatementUpdater.crawl_equity_changes_one()`
    consecutive_errors: int = 0

    def summary_line(self, year: int, season: int, pending: int) -> str:
        """單行統計字串"""

        line: str = (
            f"{year}Q{season} done: {self.requested} requested / {self.ok} ok / "
            f"{self.no_data} no data / {self.unreachable} unreachable / "
            f"{self.cleaned_empty} cleaned empty"
        )
        if self.clean_failed:
            line += f"（另有 {self.clean_failed} 檔清洗拋錯）"
        if self.stopped:
            line += f"；**依中止訊號收工**，本季待補 {pending - self.requested} 檔"
        return line

    def report(self, year: int, season: int, pending: int) -> None:
        """
        - Description:
            輸出統計行；任何一種「沒拿到資料」都提升為 warning

            **`cleaned_empty` 是這行最重要的數字。** 2026-09-03 的 2020Q2 全市場
            回補打了 2,087 次請求、跑 1.5 小時、入庫 0 列，而當時的統計行是
            `2087 requested, 244 no data, 0 unreachable`——三個數字全都正常，
            結束碼 0、沒有任何 ERROR。原因是統計只數「請求」層的結果，
            不數「清洗後真的產出資料的有幾檔」，於是 1,843 檔清成空表完全不顯示
            （根因是非 Q1 的期別標籤比對錯誤，見 cleaner 的
            `EQUITY_CHANGE_PERIOD_LABELS`）。
        - Parameters:
            - year / season: int
                年季
            - pending: int
                本季原本要爬的檔數，用於算出中止時還差幾檔
        """

        line: str = self.summary_line(year, season, pending)
        if self.unreachable or self.cleaned_empty or self.clean_failed or self.stopped:
            logger.warning(f"{line}；未取得資料的檔數下次執行會自動重試")
        else:
            logger.info(line)


class FinancialStatementUpdater(BaseDataUpdater):
    """Financial Statement Updater"""

    BATCH_SLEEP_EVERY_N_FILES: int = 10
    BATCH_SLEEP_DURATION_SECONDS: int = 30
    BATCH_RANDOM_DELAY_MIN: int = 1
    BATCH_RANDOM_DELAY_MAX: int = 5

    # 權益變動表專用（逐檔查詢，量級與其他三張報表差了三個數量級）
    EQUITY_CHANGE_LOAD_BATCH_SIZE: int = 100  # 每 100 檔入庫一次
    # 節流參數不與其他三張報表共用：那三張是「全市場一次查完」，整段回補也才幾十次
    # 請求，沒有放寬的必要；權益變動表一個年季就要兩千多次，節流直接決定回補要跑幾天。
    # 現行值為 2026-08-28 放寬後的設定（原為共用的 1~5 秒／每 10 檔睡 30 秒，約 6 秒/檔）：
    # 平均約 1.3 秒/檔，一個年季約 0.8 小時、56 個年季約 42 小時。
    # 放寬的依據是 2020Q1 全市場回補連續近 4 小時 unreachable = 0，代表原設定過於保守；
    # 但「放寬多少才會被擋」沒有實測過，若 log 尾端開始出現大量 unreachable 就調回來
    EQUITY_CHANGE_RANDOM_DELAY_MIN: float = 0.5
    EQUITY_CHANGE_RANDOM_DELAY_MAX: float = 1.5
    EQUITY_CHANGE_BATCH_SLEEP_EVERY_N_FILES: int = 50
    EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS: int = 15
    # 判斷「該年季是否已申報」用的試探標的：都是 2013 年前就上市的權值股，
    # 只要有任何一檔查得到，該年季就確定已申報（理由見 is_season_filed()）
    EQUITY_CHANGE_PROBE_STOCK_IDS: Tuple[str, ...] = ("2330", "2317", "1101")
    # 每 N 檔把爬取進度寫回磁碟。進度檔記的是「哪些公司確認沒有資料」，
    # 不存檔就等於下次重跑要重打那些無效請求（2020Q1 是 343 檔、佔 16%）；
    # 而中斷重跑是這條回補的常態，故不能只在收尾存一次
    EQUITY_CHANGE_PROGRESS_SAVE_EVERY_N_FILES: int = 100
    # 連續幾檔拋出非預期例外就中止整段回補。單一頁的例外要隔離（一頁的版面問題
    # 不該炸掉幾十小時的回補），但**連續**的例外不是「某一頁怪」，而是環境或程式
    # 壞了（2026-09-04 實際發生過：`pd.read_html` 因缺 html5lib 而 ImportError）。
    # 那種情況下繼續跑只會用 2 秒/檔的速度把整段回補變成一長串失敗。
    # 注意這與舊版「連續 N 檔查無資料就早退」不同：那裡拿「沒有資料」這種
    # 正常結果當統計樣本，這裡數的是例外——例外從來不是合法的業務結果
    EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS: int = 20
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

    def update(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """Update the Database"""

        # Update Balance Sheet
        self.update_balance_sheet(start_year, end_year, start_season, end_season)

        # Update Comprehensive Income
        self.update_comprehensive_income(start_year, end_year, start_season, end_season)

        # Update Cash Flow
        self.update_cash_flow(start_year, end_year, start_season, end_season)

        # Update Equity Changes
        # 逐檔查詢，量級與前三張報表差三個數量級（一個年季約兩千次請求），
        # 故放在最後：即使這裡耗時或中斷，前三張報表已經入庫完成
        self.update_equity_changes(start_year, end_year, start_season, end_season)

    def update_balance_sheet(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """Update Balance Sheet"""

        logger.info("* Start Updating Balance Sheet Data...")

        # Step 1: Crawl
        # 候選年季是差集而不是 `MAX + 1`（理由見 `plan_pending_year_seasons()`）
        year_seasons: List[Tuple[int, int]] = self.plan_pending_year_seasons(
            table_name=BALANCE_SHEET_TABLE_NAME,
            start_year=start_year,
            start_season=start_season,
            end_year=end_year,
            end_season=end_season,
        )
        file_cnt: int = 0

        for year, season in year_seasons:
            logger.info(f"* {year}Q{season}")
            df_list: Optional[List[pd.DataFrame]] = self.crawler.crawl_balance_sheet(
                year, season
            )

            # Step 2: Clean
            if df_list is None or not df_list:
                continue

            cleaned_df: pd.DataFrame = self.cleaner.clean_balance_sheet(
                df_list, year, season
            )

            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned balance sheet dataframe empty on {year}Q{season}"
                )
                continue

            file_cnt += 1
            if file_cnt == self.BATCH_SLEEP_EVERY_N_FILES:
                logger.info("Sleep 30 seconds...")
                file_cnt = 0
                time.sleep(self.BATCH_SLEEP_DURATION_SECONDS)
            else:
                delay: int = random.randint(
                    self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
                )
                time.sleep(delay)

        # Step 3: Load
        self.loader.add_to_db(
            dir_path=self.balance_sheet_dir,
            table_name=BALANCE_SHEET_TABLE_NAME,
            remove_files=False,
        )

        # 重新取得更新後的最新年度跟季度
        self.log_latest_year_season(BALANCE_SHEET_TABLE_NAME, "Balance sheet")

    def update_comprehensive_income(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """Update Comprehensive Income"""

        logger.info("* Start Updating Comprehensive Income Data...")

        # Step 1: Crawl
        # 候選年季是差集而不是 `MAX + 1`（理由見 `plan_pending_year_seasons()`）
        year_seasons: List[Tuple[int, int]] = self.plan_pending_year_seasons(
            table_name=COMPREHENSIVE_INCOME_TABLE_NAME,
            start_year=start_year,
            start_season=start_season,
            end_year=end_year,
            end_season=end_season,
        )
        file_cnt: int = 0

        for year, season in year_seasons:
            logger.info(f"* {year}Q{season}")
            df_list: Optional[List[pd.DataFrame]] = (
                self.crawler.crawl_comprehensive_income(year, season)
            )

            # Step 2: Clean
            if df_list is None or not df_list:
                continue

            cleaned_df: pd.DataFrame = self.cleaner.clean_comprehensive_income(
                df_list, year, season
            )

            if cleaned_df is None or cleaned_df.empty:
                logger.warning(
                    f"Cleaned comprehensive income dataframe empty on {year}Q{season}"
                )
                continue

            file_cnt += 1
            if file_cnt == self.BATCH_SLEEP_EVERY_N_FILES:
                logger.info("Sleep 30 seconds...")
                file_cnt = 0
                time.sleep(self.BATCH_SLEEP_DURATION_SECONDS)
            else:
                delay: int = random.randint(
                    self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
                )
                time.sleep(delay)

        # Step 3: Load
        self.loader.add_to_db(
            dir_path=self.comprehensive_income_dir,
            table_name=COMPREHENSIVE_INCOME_TABLE_NAME,
            remove_files=False,
        )

        # 重新取得更新後的最新年度跟季度
        self.log_latest_year_season(
            COMPREHENSIVE_INCOME_TABLE_NAME, "Comprehensive income"
        )

    def update_cash_flow(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
    ) -> None:
        """Update Cash Flow"""

        logger.info("* Start Updating Cash Flow Data...")

        # Step 1: Crawl
        # 候選年季是差集而不是 `MAX + 1`（理由見 `plan_pending_year_seasons()`）
        year_seasons: List[Tuple[int, int]] = self.plan_pending_year_seasons(
            table_name=CASH_FLOW_TABLE_NAME,
            start_year=start_year,
            start_season=start_season,
            end_year=end_year,
            end_season=end_season,
        )
        file_cnt: int = 0

        for year, season in year_seasons:
            logger.info(f"* {year}Q{season}")
            df_list: Optional[List[pd.DataFrame]] = self.crawler.crawl_cash_flow(
                year, season
            )

            # Step 2: Clean
            if df_list is None or not df_list:
                continue

            cleaned_df: pd.DataFrame = self.cleaner.clean_cash_flow(
                df_list, year, season
            )

            if cleaned_df is None or cleaned_df.empty:
                logger.warning(f"Cleaned cash flow dataframe empty on {year}Q{season}")
                continue

            file_cnt += 1
            if file_cnt == self.BATCH_SLEEP_EVERY_N_FILES:
                logger.info("Sleep 30 seconds...")
                file_cnt = 0
                time.sleep(self.BATCH_SLEEP_DURATION_SECONDS)
            else:
                delay: int = random.randint(
                    self.BATCH_RANDOM_DELAY_MIN, self.BATCH_RANDOM_DELAY_MAX
                )
                time.sleep(delay)

        # Step 3: Load
        self.loader.add_to_db(
            dir_path=self.cash_flow_dir,
            table_name=CASH_FLOW_TABLE_NAME,
            remove_files=False,
        )

        # 重新取得更新後的最新年度跟季度
        self.log_latest_year_season(CASH_FLOW_TABLE_NAME, "Cash flow")

    def update_equity_changes(
        self,
        start_year: int,
        end_year: int,
        start_season: int,
        end_season: int,
        stock_ids: Optional[List[str]] = None,
    ) -> None:
        """
        - Description:
            Update Equity Changes

            與其他三張報表不同，MOPS 的權益變動表端點是**逐檔查詢**，一個年季要打
            兩千多次請求、整段回補以十萬次計（數十小時）。因此這條流程的前提是
            **「隨時會被中斷」是常態，不是例外**，四道機制都為此而存在：

            | 機制 | 沒有它會發生什麼 |
            |------|------------------|
            | 逐檔 resume（差集） | 用「表內最新年季 +1」的話，爬到一半的年季會被當成已完成而整季跳過，缺的公司永遠補不回來 |
            | `no_data` 進度檔 | 「查無資料」的公司在表裡不留任何列，與「還沒爬」無法區分，每次重跑都重打（2020Q1 是 343 檔、16%） |
            | 收到訊號才收工（`GracefulStop`） | `KeyboardInterrupt` 從當下那行炸出去，記憶體裡那批（最多 100 檔）全部作廢 |
            | 磁碟 CSV 對帳 | 「CSV 已落地、尚未入庫」時被中止，那批的請求成本會白付一次 |
        - Parameters:
            - start_year / end_year / start_season / end_season: int
                更新區間
            - stock_ids: Optional[List[str]]
                要爬的股票清單；None 時取 `taiwan_stock_info` 的上市櫃股票
        - Raise:
            - DataLoadError
                有批次入庫失敗時，於整段跑完後拋出（中途不中斷）
        """

        logger.info("* Start Updating Equity Changes Data...")

        target_stock_ids: List[str] = (
            stock_ids if stock_ids is not None else self.get_target_stock_ids()
        )

        if not target_stock_ids:
            logger.warning("No target stocks for equity changes, skipped")
            return

        # **不可用 years × seasons 的笛卡兒積**：起點 2024Q3、終點 2026Q4 時
        # `seasons` 只會是 [3, 4]，2025Q1／Q2 與 2026Q1／Q2 整整四季不會被爬，
        # 且不會有任何錯誤——它們只是從來沒出現在迴圈裡
        year_seasons: List[Tuple[int, int]] = TimeUtils.generate_year_period_range(
            start_year, start_season, end_year, end_season, periods_per_year=4
        )

        progress: SeasonProgressStore = SeasonProgressStore(
            source=EQUITY_CHANGE_TABLE_NAME, path=self.equity_change_progress_path
        )
        failed_files: List[str] = []
        unreachable_cnt: int = 0
        stopped: bool = False

        with GracefulStop(label=EQUITY_CHANGE_TABLE_NAME) as stop:
            try:
                for year, season in year_seasons:
                    if stop.requested:
                        stopped = True
                        break

                    # Step 1: 逐檔 resume——只補這個年季還沒入庫的公司
                    pending_stock_ids: List[str] = self.plan_pending_stock_ids(
                        target_stock_ids=target_stock_ids,
                        progress=progress,
                        year=year,
                        season=season,
                    )

                    if not pending_stock_ids:
                        logger.info(f"* {year}Q{season} already complete, skipped")
                        continue

                    # Step 2: 有待補的公司才與磁碟對帳（整季已完成時掃全季 CSV 純屬浪費）
                    if self.reconcile_season_csv_files(year, season, failed_files):
                        pending_stock_ids = self.plan_pending_stock_ids(
                            target_stock_ids=target_stock_ids,
                            progress=progress,
                            year=year,
                            season=season,
                        )
                        if not pending_stock_ids:
                            logger.info(
                                f"* {year}Q{season} 由磁碟上未入庫的 CSV 補齊，"
                                f"不需要任何請求"
                            )
                            continue

                    if not self.is_season_filed(year, season, stop=stop):
                        # 收工途中的試探也會回 False，但那不是「尚未申報」——
                        # 印成 not yet filed 會讓下次重跑的人以為這季不用再問
                        if stop.requested:
                            stopped = True
                            break
                        logger.info(f"* {year}Q{season} not yet filed, skipped")
                        continue

                    retry_cnt: int = len(progress.get_incomplete(year, season))
                    logger.info(
                        f"* {year}Q{season}: {len(pending_stock_ids)} stocks pending"
                        + (
                            f"（其中 {retry_cnt} 檔是上次沒問到的）"
                            if retry_cnt
                            else ""
                        )
                    )

                    # Step 3: 逐檔爬取並分批入庫
                    stats: EquityChangeSeasonStats = self.update_equity_changes_season(
                        year=year,
                        season=season,
                        stock_ids=pending_stock_ids,
                        progress=progress,
                        stop=stop,
                        failed_files=failed_files,
                    )
                    unreachable_cnt += stats.unreachable

                    if stats.stopped:
                        stopped = True
                        break
            finally:
                # 中止或例外都要存進度：這一趟問出來的「哪些公司沒資料」
                # 不存檔就等於白問，下次重跑會再打一次
                progress.save()

        if stopped:
            logger.warning(
                "* Equity changes 已依中止訊號收工；直接重跑即可續跑"
                "（已入庫的公司與已確認沒資料的公司都不會重打）"
            )

        if unreachable_cnt:
            # 站方過載造成的失敗不是「這檔沒資料」，下次重跑會自動補；但不講出來，
            # 就會變成「回補跑完了卻莫名少了幾百檔」
            logger.warning(
                f"Equity changes: {unreachable_cnt} requests unreachable after retries, "
                f"rerun to fill them"
            )

        # 重新取得更新後的最新年度跟季度
        self.log_latest_year_season(EQUITY_CHANGE_TABLE_NAME, "Equity changes")

        # 入庫失敗**跑完才拋**：單一批次失敗不該中止其餘幾十小時的回補，
        # 但整段結束後必須讓行程非零結束，否則缺漏要靠事後對帳才會發現
        if failed_files:
            raise DataLoadError(EQUITY_CHANGE_TABLE_NAME, failed_files)

    def plan_pending_stock_ids(
        self,
        target_stock_ids: List[str],
        progress: SeasonProgressStore,
        year: int,
        season: int,
    ) -> List[str]:
        """該年季還要向站方請求的個股（差集公式見 `SeasonPlanner`）"""

        return SeasonPlanner.plan(
            target_stock_ids=target_stock_ids,
            crawled_stock_ids=self.get_crawled_stock_ids(year, season),
            no_data_stock_ids=progress.get_no_data(year, season),
            retry_stock_ids=progress.get_incomplete(year, season),
        )

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

            self.throttle(stop)

        return False

    def update_equity_changes_season(
        self,
        year: int,
        season: int,
        stock_ids: List[str],
        progress: SeasonProgressStore,
        stop: GracefulStop,
        failed_files: List[str],
    ) -> EquityChangeSeasonStats:
        """
        - Description:
            逐檔爬取單一年季的權益變動表並分批入庫
        - Parameters:
            - year / season: int
                年季
            - stock_ids: List[str]
                本次要爬的個股（已扣掉入庫過與確認沒資料的）
            - progress: SeasonProgressStore
                爬取進度，逐檔更新
            - stop: GracefulStop
                中止旗標；每一檔之間檢查一次
            - failed_files: List[str]
                入庫失敗的檔案會被附加進來，由呼叫端於整段結束後拋出
        - Return:
            - EquityChangeSeasonStats
                本季統計；`stopped` 為 True 代表是依中止訊號提早收工
        """

        stats: EquityChangeSeasonStats = EquityChangeSeasonStats()
        cleaned_df_list: List[pd.DataFrame] = []
        # 申報期是否已關閉只與年季有關，逐檔問一次是浪費
        settled: bool = self.is_season_settled(year, season)

        try:
            for stock_id in stock_ids:
                # Step 1: Crawl
                df_list: Optional[List[pd.DataFrame]] = self.crawl_equity_changes_one(
                    year, season, stock_id, stats
                )

                if df_list is None:
                    # 站方過載或連線失敗：**沒問到**，不是「沒有資料」
                    stats.unreachable += 1
                    progress.record_incomplete(year, season, stock_id)
                elif not df_list:
                    # 該年季這檔沒有報表（多半是當時尚未上市）；本迴圈**不做任何早退**，
                    # 見 update_equity_changes() 對「未申報年季」的處理
                    stats.no_data += 1
                    progress.record_no_data(year, season, stock_id, settled=settled)
                else:
                    # Step 2: Clean
                    cleaned_df: Optional[pd.DataFrame] = self.clean_equity_changes_one(
                        df_list, year, season, stock_id
                    )

                    if cleaned_df is None:
                        # 清洗拋出例外
                        stats.clean_failed += 1
                        progress.record_incomplete(year, season, stock_id)
                    elif cleaned_df.empty:
                        # 抓到報表卻清不出東西：版面與預期不符，**不是「沒有資料」**
                        stats.cleaned_empty += 1
                        progress.record_incomplete(year, season, stock_id)
                    else:
                        stats.ok += 1
                        cleaned_df_list.append(cleaned_df)
                        progress.record_complete(year, season, stock_id)

                stats.requested += 1

                # Step 3: Load（分批入庫，中斷最多只損失最後一批）
                if len(cleaned_df_list) >= self.EQUITY_CHANGE_LOAD_BATCH_SIZE:
                    self.load_equity_changes_batch(
                        cleaned_df_list, year, season, failed_files
                    )
                    cleaned_df_list = []
                    progress.save()
                elif (
                    stats.requested % self.EQUITY_CHANGE_PROGRESS_SAVE_EVERY_N_FILES
                    == 0
                ):
                    progress.save()

                # Step 4: 收工檢查放在節流之前——被中止時沒有理由再睡一輪
                if stop.requested:
                    stats.stopped = True
                    break

                self.throttle(stop, request_cnt=stats.requested)
        finally:
            # 收尾：不滿一批的資料一律先入庫。**這個 finally 是「隨時可中斷」的關鍵**——
            # 中止訊號、清洗以外的例外、第二次 Ctrl+C 的 KeyboardInterrupt 都會走到這裡，
            # 手上這批（最多 100 檔）的請求成本已經付出去了，不寫下來就得再爬一次
            if cleaned_df_list:
                self.load_equity_changes_batch(
                    cleaned_df_list, year, season, failed_files
                )

        stats.report(year, season, pending=len(stock_ids))

        return stats

    def throttle(
        self, stop: Optional[GracefulStop] = None, request_cnt: int = 0
    ) -> None:
        """
        - Description:
            權益變動表逐檔查詢之間的節流

            **不直接用 `time.sleep()`**：它被訊號打斷會自動續睡（PEP 475），
            於是「每 50 檔睡 15 秒」那一段按下 Ctrl+C 得等滿 15 秒才有反應。
        - Parameters:
            - stop: Optional[GracefulStop]
                中止旗標；None 時退化成不可打斷的 sleep
            - request_cnt: int
                目前已送出的請求數，決定是否輪到「每 N 檔多睡一段」
        """

        seconds: float = random.uniform(
            self.EQUITY_CHANGE_RANDOM_DELAY_MIN,
            self.EQUITY_CHANGE_RANDOM_DELAY_MAX,
        )

        if (
            request_cnt
            and request_cnt % self.EQUITY_CHANGE_BATCH_SLEEP_EVERY_N_FILES == 0
        ):
            seconds = float(self.EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS)
            logger.info(
                f"Sleep {self.EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS} seconds..."
            )

        if stop is None:
            time.sleep(seconds)
            return

        stop.sleep(seconds)

    def crawl_equity_changes_one(
        self,
        year: int,
        season: int,
        stock_id: str,
        stats: Optional["EquityChangeSeasonStats"] = None,
    ) -> Optional[List[pd.DataFrame]]:
        """
        - Description:
            爬取單一檔的權益變動表，**把非預期例外隔離在這一檔之內**

            crawler 內部只處理得了它預期的失敗（連線失敗、站方過載、解析不出表格）。
            2026-09-04 的整段回補實際炸在一個它沒預期的例外上——某一檔的頁面讓
            lxml 解不動，`pd.read_html` 回退到 bs4 flavor 時發現缺 html5lib 而拋
            `ImportError`，一路炸穿整個回補。缺的套件已補上，但**「一頁的問題不該
            炸掉幾十小時的回補」是結構問題**，跟那個套件無關。

            隔離的代價是「環境壞了」會退化成一長串失敗，故加了連續例外的斷路器
            （`EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS`）：連續而非零星的例外不是
            某一頁怪，是環境或程式壞了，該停下來讓人看。
        - Parameters:
            - year / season: int
                年季
            - stock_id: str
                股票代號
            - stats: Optional[EquityChangeSeasonStats]
                本季統計；用來累計連續例外數，None 時不啟用斷路器（試探用）
        - Return:
            - Optional[List[pd.DataFrame]]
                crawler 的三態回傳值；非預期例外一律回 None（＝待重試）
        - Raise:
            - Exception
                連續 `EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS` 檔都拋例外時原樣往上拋
        """

        try:
            df_list: Optional[List[pd.DataFrame]] = self.crawler.crawl_equity_changes(
                year, season, stock_id
            )
        except Exception as error:
            if stats is not None:
                stats.consecutive_errors += 1
                if (
                    stats.consecutive_errors
                    >= self.EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS
                ):
                    logger.error(
                        f"Equity changes 連續 {stats.consecutive_errors} 檔爬取拋例外"
                        f"（最後一檔 {stock_id} {year}Q{season}），"
                        f"這不是單一頁面的問題，中止回補"
                    )
                    raise
            logger.error(
                f"Failed to crawl equity changes on {stock_id} {year}Q{season}"
                f"（{type(error).__name__}: {error}），本檔計為待重試"
            )
            return None

        if stats is not None:
            stats.consecutive_errors = 0

        return df_list

    def clean_equity_changes_one(
        self,
        df_list: List[pd.DataFrame],
        year: int,
        season: int,
        stock_id: str,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗單一檔的權益變動表，**把失敗隔離在這一檔之內**

            一頁的版面異常不該中止整段十萬次請求的回補——而在有這道隔離之前，
            cleaner 拋出的例外會一路炸到 `update()`，連手上那批已爬好的
            資料都跟著作廢。理由與 `BaseDataUpdater.clean_one()` 相同。
        - Parameters:
            - df_list: List[pd.DataFrame]
                crawler 取回的整頁表格
            - year / season: int
                年季
            - stock_id: str
                股票代號
        - Return:
            - Optional[pd.DataFrame]
                清洗結果；**None 代表清洗拋出例外**。回傳空表代表比對不到本期表
                （cleaner 刻意不做 fallback，否則會把去年同季的數字記成今年），
                那是版面問題、不是「沒有資料」，由呼叫端計入 `cleaned_empty`
        """

        try:
            cleaned_df: pd.DataFrame = self.cleaner.clean_equity_changes(
                df_list, year, season, stock_id
            )
        except Exception as error:
            logger.error(
                f"Failed to clean equity changes on {stock_id} {year}Q{season}"
                f"（{type(error).__name__}: {error}），本檔計為待重試"
            )
            return None

        if cleaned_df is None:
            return pd.DataFrame()

        if cleaned_df.empty:
            logger.warning(
                f"Cleaned equity changes dataframe empty on {stock_id} {year}Q{season}"
            )

        return cleaned_df

    def load_equity_changes_batch(
        self,
        cleaned_df_list: List[pd.DataFrame],
        year: int,
        season: int,
        failed_files: List[str],
    ) -> None:
        """把一批已清洗的權益變動表落地成 CSV 並入庫"""

        # 批次序號由 cleaner 依目錄現況決定，不在這裡累加——同一年季跑第二次時
        # 從 0 重數會蓋掉前一次的檔案（見 next_equity_changes_batch_index()）
        file_path: Optional[Path] = self.cleaner.save_equity_changes(
            df_list=cleaned_df_list,
            year=year,
            season=season,
        )

        if file_path is None:
            return

        failed_files.extend(self.load_equity_changes_files([file_path]))

    def reconcile_season_csv_files(
        self,
        year: int,
        season: int,
        failed_files: List[str],
    ) -> bool:
        """
        - Description:
            把該年季**已落地但可能沒入庫**的 CSV 補進資料庫，回傳是否有檔案可對帳

            落地與入庫是兩個步驟（`save_equity_changes()` → `add_to_db()`），
            行程若剛好在兩者之間被中止，那批 CSV 就只存在於磁碟上。而 resume 的
            依據是資料表，於是那 100 檔會被當成「還沒爬」再打一次——已經付過的
            請求成本白付。`INSERT OR IGNORE` 讓重複入庫是安全的，
            所以這裡寧可多掃一次目錄。

            **只在該年季還有待補公司時才呼叫**：整季已完成時掃全季 CSV
            （2020Q1 有 16 個檔、23 萬列）純屬浪費。
        - Parameters:
            - year / season: int
                年季
            - failed_files: List[str]
                入庫失敗的檔案會被附加進來
        - Return:
            - bool
                目錄裡有該年季的 CSV 為 True（呼叫端據此決定要不要重算待補清單）
        """

        season_files: List[Path] = sorted(
            self.equity_change_dir.glob(f"equity_change_{year}Q{season}_*.csv")
        )

        if not season_files:
            return False

        logger.info(
            f"* {year}Q{season}: 與磁碟對帳 {len(season_files)} 個已落地的 CSV"
            f"（重複列由 INSERT OR IGNORE 跳過）"
        )
        failed_files.extend(self.load_equity_changes_files(season_files))

        return True

    def load_equity_changes_files(self, files: List[Path]) -> List[str]:
        """
        - Description:
            入庫指定的權益變動表 CSV，回傳失敗的檔案清單

            **入庫失敗不在這裡拋出**：`finish_load()` 會拋 `DataLoadError`，
            若讓它一路往上炸，一個壞批次就會中止剩下幾十小時的回補。
            改為收集起來，由 `update_equity_changes()` 於整段結束後一次拋出——
            「單檔失敗不中止整批，但跑完必須讓失敗浮出來」。
        - Parameters:
            - files: List[Path]
                要入庫的 CSV
        - Return:
            - List[str]
                入庫失敗的檔案路徑
        """

        try:
            self.loader.add_to_db(
                dir_path=self.equity_change_dir,
                table_name=EQUITY_CHANGE_TABLE_NAME,
                remove_files=False,
                only_files=files,
            )
        except DataLoadError as error:
            logger.error(
                f"Equity changes 批次入庫失敗（{len(error.failed_files)} 檔），"
                f"其餘年季照常繼續：{error.failed_files[:5]}"
            )
            return list(error.failed_files)
        except Exception as error:
            # 連 loader 都沒跑完（例如資料庫被鎖）：同樣不中止回補，但要記下來
            logger.error(
                f"Equity changes 批次入庫異常（{type(error).__name__}: {error}），"
                f"其餘年季照常繼續"
            )
            return [str(path) for path in files]

        return []

    def get_target_stock_ids(self) -> List[str]:
        """
        - Description:
            取得要逐檔爬取權益變動表的股票清單（上市櫃普通股，排除 ETF 與興櫃）

            `taiwan_stock_info` 不存在時回空清單；其他查詢錯誤往外拋（舊版一律吞掉
            回空清單，「DB 被鎖住」會變成「沒有目標股票，略過」，行程照樣成功結束）。
        """

        return StockInfoDAO(conn=self.conn).get_listed_common_stock_ids()

    def get_crawled_stock_ids(self, year: int, season: int) -> Set[str]:
        """取得指定年季已入庫的 stock_id，供逐檔爬取的中斷續跑使用；查詢錯誤往外拋"""

        return self.get_dao(EQUITY_CHANGE_TABLE_NAME).get_stock_ids(year, season)

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
