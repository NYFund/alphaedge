import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_UNIVERSE_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.tw.crawlers.futures_stock_universe_crawler import (
    FuturesStockUniverseCrawler,
)
from core.utils import STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE, TimeUtils

"""
Futures Stock Universe Cleaner：把 TAIFEX 標的一覽表收斂成一列一商品的標的池快照。

1. 一律以「位置」重新命名 14 欄
    來源的標題文字帶換行與空白（`股票期貨、 選擇權 商品代碼`），照字面比對很脆弱；
    欄位順序則自 2026-08-29 實查以來固定，故沿用 `futures_price_cleaner` 的作法。

2. **主鍵是商品代碼，不是證券代號**
    同一檔標的可能有標準型與小型兩個商品（台積電 CD／QF、世紀鋼 RU／SW），
    以證券代號當鍵會讓其中一個被覆蓋掉。

3. 商品類型由「標準型證券股數／受益權單位」反推
    來源沒有類型欄位，見 `STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE` 的說明。
    出現沒見過的數量時**當場中止**，不猜也不歸類——那代表 TAIFEX 新增了商品類型。

4. 夜盤欄位的 `-` 清成 NaN
    2026-08-29 實查僅 6 檔股期／ETF 期有盤後交易時段（CC 聯電、CD 台積電、
    QF 小型台積電、NY 元大台灣50、SR 小型台灣50、RZ 元大美債20年），其餘皆為 `-`。
    也就是說：**股期絕大多數沒有夜盤**。
    填 0 或空字串會讓「沒有夜盤」與「夜盤時段未知」混為一談，故一律 NULL。

5. **不推導掛牌日／下市日**
    來源是當下快照，沒有這兩個欄位。清洗層只忠實記下「這一天看到這些」，
    掛牌／下市由 `futures_stock_universe` 的快照序列差分推得。
"""


