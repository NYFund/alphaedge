import datetime
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from loguru import logger

from core.adapters.quote_validation import has_valid_price, warn_duplicate_symbols
from core.config.schema import PriceColumn
from core.models import StockQuote, TickQuote
from core.utils import Scale
from core.utils.instrument import StockUtils

"""
台股的 raw → `StockQuote` 轉換

**一個級別一條路徑，沒有 `if scale ==`**：日線與 tick 的欄位語意完全不同
（tick 的 `volume` 原本就是張、日線要做股→張換算；tick 沒有 OHLC、日線有），
用同一個函式帶分支的話，每個讀者都得在腦內同時維持兩套語意，
而「不支援的級別」那條死路永遠測不到。

共用的只有與級別無關的規則（一般股過濾、價格驗證、重複代號），
它們住在 `core/adapters/quote_validation.py`——**不為了共用而保留分支**。
"""


class StockQuoteAdapter:
    """
    將不同資料型態（Tick Data 或 Day Data）轉換為統一格式的 StockQuote 物件

    兩個進入點各自完整：`from_day_rows()` 與 `from_tick_rows()`。
    """

    # === Tick ===
    @staticmethod
    def from_tick_rows(ticks: pd.DataFrame, date: datetime.date) -> List[StockQuote]:
        """
        - Description:
            把當日的 tick 表轉成 `StockQuote`

            **查詢由呼叫端負責**：本層是純轉換，自己查資料會讓
            `core.adapters` 相依 `core.api`，也讓測試為了驗一條轉換規則
            得先有連線或先造假 API。tick 一次只取一天，避免 RAM 爆掉，
            那個限制留在 feed 那一側。

            **不做一般股過濾，也不驗價格**：tick 表本來就只會有被訂閱的標的，
            而單一 tick 的成交價是交易所給的，沒有「無成交日」這種形態。
        - Parameters:
            - ticks: pd.DataFrame
                當日的 tick 表
            - date: datetime.date
                要轉換的日期
        - Returns:
            - List[StockQuote]
                轉換後的 StockQuote 物件列表
        """

        if ticks is None or ticks.empty:
            return []

        return [
            StockQuoteAdapter.to_tick_quote(tick, date)
            for tick in ticks.itertuples(index=False)
        ]

    @staticmethod
    def to_tick_quote(row: Any, date: datetime.date) -> StockQuote:
        """
        - Description:
            單筆 tick → `StockQuote`

            **`cur_price`／`close`／`volume` 一定要帶**：舊版只掛 `tick=tick_quote`，
            OHLC 與 `cur_price` 全部留在預設值 0.0，於是任何讀 `quote.close` 的
            地方（部位盯市、報表、策略）都拿到 0 元。

            **`open`／`high`／`low` 維持 0**：單一 tick 本來就沒有 OHLC，
            需要當日區間的地方（`FillModel`）自己累計 `intraday_range`。

            **`volume` 不做換算**：tick 表的單位原本就是張。
        - Parameters:
            - row: Any
                tick 表的一列
            - date: datetime.date
                要轉換的日期
        - Returns:
            - StockQuote
                轉換後的報價
        """

        tick_quote: TickQuote = TickQuote(
            stock_id=row.stock_id,
            time=row.time,
            close=row.close,
            volume=row.volume,
            bid_price=row.bid_price,
            bid_volume=row.bid_volume,
            ask_price=row.ask_price,
            ask_volume=row.ask_volume,
            tick_type=row.tick_type,
        )

        return StockQuote(
            stock_id=row.stock_id,
            scale=Scale.TICK,
            date=date,
            cur_price=tick_quote.close,
            volume=tick_quote.volume,
            close=tick_quote.close,
            tick=tick_quote,
        )

    # === Day ===
    @staticmethod
    def from_day_rows(
        price_df: pd.DataFrame,
        date: datetime.date,
        adjusted_close_map: Optional[Dict[str, Any]] = None,
    ) -> List[StockQuote]:
        """
        - Description:
            把當日的價格表轉成 `StockQuote`；還原價由呼叫端備好後傳入

            過濾兩層：非一般股（ETF、權證等）與無成交價的個股。
            還原價只掛在 `adj_close`，OHLC 一律維持原始成交價——
            未啟用時 `adj_close` 為空，`StockQuote.signal_close` 會退回 `close`。
        - Parameters:
            - price_df: pd.DataFrame
                當日全市場的價格表
            - date: datetime.date
                要轉換的日期
            - adjusted_close_map: Optional[Dict[str, Any]]
                `{stock_id: 還原收盤價}`；不還原時給 None
        - Returns:
            - List[StockQuote]
                轉換後的 StockQuote 物件列表
        """

        # Type: Pandas(date='2025-07-01', stock_id='0050', 證券名稱='元大台灣50',
        #              開盤價=48.38, 最高價=49.15, 最低價=48.38, 收盤價=48.64,
        #              成交股數=77081298, ...)
        rows: List[Any] = list(price_df.itertuples(index=False))
        return StockQuoteAdapter.from_day_records(rows, date, adjusted_close_map)

    @staticmethod
    def from_day_records(
        rows: List[Any],
        date: datetime.date,
        adjusted_close_map: Optional[Dict[str, Any]] = None,
    ) -> List[StockQuote]:
        """
        - Description:
            由**已經攤成列**的日線資料建立報價清單

            與 `from_day_rows()` 分開，是為了讓測試能直接餵幾個假列進來，
            不必先組一個 DataFrame。
        - Parameters:
            - rows: List[Any]
                日線資料的列（具備 `stock_id` 與 `PriceColumn` 的欄位）
            - date: datetime.date
                要轉換的日期
            - adjusted_close_map: Optional[Dict[str, Any]]
                `{stock_id: 還原收盤價}`
        - Returns:
            - List[StockQuote]
                轉換後的報價清單
        """

        all_stock_ids: List[str] = [row.stock_id for row in rows]

        # 過濾掉非一般股票（ETF、權證等）。
        # **一定要轉成 set**：這是每根 bar 都會跑的熱路徑，
        # 對 list 做 `in` 是線性掃描，1,000 檔就是每天百萬次比較
        filtered_stock_ids: Set[str] = set(
            StockUtils.filter_common_stocks(all_stock_ids)
        )
        adjusted: Dict[str, Any] = adjusted_close_map or {}

        tradable: List[Any] = []
        skipped: int = 0
        for row in rows:
            if row.stock_id not in filtered_stock_ids:
                continue
            if not has_valid_price(getattr(row, PriceColumn.CLOSE)):
                skipped += 1
                continue
            tradable.append(row)

        if skipped:
            logger.debug(f"{date}: 略過 {skipped} 檔無成交價的個股（無成交日）")

        quotes: List[StockQuote] = [
            StockQuoteAdapter.to_day_quote(row, date, adjusted.get(row.stock_id))
            for row in tradable
        ]

        warn_duplicate_symbols(quotes, date, source="Stock")
        return quotes

    @staticmethod
    def to_day_quote(
        row: Any, date: datetime.date, adj_close: Optional[float] = None
    ) -> StockQuote:
        """
        - Description:
            單列日線 → `StockQuote`

            **`volume` 要做股→張換算**：`price` 表的 `成交股數` 單位是股，
            而 `StockQuote.volume` 標註 `Unit: Lot`。少這一次換算會讓張數
            差 1000 倍，而「成交量 ≥ N 張」這類門檻會變成永遠成立或永遠不成立。
        - Parameters:
            - row: Any
                `price` 表的一列
            - date: datetime.date
                要轉換的日期
            - adj_close: Optional[float]
                還原收盤價；不還原時為 None
        - Returns:
            - StockQuote
                轉換後的報價
        """

        return StockQuote(
            stock_id=row.stock_id,
            scale=Scale.DAY,
            date=date,
            cur_price=getattr(row, PriceColumn.CLOSE),
            volume=StockUtils.convert_share_to_lot(getattr(row, PriceColumn.SHARES)),
            open=getattr(row, PriceColumn.OPEN),
            high=getattr(row, PriceColumn.HIGH),
            low=getattr(row, PriceColumn.LOW),
            close=getattr(row, PriceColumn.CLOSE),
            adj_close=adj_close,
        )
