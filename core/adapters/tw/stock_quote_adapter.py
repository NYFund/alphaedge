import datetime
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from loguru import logger

from core.config.schema import PriceColumn
from core.models import StockQuote, TickQuote
from core.utils import Scale
from core.utils.instrument import StockUtils


class StockQuoteAdapter:
    """
    將不同資料型態（Tick Data 或 Day Data）轉換為統一格式的 StockQuote 物件
    - 支援 Scale.TICK：從 tick dataframe 建立 TickQuote
    - 支援 Scale.DAY：從每日價格 dict 建立 StockQuote
    - 適用於回測框架中資料與策略之間的適配轉換
    """

    @staticmethod
    def from_tick_rows(ticks: pd.DataFrame, date: datetime.date) -> List[StockQuote]:
        """
        - Description:
            把當日的 tick 表轉成 `StockQuote`

            **查詢由呼叫端負責**：本層是純轉換，自己查資料會讓
            `core.adapters` 相依 `core.api`，也讓測試為了驗一條轉換規則
            得先有連線或先造假 API。tick 一次只取一天，避免 RAM 爆掉，
            那個限制留在 feed 那一側。
        - Parameters:
            - ticks: pd.DataFrame
                當日的 tick 表
            - date: datetime.date
                要轉換的日期
        - Returns:
            - List[StockQuote]
                轉換後的 StockQuote 物件列表
        """

        return StockQuoteAdapter.generate_stock_quotes(ticks, date, Scale.TICK)

    @staticmethod
    def from_day_rows(
        price_df: pd.DataFrame,
        date: datetime.date,
        adjusted_close_map: Optional[Dict[str, Any]] = None,
    ) -> List[StockQuote]:
        """
        - Description:
            把當日的價格表轉成 `StockQuote`；還原價由呼叫端備好後傳入
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
                Ex: [StockQuote(stock_id='0050', scale=Scale.DAY, date=datetime.date(2025, 7, 1), cur_price=48.64, volume=77081298, open=48.38, high=49.15, low=48.38, close=48.64, tick=None), StockQuote(stock_id='0051', scale=Scale.DAY, date=datetime.date(2025, 7, 1), cur_price=48.64, volume=77081298, open=48.38, high=49.15, low=48.38, close=48.64, tick=None), ...]
        """

        # 還原價只掛在 adj_close，OHLC 一律維持原始成交價；
        # 未啟用時 adj_close 為空，StockQuote.signal_close 會退回 close，行為零改變

        # Type: Pandas(date='2025-07-01', stock_id='0050', 證券名稱='元大台灣50', 開盤價=48.38, 最高價=49.15, 最低價=48.38, 收盤價=48.64, 漲跌價差=0.28, 成交股數=77081298, 成交金額=3767256390, 成交筆數=50311, 最後揭示買價=48.63, 最後揭示買量=89, 最後揭示賣價=48.64, 最後揭示賣量=104, 本益比=0.0)
        # Ex: [Pandas(date='2025-07-01', stock_id='0050',...), Pandas(date='2025-07-01', stock_id='0051',...), ...]
        price_rows: List[Any] = [row for row in price_df.itertuples(index=False)]

        return StockQuoteAdapter.generate_stock_quotes(
            price_rows, date, Scale.DAY, adjusted_close_map or {}
        )

    @staticmethod
    def generate_stock_quotes(
        data: pd.DataFrame | List[Any],
        date: datetime.date,
        scale: Scale,
        adjusted_close_map: Optional[Dict[str, Any]] = None,
    ) -> List[StockQuote]:
        """
        - Description:
            根據當日資料建立有效的 StockQuote 清單
        - Parameters:
            - data: pd.DataFrame | List[Any]
                當日資料
            - date: datetime.date
                要轉換的日期
            - scale: Scale
                要轉換的 Scale
                1. 支援 Scale.DAY（從價格欄位 Dict 建立）
                2. 支援 Scale.TICK（從 tick dataframe 建立）
        - Returns:
            - List[StockQuote]
                轉換後的 StockQuote 物件列表
                Ex: [StockQuote(stock_id='0050', scale=Scale.DAY, date=datetime.date(2025, 7, 1), cur_price=48.64, volume=77081298, open=48.38, high=49.15, low=48.38, close=48.64, tick=None), StockQuote(stock_id='0051', scale=Scale.DAY, date=datetime.date(2025, 7, 1), cur_price=48.64, volume=77081298, open=48.38, high=49.15, low=48.38, close=48.64, tick=None), ...]
        """

        if scale == Scale.TICK:
            if data.empty:
                return []

            return [
                StockQuoteAdapter.generate_stock_quote(tick, tick.stock_id, date, scale)
                for tick in data.itertuples(index=False)
            ]

        elif scale == Scale.DAY:
            all_stock_ids: List[str] = [stock.stock_id for stock in data]

            # 過濾掉非一般股票（ETF、權證等）。
            # **一定要轉成 set**：這是每根 bar 都會跑的熱路徑，
            # 對 list 做 `in` 是線性掃描，1,000 檔就是每天百萬次比較
            filtered_stock_ids: Set[str] = set(
                StockUtils.filter_common_stocks(all_stock_ids)
            )

            adjusted_close_map = adjusted_close_map or {}

            tradable: List[Any] = []
            skipped: int = 0
            for stock in data:
                if stock.stock_id not in filtered_stock_ids:
                    continue
                if not StockQuoteAdapter.has_valid_price(stock):
                    skipped += 1
                    continue
                tradable.append(stock)

            if skipped:
                logger.debug(f"{date}: 略過 {skipped} 檔無成交價的個股（無成交日）")

            quotes: List[StockQuote] = [
                StockQuoteAdapter.generate_stock_quote(
                    stock,
                    stock.stock_id,
                    date,
                    scale,
                    adjusted_close_map.get(stock.stock_id),
                )
                for stock in tradable
            ]

            StockQuoteAdapter.warn_duplicate_symbols(quotes, date)
            return quotes

    @staticmethod
    def warn_duplicate_symbols(quotes: List[StockQuote], date: datetime.date) -> None:
        """
        - Description:
            同一根 bar 內出現重複 symbol 時發出警告

            重複代表資料層無法唯一識別商品——例如上市股與上櫃 ETF 共用同一個
            4 碼代號。引擎後續會以 `{q.symbol: q for q in quotes}` 建對照表，
            重複的只會留下最後一筆，**成交價與訊號都可能取到另一檔商品**，
            而且整個過程不會有任何錯誤。

            這裡只警告不排除：要留哪一筆屬資料修正的範疇，靜默挑一筆才是更糟的選擇。
        - Parameters:
            - quotes: List[StockQuote]
                當根 bar 的報價
            - date: datetime.date
                當前交易日（僅供訊息辨識）
        """

        seen: Dict[str, int] = {}
        for quote in quotes:
            seen[quote.symbol] = seen.get(quote.symbol, 0) + 1

        duplicates: List[str] = [symbol for symbol, n in seen.items() if n > 1]
        if duplicates:
            logger.warning(
                f"[Quote] {date} 有 {len(duplicates)} 個代號對應多筆報價："
                f"{sorted(duplicates)[:10]}；建對照表時只會留下最後一筆，"
                f"請確認該代號是否被不同商品共用"
            )

    @staticmethod
    def has_valid_price(stock: Any) -> bool:
        """
        - Description:
            該列是否有可交易的成交價

            **無成交日的 OHLC 是 NULL（或歷史資料裡的 0）**：來源給的是 `--`，
            舊版 cleaner 填成 0 之後就變成「當天成交價是 0 元」，回測會照著它成交。
            cleaner 已改為保留 NULL，這裡把兩種形態一起濾掉，
            讓尚未執行修復腳本的資料庫也不會拿 0 元價去成交。
        - Parameters:
            - stock: Any
                `price` 表的一列
        - Return:
            - bool
                收盤價存在且大於 0 為 True
        """

        close: Any = getattr(stock, PriceColumn.CLOSE)
        if close is None or pd.isna(close):
            return False
        return float(close) > 0

    @staticmethod
    def generate_stock_quote(
        data: Any,
        stock_id: str,
        date: datetime.date,
        scale: Scale,
        adj_close: Optional[float] = None,
    ) -> StockQuote:
        """
        - Description:
            建立個股的 Stock Quote
        - Parameters:
            - data: Any
                當日資料
            - stock_id: str
                股票代號
            - date: datetime.date
                要轉換的日期
            - scale: Scale
                要轉換的 Scale
        - Returns:
            - StockQuote
                建立後的 StockQuote 物件
        - Notes:
            - Volume:
                - Scale.TICK: Unit 資料原本就是 Lot
                - Scale.DAY: Unit: Shares
        """

        if scale == Scale.TICK:
            tick_quote: TickQuote = TickQuote(
                stock_id=data.stock_id,
                time=data.time,
                close=data.close,
                volume=data.volume,
                bid_price=data.bid_price,
                bid_volume=data.bid_volume,
                ask_price=data.ask_price,
                ask_volume=data.ask_volume,
                tick_type=data.tick_type,
            )
            # **cur_price／close／volume 一定要帶**：
            # 舊版只掛 `tick=tick_quote`，OHLC 與 cur_price 全部留在預設值 0.0，
            # 於是任何讀 `quote.close` 的地方（部位盯市、報表、策略）都拿到 0 元。
            #
            # `open`／`high`／`low` 維持 0：單一 tick 本來就沒有 OHLC，
            # 需要當日區間的地方（`FillModel`）自己累計 `intraday_range`。
            return StockQuote(
                stock_id=data.stock_id,
                scale=scale,
                date=date,
                cur_price=tick_quote.close,
                volume=tick_quote.volume,
                close=tick_quote.close,
                tick=tick_quote,
            )

        elif scale == Scale.DAY:
            return StockQuote(
                stock_id=stock_id,
                scale=scale,
                date=date,
                cur_price=getattr(data, PriceColumn.CLOSE),
                volume=StockUtils.convert_share_to_lot(
                    getattr(data, PriceColumn.SHARES)
                ),
                open=getattr(data, PriceColumn.OPEN),
                high=getattr(data, PriceColumn.HIGH),
                low=getattr(data, PriceColumn.LOW),
                close=getattr(data, PriceColumn.CLOSE),
                adj_close=adj_close,
            )

        raise ValueError(f"Unsupported scale: {scale.name}")
