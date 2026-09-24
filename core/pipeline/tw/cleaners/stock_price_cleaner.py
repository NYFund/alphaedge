import datetime
from pathlib import Path
from typing import List

import pandas as pd

from core.config import PRICE_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.utils import TimeUtils

"""
收盤行情清洗

**無成交日的價格欄保持 NULL，不填 0**：來源給的是 `--`，
轉數值後是 NaN，填成 0 之後就變成「當天成交價是 0 元」——一個看起來完全正常的
假價格。下游由 `StockQuoteAdapter` 濾掉無價的列。

上櫃的欄位是**依位置**命名的（來源不給欄名），故命名前一定要先檢查欄位數：
版面一改，位置命名會把每一欄都對到錯的名字，而且完全不會報錯。
"""


class StockPriceCleaner(BaseDataCleaner):
    """Stock Price Cleaner (Transform)"""

    # 上櫃從此日起 csv 欄位格式不同（109/4/30）
    TPEX_TABLE_CHANGE_DATE: datetime.date = datetime.date(2020, 4, 30)

    # 無成交時維持 NULL 的欄位；成交量／金額／筆數不在此列，填 0 是正確的
    PRICE_COLUMNS: List[str] = [
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "最後揭示買價",
        "最後揭示賣價",
    ]

    # 上櫃依位置命名時的欄位數（改制前後各一組）
    TPEX_COLUMN_COUNT_AFTER_CHANGE: int = 15
    TPEX_COLUMN_COUNT_BEFORE_CHANGE: int = 13

    def __init__(self) -> None:
        super().__init__()

        self.tpex_table_change_date: datetime.date = self.TPEX_TABLE_CHANGE_DATE

        # Downloads directory Path
        self.price_dir: Path = PRICE_DOWNLOADS_PATH
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # Generate downloads directory
        self.price_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def apply_change_sign(sign: pd.Series, change: pd.Series) -> pd.Series:
        """
        - Description:
            把 TWSE 的 `漲跌(+/-)` 欄併進 `漲跌價差`

            來源的符號欄有三種值：`+`、`-`、`X`（除權息，該列價差為 0）。
            只有 `-` 需要轉負，其餘原樣。
        - Parameters:
            - sign: pd.Series
                `漲跌(+/-)` 欄
            - change: pd.Series
                `漲跌價差` 欄（絕對值）
        - Return:
            - pd.Series
                帶正負號的漲跌價差
        """

        values: pd.Series = pd.to_numeric(change, errors="coerce").abs()
        is_down: pd.Series = sign.astype(str).str.strip() == "-"
        return values.where(~is_down, -values)

    def clean_twse_price(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> pd.DataFrame:
        """
        - Description:
            清洗 TWSE 收盤行情（站方提供 2004/2/11 起的資料）
        - Parameters:
            - df: pd.DataFrame
                crawler 取得的原始表格
            - date: datetime.date
                資料日期
        - Return:
            - pd.DataFrame
                清洗後的收盤行情
        """

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(0)

        # **正負號只在 `漲跌(+/-)` 這一欄**：`漲跌價差` 本身是絕對值，直接丟掉
        # 這一欄會讓下跌日在庫裡變成上漲。上櫃的 `漲跌價差` 自帶正負號，
        # 不先併進來，同一張表的上市、上櫃語意就不一樣。
        # （`X` 代表除權息，該列的 `漲跌價差` 一律是 0，不受影響）
        df["漲跌價差"] = self.apply_change_sign(df["漲跌(+/-)"], df["漲跌價差"])

        df: pd.DataFrame = (
            df.drop(columns=["漲跌(+/-)"])
            .rename(columns={"證券代號": "stock_id"})
            .astype(str)
            .pipe(
                DataUtils.convert_col_to_numeric,
                exclude_cols=["date", "stock_id", "證券名稱"],
            )
        )
        df.insert(0, "date", date)
        DataUtils.move_col(df, "成交股數", "漲跌價差")
        DataUtils.move_col(df, "成交金額", "成交股數")
        DataUtils.move_col(df, "成交筆數", "成交金額")

        # 根據指定 columns 移除重複的 rows
        df = DataUtils.remove_duplicate_rows(
            df=df,
            subset=["date", "stock_id", "證券名稱"],
            keep="first",
        )

        # 價格欄維持 NaN（入庫為 NULL），其餘填 0
        df = DataUtils.fill_nan(df, 0, exclude_cols=self.PRICE_COLUMNS)

        df.to_csv(
            self.price_dir / f"twse_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return df

    def clean_tpex_price(
        self,
        df: pd.DataFrame,
        date: datetime.date,
    ) -> pd.DataFrame:
        """
        - Description:
            清洗 TPEX 收盤行情（站方提供民國 96/7/2 起的資料）

            民國 109/4/30 起 csv 的欄位版面改制，兩種版面的欄位數不同，
            故依 `date` 選用對應的欄位數與欄名。
        - Parameters:
            - df: pd.DataFrame
                crawler 取得的原始表格
            - date: datetime.date
                資料日期
        - Return:
            - pd.DataFrame
                清洗後的收盤行情
        """

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(0)

        df: pd.DataFrame = df.drop(
            columns=["發行股數", "次日漲停價", "次日跌停價"]
        ).astype(str)
        df.insert(0, "date", date)

        expected_columns: int = (
            self.TPEX_COLUMN_COUNT_AFTER_CHANGE
            if date >= self.tpex_table_change_date
            else self.TPEX_COLUMN_COUNT_BEFORE_CHANGE
        )
        self.check_column_count(df, expected_columns, f"TPEX price {date}")

        if date >= self.tpex_table_change_date:
            df.columns = [
                "date",
                "stock_id",
                "證券名稱",
                "收盤價",
                "漲跌價差",
                "開盤價",
                "最高價",
                "最低價",
                "成交股數",
                "成交金額",
                "成交筆數",
                "最後揭示買價",
                "最後揭示買量",
                "最後揭示賣價",
                "最後揭示賣量",
            ]
        else:
            df.columns = [
                "date",
                "stock_id",
                "證券名稱",
                "收盤價",
                "漲跌價差",
                "開盤價",
                "最高價",
                "最低價",
                "成交股數",
                "成交金額",
                "成交筆數",
                "最後揭示買價",
                "最後揭示賣價",
            ]
        DataUtils.move_col(df, "收盤價", "最低價")
        DataUtils.move_col(df, "漲跌價差", "收盤價")
        df = DataUtils.remove_last_n_rows(df, n_rows=2)
        df = DataUtils.convert_col_to_numeric(
            df, exclude_cols=["date", "stock_id", "證券名稱"]
        )

        # 根據指定 columns 移除重複的 rows
        df = DataUtils.remove_duplicate_rows(
            df=df,
            subset=["date", "stock_id", "證券名稱"],
            keep="first",
        )

        # 價格欄維持 NaN（入庫為 NULL），其餘填 0
        df = DataUtils.fill_nan(df, 0, exclude_cols=self.PRICE_COLUMNS)

        df.to_csv(
            self.price_dir / f"tpex_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return df
