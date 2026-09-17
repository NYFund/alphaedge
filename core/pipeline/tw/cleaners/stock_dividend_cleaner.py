import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import DIVIDEND_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.pipeline.utils.exceptions import ColumnLayoutError
from core.utils import TimeUtils

"""
Stock Dividend Cleaner：將 TWSE（證交所）與 TPEX（櫃買中心）兩種版面的
除權除息計算結果表收斂為同一組欄位。

兩者的價格欄位語意相同，差異在欄位名稱、日期格式（`113年01月04日` vs `113/01/03`）、
權息別寫法（`權`／`息` vs `除權`／`除息`），以及**櫃買中心多提供官方的股利拆分欄位**。

衍生欄位一律在本檔集中計算，避免兩條清洗路徑各算一份而漂移：
- 還原係數 = 除權息參考價 / 除權息前收盤價（除權息造成的價格落差比例，供後復權累乘）
- 現金股利 / 配股率：上櫃直接採用官方值；上市須推導且無法拆分時留 NULL，見 `split_dividend()`
"""


class StockDividendCleaner(BaseDataCleaner):
    """Stock Dividend Cleaner (Transform)"""

    # TWSE 原始表格欄位（依位置，共 15 欄）
    TWSE_RAW_COLS: List[str] = [
        "資料日期",
        "stock_id",
        "證券名稱",
        "除權息前收盤價",
        "除權息參考價",
        "權息值合計",
        "權息別",
        "漲停價",
        "跌停價",
        "開盤競價基準",
        "減除股利參考價",
        "詳細資料",
        "最近一次申報季別",
        "最近一次申報每股淨值",
        "最近一次申報每股盈餘",
    ]

    # TPEX（櫃買中心）欄位對照（官方中文欄名 → 統一欄名）
    #
    # **依名稱對應而非位置**：櫃買中心的版面於 2016 年改制（2013~2015 為 22 欄，
    # 多一欄 `員工紅利轉增資`），依位置對應會讓改制前三年的資料整段錯位。
    # 依名稱對應則多出來的欄位會被 reindex 自然忽略，兩種版面共用同一條路徑。
    TPEX_FIELD_MAP: Dict[str, str] = {
        "除權息日期": "資料日期",
        "代號": "stock_id",
        "名稱": "證券名稱",
        "除權息前收盤價": "除權息前收盤價",
        "除權息參考價": "除權息參考價",
        "權值+息值": "權息值合計",
        "權/息": "權息別",
        "漲停價": "漲停價",
        "跌停價": "跌停價",
        "開始交易基準價": "開盤競價基準",
        "減除股利參考價": "減除股利參考價",
        "現金股利": "現金股利",
        "每仟股無償配股": "每仟股無償配股",
    }

    # 每仟股無償配股 → 每股配股數
    SHARES_PER_THOUSAND: float = 1000.0

    # 權息別統一寫法（TPEX 用「除權」「除息」，TWSE 用「權」「息」）
    EX_TYPE_MAP: Dict[str, str] = {
        "除權": "權",
        "除息": "息",
        "除權息": "權息",
    }

    # 價格比對容差（官方欄位最小報價單位遠大於此）
    PRICE_TOLERANCE: float = 1e-9

    # 需轉為浮點數的價格欄位
    FLOAT_COLS: List[str] = [
        "除權息前收盤價",
        "除權息參考價",
        "權息值合計",
        "漲停價",
        "跌停價",
        "開盤競價基準",
        "減除股利參考價",
    ]

    def __init__(self) -> None:
        super().__init__()

        # Dividend DataFrame Cleaned Columns
        self.dividend_cleaned_cols: Optional[List[str]] = None

        # Downloads directory Path
        self.dividend_dir: Path = DIVIDEND_DOWNLOADS_PATH

        # Set Up
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Set Up Dividend DataFrame Cleaned Columns
        self.dividend_cleaned_cols = [
            "date",
            "stock_id",
            "證券名稱",
            "除權息前收盤價",
            "除權息參考價",
            "權息值合計",
            "權息別",
            "現金股利",
            "配股率",
            "漲停價",
            "跌停價",
            "開盤競價基準",
            "減除股利參考價",
            "還原係數",
            "資料來源",
        ]

        # Generate downloads directory
        self.dividend_dir.mkdir(parents=True, exist_ok=True)

    def clean_twse_dividend(
        self,
        df: pd.DataFrame,
        file_name: str,
    ) -> Optional[pd.DataFrame]:
        """Clean TWSE Stock Dividend Data；版面改制時拋 `ColumnLayoutError`"""

        if df is None or df.empty:
            return None

        # 欄位數不符代表來源版面改制。拋例外而不是回 None：回 None 在 updater 端
        # 與「區間內沒有除權息」長得一樣，除權息表會無聲地停止更新
        self.check_column_count(
            df, len(self.TWSE_RAW_COLS), f"TWSE dividend {file_name}"
        )

        df = df.copy()
        df.columns = self.TWSE_RAW_COLS
        df["date"] = df["資料日期"].apply(self.parse_roc_date)

        return self.finalize(df=df, source="twse", file_name=file_name)

    def clean_tpex_dividend(
        self,
        df: pd.DataFrame,
        file_name: str,
    ) -> Optional[pd.DataFrame]:
        """Clean TPEX Stock Dividend Data（櫃買中心）；缺欄位時拋 `ColumnLayoutError`"""

        if df is None or df.empty:
            return None

        missing_cols: List[str] = [
            col for col in self.TPEX_FIELD_MAP if col not in df.columns
        ]
        if missing_cols:
            raise ColumnLayoutError(
                f"TPEX dividend {file_name} 缺欄位 {missing_cols}",
                expected=len(self.TPEX_FIELD_MAP),
                actual=len(self.TPEX_FIELD_MAP) - len(missing_cols),
                columns=list(df.columns),
            )

        df = df.rename(columns=self.TPEX_FIELD_MAP).copy()
        df["date"] = df["資料日期"].apply(self.parse_roc_slash_date)

        return self.finalize(df=df, source="tpex", file_name=file_name)

    def finalize(
        self,
        df: pd.DataFrame,
        source: str,
        file_name: str,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            三個來源共用的清洗尾段：型別轉換、衍生欄位、去重與落地 CSV

        - Parameters:
            - df: pd.DataFrame
                已完成欄位改名與 date 轉換的 DataFrame
            - source: str
                資料來源標記（twse／tpex），寫入「資料來源」欄供追溯
            - file_name: str
                輸出 CSV 檔名（不含副檔名）

        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；無有效資料時回傳 None
        """

        df["stock_id"] = df["stock_id"].astype(str).str.strip()
        # TPEX OpenAPI 的名稱帶有全形補齊空白
        if "證券名稱" in df.columns:
            df["證券名稱"] = df["證券名稱"].fillna("").astype(str).str.strip()
        else:
            df["證券名稱"] = ""

        # 權息別統一為 權／息／權息
        df["權息別"] = (
            df["權息別"].fillna("").astype(str).str.strip().replace(self.EX_TYPE_MAP)
        )

        df = DataUtils.convert_col_to_numeric(df, exclude_cols=self.non_numeric_cols())

        # 日期解析失敗的列一律剔除，不可讓 NaT 進資料庫
        invalid_date_cnt: int = int(df["date"].isna().sum())
        if invalid_date_cnt:
            logger.warning(f"Drop {invalid_date_cnt} rows with unparsable date")
            df = df[df["date"].notna()]

        # 前收盤價或參考價缺漏／非正值時無法計算還原係數。
        # 這裡直接剔除並記錄，不可填 0 後靜默產生 inf／0 係數（見 CLAUDE.md 的不可靜默失敗原則）
        valid_mask: pd.Series = (df["除權息前收盤價"] > 0) & (df["除權息參考價"] > 0)
        dropped_cnt: int = int((~valid_mask).sum())
        if dropped_cnt:
            logger.warning(
                f"Drop {dropped_cnt} rows with invalid close/reference price from {source}"
            )
        df = df[valid_mask]

        if df.empty:
            logger.warning(f"No valid dividend rows from {source}")
            return None

        # 還原係數：除權息造成的價格落差比例（< 1），查詢端以此往前累乘做後復權
        df["還原係數"] = (df["除權息參考價"] / df["除權息前收盤價"]).round(8)
        df = self.split_dividend(df)
        df["資料來源"] = source

        aligned_df: pd.DataFrame = df.reindex(columns=self.dividend_cleaned_cols)

        # 根據指定 columns 移除重複的 rows
        aligned_df = DataUtils.remove_duplicate_rows(
            df=aligned_df,
            subset=["date", "stock_id"],
            keep="first",
        )

        # Save df to csv file
        aligned_df.to_csv(self.dividend_dir / f"{file_name}.csv", index=False)

        return aligned_df

    def split_dividend(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        - Description:
            補上「現金股利」與「配股率」，依來源走兩條路

            **上櫃（TPEX）**：櫃買中心直接提供 `現金股利` 與 `每仟股無償配股`，
            直接採用官方值，不做任何推導。

            **上市（TWSE）**：TWT49U 只有「權值+息值」合計，須由「減除股利參考價」推導，
            但該欄語意不一致——純除息時等於除權息參考價、純除權時等於除權息前收盤價，
            **權息並存時兩種寫法都出現過**（2024 年 62 筆權息列中有 57 筆等於參考價）。
            若一律套同一條公式，會把股票股利整包誤算成現金股利，這種值餵進放空的
            股利補償會直接算錯且不會有任何錯誤訊息，正是 `CLAUDE.md` 要避免的靜默失效。
            因此上市只處理兩種可驗證的情形，其餘留 NaN 並記錄筆數。

            **還原係數不受影響**：它只用到前收盤價與參考價，與拆分無關

        - Parameters:
            - df: pd.DataFrame
                已算出還原係數的 DataFrame

        - Return:
            - pd.DataFrame
                補上「現金股利」與「配股率」的 DataFrame
        """

        # 來源已提供官方拆分值（TPEX）：直接採用，不推導
        if "每仟股無償配股" in df.columns:
            df["配股率"] = (df["每仟股無償配股"] / self.SHARES_PER_THOUSAND).round(8)
            df["現金股利"] = df["現金股利"].round(8)
            return df

        before: pd.Series = df["除權息前收盤價"]
        reference: pd.Series = df["除權息參考價"]
        deducted: pd.Series = df["減除股利參考價"]

        # 純除息：參考價 = 前收盤價 − 現金股利，無配股
        cash_only: pd.Series = df["權息別"] == "息"
        # 純除權：減除股利參考價 = 前收盤價（未扣息），落差全部來自配股
        stock_only: pd.Series = (df["權息別"] == "權") & (
            (deducted - before).abs() < self.PRICE_TOLERANCE
        )

        df["現金股利"] = pd.NA
        df["配股率"] = pd.NA
        df.loc[cash_only, "現金股利"] = (before - reference)[cash_only].round(8)
        df.loc[cash_only, "配股率"] = 0.0
        df.loc[stock_only, "現金股利"] = 0.0
        df.loc[stock_only, "配股率"] = (before / reference - 1)[stock_only].round(8)

        unresolved_cnt: int = int(len(df) - cash_only.sum() - stock_only.sum())
        if unresolved_cnt:
            logger.info(
                f"{unresolved_cnt}/{len(df)} rows keep 現金股利／配股率 as NULL "
                f"(權息並存或含現金增資，官方欄位不足以拆分)"
            )

        return df

    def non_numeric_cols(self) -> List[str]:
        """回傳不做數值轉換的欄位"""

        return ["date", "資料日期", "stock_id", "證券名稱", "權息別", "資料來源"]

    @staticmethod
    def parse_roc_date(value: str) -> Optional[datetime.date]:
        """解析 TWSE 的民國日期字串（例：`113年01月04日`）"""

        try:
            text: str = str(value).strip()
            roc_year, rest = text.split("年")
            month, rest = rest.split("月")
            day: str = rest.replace("日", "")
            year: int = int(TimeUtils.convert_roc_to_ad_year(roc_year))
            return datetime.date(year, int(month), int(day))
        except (ValueError, TypeError):
            # 只收「這個字串不是日期」（拆不出段、非數字、日期超出範圍）；
            # 其餘錯誤是程式或 schema 問題，不該被清成 None
            return None

    @staticmethod
    def parse_roc_slash_date(value: str) -> Optional[datetime.date]:
        """解析 TPEX 的民國日期字串（例：`113/01/03`）"""

        try:
            roc_year, month, day = str(value).strip().split("/")
            year: int = int(TimeUtils.convert_roc_to_ad_year(roc_year))
            return datetime.date(year, int(month), int(day))
        except (ValueError, TypeError):
            # 只收「這個字串不是日期」（拆不出段、非數字、日期超出範圍）；
            # 其餘錯誤是程式或 schema 問題，不該被清成 None
            return None
