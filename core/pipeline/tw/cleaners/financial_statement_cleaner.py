from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.config import (
    FINANCIAL_STATEMENT_DOWNLOADS_PATH,
    FINANCIAL_STATEMENT_META_DIR_PATH,
)
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils import FinancialStatementType
from core.pipeline.utils.data_utils import DataUtils
from core.utils import FileEncoding, TimeUtils

"""
季報財務報表清洗：四張報表與權益變動表

- Features:
    1. 資產負債表、綜合損益表、現金流量表的清洗（走欄名對照表）
    2. 權益變動表的清洗（**攤平後的長表**，欄位固定六個，不走對照表）
    3. 權益變動表的分批落地與續跑
- 使用場景:
    前三張報表的欄名逐年逐公司不同，靠對照表推導；權益變動表的來源版面是
    「權益項目 × 變動原因」的交叉表，攤平成長表後欄位固定，兩條路不共用。

    **各季的期別標籤不一致**（只有 Q1 是「第N季」，其餘三季用別的寫法），
    認錯就抓到別季的表，故以來源表左上角的固定字樣先確認「這張是報表」
    而不是頁面上的公告文字表格。
"""


class FinancialStatementCleaner(BaseDataCleaner):
    """Cleaner for quarterly financial Statement"""

    # 權益變動表是攤平後的長表，欄位固定六個，不像其他三張報表要靠 column map 推導
    EQUITY_CHANGE_COLS: List[str] = [
        "year",
        "season",
        "stock_id",
        "權益項目",  # 來源表的欄（普通股股本、資本公積、保留盈餘合計…）
        "變動原因",  # 來源表的列（期初餘額、本期淨利（淨損）、期末餘額…）
        "金額",  # 單位：新台幣仟元
    ]
    # 來源表左上角的固定字樣，用來認出「這張是報表」而非頁面上的公告文字表格
    EQUITY_CHANGE_HEADER_CELL: str = "會計項目"
    # 各季在 MOPS 版面上的期別標籤。**只有 Q1 是「第N季」**，其餘三季用的是
    # 累計期間的說法（2026-09-03 以 2330 逐季實查）：
    #
    # | 季別 | 實際標籤 |
    # |:----:|----------|
    # | Q1 | 民國109年第1季 |
    # | Q2 | 民國109年上半年度 |
    # | Q3 | 民國109年前3季 |
    # | Q4 | 民國109年度 |
    #
    # **不可寫死成 `f"民國{roc_year}年第{season}季"`**：Q2~Q4 的措辭不是這樣，
    # 一律比對不到而回空表，且失敗完全沒有跡象——2026-09-03 的 2020Q2 全市場回補
    # 實測打了 2,087 次請求、跑 1.5 小時、入庫 0 列，統計行三個數字都正常、結束碼 0。
    # 每季留一組候選（把「第N季」也列為可接受）是為了站方調整措辭時不再整季空轉；
    # 本期與去年同期仍由「民國{roc_year}年」前綴區分，故多列幾個候選不會抓錯期別
    EQUITY_CHANGE_PERIOD_LABELS: Dict[int, Tuple[str, ...]] = {
        1: ("民國{roc_year}年第1季",),
        2: ("民國{roc_year}年上半年度", "民國{roc_year}年第2季"),
        3: ("民國{roc_year}年前3季", "民國{roc_year}年第3季"),
        4: ("民國{roc_year}年度", "民國{roc_year}年第4季"),
    }

    def __init__(self) -> None:
        super().__init__()

        # Raw and cleaned column names for each report type (Load from .json)
        self.balance_sheet_cols: List[str] = []
        self.balance_sheet_cleaned_cols: List[str] = []
        self.comprehensive_income_cols: List[str] = []
        self.comprehensive_income_cleaned_cols: List[str] = []
        self.cash_flow_cols: List[str] = []
        self.cash_flow_cleaned_cols: List[str] = []
        # 權益變動表沒有 raw cols 與 col map：欄位由攤平規則固定（見 EQUITY_CHANGE_COLS）
        self.equity_change_cleaned_cols: List[str] = []

        # Column mapping for each report type (Load from .json)
        self.balance_sheet_col_map: Dict[str, List[str]] = {}
        self.comprehensive_income_col_map: Dict[str, List[str]] = {}
        self.cash_flow_col_map: Dict[str, List[str]] = {}

        # Reports Cleaned Columns Path
        self.balance_sheet_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.BALANCE_SHEET.lower()
            / f"{FinancialStatementType.BALANCE_SHEET.lower()}_cleaned_columns.json"
        )
        self.comprehensive_income_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.COMPREHENSIVE_INCOME.lower()
            / f"{FinancialStatementType.COMPREHENSIVE_INCOME.lower()}_cleaned_columns.json"
        )
        self.cash_flow_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.CASH_FLOW.lower()
            / f"{FinancialStatementType.CASH_FLOW.lower()}_cleaned_columns.json"
        )
        self.equity_change_cleaned_cols_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / FinancialStatementType.EQUITY_CHANGE.lower()
            / f"{FinancialStatementType.EQUITY_CHANGE.lower()}_cleaned_columns.json"
        )

        # Output directories for each report
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

        # Clean Set Up
        self.removed_cols: List[str] = ["Unnamed", "0"]

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Create Downloads Directory For Financial Reports
        self.fs_dir.mkdir(parents=True, exist_ok=True)
        self.balance_sheet_dir.mkdir(parents=True, exist_ok=True)
        self.comprehensive_income_dir.mkdir(parents=True, exist_ok=True)
        self.cash_flow_dir.mkdir(parents=True, exist_ok=True)
        self.equity_change_dir.mkdir(parents=True, exist_ok=True)

        # Load Report Column Names & Map
        self.load_all_column_names()
        self.load_column_maps()

        # 權益變動表的欄位不是從來源表推導出來的，而是攤平規則決定的固定六欄，
        # 這裡直接落地成 JSON，讓 loader 沿用與其他三張報表相同的建表流程
        self.equity_change_cleaned_cols = self.EQUITY_CHANGE_COLS
        if not self.equity_change_cleaned_cols_path.exists():
            DataUtils.save_json(
                data=self.EQUITY_CHANGE_COLS,
                file_path=self.equity_change_cleaned_cols_path,
            )

    def clean_balance_sheet(
        self, df_list: List[pd.DataFrame], year: int, season: int
    ) -> pd.DataFrame:
        """
        Clean Balance Sheet (資產負債表)

        資料區間（但是只有 102 年以後才可以爬）
        上市: 民國 78 (1989) 年 ~ present
        上櫃: 民國 82 (1993) 年 ~ present
        """

        # Step 1: 載入已清洗欄位，若未成功則執行清洗流程
        if not self.balance_sheet_cleaned_cols:
            self.load_cleaned_column_names(
                report_type=FinancialStatementType.BALANCE_SHEET
            )
            if not self.balance_sheet_cleaned_cols:
                self.balance_sheet_cleaned_cols = self.clean_report_column_names(
                    raw_cols=self.balance_sheet_cols,
                    col_map=self.balance_sheet_col_map,
                    front_cols=["year", "season", "公司代號", "公司名稱"],
                    save_path=self.balance_sheet_cleaned_cols_path,
                )

        # Step 2: 清理 df_list 欄位名稱
        # 建立涵蓋所有 columns 的 df
        new_df: pd.DataFrame = pd.DataFrame(columns=self.balance_sheet_cleaned_cols)
        # 篩掉沒有 "公司名稱" 的 df
        required_cols: List[str] = ["公司名稱"]
        df_list: List[pd.DataFrame] = [
            df
            for df in df_list
            if DataUtils.check_required_columns(df=df, required_cols=required_cols)
        ]

        # 清洗 df Column Names
        appended_df_list: List[pd.DataFrame] = []
        for df in df_list:
            cleaned_cols: List[str] = [
                DataUtils.map_column_name(
                    DataUtils.standardize_column_name(col), self.balance_sheet_col_map
                )
                for col in df.columns
            ]
            df.columns = cleaned_cols
            DataUtils.remove_cols_by_keywords(df, startswith=self.removed_cols)

            # 對齊欄位並補上欄位
            aligned_df: pd.DataFrame = df.reindex(columns=new_df.columns)
            aligned_df["year"] = year
            aligned_df["season"] = season
            appended_df_list.append(aligned_df)

        new_df = (
            pd.concat(appended_df_list, ignore_index=True)
            .astype(str)
            .pipe(
                DataUtils.convert_col_to_numeric, exclude_cols=["stock_id", "公司名稱"]
            )
        )

        # 根據指定 columns 移除重複的 rows
        new_df = DataUtils.remove_duplicate_rows(
            df=new_df,
            subset=["year", "season", "stock_id", "公司名稱"],
            keep="first",
        )

        new_df.to_csv(
            self.balance_sheet_dir / f"balance_sheet_{year}Q{season}.csv",
            index=False,
            encoding=FileEncoding.UTF8.value,
        )

        return new_df

    def clean_comprehensive_income(
        self, df_list: List[pd.DataFrame], year: int, season: int
    ) -> pd.DataFrame:
        """
        Clean Statement of Comprehensive Income (綜合損益表)

        資料區間（但是只有 102 年以後才可以爬）
        上市: 民國 77 (1988) 年 ~ present
        上櫃: 民國 82 (1993) 年 ~ present
        """

        # Step 1: 載入已清洗欄位，若未成功則執行清洗流程
        if not self.comprehensive_income_cleaned_cols:
            self.load_cleaned_column_names(
                report_type=FinancialStatementType.COMPREHENSIVE_INCOME
            )
            if not self.comprehensive_income_cleaned_cols:
                self.comprehensive_income_cleaned_cols = self.clean_report_column_names(
                    raw_cols=self.comprehensive_income_cols,
                    col_map=self.comprehensive_income_col_map,
                    front_cols=["year", "season", "公司代號", "公司名稱"],
                    save_path=self.comprehensive_income_cleaned_cols_path,
                )

        # Step 2: 清理 df_list 欄位名稱
        # 建立涵蓋所有 columns 的 df
        new_df: pd.DataFrame = pd.DataFrame(
            columns=self.comprehensive_income_cleaned_cols
        )
        # 篩掉沒有 "公司名稱" 的 df
        required_cols: List[str] = ["公司名稱"]
        df_list: List[pd.DataFrame] = [
            df
            for df in df_list
            if DataUtils.check_required_columns(df=df, required_cols=required_cols)
        ]

        # 清洗 df Column Names
        appended_df_list: List[pd.DataFrame] = []
        for df in df_list:
            cleaned_cols: List[str] = [
                DataUtils.map_column_name(
                    DataUtils.standardize_column_name(col),
                    self.comprehensive_income_col_map,
                )
                for col in df.columns
            ]
            df.columns = cleaned_cols
            DataUtils.remove_cols_by_keywords(df, startswith=["0"])

            aligned_df: pd.DataFrame = df.reindex(columns=new_df.columns)
            aligned_df["year"] = year
            aligned_df["season"] = season
            appended_df_list.append(aligned_df)

        new_df = (
            pd.concat(appended_df_list, ignore_index=True)
            .astype(str)
            .pipe(
                DataUtils.convert_col_to_numeric, exclude_cols=["stock_id", "公司名稱"]
            )
        )

        # 根據指定 columns 移除重複的 rows
        new_df = DataUtils.remove_duplicate_rows(
            df=new_df,
            subset=["year", "season", "stock_id", "公司名稱"],
            keep="first",
        )

        new_df.to_csv(
            self.comprehensive_income_dir / f"comprehensive_income_{year}Q{season}.csv",
            index=False,
            encoding=FileEncoding.UTF8.value,
        )

        return new_df

    def clean_cash_flow(
        self, df_list: List[pd.DataFrame], year: int, season: int
    ) -> pd.DataFrame:
        """
        Clean Cash flow Statement (現金流量表)

        資料區間
        上市: 民國 102 (2013) 年 ~ present
        上櫃: 民國 102 (2013) 年 ~ present
        """

        # Step 1: 載入已清洗欄位，若未成功則執行清洗流程
        if not self.cash_flow_cleaned_cols:
            self.load_cleaned_column_names(report_type=FinancialStatementType.CASH_FLOW)
            if not self.cash_flow_cleaned_cols:
                self.cash_flow_cleaned_cols = self.clean_report_column_names(
                    raw_cols=self.cash_flow_cols,
                    col_map=self.cash_flow_col_map,
                    front_cols=["year", "season", "公司代號", "公司名稱"],
                    save_path=self.cash_flow_cleaned_cols_path,
                )

        # Step 2: 清理 df_list 欄位名稱
        # 建立涵蓋所有 columns 的 df
        new_df: pd.DataFrame = pd.DataFrame(columns=self.cash_flow_cleaned_cols)
        # 篩掉沒有 "公司名稱" 的 df
        required_cols: List[str] = ["公司名稱"]
        df_list: List[pd.DataFrame] = [
            df
            for df in df_list
            if DataUtils.check_required_columns(df=df, required_cols=required_cols)
        ]

        # 清洗 df Column Names
        appended_df_list: List[pd.DataFrame] = []
        for df in df_list:
            cleaned_cols: List[str] = [
                DataUtils.map_column_name(
                    DataUtils.standardize_column_name(col), self.cash_flow_col_map
                )
                for col in df.columns
            ]
            df.columns = cleaned_cols
            DataUtils.remove_cols_by_keywords(df, startswith=["0"])

            aligned_df: pd.DataFrame = df.reindex(columns=new_df.columns)
            aligned_df["year"] = year
            aligned_df["season"] = season
            appended_df_list.append(aligned_df)

        new_df = (
            pd.concat(appended_df_list, ignore_index=True)
            .astype(str)
            .pipe(
                DataUtils.convert_col_to_numeric, exclude_cols=["stock_id", "公司名稱"]
            )
        )

        # 根據指定 columns 移除重複的 rows
        new_df = DataUtils.remove_duplicate_rows(
            df=new_df,
            subset=["year", "season", "stock_id", "公司名稱"],
            keep="first",
        )

        new_df.to_csv(
            self.cash_flow_dir / f"cash_flow_{year}Q{season}.csv",
            index=False,
            encoding=FileEncoding.UTF8.value,
        )

        return new_df

    def clean_equity_changes(
        self, df_list: List[pd.DataFrame], year: int, season: int, stock_id: str
    ) -> pd.DataFrame:
        """
        Clean Statement of Changes in Equity (權益變動表)

        資料區間
        上市: 民國 102 (2013) 年 ~ present
        上櫃: 民國 102 (2013) 年 ~ present

        與其他三張報表不同，本表在來源網站是二維表（欄＝權益項目、列＝變動原因），
        且各公司的權益項目集合差異極大（金控多出特別股股本、採用覆蓋法重分類…），
        寬表要存所有公司的欄位聯集、且每遇到新項目就得改 schema，
        故一律攤平成長表：一列 = 一個（權益項目 × 變動原因）的金額。
        """

        # Step 1: 取出「本期」那張表
        # 同一頁還附了去年同季的比較表，兩張的表頭一模一樣，
        # 只有 column 第一層的「民國 X 年第 N 季」能區分；抓錯會把去年的數字記成今年的
        target_df: Optional[pd.DataFrame] = self.select_equity_changes_period_table(
            df_list=df_list, year=year, season=season
        )

        if target_df is None:
            logger.warning(
                f"No equity changes table matched: {stock_id} {year}Q{season}"
            )
            return pd.DataFrame(columns=self.EQUITY_CHANGE_COLS)

        # Step 2: 首列才是真表頭（read_html 讀到的 column 名稱是「單位：新台幣仟元」）
        header: List[str] = [str(value).strip() for value in target_df.iloc[0].tolist()]
        body: pd.DataFrame = target_df.iloc[1:].reset_index(drop=True)

        # 來源表右側固定帶著一串空白欄，表頭為 NaN，直接丟掉
        kept_indexes: List[int] = [
            index for index, name in enumerate(header) if name and name.lower() != "nan"
        ]
        body = body.iloc[:, kept_indexes]
        body.columns = [
            DataUtils.standardize_column_name(header[index]) for index in kept_indexes
        ]

        # Step 3: 攤平成長表（第一欄是變動原因，其餘每欄是一個權益項目）
        reason_col: str = body.columns[0]
        new_df: pd.DataFrame = body.melt(
            id_vars=[reason_col],
            var_name="權益項目",
            value_name="金額",
        ).rename(columns={reason_col: "變動原因"})

        new_df["變動原因"] = new_df["變動原因"].map(
            lambda value: DataUtils.standardize_column_name(str(value))
        )
        new_df["金額"] = pd.to_numeric(new_df["金額"], errors="coerce")
        # 空白儲存格（例如台積電沒有庫藏股票）不存 NULL，長表只留實際有金額的組合
        new_df = new_df.dropna(subset=["金額"])

        new_df.insert(0, "year", year)
        new_df.insert(1, "season", season)
        new_df.insert(2, "stock_id", stock_id)
        new_df = new_df[self.EQUITY_CHANGE_COLS]

        # 根據指定 columns 移除重複的 rows
        new_df = DataUtils.remove_duplicate_rows(
            df=new_df,
            subset=["year", "season", "stock_id", "權益項目", "變動原因"],
            keep="first",
        )

        return new_df

    def select_equity_changes_period_table(
        self, df_list: List[pd.DataFrame], year: int, season: int
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            從權益變動表頁面的表格中挑出「本期」那一張，找不到則回傳 None

            同一頁還附了去年同季的比較表，版面與本期表一模一樣，只有 column
            第一層的期別標籤能區分（見 `EQUITY_CHANGE_PERIOD_LABELS`）。

            **比對不到一律回 None，不做退而求其次的 fallback**：抓錯會把去年的
            數字記成今年，而長表的主鍵相同，錯的那份會安靜地佔住正確資料的位置。
        - Parameters:
            - df_list: List[pd.DataFrame]
                該頁的所有表格
            - year / season: int
                要取的年季
        - Return:
            - Optional[pd.DataFrame]
                本期那張表；比對不到為 None
        """

        roc_year: str = TimeUtils.convert_ad_to_roc_year(year)
        period_labels: Tuple[str, ...] = tuple(
            template.format(roc_year=roc_year)
            for template in self.EQUITY_CHANGE_PERIOD_LABELS[season]
        )

        for df in df_list:
            if df.empty or df.shape[1] < 2:
                continue
            if str(df.iloc[0, 0]).strip() != self.EQUITY_CHANGE_HEADER_CELL:
                continue
            if df.columns.nlevels < 2:
                continue

            label: str = str(df.columns[0][0]).replace(" ", "")
            if label in period_labels:
                return df

        return None

    def save_equity_changes(
        self,
        df_list: List[pd.DataFrame],
        year: int,
        season: int,
    ) -> Optional[Path]:
        """把一批（多檔股票）已清洗的權益變動表合併寫成一個 CSV，回傳檔案路徑"""

        valid_df_list: List[pd.DataFrame] = [
            df for df in df_list if df is not None and not df.empty
        ]

        if not valid_df_list:
            return None

        # 逐檔查詢若一檔一個 CSV，全市場全季會產生十萬個檔案，而 loader 每次都掃整個
        # 目錄；故以「批」為單位落地，檔名帶批次序號讓 loader 只讀本批
        batch_df: pd.DataFrame = pd.concat(valid_df_list, ignore_index=True)
        file_path: Path = self.equity_change_dir / (
            f"equity_change_{year}Q{season}_"
            f"{self.next_equity_changes_batch_index(year, season):04d}.csv"
        )
        batch_df.to_csv(file_path, index=False, encoding=FileEncoding.UTF8.value)

        return file_path

    def next_equity_changes_batch_index(self, year: int, season: int) -> int:
        """
        - Description:
            取得該年季下一個可用的批次序號（現有最大序號 +1，沒有檔案則為 0）

            **序號由目錄現況決定，不能由呼叫端從 0 重數。** 同一年季跑第二次是常態
            （resume 續跑、補暫時性失敗、新上市公司補舊季），而每次執行涵蓋的股票
            子集都不同，同名檔的內容也就不同——2026-08-22 的 2020Q1 補跑實際發生過：
            首輪寫了 16 批，補跑從 0000 重數而蓋掉前 3 批，磁碟上少了 300 檔的紀錄。

            資料本身不會遺失（每批寫完就立刻入庫，且 DB 有主鍵擋重複），但覆寫會讓
            downloads 目錄無法反映實際跑過什麼；更麻煩的是**入庫失敗的批次**——
            那種檔案的資料只存在於磁碟上，被蓋掉就斷了手動補救的路，而 loader 特地
            保留 `remove_files=False` 不刪來源檔，本意正是留這條路。
        - Parameters:
            - year / season: int
                目標年季
        - Return:
            - int
                下一個可用的批次序號
        """

        max_index: int = -1
        for file_path in self.equity_change_dir.glob(
            f"equity_change_{year}Q{season}_*.csv"
        ):
            try:
                max_index = max(max_index, int(file_path.stem.split("_")[-1]))
            except ValueError:
                # 手動改過名的檔案不該讓整個流程停擺，略過即可
                logger.warning(f"Unexpected equity changes batch file: {file_path}")

        return max_index + 1

    def clean_report_column_names(
        self,
        raw_cols: List[str],
        col_map: Dict[str, List[str]],
        front_cols: List[str],
        save_path: Path,
    ) -> List[str]:
        """
        - Description:
            清洗指定的 Report Column Names
        - Parameters:
            - raw_cols: List[str]
                原始欄位名稱清單
            - col_map: Dict[str, List[str]]
                欄位對應映射表（舊名對應標準名）
            - front_cols: List[str]
                優先排序欄位（例如 year、season 等）
            - save_path: Path
                儲存清洗後欄位的 JSON 路徑
        - Return:
            - List[str]
                已清洗、排序、去重後的欄位名稱清單
        """

        # Step 1: 欄位排序
        tail_columns: List[str] = [col for col in raw_cols if col not in front_cols]
        cleaned_cols: List[str] = front_cols + tail_columns

        # Step 2: 移除不必要欄位
        cleaned_cols = DataUtils.remove_items_by_keywords(
            cleaned_cols, startswith=self.removed_cols
        )

        # Step 3: 清洗欄位並做名稱對應
        cleaned_cols = [
            DataUtils.map_column_name(
                DataUtils.standardize_column_name(word=col), col_map
            )
            for col in cleaned_cols
        ]

        # Step 4: 去除重複欄位（保留順序）
        cleaned_cols = list(dict.fromkeys(cleaned_cols))

        # Step 5: 儲存清洗結果
        DataUtils.save_json(data=cleaned_cols, file_path=save_path)
        logger.info(f"已儲存清洗後欄位名稱: {save_path.name}")

        return cleaned_cols

    def load_all_column_names(self) -> None:
        """載入 Report Column Names"""

        attr_map: Dict[FinancialStatementType, str] = {
            FinancialStatementType.BALANCE_SHEET: f"{FinancialStatementType.BALANCE_SHEET.lower()}_cols",
            FinancialStatementType.COMPREHENSIVE_INCOME: f"{FinancialStatementType.COMPREHENSIVE_INCOME.lower()}_cols",
            FinancialStatementType.CASH_FLOW: f"{FinancialStatementType.CASH_FLOW.lower()}_cols",
        }
        # 權益變動表不在此列：它的欄位由攤平規則固定成六欄（見 EQUITY_CHANGE_COLS），
        # 不是從來源表頭蒐集出來的，列進來只會每次建構都警告缺檔

        for report_type, attr_name in attr_map.items():
            file_path: Path = (
                FINANCIAL_STATEMENT_META_DIR_PATH
                / report_type.lower()
                / f"{report_type.lower()}_all_columns.json"
            )

            if not file_path.exists():
                logger.warning(f"Metadata file not found: {file_path}")
                continue

            cols: List[str] = DataUtils.load_json(file_path=file_path)

            if hasattr(self, attr_name):
                setattr(self, attr_name, cols)

    def load_cleaned_column_names(
        self, report_type: FinancialStatementType
    ) -> List[str]:
        """根據報表類型載入已清洗過的 Column Names"""

        cleaned_cols: List[str] = []
        attr_name: str = f"{report_type.lower()}_cleaned_cols"
        file_path: Path = (
            FINANCIAL_STATEMENT_META_DIR_PATH
            / report_type.lower()
            / f"{report_type.lower()}_cleaned_columns.json"
        )

        if file_path.exists():
            cleaned_cols = DataUtils.load_json(file_path=file_path)
            if hasattr(self, attr_name):
                setattr(self, attr_name, cleaned_cols)

        return cleaned_cols

    def load_column_maps(self) -> None:
        """載入 Report Column Maps"""

        attr_map: Dict[FinancialStatementType, str] = {
            FinancialStatementType.BALANCE_SHEET: f"{FinancialStatementType.BALANCE_SHEET.lower()}_col_map",
            FinancialStatementType.COMPREHENSIVE_INCOME: f"{FinancialStatementType.COMPREHENSIVE_INCOME.lower()}_col_map",
            FinancialStatementType.CASH_FLOW: f"{FinancialStatementType.CASH_FLOW.lower()}_col_map",
        }
        # 權益變動表不在此列：長表的兩個維度值直接用 standardize_column_name 正規化，
        # 不需要欄位對照表（理由同 load_all_column_names）

        for report_type, attr_name in attr_map.items():
            file_path: Path = (
                FINANCIAL_STATEMENT_META_DIR_PATH
                / report_type.lower()
                / f"{report_type.lower()}_column_map.json"
            )

            if not file_path.exists():
                logger.warning(f"Metadata file not found: {file_path}")
                continue

            col_map: Dict[str, List[str]] = DataUtils.load_json(file_path=file_path)

            if hasattr(self, attr_name):
                setattr(self, attr_name, col_map)
