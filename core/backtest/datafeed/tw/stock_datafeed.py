import datetime
from typing import Dict, List, Optional, Set

import pandas as pd
from loguru import logger

from core.adapters import StockQuoteAdapter
from core.api.tw.financial_statement_api import FinancialStatementAPI
from core.api.tw.monthly_revenue_report_api import MonthlyRevenueReportAPI
from core.api.tw.stock_chip_api import StockChipAPI
from core.api.tw.stock_dividend_api import StockDividendAPI
from core.api.tw.stock_margin_api import StockMarginAPI
from core.api.tw.stock_price_api import StockPriceAPI
from core.api.tw.stock_tick_api import StockTickAPI
from core.backtest.datafeed.base import BaseDataFeed
from core.backtest.datafeed.tw.market_calendar import MarketCalendar
from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.models import StockQuote
from core.pipeline.shared.date_planner import DatePlanner, DateProgressStore
from core.strategies.base import BaseStrategy
from core.utils import Scale

"""TwStockDataFeed: 台股資料源（五個資料 API ＋ 報價轉換 ＋ 交易日判定）"""


class TwStockDataFeed(BaseDataFeed):
    """台股資料源：SQLite 的日 K 與籌碼、DolphinDB 的 Tick"""

    # 融券最後回補日 = 除權息交易日往前推 4 個營業日。
    #
    # 法規原文是「停止過戶日前 6 個營業日」，而停止過戶日 = 除權息交易日 + 2 個營業日
    # （除權息交易日的次一營業日為最後過戶日），兩者相減即得 4。
    # `dividend` 表只有除權息交易日、沒有停止過戶日，故一律以此換算。
    FORCE_COVER_TRADING_DAYS_BEFORE_EX_DATE: int = 4

    # 推導停券日時往後多查的曆日數：落在回測末段的回補日，其對應的除權息交易日
    # 會在 end_date 之後，只查回測區間本身會整段漏掉
    FORCE_COVER_LOOKAHEAD_DAYS: int = 21

    def __init__(self) -> None:
        # 單次回測共用一條 SQLite 連線：四個 API 查的是同一個 DB 檔，
        # 各開一條沒有任何好處，只會讓連線數隨 API 數量線性成長
        self.conn: Optional[DBConnection] = None

        self.tick: Optional[StockTickAPI] = None  # Ticks data
        self.chip: Optional[StockChipAPI] = None  # Chips data
        self.price: Optional[StockPriceAPI] = None  # Price data
        self.dividend: Optional[StockDividendAPI] = None  # Ex-rights/dividend data
        self.margin: Optional[StockMarginAPI] = None  # Margin/short balance data
        self.mrr: Optional[MonthlyRevenueReportAPI] = (
            None  # Monthly Revenue Report data
        )
        self.fs: Optional[FinancialStatementAPI] = None  # Financial Statement data

        # 回測區間：停券日推導需要往後多看幾個交易日，故必須知道區間
        self.start_date: Optional[datetime.date] = None
        self.end_date: Optional[datetime.date] = None

        # {融券最後回補日: {stock_id}}；由除權息行事曆推導，整場回測只建一次
        self.force_cover_map: Optional[Dict[datetime.date, Set[str]]] = None

        # 回測區間內的交易日集合；`setup()` 建一次，供 `is_market_open()` 查表。
        # **不建的話每個曆日都會對 price 表做一次 `SELECT *` 只為了判斷空不空**：
        # 13 年就是 4,700 次全表掃描
        self.trading_days: Optional[Set[datetime.date]] = None

    def setup(self, strategy: BaseStrategy) -> None:
        """從資料庫載入資料；Tick 級別才建立 DolphinDB 連線"""

        self.conn = connect_sqlite(TW_STOCK_DB_PATH)
        self.start_date = strategy.start_date
        self.end_date = strategy.end_date

        self.chip = StockChipAPI(conn=self.conn)
        self.mrr = MonthlyRevenueReportAPI(conn=self.conn)
        self.fs = FinancialStatementAPI(conn=self.conn)
        self.dividend = StockDividendAPI(conn=self.conn)
        self.margin = StockMarginAPI(conn=self.conn)
        self.price = StockPriceAPI(conn=self.conn, dividend_api=self.dividend)

        if strategy.scale == Scale.TICK:
            self.tick = StockTickAPI()

        # 交易日集合一次建立，順便報告區間內的可疑缺日。
        # **`start_date`／`end_date` 在 `BaseStrategy` 是 Optional 且預設 None**，
        # 沒設的策略在這裡查 `get_trading_days(None, None)` 會 TypeError；
        # 那種策略退回逐日查詢即可（`is_market_open()` 有 fallback）
        if self.start_date and self.end_date:
            self.trading_days = set(
                self.price.get_trading_days(self.start_date, self.end_date)
            )
            self.report_calendar_gaps()
        else:
            logger.warning(
                "[DataFeed] 策略未設定回測區間，交易日集合不預先建立，"
                "改為逐日查詢（每個曆日一次 SELECT）"
            )

    def is_market_open(self, date: datetime.date) -> bool:
        """台股開盤日判定：當日有日 K 資料即視為開盤"""

        if self.trading_days is not None:
            return date in self.trading_days

        # `setup()` 尚未跑過（單元測試直接呼叫）時退回逐日查詢
        return MarketCalendar.check_stock_market_open(api=self.price, date=date)

    def report_calendar_gaps(self) -> int:
        """
        - Description:
            報告回測區間內「平日卻沒有行情」的日期

            **回測遇到缺日會當成休市靜默跳過**：資料缺一天與
            當天休市在引擎眼裡完全相同，策略少做一天的判斷卻不會有任何跡象。
            根治在 ETL（見 `core/pipeline/shared/date_planner.py`），這裡負責
            **讓它在回測起跑時就被看見**。

            **扣掉 ETL 已確認為休市的日期**：`DateProgressStore("price").no_data`
            記的是「已向交易所確認過、當天確實沒有資料」，那些是國定假日、
            不是缺口。這份紀錄不存在時（剛 clone 的環境）就全部算成不確定，
            只報數字不下判斷——寧可說不知道，也不要把連假講成資料缺失。
        - Return:
            - int
                無法歸因為休市的缺日數
        """

        if not self.start_date or not self.end_date or self.trading_days is None:
            return 0

        missing: Set[datetime.date] = (
            DatePlanner.generate_weekdays(self.start_date, self.end_date)
            - self.trading_days
        )
        if not missing:
            return 0

        confirmed_holidays: Set[datetime.date] = DateProgressStore("price").no_data
        unexplained: List[datetime.date] = sorted(missing - confirmed_holidays)

        if not unexplained:
            logger.info(
                f"[Calendar] 區間內有 {len(missing)} 個平日沒有行情，"
                f"皆已由 ETL 確認為休市"
            )
            return 0

        if not confirmed_holidays:
            logger.info(
                f"[Calendar] 區間內有 {len(unexplained)} 個平日沒有行情。"
                f"本機沒有 ETL 的休市紀錄，無法分辨連假與缺日——"
                f"跑過一次 `--target price` 之後這行會變精確"
            )
            return len(unexplained)

        logger.warning(
            f"[Calendar] 區間內有 {len(unexplained)} 個平日沒有行情、"
            f"且**不在** ETL 已確認的休市清單裡，很可能是資料缺口："
            f"{unexplained[:10]}"
            + ("…（僅列前 10 筆）" if len(unexplained) > 10 else "")
        )
        return len(unexplained)

    def get_quotes(
        self,
        date: datetime.date,
        scale: Scale,
        adjusted: bool = False,
    ) -> List[StockQuote]:
        """
        依級別取得當日報價

        tick 不做還原：tick 為當日盤中資料，跨日還原無意義
        """

        if scale == Scale.TICK:
            return StockQuoteAdapter.convert_to_tick_quotes(self.tick, date)

        return StockQuoteAdapter.convert_to_day_quotes(self.price, date, adjusted)

    def get_price_limit_basis(self, date: datetime.date) -> Dict[str, float]:
        """
        除權息日的漲跌停基準：交易所另行公告的開盤競價基準

        非除權息日回傳空 dict，`FillModel` 維持沿用前一交易日收盤。
        `setup()` 未執行時（純記憶體測試會跳過）同樣回傳空 dict，
        此時本來就沒有資料源可查，不是「查不到資料」
        """

        if self.dividend is None:
            return {}

        return self.dividend.get_opening_reference_price_map(date)

    def get_short_balance(self, date: datetime.date) -> Dict[str, int]:
        """
        當日融券今日餘額（張），供券源檢核使用

        `margin` 表尚未建立或該日無資料時回傳空 dict——`FillModel` 會據此放行並記錄，
        不會把「查無資料」當成「借不到券」
        """

        if self.margin is None:
            return {}

        return self.margin.get_short_balance_map(date)

    def get_force_cover_symbols(self, date: datetime.date) -> Set[str]:
        """
        當日觸及融券最後回補日的標的（由除權息行事曆推導）

        `setup()` 未執行時（純記憶體測試會跳過）回傳空集合——此時本來就沒有
        資料源可查，不是「今日無標的停券」
        """

        if self.dividend is None or self.price is None:
            return set()

        if self.force_cover_map is None:
            self.force_cover_map = self.build_force_cover_map()

        return self.force_cover_map.get(date, set())

    def build_force_cover_map(self) -> Dict[datetime.date, Set[str]]:
        """
        - Description:
            把 `dividend` 表的除權息交易日換算成融券最後回補日

            台股沒有可直接取用的「停券預告表」資料源，但停券日是**行事曆推導的
            結果**而非獨立資料：除權息交易日往前推
            `FORCE_COVER_TRADING_DAYS_BEFORE_EX_DATE` 個營業日即為融券最後回補日。
            推算必須以實際開盤日為單位，故交易日曆取自 `price` 表。

            **只涵蓋除權息停券，不含股東會停券**：後者需要一份股東會行事曆
            （常會、臨時會的停止過戶日），目前無資料源。因此本方法產出的回補日
            是實際停券日的**子集**，留倉放空的持有天數仍會被高估一部分。

            整場回測只建一次：`dividend` 與 `price` 表在回測期間不會變動。
        - Return:
            - Dict[datetime.date, Set[str]]
                `{融券最後回補日: {stock_id}}`；資料不足時回傳空 dict
        """

        if self.start_date is None or self.end_date is None:
            return {}

        # 回補日在回測區間內，其對應的除權息交易日則可能落在 end_date 之後
        lookahead_end: datetime.date = self.end_date + datetime.timedelta(
            days=self.FORCE_COVER_LOOKAHEAD_DAYS
        )
        trading_days: List[datetime.date] = self.price.get_trading_days(
            self.start_date, lookahead_end
        )
        if not trading_days:
            return {}

        ex_dividend_df: pd.DataFrame = self.dividend.get_range(
            self.start_date, lookahead_end
        )
        if ex_dividend_df.empty:
            return {}

        force_cover_map: Dict[datetime.date, Set[str]] = {}
        for ex_date, stock_id in zip(
            pd.to_datetime(ex_dividend_df["date"]).dt.date,
            ex_dividend_df["stock_id"].astype(str),
        ):
            cover_date: Optional[datetime.date] = MarketCalendar.shift_trading_days(
                trading_days,
                ex_date,
                -self.FORCE_COVER_TRADING_DAYS_BEFORE_EX_DATE,
            )
            # 回補日早於 start_date 時 shift 會回傳 None（清單起點就是 start_date），
            # 代表那筆除權息的停券已發生在回測開始之前，本場回測不需要處理
            if cover_date is None:
                continue

            force_cover_map.setdefault(cover_date, set()).add(stock_id)

        return force_cover_map

    def get_cash_dividend_map(self, date: datetime.date) -> Dict[str, float]:
        """
        當日除息的每股現金股利（元／股），供放空的股利補償使用

        值可能為 `NaN`（上市權息並存的標的無法拆出現金股利），
        呼叫端須自行處置
        """

        if self.dividend is None:
            return {}

        return self.dividend.get_cash_dividend_map(date)

    def get_share_ratio_map(self, date: datetime.date) -> Dict[str, float]:
        """當日的股數倍率（配股、分割、減資），供做多的股數調整與空單補償使用"""

        if self.dividend is None:
            return {}

        return self.dividend.get_share_ratio_map(date)

    def close(self) -> None:
        """關閉所有資料連線（回測結束時呼叫；原本全專案的 conn 從不 close）"""

        for api in (
            self.chip,
            self.mrr,
            self.fs,
            self.price,
            self.dividend,
            self.margin,
            self.tick,
        ):
            if api is not None:
                api.close()

        if self.conn is not None:
            self.conn.close()
            self.conn = None