class FuturesStockUniverseCleaner(BaseDataCleaner):
    """Futures Stock Universe Cleaner (Transform)"""

    # 標的一覽表原始欄位（依位置，共 14 欄）
    RAW_COLS: List[str] = [
        "base_code",
        "underlying_full_name",
        "underlying_stock_id",
        "underlying_name",
        "是否為股票期貨標的",
        "是否為股票選擇權標的",
        "是否為股票選擇權週契約標的",
        "上市普通股",
        "上櫃普通股",
        "上市ETF",
        "上櫃ETF",
        "contract_size",
        "day_session_time",
        "night_session_time",
    ]

    # 上市／上櫃判定欄位 → 掛牌板別；四欄互斥，同一列只會有一欄被標記
    LISTING_BOARD_COLS: dict = {
        "上市普通股": "上市",
        "上櫃普通股": "上櫃",
        "上市ETF": "上市",
        "上櫃ETF": "上櫃",
    }

    # 來源以 `-` 表示「沒有這個交易時段」
    NULL_TOKENS: List[str] = ["-", "－", ""]

    def __init__(self) -> None:
        super().__init__()

        # Futures Stock Universe DataFrame Cleaned Columns
        self.universe_cleaned_cols: Optional[List[str]] = None

        # Downloads directory Path
        self.universe_dir: Path = FUTURES_UNIVERSE_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # 主鍵為 (snapshot_date, product_id)：本表是快照序列，不是現況表
        self.universe_cleaned_cols = [
            "snapshot_date",
            "product_id",
            "base_code",
            "product_type",
            "underlying_stock_id",
            "underlying_name",
            "underlying_listing_board",
            "contract_size",
            "day_session_time",
            "night_session_time",
        ]

        self.universe_dir.mkdir(parents=True, exist_ok=True)

    def clean_stock_universe(
        self,
        df: pd.DataFrame,
        snapshot_date: datetime.date,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            標的一覽表的清洗
        - Parameters:
            - df: pd.DataFrame
                crawler 取得的原始表格
            - snapshot_date: datetime.date
                快照日期（＝ 執行日，不是資料日期）
        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；欄位數不符或無有效資料時回傳 None
        """

        if df is None or df.empty:
            return None

        # 欄位數不符代表來源版面改制，直接中止避免錯位入庫
        if df.shape[1] != len(self.RAW_COLS):
            logger.warning(
                f"Unexpected futures stock universe table structure: "
                f"{df.shape[1]} columns (expected {len(self.RAW_COLS)})"
            )
            return None

        df = df.copy()
        df.columns = self.RAW_COLS

        # 濾掉最後一列的「標的合計數」小計列
        df["base_code"] = df["base_code"].astype(str).str.strip()
        df = df[
            df["base_code"].map(FuturesStockUniverseCrawler.is_valid_base_code)
        ].copy()

        if df.empty:
            logger.warning("No valid futures stock universe rows")
            return None

        df["snapshot_date"] = snapshot_date
        df["product_id"] = df["base_code"].map(
            FuturesStockUniverseCrawler.to_commodity_id
        )
        df["underlying_stock_id"] = df["underlying_stock_id"].astype(str).str.strip()
        df["underlying_name"] = df["underlying_name"].astype(str).str.strip()
        df["underlying_listing_board"] = self.resolve_listing_board(df)

        contract_size: Optional[pd.Series] = self.resolve_contract_size(df)
        if contract_size is None:
            return None
        df["contract_size"] = contract_size
        df["product_type"] = contract_size.map(STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE)

        aligned_df: pd.DataFrame = df.reindex(columns=self.universe_cleaned_cols)
        # 交易時段的 `-` 代表沒有該時段，須以 NULL 表達：填 0 或空字串會讓
        # 「沒有夜盤」與「夜盤時段未知」混為一談
        aligned_df[["day_session_time", "night_session_time"]] = aligned_df[
            ["day_session_time", "night_session_time"]
        ].replace(self.NULL_TOKENS, pd.NA)

        aligned_df.to_csv(
            self.universe_dir
            / f"futures_stock_universe_{TimeUtils.format_date(snapshot_date)}.csv",
            index=False,
        )

        return aligned_df

    @classmethod
    def resolve_listing_board(cls, df: pd.DataFrame) -> pd.Series:
        """
        - Description:
            由四個標記欄位判定標的的掛牌板別（上市／上櫃）

            來源以 `◎ 是上市普通股標的證券` 這類文字標記，未標記者為空字串
            （crawler 已關掉 `keep_default_na`，故不是 NaN）。四欄互斥。
        - Parameters:
            - df: pd.DataFrame
                已依位置命名的 DataFrame
        - Return:
            - pd.Series
                掛牌板別；四欄皆未標記時為 NaN
        """

        board: pd.Series = pd.Series(pd.NA, index=df.index, dtype="object")

        for col, name in cls.LISTING_BOARD_COLS.items():
            marked: pd.Series = df[col].astype(str).str.strip().ne("")
            board = board.mask(marked & board.isna(), name)

        if board.isna().any():
            logger.warning(
                f"[Futures Universe] {int(board.isna().sum())} 檔標的的掛牌板別無法判定"
            )

        return board

    @classmethod
    def resolve_contract_size(cls, df: pd.DataFrame) -> Optional[pd.Series]:
        """
        - Description:
            取出標準型證券股數／受益權單位並轉成整數

            **出現未登錄的數量就整批中止**：那代表 TAIFEX 新增了商品類型，
            靜靜歸到某個既有類型會讓下游拿錯契約單位算 PnL，比中斷難查得多。
        - Parameters:
            - df: pd.DataFrame
                已依位置命名的 DataFrame
        - Return:
            - Optional[pd.Series]
                契約單位；有未登錄的數量時回傳 None
        """

        size: pd.Series = pd.to_numeric(df["contract_size"], errors="coerce")

        if size.isna().any():
            logger.warning(
                f"[Futures Universe] {int(size.isna().sum())} 檔標的缺少契約單位，"
                f"無法判定商品類型"
            )
            return None

        size = size.astype(int)
        unknown: set = set(size.unique()) - set(STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE)
        if unknown:
            logger.warning(
                f"[Futures Universe] 出現未登錄的契約單位 {sorted(unknown)}；"
                f"TAIFEX 可能新增了商品類型，請先確認後再登錄 "
                f"STOCK_FUTURES_TYPE_BY_CONTRACT_SIZE"
            )
            return None

        return size
