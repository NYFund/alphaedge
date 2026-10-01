import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import DAY_TRADE_LIST_DOWNLOADS_PATH, SHORT_SALE_LIST_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.utils import TimeUtils

"""
Stock Trading List Cleaner：把兩份交易所資格名單收斂成各自一組欄位

- 註記欄一律轉成 0／1：站方以 `*`、`Y` 或全形 `＊` 表示「有」，空白表示「沒有」，
  三種寫法都出現過（TPEX 當沖名單用全形）。
- **欄位依位置命名**：2013～2026 逐年抽樣，平盤下名單兩個市場固定 5 欄、
  上市當沖名單固定 6 欄；**上櫃當沖名單早年只有 2 欄**（2014-01-06 實測沒有
  「暫停先賣後買」註記欄，2016 起為 3 欄）——當時還沒開放先賣後買，註記一律為 0。
"""


class StockTradingListCleaner(BaseDataCleaner):
    """Stock Trading List Cleaner (Transform)"""

    SHORT_SALE_RAW_COLS: List[str] = [
        "stock_id",
        "證券名稱",
        "暫停融券賣出",
        "暫停借券賣出",
        "禁止平盤下融借券賣出",
    ]
    SHORT_SALE_FLAG_COLS: List[str] = SHORT_SALE_RAW_COLS[2:]

    # 上市當沖名單後三欄是當沖成交量值，只用名單、不入庫
    TWSE_DAY_TRADE_RAW_COLS: List[str] = [
        "stock_id",
        "證券名稱",
        "暫停先賣後買當沖",
        "當沖成交股數",
        "當沖買進成交金額",
        "當沖賣出成交金額",
    ]
    TPEX_DAY_TRADE_RAW_COLS: List[str] = ["stock_id", "證券名稱", "暫停先賣後買當沖"]
    DAY_TRADE_FLAG_COLS: List[str] = ["暫停先賣後買當沖"]

    # 合法證券代號樣式（濾掉合計、說明等非個股列）
    STOCK_ID_PATTERN: str = r"[0-9A-Z]{4,6}"

    def __init__(self) -> None:
        super().__init__()

        self.short_sale_dir: Path = SHORT_SALE_LIST_DOWNLOADS_PATH
        self.day_trade_dir: Path = DAY_TRADE_LIST_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        self.short_sale_dir.mkdir(parents=True, exist_ok=True)
        self.day_trade_dir.mkdir(parents=True, exist_ok=True)

    def clean_twse_short_sale_list(
        self, df: pd.DataFrame, date: datetime.date
    ) -> Optional[pd.DataFrame]:
        """Clean TWSE 平盤下得融（借）券賣出名單"""

        return self.clean_list(
            df,
            date,
            self.SHORT_SALE_RAW_COLS,
            self.SHORT_SALE_FLAG_COLS,
            self.short_sale_dir / f"twse_{TimeUtils.format_date(date)}.csv",
        )

    def clean_tpex_short_sale_list(
        self, df: pd.DataFrame, date: datetime.date
    ) -> Optional[pd.DataFrame]:
        """Clean TPEX 平盤下得融（借）券賣出名單"""

        return self.clean_list(
            df,
            date,
            self.SHORT_SALE_RAW_COLS,
            self.SHORT_SALE_FLAG_COLS,
            self.short_sale_dir / f"tpex_{TimeUtils.format_date(date)}.csv",
        )

    def clean_twse_day_trade_list(
        self, df: pd.DataFrame, date: datetime.date
    ) -> Optional[pd.DataFrame]:
        """Clean TWSE 現股當沖標的名單"""

        return self.clean_list(
            df,
            date,
            self.TWSE_DAY_TRADE_RAW_COLS,
            self.DAY_TRADE_FLAG_COLS,
            self.day_trade_dir / f"twse_{TimeUtils.format_date(date)}.csv",
        )

    def clean_tpex_day_trade_list(
        self, df: pd.DataFrame, date: datetime.date
    ) -> Optional[pd.DataFrame]:
        """Clean TPEX 現股當沖標的名單（早年沒有註記欄，見模組說明）"""

        raw_cols: List[str] = self.TPEX_DAY_TRADE_RAW_COLS
        if df is not None and len(df.columns) == len(raw_cols) - 1:
            raw_cols = raw_cols[:-1]

        return self.clean_list(
            df,
            date,
            raw_cols,
            self.DAY_TRADE_FLAG_COLS,
            self.day_trade_dir / f"tpex_{TimeUtils.format_date(date)}.csv",
        )

    def clean_list(
        self,
        df: pd.DataFrame,
        date: datetime.date,
        raw_cols: List[str],
        flag_cols: List[str],
        output_path: Path,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            兩份名單、兩個市場共用的清洗流程：依位置命名 → 濾非個股列 →
            註記轉 0／1 → 去重 → 寫 CSV
        - Parameters:
            - df: pd.DataFrame
                爬蟲取得的原始表格
            - date: datetime.date
                資料日期
            - raw_cols: List[str]
                原始表格的欄位名稱（依位置）
            - flag_cols: List[str]
                要入庫的註記欄；原始表沒有的註記欄補 0
            - output_path: Path
                輸出 CSV 路徑
        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；原始表為空或無有效資料時回傳 None
        - Raise:
            - ColumnLayoutError
                欄位數不符（來源版面改制）
        """

        if df is None or df.empty:
            return None

        # 欄位數不符代表版面改制，拋例外讓那一天記為失敗，而不是錯位入庫
        self.check_column_count(df, len(raw_cols), f"{output_path.stem} {date}")

        df = df.copy()
        df.columns = raw_cols

        stock_id: pd.Series = df["stock_id"].astype(str).str.strip()
        df = df[stock_id.str.fullmatch(self.STOCK_ID_PATTERN).fillna(False)].copy()
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
        df["證券名稱"] = df["證券名稱"].astype(str).str.strip()

        if df.empty:
            logger.warning(f"No valid rows in {output_path.stem} on {date}")
            return None

        for col in flag_cols:
            if col in df.columns:
                df[col] = df[col].fillna("").astype(str).str.strip().ne("").astype(int)
            else:
                df[col] = 0

        df.insert(0, "date", date)
        cleaned: pd.DataFrame = df[["date", "stock_id", "證券名稱", *flag_cols]]
        cleaned = DataUtils.remove_duplicate_rows(
            df=cleaned, subset=["date", "stock_id"], keep="first"
        )

        cleaned.to_csv(output_path, index=False)
        return cleaned
