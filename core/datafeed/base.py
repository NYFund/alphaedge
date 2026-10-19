import datetime
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from loguru import logger

from core.dao.connection import DBConnection, connect_sqlite
from core.market.calendar import BaseTradingCalendar
from core.models import BaseQuote, DayTradeListSnapshot, ShortSaleListSnapshot
from core.strategies.base import BaseStrategy
from core.utils import Scale

"""
BaseDataFeed: 資料載入、報價轉換與交易日判定

**引擎與資料源之間的共用契約**：回測與實盤都吃這個介面，所以它不屬於
`core/backtest/`——放在那裡的話，實盤與策略層為了拿一個介面就得 import 回測套件。

〈三種方法的判準〉本檔是基底類別的範本，新增方法前先決定它屬於哪一種：

1. **真抽象（`@abstractmethod`，不給實作）**：沒有任何通用答案、每個市場都不同，
   而且**少了它引擎就跑不動**。`open_data_sources()`（哪些 API 要建）與
   `build_calendar()`（交易日清單從哪張表來）屬於這類。
2. **具體實作（基底就寫完）**：邏輯與市場無關，每個子類抄一次只會抄出分歧。
   有共用骨架要收的流程也放這裡，把可變的部分留給第 3 種——`setup()` 的外框、
   `is_market_open()` 查日曆、`report_calendar_gaps()`、`close()` 都是。
3. **有預設的 hook（基底給一個安全的預設）**：多數市場不需要，少數市場才覆寫。
   `close()` 預設 no-op、`get_price_limit_basis()` 預設回空 dict 都是——
   **預設值必須是「安全」而不是「常見」**：沒有除權息公告的市場回空 dict 會少調整
   幾檔的漲跌停基準，回錯的基準則會讓整段區間偏移。

把第 1 種寫成第 3 種，子類會靜靜沿用一個錯的預設；把第 3 種寫成第 1 種，
每個子類都被迫寫一份 `pass`，而那等於把「這裡本來就沒事要做」這個資訊丟掉。
"""


class TradingListCoverageError(RuntimeError):
    """開啟了交易所名單檢核，但回測區間早於名單起點或名單有缺日"""


class EmptyCalendarError(RuntimeError):
    """回測區間內一個交易日都沒有：行情表缺表、是空的，或沒有這段期間的資料"""


