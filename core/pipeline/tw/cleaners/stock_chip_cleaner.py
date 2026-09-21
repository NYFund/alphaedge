import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from core.config import CHIP_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.utils import TimeUtils


class StockChipCleaner(BaseDataCleaner):
    """Stock Chip Cleaner (Transform)"""

    # TWSE 三大法人資料改制日期
    TWSE_FIRST_REFORM_DATE: datetime.date = datetime.date(2014, 12, 1)
    TWSE_SECOND_REFORM_DATE: datetime.date = datetime.date(2017, 12, 18)
    # TPEX 三大法人資料改制日期
    TPEX_FIRST_REFORM_DATE: datetime.date = datetime.date(2014, 12, 1)
    TPEX_SECOND_REFORM_DATE: datetime.date = datetime.date(2018, 1, 15)

    def __init__(self) -> None:
        super().__init__()

        # Chip DataFrame Cleaned Columns
        self.chip_cleaned_cols: Optional[List[str]] = None

        self.twse_first_reform_date: datetime.date = self.TWSE_FIRST_REFORM_DATE
        self.twse_second_reform_date: datetime.date = self.TWSE_SECOND_REFORM_DATE
        self.tpex_first_reform_date: datetime.date = self.TPEX_FIRST_REFORM_DATE
        self.tpex_second_reform_date: datetime.date = self.TPEX_SECOND_REFORM_DATE

        # Downloads directory Path
        self.chip_dir: Path = CHIP_DOWNLOADS_PATH

        # Set Up
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Set Up Chip DataFrame Cleaned Columns
        self.chip_cleaned_cols = [
            "date",
            "stock_id",
            "證券名稱",
            "外資買進股數",
            "外資賣出股數",
            "外資買賣超股數",
            "投信買進股數",
            "投信賣出股數",
            "投信買賣超股數",
            "自營商買進股數(自行買賣)",
            "自營商賣出股數(自行買賣)",
            "自營商買賣超股數(自行買賣)",
            "自營商買進股數(避險)",
            "自營商賣出股數(避險)",
            "自營商買賣超股數(避險)",
            "自營商買進股數",
            "自營商賣出股數",
            "自營商買賣超股數",
            "三大法人買賣超股數",
        ]

        # Generate downloads directory
        self.chip_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def combine_dealer_columns(df: pd.DataFrame) -> None:
        """
        - Description:
            把自營商的「自行買賣」與「避險」兩欄相加成合計欄（就地修改）

            **拆分欄不存在時完全不動**：改制前的來源直接給合計欄，
            蓋成 0 會讓買進、賣出兩欄失去資料，而買賣超欄仍是對的。
        - Parameters:
            - df: pd.DataFrame
                已標準化欄名的原始表
        """

        for action in ("買進", "賣出"):
            self_trade: str = f"自營商{action}股數(自行買賣)"
            hedge: str = f"自營商{action}股數(避險)"
            if self_trade not in df.columns and hedge not in df.columns:
                continue

            df[f"自營商{action}股數"] = df.get(self_trade, 0) + df.get(hedge, 0)

    def clean_twse_chip(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> pd.DataFrame:
        """Clean TWSE Stock Chip Data"""

        if isinstance(df.columns, pd.MultiIndex) and df.columns.nlevels > 1:
            df.columns = df.columns.droplevel(0)

        # 先處理 raw df
        df.columns = [DataUtils.standardize_column_name(col) for col in df.columns]
        df.insert(0, "date", date)
        df = df.rename(columns={"證券代號": "stock_id"})
        # 合併自營商自行買賣與避險欄位。
        # **只在拆分欄存在時才相加**：第一次改制（2014-12-01）之前的來源沒有
        # 「自行買賣／避險」兩欄，直接相加會是 `0 + 0`，把來源原本就給的
        # `自營商買進股數` 蓋成 0——而買賣超欄不受影響，於是庫裡出現
        # 「買賣皆 0、買賣超非 0」這種不可能的組合（2330 在 2013-06-03 即如此）
        self.combine_dealer_columns(df)

        # 第二次格式改制前
        if date < self.twse_second_reform_date:
            aligned_df: pd.DataFrame = df.reindex(
                columns=self.chip_cleaned_cols, fill_value=0
            )

        # 第二次格式改制後
        elif date >= self.twse_second_reform_date:
            df["外資買進股數"] = df.get("外陸資買進股數(不含外資自營商)", 0) + df.get(
                "外資自營商買進股數", 0
            )
            df["外資賣出股數"] = df.get("外陸資賣出股數(不含外資自營商)", 0) + df.get(
                "外資自營商賣出股數", 0
            )
            df["外資買賣超股數"] = df.get(
                "外陸資買賣超股數(不含外資自營商)", 0
            ) + df.get("外資自營商買賣超股數", 0)
            aligned_df: pd.DataFrame = df.reindex(
                columns=self.chip_cleaned_cols, fill_value=0
            )

        aligned_df = DataUtils.convert_col_to_numeric(
            aligned_df, exclude_cols=["date", "stock_id", "證券名稱"]
        )
        aligned_df = DataUtils.fill_nan(aligned_df, 0)

        # 根據指定 columns 移除重複的 rows
        aligned_df = DataUtils.remove_duplicate_rows(
            df=aligned_df,
            subset=["date", "stock_id", "證券名稱"],
            keep="first",
        )

        # Save df to csv file
        aligned_df.to_csv(
            self.chip_dir / f"twse_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return aligned_df

    def clean_tpex_chip(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> pd.DataFrame:
        """Clean TPEX Stock Chip Data"""

        if isinstance(df.columns, pd.MultiIndex) and df.columns.nlevels > 1:
            df.columns = df.columns.droplevel(0)

        # Remove last row
        df = DataUtils.remove_last_n_rows(df, n_rows=1)

        # date < 第一次格式改制（2014/12/1）
        if date < self.tpex_first_reform_date:
            df.columns = [DataUtils.standardize_column_name(col) for col in df.columns]
            old_col_name: List[str] = list(df.columns)
            new_col_name: List[str] = [
                "stock_id",
                "證券名稱",
                "外資買進股數",
                "外資賣出股數",
                "外資買賣超股數",
                "投信買進股數",
                "投信賣出股數",
                "投信買賣超股數",
                "自營商買進股數",
                "自營商賣出股數",
                "自營商買賣超股數",
            ]
            # `zip()` 長度不一時會**安靜地截斷**，多出來的欄位保留原名、
            # 之後被 reindex 填成 0——版面一改就整批變成假的 0
            self.check_column_count(
                df, len(new_col_name), f"TPEX chip {date}（依位置命名）"
            )
            rename_map: Dict[str, str] = dict(zip(old_col_name, new_col_name))
            df = df.rename(columns=rename_map)
            df.insert(0, "date", date)
            df["三大法人買賣超股數"] = (
                df.get("外資買賣超股數", 0)
                + df.get("投信買賣超股數", 0)
                + df.get("自營商買賣超股數", 0)
            )

        # 第一次格式改制 <= date < 第二次格式改制（2018/1/15）
        elif self.tpex_first_reform_date <= date < self.tpex_second_reform_date:
            df.columns = [DataUtils.standardize_column_name(col) for col in df.columns]
            old_col_name: List[str] = list(df.columns)
            new_col_name: List[str] = [
                "stock_id",
                "證券名稱",
                "外資買進股數",
                "外資賣出股數",
                "外資買賣超股數",
                "投信買進股數",
                "投信賣出股數",
                "投信買賣超股數",
                "自營商買賣超股數",
                "自營商買進股數(自行買賣)",
                "自營商賣出股數(自行買賣)",
                "自營商買賣超股數(自行買賣)",
                "自營商買進股數(避險)",
                "自營商賣出股數(避險)",
                "自營商買賣超股數(避險)",
                "三大法人買賣超股數",
            ]
            # `zip()` 長度不一時會**安靜地截斷**，多出來的欄位保留原名、
            # 之後被 reindex 填成 0——版面一改就整批變成假的 0
            self.check_column_count(
                df, len(new_col_name), f"TPEX chip {date}（依位置命名）"
            )
            rename_map: Dict[str, str] = dict(zip(old_col_name, new_col_name))
            df = df.rename(columns=rename_map)
            df.insert(0, "date", date)

        # date >= 第二次格式改制（2018/1/15）
        elif date >= self.tpex_second_reform_date:
            # 因為 df.columns 是 MultiIndex(2層)，所以將其轉為1層
            df.columns = [
                f"{col1}{col2}" if col1 != col2 else col1 for col1, col2 in df.columns
            ]
            df.columns = [DataUtils.standardize_column_name(col) for col in df.columns]
            drop_cols: List[str] = [
                "外資及陸資(不含外資自營商)買進股數",
                "外資及陸資(不含外資自營商)賣出股數",
                "外資及陸資(不含外資自營商)買賣超股數",
                "外資自營商買進股數",
                "外資自營商賣出股數",
                "外資自營商買賣超股數",
            ]
            df = df.drop(columns=drop_cols)
            df.insert(0, "date", date)

            # Rename df.columns
            old_col_name: List[str] = list(df.columns)
            new_col_name: List[str] = self.chip_cleaned_cols
            # `zip()` 長度不一時會**安靜地截斷**，多出來的欄位保留原名、
            # 之後被 reindex 填成 0——版面一改就整批變成假的 0
            self.check_column_count(
                df, len(new_col_name), f"TPEX chip {date}（依位置命名）"
            )
            rename_map: Dict[str, str] = dict(zip(old_col_name, new_col_name))
            df = df.rename(columns=rename_map)

        aligned_df: pd.DataFrame = df.reindex(
            columns=self.chip_cleaned_cols, fill_value=0
        )
        aligned_df = DataUtils.convert_col_to_numeric(
            aligned_df, exclude_cols=["date", "stock_id", "證券名稱"]
        )
        aligned_df = DataUtils.fill_nan(aligned_df, 0)

        # 中段（第一次到第二次改制之間）來源只有「自行買賣／避險」兩組拆分欄、
        # 沒有買進與賣出的合計欄；不補的話 reindex 把合計填成 0，庫裡就出現
        # 「買賣皆 0、買賣超非 0」。放在轉成數值之後：拆分欄此時保證是數字，
        # 不必依賴爬蟲那一端有沒有把千分位解析掉
        if self.tpex_first_reform_date <= date < self.tpex_second_reform_date:
            self.combine_dealer_columns(aligned_df)

        # 根據指定 columns 移除重複的 rows
        aligned_df = DataUtils.remove_duplicate_rows(
            df=aligned_df,
            subset=["date", "stock_id", "證券名稱"],
            keep="first",
        )

        # Save df to csv file
        aligned_df.to_csv(
            self.chip_dir / f"tpex_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return aligned_df
