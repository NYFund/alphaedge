import bisect
import datetime
from typing import List, Optional

import pandas as pd

from core.api.tw.stock_price_api import StockPriceAPI


class MarketCalendar:
    """台股交易日曆：開盤日判定與營業日平移"""

    # 往前找前一個交易日時，最多回推幾個曆日。
    #
    # **上界不是效能考量，是防卡死**：沒有上界的話，起始日落在資料庫最早一筆
    # 之前時會一天一天往回查到 1970 年也不會停，而且不會有任何錯誤訊息
    # ——回測看起來就是「卡住了」。
    #
    # **90 天而不是 30 天**：上界要按「`price` 表可能缺多久」抓，不是按連假長度
    # （史上最長是 2023 年春節的 12 天）；表裡真的會有缺口，上界抓 30 天的話，
    # 一段一個月的缺漏就會讓整場回測以 `LookupError` 中止。90 天仍在毫秒內結束，
    # 又不會把「有資料、只是隔得遠」誤判成「沒有資料」。
    MAX_LOOKBACK_DAYS: int = 90

    @staticmethod
    def check_stock_market_open(api: StockPriceAPI, date: datetime.date) -> bool:
        """
        - Description:
            判斷指定日期是否為台股開盤日

            **判準是資料而非規則**：`price` 表當日有列就是開盤日，臨時休市、
            颱風假與補行交易日因此自動涵蓋，那些是公告出來的事實，推不出來。
        - Parameters:
            - api: StockPriceAPI
                資料 API
            - date: datetime.date
                要確認是否為開盤日的日期
        - Return:
            - bool
                該日是否為開盤日
        """

        df: pd.DataFrame = api.get(date)
        return True if not df.empty else False

    @staticmethod
    def shift_trading_days(
        trading_days: List[datetime.date],
        date: datetime.date,
        offset: int,
    ) -> Optional[datetime.date]:
        """
        - Description:
            以**營業日**為單位平移日期；`offset` 為負代表往前推

            台股多數「前 N 個營業日」的規則（融券最後回補日、停券起始日）都必須
            以實際開盤日計算，用曆日相減會在連假整段位移。與
            `get_last_trading_date()` 的差異：後者逐日往前查資料庫，只適合推一天；
            本方法吃已備妥的交易日清單，適合一次換算整批日期。
        - Parameters:
            - trading_days: List[datetime.date]
                已排序的交易日清單（由 `StockPriceAPI.get_trading_days()` 提供）
            - date: datetime.date
                基準日；不在清單內時以「不早於它的第一個交易日」為基準
            - offset: int
                平移的營業日數，負值往前
        - Return:
            - Optional[datetime.date]
                平移後的交易日；超出清單範圍時為 None（代表交易日資料不足以推算）
        """

        index: int = bisect.bisect_left(trading_days, date) + offset

        if index < 0 or index >= len(trading_days):
            return None
        return trading_days[index]

    @staticmethod
    def get_last_trading_date(api: StockPriceAPI, date: datetime.date) -> datetime.date:
        """
        - Description:
            取得指定日期的前一個交易日

            **最多往前找 `MAX_LOOKBACK_DAYS` 個曆日**：不設上界的話，起始日落在
            資料庫最早一筆之前時會一路查到 1970 年也不會停，而且沒有任何錯誤訊息
            ——回測看起來就是「卡住了」。
        - Parameters:
            - api: StockPriceAPI
                資料 API
            - date: datetime.date
                指定的日期
        - Return:
            - datetime.date
                前一個交易日
        - Raise:
            - LookupError
                回推上界內都找不到交易日（多半是起始日早於資料涵蓋範圍）
        """

        for offset in range(1, MarketCalendar.MAX_LOOKBACK_DAYS + 1):
            candidate: datetime.date = date - datetime.timedelta(days=offset)
            if MarketCalendar.check_price_api_has_data(api, candidate):
                return candidate

        raise LookupError(
            f"自 {date} 往前 {MarketCalendar.MAX_LOOKBACK_DAYS} 個曆日內找不到交易日；"
            f"多半是回測起始日早於資料涵蓋範圍，或 price 表缺了一整段"
        )

    @staticmethod
    def check_price_api_has_data(api: StockPriceAPI, date: datetime.date) -> bool:
        """資料庫路徑：該日 `price` 表是否有資料"""

        price_df: pd.DataFrame = api.get(date)
        return price_df is not None and not price_df.empty
