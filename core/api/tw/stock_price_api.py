import datetime
import sqlite3
from typing import Any, Dict, List, Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.api.tw.stock_dividend_api import StockDividendAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_STOCK_DB_PATH
from core.config.schema import PriceColumn
from core.dao.connection import connect_sqlite
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.utils.constant import Units
from core.utils.log_manager import LogManager

"""Stock Price API: query price table through StockPriceDAO"""


class StockPriceAPI(BaseDataAPI):
    """Stock Price API"""

    def __init__(
        self,
        conn: Optional[sqlite3.Connection] = None,
        dividend_api: Optional[StockDividendAPI] = None,
    ) -> None:
        # 由 DataFeed 傳入共用連線；未指定時自行建立
        self.conn: Optional[sqlite3.Connection] = conn
        self.owns_conn: bool = conn is None

        # 還原價專用；只有 get_adjusted_* 系列會用到，故延遲建立
        self.dividend_api: Optional[StockDividendAPI] = dividend_api

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.dao: Optional[StockPriceDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_STOCK_DB_PATH)
        self.dao = StockPriceDAO(conn=self.conn)
        LogManager.setup_logger(
            "stock_price_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get_dividend_api(self) -> StockDividendAPI:
        """取得除權息 API（延遲建立，與本 API 共用同一條連線）"""

        if self.dividend_api is None:
            self.dividend_api = StockDividendAPI(conn=self.conn)
        return self.dividend_api

    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的 Price"""

        return self.dao.get_by_date(date)

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票指定日期範圍的 Price"""

        return self.dao.get_range(start_date, end_date)

    def get_stock_price(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的 Price"""

        return self.dao.get_by_stock(stock_id, start_date, end_date)

    def get_trading_days(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """
        - Description:
            取得日期範圍內的所有交易日（已排序、去重）

            交易日曆直接由 `price` 表推導：當日有日 K 資料即為開盤日，與
            `MarketCalendar.check_stock_market_open()` 同一套判準。凡是「往前推
            N 個營業日」的市場規則（例如融券最後回補日）都應吃這一份，
            自行以曆日相減會在連假整段位移。
        - Parameters:
            - start_date: datetime.date
                起始日（含）
            - end_date: datetime.date
                結束日（含）
        - Return:
            - List[datetime.date]
                區間內的交易日；無資料時回傳空 list
        """

        return self.dao.get_trading_days(start_date, end_date)

    # === 具名查詢：策略層一律走這一組，不要自行操作 DataFrame 欄位 ===
    def get_close_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的收盤價對照表

            策略的共通樣式是「拿一整天的全市場 DataFrame → 逐檔建 mask → 取欄位」，
            那是 O(n²) 且把資料表欄位名洩漏到策略層。本方法一次查詢建成 dict，
            策略改為 O(1) 查表，也不需要知道欄位叫什麼。

            取值細節（重複、缺欄位、`NaN`）見 `BaseDataAPI.build_column_map()`。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - close_map: Dict[str, Any]
                {stock_id: 收盤價}；同一檔重複出現時取第一筆
        """

        df: pd.DataFrame = self.get(date)
        return self.build_column_map(df, PriceColumn.CLOSE.value)

    def get_volume_lots_map(self, date: datetime.date) -> Dict[str, int]:
        """
        - Description:
            取得單日全市場的成交量（張）對照表

            股 → 張的換算在 API 內完成，策略不再自行除以 `Units.LOT`。

            此處**不使用** `StockUtils.convert_share_to_lot()`：`core/utils/instrument.py`
            相依 `MarketCalendar`，而後者相依本檔案，引用會造成循環 import。
            `core/api/` 位於 `StockUtils` 之下，不應反向相依。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - volume_map: Dict[str, int]
                {stock_id: 成交量（張）}；成交股數無法轉為整數者不納入
        """

        df: pd.DataFrame = self.get(date)
        share_map: Dict[str, Any] = self.build_column_map(df, PriceColumn.SHARES.value)

        volume_map: Dict[str, int] = {}
        for stock_id, shares in share_map.items():
            try:
                volume_map[stock_id] = int(int(shares) / Units.LOT)
            except (TypeError, ValueError):
                continue

        return volume_map

    def get_close_series(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.Series:
        """
        - Description:
            取得指定個股在區間內的收盤價序列
        - Parameters:
            - stock_id: str
                股票代號
            - start_date: datetime.date
                起始日（含）
            - end_date: datetime.date
                結束日（含）
        - Return:
            - series: pd.Series
                index 為原始 date 欄位、已依日期排序；查無資料時為空 Series
        """

        df: pd.DataFrame = self.get_stock_price(stock_id, start_date, end_date)

        if df.empty or PriceColumn.CLOSE.value not in df.columns:
            return pd.Series(dtype=float, name=PriceColumn.CLOSE.value)

        df = df.sort_values("date")
        series: pd.Series = df[PriceColumn.CLOSE.value]
        series.index = df["date"]
        return series

    # === 還原價（後復權）：僅供訊號計算，成交與成本一律走上面的原始價 ===
    def get_adjusted_close_map(self, date: datetime.date) -> Dict[str, Any]:
        """
        - Description:
            取得單日全市場的**還原**收盤價對照表，與 `get_close_map()` 一一對應

            **用途界線（取錯會直接算錯錢）**：
            - 訊號計算（漲跌幅、均線、動能）→ 本方法
            - 成交價、手續費、證交稅 → `get_close_map()`（原始價，稅費對實際成交金額課徵）
            - 漲跌停與檔位判定 → 原始價

            刻意不做成 `get_close_map(adjusted=True)`：同一個方法回傳兩種語意的價格，
            呼叫端很容易拿錯而且不會有任何錯誤訊息。

            無除權息紀錄的股票其係數為 1，回傳值與 `get_close_map()` 相同。
        - Parameters:
            - date: datetime.date
                查詢日期
        - Return:
            - adjusted_close_map: Dict[str, Any]
                {stock_id: 還原收盤價}；無法轉為數值的價格維持原值不還原
        """

        close_map: Dict[str, Any] = self.get_close_map(date)
        factor_map: Dict[str, float] = (
            self.get_dividend_api().get_cumulative_factor_map(date)
        )

        adjusted_close_map: Dict[str, Any] = {}
        for stock_id, close in close_map.items():
            factor: float = factor_map.get(stock_id, 1.0)
            if factor == 1.0:
                adjusted_close_map[stock_id] = close
                continue
            try:
                adjusted_close_map[stock_id] = float(close) * factor
            except (TypeError, ValueError):
                # 價格本身無法轉為數值時維持原樣，讓缺資料仍以原本的形式往下傳
                adjusted_close_map[stock_id] = close

        return adjusted_close_map

    def get_adjusted_close_series(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.Series:
        """
        - Description:
            取得指定個股在區間內的**還原**收盤價序列，與 `get_close_series()` 一一對應

            區間內若跨過除權息日，序列在該日不再出現虛假跳空——這正是本系列方法要解的問題。
        - Parameters:
            - stock_id: str
                股票代號
            - start_date: datetime.date
                起始日（含）
            - end_date: datetime.date
                結束日（含）
        - Return:
            - series: pd.Series
                index 為原始 date 欄位、已依日期排序；查無資料時為空 Series
        """

        series: pd.Series = self.get_close_series(stock_id, start_date, end_date)

        if series.empty:
            return series

        dividend_api: StockDividendAPI = self.get_dividend_api()
        factors: pd.Series = pd.Series(
            [
                dividend_api.get_cumulative_factor(stock_id, self.to_date(index_value))
                for index_value in series.index
            ],
            index=series.index,
            dtype=float,
        )
        return pd.to_numeric(series, errors="coerce") * factors

    @staticmethod
    def to_date(value: Any) -> datetime.date:
        """把 price 表的 date 欄位（字串或 datetime）統一轉為 `datetime.date`"""

        if isinstance(value, datetime.date) and not isinstance(
            value, datetime.datetime
        ):
            return value
        return pd.to_datetime(value).date()
