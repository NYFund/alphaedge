import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import MARGIN_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.utils import TimeUtils

"""
Stock Margin Cleaner：將 TWSE／TPEX 兩種版面的信用交易表格收斂為同一組欄位。

兩個來源的數量單位皆為「張」，欄位順序固定（2013 年起未再改制），
因此一律以「位置」重新命名欄位，不依賴來源網站的標題文字。
"""


class StockMarginCleaner(BaseDataCleaner):
    """Stock Margin Cleaner (Transform)"""

    # TWSE 原始表格欄位（依位置，共 16 欄）
    TWSE_RAW_COLS: List[str] = [
        "stock_id",
        "證券名稱",
        "融資買進",
        "融資賣出",
        "融資現金償還",
        "融資前日餘額",
        "融資今日餘額",
        "融資限額",
        "融券買進",
        "融券賣出",
        "融券現券償還",
        "融券前日餘額",
        "融券今日餘額",
        "融券限額",
        "資券互抵",
        "註記",
    ]

    # TPEX 原始表格欄位（依位置，共 20 欄；「資屬證金」等衍生欄位不入庫）
    TPEX_RAW_COLS: List[str] = [
        "stock_id",
        "證券名稱",
        "融資前日餘額",
        "融資買進",
        "融資賣出",
        "融資現金償還",
        "融資今日餘額",
        "融資屬證金",
        "融資使用率",
        "融資限額",
        "融券前日餘額",
        "融券賣出",
        "融券買進",
        "融券現券償還",
        "融券今日餘額",
        "融券屬證金",
        "融券使用率",
        "融券限額",
        "資券互抵",
        "註記",
    ]

    # 需轉為整數的欄位（單位皆為張）
    INT_COLS: List[str] = [
        "融資買進",
        "融資賣出",
        "融資現金償還",
        "融資前日餘額",
        "融資今日餘額",
        "融資限額",
        "融券買進",
        "融券賣出",
        "融券現券償還",
        "融券前日餘額",
        "融券今日餘額",
        "融券限額",
        "資券互抵",
    ]

    # 合法證券代號樣式（用於濾掉「合計」「共 N 筆」等統計列）
    STOCK_ID_PATTERN: str = r"[0-9A-Z]{4,6}"

    def __init__(self) -> None:
        super().__init__()

        # Margin DataFrame Cleaned Columns
        self.margin_cleaned_cols: Optional[List[str]] = None

        # Downloads directory Path
        self.margin_dir: Path = MARGIN_DOWNLOADS_PATH

        # Set Up
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Set Up Margin DataFrame Cleaned Columns
        self.margin_cleaned_cols = [
            "date",
            "stock_id",
            "證券名稱",
            "融資買進",
            "融資賣出",
            "融資現金償還",
            "融資前日餘額",
            "融資今日餘額",
            "融資限額",
            "融券買進",
            "融券賣出",
            "融券現券償還",
            "融券前日餘額",
            "融券今日餘額",
            "融券限額",
            "資券互抵",
            "券資比",
            "註記",
        ]

        # Generate downloads directory
        self.margin_dir.mkdir(parents=True, exist_ok=True)

    def clean_twse_margin(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> Optional[pd.DataFrame]:
        """Clean TWSE Stock Margin Data"""

        return self.clean_margin(
            df=df,
            date=date,
            raw_cols=self.TWSE_RAW_COLS,
            file_prefix="twse",
        )

    def clean_tpex_margin(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> Optional[pd.DataFrame]:
        """Clean TPEX Stock Margin Data"""

        return self.clean_margin(
            df=df,
            date=date,
            raw_cols=self.TPEX_RAW_COLS,
            file_prefix="tpex",
        )

    def clean_margin(
        self,
        df: pd.DataFrame,
        date: datetime.date,
        raw_cols: List[str],
        file_prefix: str,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            TWSE／TPEX 共用的清洗流程。兩者只差在原始欄位順序，
            故以 raw_cols 依位置命名後走同一條路徑
        - Parameters:
            - df: pd.DataFrame
                爬蟲取得的原始表格
            - date: datetime.date
                資料日期
            - raw_cols: List[str]
                原始表格的欄位名稱（依位置）
            - file_prefix: str
                輸出 CSV 的檔名前綴（twse／tpex）
        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；原始表為空或無有效資料時回傳 None
        - Raise:
            - ColumnLayoutError
                欄位數不符（來源版面改制）
        """

        if df is None or df.empty:
            return None

        # 欄位數不符代表來源版面改制，拋例外而不是回 None：
        # 呼叫端才分得出「版面壞了」與「這天沒資料」，前者當天必須記為失敗
        self.check_column_count(
            df, len(raw_cols), f"{file_prefix.upper()} margin {date}"
        )

        df.columns = raw_cols

        # 濾掉「合計」「融資金(仟元)」「共 N 筆」等統計列
        stock_id: pd.Series = df["stock_id"].astype(str).str.strip()
        df = df[stock_id.str.fullmatch(self.STOCK_ID_PATTERN).fillna(False)].copy()
        df["stock_id"] = df["stock_id"].astype(str).str.strip()

        if df.empty:
            logger.warning(f"No valid margin rows on {date}")
            return None

        df.insert(0, "date", date)
        # 註記為文字欄位，需在數值填補前先處理，避免被填成 0
        df["註記"] = df["註記"].fillna("").astype(str).str.strip()

        aligned_df: pd.DataFrame = df.reindex(
            columns=self.margin_cleaned_cols, fill_value=0
        )
        aligned_df = DataUtils.convert_col_to_numeric(
            aligned_df, exclude_cols=["date", "stock_id", "證券名稱", "註記"]
        )
        aligned_df = DataUtils.fill_nan(aligned_df, 0)

        for col in self.INT_COLS:
            aligned_df[col] = aligned_df[col].astype(int)

        # 券資比（%）：融券今日餘額 / 融資今日餘額；融資餘額為 0 時視為 0
        financing_balance: pd.Series = aligned_df["融資今日餘額"].where(
            aligned_df["融資今日餘額"] != 0
        )
        aligned_df["券資比"] = (
            aligned_df["融券今日餘額"]
            .div(financing_balance)
            .mul(100)
            .round(2)
            .fillna(0.0)
            .astype(float)
        )

        # 根據指定 columns 移除重複的 rows
        aligned_df = DataUtils.remove_duplicate_rows(
            df=aligned_df,
            subset=["date", "stock_id"],
            keep="first",
        )

        # Save df to csv file
        aligned_df.to_csv(
            self.margin_dir / f"{file_prefix}_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return aligned_df
