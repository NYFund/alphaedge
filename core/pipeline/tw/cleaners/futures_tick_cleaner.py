import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_TICK_DOWNLOADS_PATH
from core.market.tw.futures_calendar import FuturesCalendar
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.utils import FuturesSession

"""
台期貨 Tick 清洗器

**與台股 tick 的兩個差異**：

1. **識別欄是「商品 ＋ 到期月」不是單一代號**：同一天同一商品有多個契約，
   只存 `symbol` 的話下游得自己拆字串。故一律拆成 `product` ／ `expiry` 兩欄，
   與 `futures_price_daily` 的主鍵一致。

2. **要標出時段**：Shioaji 回的是整個交易日的逐筆（含前一日 15:00 開始的夜盤），
   時段得由**時間戳**判定（`FuturesCalendar.resolve_session()`）。
   不標的話，日盤策略會吃到夜盤的成交而完全不知情——夜盤的量能與價格行為
   與日盤差很多。

⚠️ **非交易時段的成交會被標成 None**：13:45~15:00 之間理論上沒有成交，
真的出現代表資料或時區有問題，故保留該列但把 `session` 留空，讓它在下游顯眼。
"""


class FuturesTickCleaner(BaseDataCleaner):
    """把 Shioaji 的期貨逐筆成交清成可入庫的格式"""

    # 入庫欄位順序（與 DolphinDB 的表結構一致）
    COLUMNS: List[str] = [
        "product",
        "expiry",
        "session",
        "time",
        "close",
        "volume",
        "bid_price",
        "bid_volume",
        "ask_price",
        "ask_volume",
        "tick_type",
    ]

    def __init__(self) -> None:
        super().__init__()

        self.tick_dir: Path = FUTURES_TICK_DOWNLOADS_PATH
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        self.tick_dir.mkdir(parents=True, exist_ok=True)

    def clean(
        self, df: pd.DataFrame, product: str, expiry: str
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗單一契約單日的逐筆成交

            **時間戳無效的列直接丟掉並記數**：tick 的時間是它唯一的排序依據，
            補值或猜測都會讓成交順序錯亂。
        - Parameters:
            - df: pd.DataFrame
                Shioaji 回傳的原始 ticks
            - product / expiry: str
                TAIFEX 的商品代碼與到期月
        - Return:
            - Optional[pd.DataFrame]
                清洗後的資料；無有效列時為 None
        """

        if df is None or df.empty:
            return None

        cleaned: pd.DataFrame = df.copy()
        cleaned["time"] = pd.to_datetime(cleaned.get("ts"), errors="coerce")

        invalid: int = int(cleaned["time"].isna().sum())
        if invalid:
            logger.warning(
                f"[Futures Tick] {product}{expiry}：{invalid} 列時間戳無效，已丟棄"
            )
            cleaned = cleaned.dropna(subset=["time"])

        if cleaned.empty:
            return None

        cleaned["product"] = product
        cleaned["expiry"] = expiry
        cleaned["session"] = cleaned["time"].map(self.resolve_session)

        for column in self.COLUMNS:
            if column not in cleaned.columns:
                cleaned[column] = None

        return cleaned[self.COLUMNS].reset_index(drop=True)

    @staticmethod
    def resolve_session(moment: datetime.datetime) -> Optional[str]:
        """
        由時間戳判定時段

        **非交易時段回 None 而不是猜一個**：13:45~15:00 之間理論上沒有成交，
        真的出現代表資料或時區有問題，硬歸到某個時段只會把問題藏起來。
        """

        session: Optional[FuturesSession] = FuturesCalendar.resolve_session(moment)
        return None if session is None else session.value

    def save(
        self, df: pd.DataFrame, product: str, expiry: str, date: datetime.date
    ) -> Optional[Path]:
        """存成中繼檔（一契約一日一檔），供入庫與稽核"""

        if df is None or df.empty:
            return None

        path: Path = self.tick_dir / f"{product}{expiry}_{date.strftime('%Y%m%d')}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
