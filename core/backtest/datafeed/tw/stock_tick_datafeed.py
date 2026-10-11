import datetime
from typing import Dict, Iterator, List, Optional, Set

import pandas as pd
from loguru import logger

from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.market.tw.instrument_spec import TwStockSpec
from core.models import PreOpenStockQuote, StockQuote
from core.utils.instrument import StockUtils

"""
台股逐筆資料源：把一天的 tick 依時間順序逐筆交給引擎

日頻資料（參考價、漲跌停基準、除權息、融券與當沖名單）沿用 `TwStockDataFeed`，
這裡只多了「一天的 tick 怎麼取、怎麼逐筆給、各段落的快照」。
"""


class StockTickDataFeed(TwStockDataFeed):
    """
    - Description:
        台股逐筆資料源

        每個交易日先 `setup_day()` 查一次當天的 tick（依標的範圍），之後：
        - `iter_day_ticks()`：依 `(time, stock_id, seq)` 逐筆產生報價，同一時間戳記跨股票的順序以代號固定，回測才可重現。
        - 段落快照：盤前參考價（`get_reference_quotes()`）、開盤（`get_open_quotes()`）、
          某個時點之前的最後一筆（`get_snapshot_quotes()`）、收盤（`get_close_quotes()`）。

        **報價的 `volume` 是當日累計量、`tick_quote.volume` 是這一筆的量**，與實盤同語意。

        **只保留一般股**：日 K 路徑在報價轉換時就濾掉 ETF 與權證，tick 庫卻含 ETF；
        不濾的話兩種級別的策略看到的標的池不同，檔位表（只適用普通股）的對齊也會失真。
    """

    def __init__(self) -> None:
        super().__init__()

        self.instrument: TwStockSpec = TwStockSpec()

        # 目前載入的交易日與當天的 tick（`StockTickAPI` 的欄位，已依時間排序）
        self.day: Optional[datetime.date] = None
        self.day_ticks: pd.DataFrame = pd.DataFrame()
        # 當天的標的範圍：指定清單時為清單（濾掉非一般股），全市場時為當天有成交的股票
        self.day_symbols: List[str] = []

    def setup_day(self, date: datetime.date, symbols: Optional[List[str]]) -> int:
        """
        - Description:
            查當天的 tick（每天只查一次），之後的逐筆與快照都讀這一份
        - Parameters:
            - date: datetime.date
                交易日
            - symbols: Optional[List[str]]
                標的範圍；None 為全市場
        - Return:
            - int
                當天的 tick 筆數；0 表示沒有任何成交
        """

        requested: Optional[List[str]] = (
            StockUtils.filter_common_stocks(sorted(set(symbols)))
            if symbols is not None
            else None
        )

        ticks: pd.DataFrame = self.tick.get_ordered_ticks(
            date, date, stock_ids=requested
        )
        if not ticks.empty:
            common: Set[str] = set(
                StockUtils.filter_common_stocks(ticks["stock_id"].unique().tolist())
            )
            ticks = ticks[ticks["stock_id"].isin(common)].reset_index(drop=True)

        self.day = date
        self.day_ticks = ticks
        if requested is not None:
            self.day_symbols = requested
        else:
            self.day_symbols = sorted(ticks["stock_id"].unique().tolist())
        return len(ticks)

    def iter_day_ticks(self) -> Iterator[StockQuote]:
        """
        - Description:
            依時間順序逐筆產生當天的報價

            **用產生器而不是一次建好 list**：全市場一天上百萬筆，一次建好的
            `StockQuote` 要好幾秒、上 GB 的記憶體，而逐筆重放本來就一次只用一筆。
        - Return:
            - Iterator[StockQuote]
                逐筆報價（`scale=TICK`）
        """

        for row in self.day_ticks.itertuples(index=False):
            yield StockQuoteAdapter.from_tick_row(row, self.day)

    def get_reference_quotes(
        self, reference_prices: Dict[str, float]
    ) -> List[PreOpenStockQuote]:
        """
        - Description:
            盤前報價：只有參考價與漲跌停，讀 OHLC 會拋出（與實盤開盤段相同）

            **參考價由呼叫端傳入**（引擎的成交模型持有的漲跌停參考價：前一交易日收盤，
            除權息日為公告的開盤競價基準），不在這裡另算一份——兩份來源遲早會不一致。
            沒有參考價的標的略過（回測第一天就是如此），與實盤取不到參考價時的處理相同。
        - Parameters:
            - reference_prices: Dict[str, float]
                `{stock_id: 參考價}`
        - Return:
            - List[PreOpenStockQuote]
                依代號排序的盤前報價
        """

        quotes: List[PreOpenStockQuote] = []
        missing: int = 0

        for symbol in self.day_symbols:
            reference: Optional[float] = reference_prices.get(symbol)
            if not reference:
                missing += 1
                continue

            limit_down, limit_up = self.instrument.get_price_limits(
                reference, self.day, symbol
            )
            quotes.append(
                PreOpenStockQuote(
                    stock_id=symbol,
                    date=self.day,
                    reference_price=reference,
                    limit_up=limit_up,
                    limit_down=limit_down,
                )
            )

        if missing:
            logger.info(f"[Tick] {self.day} 盤前有 {missing} 檔沒有參考價，本段略過")
        return quotes

    def get_open_quotes(self) -> List[StockQuote]:
        """每檔當天的第一筆（開盤集合競價的成交），依代號排序"""

        if self.day_ticks.empty:
            return []
        return self._to_quotes(self.day_ticks.groupby("stock_id", sort=True).head(1))

    def get_snapshot_quotes(self, cutoff: datetime.time) -> List[StockQuote]:
        """
        - Description:
            每檔在某個時點之前（不含）的最後一筆，依代號排序；該時點前沒有成交的股票不會出現

            時點由呼叫端依市場的段落設定傳入（例如台股收盤集合競價開始前），資料源不寫死。
        - Parameters:
            - cutoff: datetime.time
                時點
        - Return:
            - List[StockQuote]
                快照報價
        """

        if self.day_ticks.empty:
            return []

        cutoff_at: pd.Timestamp = pd.Timestamp(
            datetime.datetime.combine(self.day, cutoff)
        )
        before: pd.DataFrame = self.day_ticks[self.day_ticks["time"] < cutoff_at]
        return self._to_quotes(before.groupby("stock_id", sort=True).tail(1))

    def get_close_quotes(self) -> List[StockQuote]:
        """每檔當天的最後一筆（收盤），依代號排序；交給日終結算與盯市"""

        if self.day_ticks.empty:
            return []
        return self._to_quotes(self.day_ticks.groupby("stock_id", sort=True).tail(1))

    def _to_quotes(self, rows: pd.DataFrame) -> List[StockQuote]:
        """把挑出來的 tick 列依代號排序後轉成報價"""

        if rows.empty:
            return []

        ordered: pd.DataFrame = rows.sort_values("stock_id", kind="stable")
        return [
            StockQuoteAdapter.from_tick_row(row, self.day)
            for row in ordered.itertuples(index=False)
        ]
