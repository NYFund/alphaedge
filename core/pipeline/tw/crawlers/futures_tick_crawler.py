import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import shioaji as sj
from loguru import logger

from core.config import FUTURES_TICK_DOWNLOADS_PATH
from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.utils import SHIOAJI_FUTURES_CATEGORY
from core.utils.log_manager import LogManager

"""
台期貨 Tick 爬蟲（Shioaji）

**與台股 tick 的三個差異**，每一個都會讓沿用股票寫法的人抓不到資料：

1. **契約代碼兩邊不一樣，且沒有規律**：小型臺指在 TAIFEX 是 `MTX`、在 Shioaji 是
   `MXF`；電子期貨是 `TE` vs `EXF`。對照表 `SHIOAJI_FUTURES_CATEGORY` 是
   2026-09-02 實際登入列出契約逐一核對的，**不是從命名規則推的**。

2. **要指定到「哪一個到期月」**：股票的 `api.Contracts.Stocks.get("2330")` 一個代號
   就定位得了；期貨要在分類裡比對 `delivery_month`（`YYYYMM`）找出那一個契約。
   契約的 `code`（`TXFI6`）跨年會重複，不能拿來定位月份。

3. **日盤與夜盤在同一天的資料裡**：Shioaji 回的 ticks 涵蓋整個交易日
   （含前一日 15:00 開始的夜盤），時段的切分要靠時間戳，見
   `FuturesCalendar.resolve_session()`。

**配額**：Shioaji 的資料 API 有每日流量上限，且**期貨 tick 一天的量比股票大得多**
（TX 近月一天數十萬筆）。配額檢查與多組金鑰輪替沿用 `StockTickUpdater` 的作法。
"""


class FuturesTickCrawler(BaseDataCrawler):
    """透過 Shioaji 爬取期貨契約的逐筆成交"""

    def __init__(self) -> None:
        super().__init__()

        self.tick_dir: Path = FUTURES_TICK_DOWNLOADS_PATH
        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""

        LogManager.setup_logger("crawl_futures_tick.log")
        self.tick_dir.mkdir(parents=True, exist_ok=True)

    def crawl(self) -> None:
        """Crawl Tick Data"""
        pass

    @staticmethod
    def _find_contract(api: sj.Shioaji, category: str, expiry: str) -> Optional[Any]:
        """
        在分類裡找到期月份相符的契約；查不到回 None

        連續月別名（`TXFR1`／`TXFR2`）與當月契約同月份，要排除，
        否則抓到的是別名而不是實際月份的那一檔。
        """

        group: Optional[Any] = getattr(api.Contracts.Futures, category, None)
        if group is None:
            return None
        return next(
            (
                contract
                for contract in group
                if str(getattr(contract, "delivery_month", "")) == expiry
                and not str(getattr(contract, "code", "")).endswith(("R1", "R2"))
            ),
            None,
        )

    @staticmethod
    def to_shioaji_symbol(product: str, expiry: str) -> Optional[str]:
        """
        - Description:
            把 TAIFEX 的 `(商品, 到期月)` 轉成 Shioaji 的契約 `symbol`

            **用 `symbol` 不用 `code`**：`code`（`TXFI6`）是「月份字母 ＋ 年末碼」，
            字母碼每 10 年重複一次，跨年回補會取到錯誤年份的契約；
            `symbol`（`TXF202609`）則是完整年月，沒有這個問題。
        - Parameters:
            - product: str
                TAIFEX 商品代碼（Ex: TX）
            - expiry: str
                到期月（`YYYYMM`；週契約帶 W 尾碼者不支援，見下）
        - Return:
            - Optional[str]
                Shioaji 的契約 symbol；商品未對照或為週契約時為 None
        """

        category: Optional[str] = SHIOAJI_FUTURES_CATEGORY.get(product)
        if category is None:
            logger.warning(
                f"[Futures Tick] {product} 沒有對照到 Shioaji 分類代碼，"
                f"請先查證後登錄 SHIOAJI_FUTURES_CATEGORY（不要用猜的）"
            )
            return None

        # 週契約在 Shioaji 是**另一個分類**（小型臺指週選為 MX1／MX2…），
        # 不是同一分類的不同到期月；硬拼會得到一個不存在的 symbol
        if "W" in expiry.upper():
            logger.warning(
                f"[Futures Tick] {product} {expiry} 是週契約，"
                f"Shioaji 以獨立分類代碼表示，本層暫不支援"
            )
            return None

        return f"{category}{expiry}"

    def crawl_futures_tick(
        self,
        api: sj.Shioaji,
        date: datetime.date,
        product: str,
        expiry: str,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            取得單一契約單日的逐筆成交

            **配額檢查在呼叫端**（與 `StockTickCrawler` 一致）：多商品多日回補時
            由 updater 統一管理，分散在此會讓每一次呼叫都要重查一次用量。
        - Parameters:
            - api: sj.Shioaji
                已登入的 Shioaji API
            - date: datetime.date
                交易日
            - product / expiry: str
                TAIFEX 的商品代碼與到期月
        - Return:
            - Optional[pd.DataFrame]
                逐筆成交；查無資料或契約不存在時為 None
        """

        symbol: Optional[str] = self.to_shioaji_symbol(product, expiry)
        if symbol is None:
            return None

        category: str = SHIOAJI_FUTURES_CATEGORY[product]

        contract: Optional[Any] = self._find_contract(api, category, expiry)

        if contract is None:
            # 契約已到期或尚未掛牌時 Shioaji 查不到它，那是正常狀態
            logger.info(f"{date} {symbol}: contract not found")
            return None

        try:
            ticks = api.ticks(contract=contract, date=date.isoformat())
            tick_df: pd.DataFrame = pd.DataFrame({**ticks})
            return tick_df if not tick_df.empty else None
        except Exception as error:
            logger.error(f"Error crawling futures tick: {symbol} {date} | {error}")
            return None
