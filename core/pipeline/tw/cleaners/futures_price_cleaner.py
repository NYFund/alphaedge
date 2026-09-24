import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_PRICE_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.data_utils import DataUtils
from core.utils import FuturesSession, TimeUtils

"""
Futures Price Cleaner：把日盤與夜盤兩種版面的 TAIFEX 行情表收斂為同一組欄位。

1. 兩種版面（2026-08-29 實測）
    - 日盤 17 欄：成交量拆成「盤後／一般／合計」三欄，有結算價與未沖銷契約量。
    - 夜盤 15 欄：成交量僅一欄，**結算價與未沖銷契約量皆為 `-`**（屬日結數字，
      於日盤時段才產出）。
    欄位順序固定，故一律以「位置」重新命名，不依賴來源網站的標題文字。

2. **`-` 一律清成 NaN，不可填 0**
    這是本 cleaner 與 `stock_margin_cleaner` 最大的差異——後者以 `fill_nan(df, 0)`
    收尾是安全的（餘額為 0 有意義），但期貨的結算價填 0 會讓損益與維持率整段歸零，
    而且不會有任何徵兆。「這根 bar 沒有結算價」與「結算價是 0」是兩件事，
    必須由 NULL 表達。

3. 每一列的成交量是**該時段自己的量**
    日盤取「一般交易時段成交量」而非「合計成交量」：日盤與夜盤各存一列，
    取合計會讓兩列相加時把夜盤的量算兩次。

4. `到期月份` 保持字串
    月契約 `202609`、週契約 `202609W1`，兩者只能都當字串（crawler 已以
    converters 保留，此處僅確保不被後續處理轉型）。
"""


class FuturesPriceCleaner(BaseDataCleaner):
    """Futures Price Cleaner (Transform)"""

    # 日盤原始表格欄位（依位置，共 17 欄）
    DAY_RAW_COLS: List[str] = [
        "product",
        "expiry",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",  # 來源稱「最後成交價」；此處對齊 price 表的命名
        "漲跌價",
        "漲跌幅",
        "盤後成交量",
        "一般成交量",
        "合計成交量",
        "結算價",
        "未沖銷契約量",
        "最後最佳買價",
        "最後最佳賣價",
        "歷史最高價",
        "歷史最低價",
    ]

    # 夜盤原始表格欄位（依位置，共 15 欄）
    NIGHT_RAW_COLS: List[str] = [
        "product",
        "expiry",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "漲跌價",
        "漲跌幅",
        "成交量",
        "結算價",  # 恆為 `-`
        "未沖銷契約量",  # 恆為 `-`
        "最後最佳買價",
        "最後最佳賣價",
        "歷史最高價",
        "歷史最低價",
    ]

    # 各時段的成交量來源欄位
    VOLUME_COL: dict = {
        FuturesSession.DAY.value: "一般成交量",
        FuturesSession.NIGHT.value: "成交量",
    }

    # 不入庫的欄位：漲跌價／幅可由前一根 bar 推得；歷史高低為累計參考值，
    # 兩者都不是「這一天發生了什麼」，留著只會讓表變寬
    DROP_COLS: List[str] = ["漲跌價", "漲跌幅", "歷史最高價", "歷史最低價"]

    # 來源以 `-` 表示「此欄在本時段不存在」，清成 NaN 後由 loader 存為 NULL
    NULL_TOKENS: List[str] = ["-", "－", ""]

    # 合法商品代碼樣式（用於濾掉小計、說明列）
    PRODUCT_PATTERN: str = r"[0-9A-Z]{2,10}"

    def __init__(self) -> None:
        super().__init__()

        # Futures Price DataFrame Cleaned Columns
        self.futures_price_cleaned_cols: Optional[List[str]] = None

        # Downloads directory Path
        self.futures_price_dir: Path = FUTURES_PRICE_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # 主鍵為 (date, product, expiry, session)：同一天同一契約的日盤與夜盤
        # 是兩筆獨立行情，少了 session 會互相覆蓋
        self.futures_price_cleaned_cols = [
            "date",
            "product",
            "expiry",
            "session",
            "開盤價",
            "最高價",
            "最低價",
            "收盤價",
            "成交量",
            "結算價",
            "未沖銷契約量",
            "最後最佳買價",
            "最後最佳賣價",
        ]

        self.futures_price_dir.mkdir(parents=True, exist_ok=True)

    def clean_futures_price(
        self,
        df: pd.DataFrame,
        date: datetime.date,
        product: str,
        session: FuturesSession,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            單一商品、單一時段、單日行情的清洗
        - Parameters:
            - df: pd.DataFrame
                crawler 取得的原始表格
            - date: datetime.date
                資料日期
            - product: str
                商品代碼（Ex: TX）
            - session: FuturesSession
                交易時段
        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；欄位數不符或無有效資料時回傳 None
        """

        if df is None or df.empty:
            return None

        raw_cols: List[str] = (
            self.DAY_RAW_COLS if session == FuturesSession.DAY else self.NIGHT_RAW_COLS
        )

        # 欄位數不符代表來源版面改制，直接中止避免錯位入庫
        if df.shape[1] != len(raw_cols):
            logger.warning(
                f"Unexpected futures price table structure on {date} "
                f"{product} {session.value}: {df.shape[1]} columns "
                f"(expected {len(raw_cols)})"
            )
            return None

        df = df.copy()
        df.columns = raw_cols

        # 濾掉小計與說明列
        product_col: pd.Series = df["product"].astype(str).str.strip()
        df = df[product_col.str.fullmatch(self.PRODUCT_PATTERN).fillna(False)].copy()

        if df.empty:
            logger.warning(f"No valid futures price rows on {date} {product}")
            return None

        df["product"] = df["product"].astype(str).str.strip()
        # 到期月份可能是 202609 或 202609W1，一律當字串處理
        df["expiry"] = df["expiry"].astype(str).str.strip()

        df["成交量"] = df[self.VOLUME_COL[session.value]]
        df.insert(0, "date", date)
        df["session"] = session.value

        aligned_df: pd.DataFrame = df.reindex(columns=self.futures_price_cleaned_cols)
        aligned_df = self.to_null(aligned_df)
        aligned_df = DataUtils.convert_col_to_numeric(
            aligned_df, exclude_cols=["date", "product", "expiry", "session"]
        )

        # 成交量缺值才填 0（沒有成交就是 0 口）；**價格欄位一律保留 NaN**：
        # 結算價填 0 會讓損益與維持率整段歸零，且不會有任何徵兆
        aligned_df["成交量"] = aligned_df["成交量"].fillna(0).astype(int)

        aligned_df = DataUtils.remove_duplicate_rows(
            df=aligned_df,
            subset=["date", "product", "expiry", "session"],
            keep="first",
        )

        aligned_df.to_csv(
            self.futures_price_dir
            / f"{product}_{session.value}_{TimeUtils.format_date(date)}.csv",
            index=False,
        )

        return aligned_df

    @classmethod
    def to_null(cls, df: pd.DataFrame) -> pd.DataFrame:
        """
        - Description:
            把來源用來表示「此欄不存在」的符號（`-` 等）換成 NaN

            **不可改用 fillna(0)**：夜盤的結算價與未沖銷契約量本來就不存在，
            填 0 會讓「沒有結算價」變成「結算價是 0」，而後者會讓損益與
            維持率整段歸零且無任何徵兆。
        - Parameters:
            - df: pd.DataFrame
                已對齊欄位的 DataFrame
        - Return:
            - pd.DataFrame
                `-` 已轉為 NaN 的 DataFrame
        """

        return df.replace(cls.NULL_TOKENS, pd.NA)