class BaseDataFeed(ABC):
    """
    資料源：把某個市場的原始資料變成引擎看得懂的報價

    不算「行為 model」，但必須一起抽——否則引擎的資料載入仍會直接 import
    各（市場，商品）組合的具體 API，`Backtester` 就不可能與兩者無關。
    """

    # 區間內沒有交易日時，錯誤訊息建議的補資料指令；各市場覆寫成自己的行情 target
    BACKFILL_COMMAND: str = "python -m apps.update_db"

    def __init__(self) -> None:
        # 回測區間；由 `setup()` 從策略記下。停券日推導、交易日曆、缺日報告都要知道區間
        self.start_date: Optional[datetime.date] = None
        self.end_date: Optional[datetime.date] = None

        # 歷史資料庫連線；由 `open_data_sources()` 開、`close()` 關
        self.conn: Optional[DBConnection] = None

        # 區間內的交易日曆；`setup()` 建一次，供 `is_market_open()` 查表。
        # **不建的話每個曆日都會對行情表做一次查詢只為了判斷空不空**：
        # 13 年就是 4,700 次全表掃描
        self.calendar: Optional[BaseTradingCalendar] = None

    def setup(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            建立資料源的外框：記下區間 → 開資料來源（連線與 API）→ 建交易日曆 → 報告缺日

            外框與市場無關，市場差異全在 `open_data_sources()` 與 `build_calendar()`。
            要在日曆建好之後多做事的市場（期貨把日曆注入換月設定）覆寫本方法並先呼叫
            `super().setup()`。
        - Parameters:
            - strategy: BaseStrategy
                本次要執行的策略（回測與實盤共用此介面）
        """

        # 實盤的策略替身可能沒有區間欄位；沒有就是 None，日曆與缺日報告隨之關閉
        self.start_date = getattr(strategy, "start_date", None)
        self.end_date = getattr(strategy, "end_date", None)

        self.open_data_sources(strategy)

        self.calendar = self.build_calendar()
        self.ensure_trading_days_in_range()
        self.report_calendar_gaps()

    @abstractmethod
    def open_data_sources(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            開連線、建立這個市場的資料 API（經 `connect_db()`，目錄不存在會先建）

            實盤資料源也實作這一支：歷史資料 API 必須與回測相同，少建一個的話用到它的
            策略會在 `setup_apis()` 當場 `AttributeError`。
        - Parameters:
            - strategy: BaseStrategy
                本次要執行的策略；要依它的級別或商品決定建哪些 API
        """

        pass

    @abstractmethod
    def build_calendar(self) -> Optional[BaseTradingCalendar]:
        """
        - Description:
            建立回測區間的交易日曆；判準一律是「行情表那天有沒有資料」

            沒有區間（策略未設 `start_date`／`end_date`）或不以日曆判定交易日的資料源
            （實盤以多個來源互相佐證）回 None，`is_market_open()` 的查表隨之關閉。
        - Return:
            - Optional[BaseTradingCalendar]
                區間內的交易日曆
        """

        pass

    def ensure_trading_days_in_range(self) -> None:
        """
        - Description:
            回測區間內一個交易日都沒有就拋出，不讓回測靜默跑完

            缺表在查詢層回空是對的（`OPTIONAL_TABLE`），但整場回測能不能跑是這一層的判斷：
            空日曆會讓每個曆日都判成休市，回測照樣跑完、結束碼 0、報表全零，
            批次跑的時候只剩一行 warning。行情表存在但為空（剛建表、尚未更新）也是同一個結果。
            **只算區間內的交易日**：期貨日曆會往結束日之後多取幾天給換月用，
            那幾天有資料不代表回測區間有。沒有區間或不建日曆的資料源（實盤）不檢查。
        - Raise:
            - EmptyCalendarError
                有區間、有日曆，但區間內沒有任何交易日
        """

        if not self.start_date or not self.end_date or self.calendar is None:
            return

        start: datetime.date = self.start_date
        end: datetime.date = self.end_date
        if any(start <= day <= end for day in self.calendar.trading_days):
            return

        raise EmptyCalendarError(
            f"回測區間 {start}～{end} 內沒有任何交易日：行情表缺表、是空的，或沒有這段期間的資料。"
            f"請先補資料（{self.BACKFILL_COMMAND}）或調整回測區間（--start／--end）"
        )

    def is_market_open(self, date: datetime.date) -> bool:
        """
        - Description:
            判斷指定日期是否為該市場的交易日：查 `setup()` 建好的日曆

            `setup()` 尚未跑過（單元測試直接呼叫）時先建一次；建不出日曆
            （沒有區間）就逐日向行情表查——每個曆日一次查詢，只留給沒設區間的策略。
            實盤覆寫成多來源互相佐證、判不出來一律拋出。
        - Parameters:
            - date: datetime.date
                待判定的日期
        - Return:
            - bool
                該日是否為交易日
        """

        if self.calendar is None:
            self.calendar = self.build_calendar()

        if self.calendar is None:
            return self.is_market_open_without_calendar(date)

        return self.calendar.is_trading_day(date)

    def is_market_open_without_calendar(self, date: datetime.date) -> bool:
        """沒有預建日曆時的逐日判定；預設視為休市，有行情表可逐日查的市場自行覆寫"""

        return False

    def report_calendar_gaps(self) -> int:
        """
        - Description:
            報告區間內「平日卻沒有行情」的日期

            **回測遇到缺日會當成休市靜默跳過**：資料缺一天與當天休市在引擎眼裡
            完全相同，策略少做一天的判斷卻不會有任何跡象。根治在 ETL，這裡負責
            **讓它在回測起跑時就被看見**。

            **扣掉交易所公告的休市日**（`get_official_closures()`）：國定假日不是缺口。
            只有官方行事曆已入庫的年度才下判斷；未入庫的年度無法分辨連假與缺日，
            那一段只報數字——寧可說不知道，也不要把連假講成資料缺失。
            沒有官方行事曆可對照的市場（期貨的歷史庫沒有那張表）整段只報數字。

            颱風假等臨時休市不在交易所年初公告的行事曆裡，這類日期會被列為
            「不在休市清單裡」。這是刻意的保守行為，不是漏判：
            看到那幾天時對照當年的颱風停班公告即可。
        - Return:
            - int
                無法歸因為休市的缺日數（含無法判斷的日期）
        """

        if not self.start_date or not self.end_date or self.calendar is None:
            return 0

        weekdays: Set[datetime.date] = {
            self.start_date + datetime.timedelta(days=offset)
            for offset in range((self.end_date - self.start_date).days + 1)
            if (self.start_date + datetime.timedelta(days=offset)).weekday() < 5
        }
        missing: Set[datetime.date] = weekdays - self.calendar.trading_day_set
        if not missing:
            return 0

        official: Optional[Tuple[Set[int], Set[datetime.date]]] = (
            self.get_official_closures(self.start_date, self.end_date)
        )
        if official is None:
            logger.info(
                f"[Calendar] 區間內有 {len(missing)} 個平日沒有行情；"
                f"本資料源沒有官方休市行事曆可對照，無法分辨連假與缺日"
            )
            return len(missing)

        covered_years, closures = official
        undetermined: List[datetime.date] = sorted(
            day for day in missing if day.year not in covered_years
        )
        gaps: List[datetime.date] = sorted(
            day for day in missing if day.year in covered_years and day not in closures
        )

        if undetermined:
            logger.info(
                f"[Calendar] 區間內有 {len(undetermined)} 個平日沒有行情，"
                f"落在官方休市行事曆未入庫的年度，無法分辨連假與缺日"
            )

        if gaps:
            logger.warning(
                f"[Calendar] 區間內有 {len(gaps)} 個平日沒有行情、"
                f"且**不在**官方休市清單裡，很可能是資料缺口："
                f"{gaps[:10]}" + ("…（僅列前 10 筆）" if len(gaps) > 10 else "")
            )
        elif not undetermined:
            logger.info(
                f"[Calendar] 區間內有 {len(missing)} 個平日沒有行情，"
                f"皆為官方公告的休市日"
            )

        return len(undetermined) + len(gaps)

    def get_official_closures(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> Optional[Tuple[Set[int], Set[datetime.date]]]:
        """
        - Description:
            官方休市行事曆：`(已入庫的年度, 區間內的休市日)`；沒有這份資料的市場回 None
        - Parameters:
            - start_date / end_date: datetime.date
                區間
        - Return:
            - Optional[Tuple[Set[int], Set[datetime.date]]]
                已入庫年度與休市日；None 表示無從對照
        """

        return None

    @staticmethod
    def connect_db(db_path: Union[str, Path], read_only: bool = False) -> DBConnection:
        """
        - Description:
            開歷史資料庫連線；**先確保目錄存在再連線**

            全新環境（CI、剛 clone 的機器）沒有 `data/db/`，`connect_sqlite()` 會丟出
            `unable to open database file`——那個訊息完全看不出是「目錄不存在」。
            此時開出來的是空 DB，查詢一律回空結果。唯讀連線不建目錄：那種情況本來就
            該讓「檔案不存在」現形。
        - Parameters:
            - db_path: Union[str, Path]
                資料庫檔案路徑
            - read_only: bool
                以唯讀模式開啟（實盤）
        - Return:
            - DBConnection
                連線
        """

        if not read_only:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return connect_sqlite(db_path, read_only=read_only)

    def data_apis(self) -> Tuple[Any, ...]:
        """本資料源建立的所有 API（供 `close()` 逐一關閉）；預設沒有"""

        return ()

    def close(self) -> None:
        """關閉所有 API 與資料連線；可重複呼叫（引擎以 `try/finally` 保證它跑到）"""

        for api in self.data_apis():
            if api is not None:
                api.close()

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_price_limit_basis(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日「漲跌停基準價」與前一交易日收盤不同的標的

            一般日子的基準就是前收盤，由 `FillModel` 自行累積即可；
            但除權息日的基準是交易所另行公告的**開盤競價基準**，沿用前收會讓
            整段漲跌停區間偏移。有這類公告的市場覆寫本方法即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 基準價}`；沒有這種公告的市場回傳空 dict（預設）
        """

        return {}

    def get_short_balance(self, date: datetime.date) -> Dict[str, int]:
        """
        - Description:
            取得當日可放空的券源餘額

            台股為融券今日餘額（張）。**空 dict 代表「查無資料」而非「都借不到」**，
            `FillModel` 查不到時一律放行。沒有券源概念的市場沿用預設即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, int]
                `{symbol: 可借券張數}`；預設為空 dict
        """

        return {}

    def get_force_cover_symbols(self, date: datetime.date) -> Set[str]:
        """
        - Description:
            取得當日觸發「停券強制回補」的標的

            台股為除權息前的融券最後回補日。**空集合代表「今日沒有標的停券」**，
            與券源餘額不同，這裡查不到就是真的沒有——停券日由行事曆推導，
            不存在「資料缺一天」的中間狀態。沒有停券制度的市場沿用預設即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Set[str]
                `{symbol}`；預設為空集合
        """

        return set()

    def get_short_suspended_symbols(self, date: datetime.date) -> Set[str]:
        """
        - Description:
            取得今日處於**停券期間**的標的

            與 `get_force_cover_symbols()` 的差別是「一天」與「一段」：後者是
            融券最後回補日當天，本方法涵蓋從那天起到除權息交易日之間的整段期間，
            這段期間制度上不得新增融券賣出。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Set[str]
                `{symbol}`；預設為空集合
        """

        return set()

    def get_short_sale_list(
        self, date: datetime.date
    ) -> Optional[ShortSaleListSnapshot]:
        """
        - Description:
            取得當日的平盤下得融（借）券賣出名單；沒有此制度的市場沿用預設即可
        - Return:
            - Optional[ShortSaleListSnapshot]
                預設為 None（沒有資料）
        """

        return None

    def get_day_trade_list(self, date: datetime.date) -> Optional[DayTradeListSnapshot]:
        """
        - Description:
            取得當日的現股當沖標的名單；沒有此制度的市場沿用預設即可
        - Return:
            - Optional[DayTradeListSnapshot]
                預設為 None（沒有資料）
        """

        return None

    def ensure_trading_list_coverage(
        self, check_short_sale_list: bool, check_day_trade_list: bool
    ) -> None:
        """
        - Description:
            開啟名單檢核時，確認回測區間每個交易日都有名單

            **預設是「有開就拒絕」而不是 no-op**：沒有名單的市場若被設定成開啟檢核，
            no-op 會讓開關靜靜失效，回測照跑、結果卻沒有任何一張單被檢查過。
        - Parameters:
            - check_short_sale_list / check_day_trade_list: bool
                成交模型是否開啟對應檢核
        - Raise:
            - TradingListCoverageError
                本市場沒有名單資料卻開啟了檢核
        """

        if check_short_sale_list or check_day_trade_list:
            raise TradingListCoverageError(
                f"{type(self).__name__} 沒有交易所名單資料，不能開啟名單檢核"
            )

    def get_cash_dividend_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日除息的每股現金股利

            供放空的股利補償計算使用（放空者須補償出借方當期股利）。
            值可能為 `NaN`——代表「有除權息但無法拆出現金股利」，
            與 key 不存在（當日未除息）語意不同，呼叫端不可一律當成 0。
        - Parameters:
            - date: datetime.date
                除權息交易日
        - Return:
            - Dict[str, float]
                `{symbol: 每股現金股利}`；預設為空 dict
        """

        return {}

    def get_share_ratio_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日的股數倍率（`新股數 / 舊股數`）

            配股、分割、減資會改變手上的股數與每股成本，而價格序列同時跳動。
            記帳端不跟著調整的話，張數不變、價格砍半，帳面就憑空虧一半——
            那與訊號面的還原價是兩回事，兩者都要做。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 股數倍率}`；預設為空 dict（沒有這種制度的市場）
        """

        return {}

    @abstractmethod
    def get_quotes(
        self,
        date: datetime.date,
        scale: Scale,
        adjusted: bool = False,
    ) -> List[BaseQuote]:
        """
        - Description:
            取得指定日期、指定級別的報價
        - Parameters:
            - date: datetime.date
                交易日
            - scale: Scale
                報價級別（DAY / TICK）
            - adjusted: bool
                是否附上還原價（掛在 `BaseQuote.adj_close`，OHLC 一律維持原始價）。
                不支援還原的市場忽略此參數即可
        - Return:
            - List[BaseQuote]
                該日報價；無資料時回傳空 list
        """

        pass
