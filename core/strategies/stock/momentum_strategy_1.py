# Python standard library
import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.backtest.datafeed.base import BaseDataFeed
from core.backtest.datafeed.tw.market_calendar import MarketCalendar
from core.models import StockAccount, StockPosition, StockQuote
from core.portfolio.signal import Signal
from core.strategies.stock import BaseStockStrategy
from core.utils import Action, PositionType, Scale


class MomentumStrategy1(BaseStockStrategy):
    """
    動能策略 1（**只支援日線**）

    買進條件（全部滿足）：
    - 當日收盤相對「前一交易日」收盤漲幅 ≥ 門檻（預設 9%）
    - 當日成交量 ≥ 門檻（預設 5000 張）

    賣出條件：
    - 已有部位且「報價日」≥ 開倉日 + 1 日曆日（持倉至少隔一天後可平倉）

    停損條件：
    - 未實作（一律不回傳停損單）

    **已持有的標的仍會再次進入開倉候選（＝允許加碼）**：本策略不過濾
    `check_has_position()`，同一檔在連續多天都符合條件時會開出多個部位，
    實際能開幾個由 `max_holdings` 與 `calculate_position_size()` 決定。
    這是刻意的語意（動能延續就繼續加），但先前 docstring 沒寫，
    看回測結果的人無從判斷那些重複的開倉是設計還是 bug。

    **TICK 級別不支援**：訊號建立在「前一交易日收盤」上，TICK 路徑沒有對應的
    取價方式；`setup_apis()` 會直接 `NotImplementedError`。
    """

    DEFAULT_MAX_HOLDINGS: int = 10
    DEFAULT_BACKTEST_START_DATE: datetime.date = datetime.date(2020, 5, 1)
    DEFAULT_BACKTEST_END_DATE: datetime.date = datetime.date(2025, 5, 31)

    # 買進參數
    MIN_PRICE_CHANGE_PCT_FOR_SIGNAL: float = 9.0  # 相對昨收之最小漲幅（%）
    MIN_VOLUME_LOTS: int = 5000  # 最小成交量（張）
    # 交易日清單往回多抓幾個曆日：第一根 bar 要的是「回測起始日之前」的交易日，
    # 只抓區間內拿不到。
    #
    # **與 `MarketCalendar.MAX_LOOKBACK_DAYS` 對齊**：這個窗若比它小，
    # `get_previous_trading_date()` 會在清單裡查不到而退回逐日查詢，
    # 等於把「交易日集合一次建立」的優化悄悄關掉——綁住的是這一邊，不是日曆那一邊
    CALENDAR_LOOKBACK_DAYS: int = MarketCalendar.MAX_LOOKBACK_DAYS

    def __init__(self) -> None:
        super().__init__()
        self.strategy_name: str = "Momentum-1"
        self.init_capital: float = 1000000.0
        self.max_holdings: int = self.DEFAULT_MAX_HOLDINGS
        self.scale: Scale = Scale.DAY

        self.start_date: datetime.date = self.DEFAULT_BACKTEST_START_DATE
        self.end_date: datetime.date = self.DEFAULT_BACKTEST_END_DATE

        # 回測區間的交易日清單；`setup_apis()` 建一次（見 `build_trading_days()`）
        self.trading_days: List[datetime.date] = []

    def setup_account(self, account: StockAccount) -> None:
        """設置虛擬帳戶資訊"""

        self.account: StockAccount = account

    def setup_apis(self, feed: BaseDataFeed) -> None:
        """
        - Description:
            宣告本策略要用的資料源；實例由 DataFeed 統一持有

            **TICK 級別當場擋下**：本策略的訊號建立在「前一交易日
            收盤」上，TICK 路徑只會掛 `self.tick`、`self.price` 維持 None，
            第一根 bar 就會在 `get_previous_trading_date()` 撞
            `ValueError("Invalid API type")`。docstring 寫的是「日線」，
            但沒有任何東西擋住把 `scale` 改成 TICK。
        - Parameters:
            - feed: BaseDataFeed
                引擎持有的資料源
        - Raise:
            - NotImplementedError
                `scale` 不是 `Scale.DAY`
        """

        if self.scale != Scale.DAY:
            raise NotImplementedError(
                f"{self.strategy_name} 只支援日線（Scale.DAY）："
                f"訊號以『前一交易日收盤』為基準，TICK 級別沒有對應的取價方式。"
                f"目前的 scale 是 {self.scale}"
            )

        self.chip = feed.chip
        self.mrr = feed.mrr
        self.fs = feed.fs
        self.price = feed.price
        self.trading_days = self.build_trading_days()

    def build_trading_days(self) -> List[datetime.date]:
        """
        - Description:
            預先取好回測區間的交易日清單

            **每根 bar 都呼叫 `get_last_trading_date()` 是每根 bar 一次
            `SELECT *`**；換成一次取清單、之後以 `bisect` 平移。

            起點往前多抓 `CALENDAR_LOOKBACK_DAYS` 天：第一根 bar 要的是
            **回測起始日之前**的那個交易日，只抓區間內是拿不到的。
        - Return:
            - List[datetime.date]
                已排序的交易日
        """

        if self.price is None or not self.start_date or not self.end_date:
            return []

        return self.price.get_trading_days(
            self.start_date - datetime.timedelta(days=self.CALENDAR_LOOKBACK_DAYS),
            self.end_date,
        )

    def get_previous_trading_date(self, date: datetime.date) -> datetime.date:
        """
        - Description:
            取得前一個交易日；優先查預備好的清單，查不到才回頭問資料庫
        - Parameters:
            - date: datetime.date
                基準日
        - Return:
            - datetime.date
                前一個交易日
        """

        if self.trading_days:
            previous: Optional[datetime.date] = MarketCalendar.shift_trading_days(
                self.trading_days, date, offset=-1
            )
            if previous is not None:
                return previous

        return MarketCalendar.get_last_trading_date(api=self.price, date=date)

    def generate_open_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """開倉訊號：昨收基準漲幅達門檻且成交量達門檻，做多；張數由 portfolio 層依 max_holdings 與資金切分決定"""

        open_positions: List[StockQuote] = []

        if self.max_holdings == 0:
            return []

        # 以「前一交易日」收盤為基準計算當日漲跌幅（非日曆昨日）
        yesterday: datetime.date = self.get_previous_trading_date(stock_quotes[0].date)

        # 訊號用收盤價：與下方的 signal_close 成對，由引擎的還原模式統一決定
        yesterday_close_map: Dict[str, Any] = self.get_signal_close_map(
            stock_quotes, yesterday
        )

        for stock_quote in stock_quotes:
            # a. 對齊該股在昨交易日的收盤價；缺資料或價格無效則跳過
            if stock_quote.stock_id not in yesterday_close_map:
                logger.warning(f"股票 {stock_quote.stock_id} {yesterday} 收盤價為空")
                continue
            yesterday_close_price: float = yesterday_close_map[stock_quote.stock_id]

            # **`NaN` 一定要在這裡擋掉**：無成交日的收盤價在資料庫是 `NULL`
            # （無成交價改存 NULL 之後），讀進來是 `NaN`。而下面的 `price_chg < 門檻`
            # 對 `NaN` 恆為 `False`——**不會 `continue`，反而一路走成買進候選**，
            # log 裡只會留下一行「漲幅 nan%」。
            #
            # 修 `price` 表那 104,046 列時實測到：少了這道防線，LONG 回歸
            # 多出 10 筆、少掉 3 筆交易（同一天的名額被 NaN 標的擠掉）。
            if pd.isna(yesterday_close_price) or not yesterday_close_price:
                logger.warning(
                    f"股票 {stock_quote.stock_id} {yesterday} 無有效收盤價"
                    f"（NULL／NaN／0），本日跳過"
                )
                continue

            # b. 當日收盤相對昨收之漲幅（%）≥ MIN_PRICE_CHANGE_PCT_FOR_SIGNAL
            price_chg: float = (
                stock_quote.signal_close / yesterday_close_price - 1
            ) * 100

            if price_chg < self.MIN_PRICE_CHANGE_PCT_FOR_SIGNAL:
                continue
            logger.info(f"股票 {stock_quote.stock_id} 漲幅 {round(price_chg, 2)}%")

            # c. 當日成交量 ≥ MIN_VOLUME_LOTS（張）
            if stock_quote.volume < self.MIN_VOLUME_LOTS:
                continue

            open_positions.append(stock_quote)

        # 算量用當日收盤、下單用 cur_price：兩者在現行資料源同值，但語意不同，不合併
        return [
            Signal(
                quote=stock_quote,
                action=Action.BUY,
                position_type=PositionType.LONG,
                order_price=stock_quote.cur_price,
                sizing_price=stock_quote.close,
            )
            for stock_quote in open_positions
        ]

    def generate_close_signals(self, stock_quotes: List[StockQuote]) -> List[Signal]:
        """平倉訊號：帳上已有該股部位時，若報價日 ≥ 開倉日 + 1 日曆日則出場（至少持有一天後可賣）"""

        signals: List[Signal] = []

        for stock_quote in stock_quotes:
            if self.account.check_has_position(stock_quote.stock_id):
                position: Optional[StockPosition] = (
                    self.account.get_first_open_position(stock_quote.stock_id)
                )
                if position is None:
                    logger.warning(f"股票 {stock_quote.stock_id} 沒有開倉記錄")
                    continue
                # 開倉當日不平：隔日（含）起才允許平倉
                if stock_quote.date >= position.date + datetime.timedelta(days=1):
                    # 依該股**第一筆**開倉部位的張數與多空類型全數賣出
                    signals.append(
                        Signal(
                            quote=stock_quote,
                            action=Action.SELL,
                            position_type=position.position_type,
                            order_price=stock_quote.cur_price,
                            volume=position.volume,
                        )
                    )

        return signals

    def generate_stop_loss_signals(
        self, stock_quotes: List[StockQuote]
    ) -> List[Signal]:
        """停損訊號：本策略未實作停損，固定回傳空列表"""

        return []
