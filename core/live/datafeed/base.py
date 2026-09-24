import datetime
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from core.config.settings import now_live
from core.dao.connection import DBConnection
from core.datafeed.base import BaseDataFeed
from core.live.datafeed.calendar import (
    TradingCalendarSource,
    resolve_trading_day,
)
from core.models import BaseOrder, BaseQuote
from core.utils import ExecutionTiming, Scale

"""
BaseLiveDataFeed：歷史資料到 T−1，今天的報價由券商提供

**繼承 `BaseDataFeed`**，因為策略的 `setup_apis(feed)` 型別就是它——策略不必改。
那個契約放在中立的 `core/datafeed/`：回測與實盤共用同一份，誰都不必 import 對方的套件。

兩道啟動檢查，少任何一道都會讓策略拿到錯的輸入而不報錯：

1. **資料新鮮度**：`price` 表要更新到前一個交易日。沒有這道檢查，
   策略會拿前天的資料當昨天用，訊號錯了也不會有任何徵兆。
2. **交易日判定**：休市日不該啟動。判斷不出來就拒絕，**不預設為開市**。
"""


@dataclass
class RollPlan:
    """
    一筆轉倉：先平舊契約、再開新契約

    **兩腿有順序相依，不可獨立送出**：平倉腿沒成交就送開倉腿，會同時持有兩個
    契約、曝險翻倍。由 `LiveTrader` 依序執行，市場語意（哪個契約、何時換）
    留在各市場的資料源裡決定。
    """

    close_order: BaseOrder  # 平掉舊契約
    open_order: BaseOrder  # 以相同方向與數量開新契約
    reason: str  # 給人看的換月原因（寫進事件與 log）


class DataFreshnessError(RuntimeError):
    """歷史資料沒有更新到前一個交易日"""


class BaseLiveDataFeed(BaseDataFeed):
    """
    - Description:
        實盤資料源的共用骨架

        歷史資料一律**唯讀**開啟：實盤行程不該寫研究庫，而唯讀也避免它與
        背景 ETL 搶寫入鎖。
    """

    # 歷史資料表最新日與今天的最大容許間隔（曆日）。
    # 取 5 是為了容納「週五收盤 → 下週一開盤」再加一天國定假日；
    # 更長的間隔代表資料真的停了，而不是連假
    MAX_DATA_GAP_DAYS: int = 5

    # 存放每日行情的歷史資料表；`get_latest_data_date()` 以它查最新交易日。
    # 只有表名不同的話，子類宣告這一行就夠了，不必各抄一份查詢
    LATEST_DATE_TABLE: str = ""

    # 策略要歷史資料時該走哪個 API；寫進 `get_quotes()` 的拒絕訊息，
    # 讓看到錯誤的人知道替代路徑是什麼，而不只是知道這條路不通
    HISTORY_API_HINT: str = "API"

    def __init__(
        self,
        broker: Any,
        calendar_sources: Optional[Sequence[TradingCalendarSource]] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
        db_path: Any = None,
    ) -> None:
        """
        - Description:
            建立實盤資料源
        - Parameters:
            - broker: Any
                券商閘道；今天的報價由它提供
            - calendar_sources: Optional[Sequence[TradingCalendarSource]]
                交易日來源；None 時由子類建立預設組合
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
            - db_path: Any
                歷史資料庫路徑；連線由子類在 `setup()` 內建立
        """

        self.broker: Any = broker
        self.calendar_sources: List[TradingCalendarSource] = list(
            calendar_sources or []
        )
        self._now: Callable[[], datetime.datetime] = now_provider
        self.db_path: Any = db_path
        self.conn: Optional[DBConnection] = None

    # === 交易日 ===
    def is_market_open(self, date: datetime.date) -> bool:
        """
        - Description:
            是否為交易日；判斷不出來一律拋出，**不預設為開市**
        - Parameters:
            - date: datetime.date
                待判定的日期
        - Return:
            - bool
                是否為交易日
        - Raise:
            - TradingCalendarUnavailableError
                沒有來源給出答案，或來源互相衝突
        """

        return resolve_trading_day(date, self.calendar_sources)

    def _broker_contract_update_date(self) -> Optional[datetime.date]:
        """
        - Description:
            券商合約檔的更新日期；取不到時回 None（**不猜**）

            合約檔每個交易日更新一次，「更新日 ＝ 今天」是有開市的佐證。
            官方日曆的年度尚未入庫時，平日就只剩這一個來源作答，
            所以它取不到值就等於當天判不出開市與否。
        - Return:
            - Optional[datetime.date]
                合約檔更新日期
        """

        resolver: Any = getattr(self.broker, "resolver", None)
        if resolver is None:
            return None

        try:
            contract: Optional[Any] = self._probe_contract(resolver)
        except Exception as exc:
            # 用 warning 而不是 debug：這是平日交易日判定的唯一佐證，
            # 它失效時整個判定跟著失效，而 debug 等級在正式部署一定看不到
            logger.warning(f"取合約檔更新日期失敗：{exc}")
            return None

        if contract is None:
            return None

        raw: Any = getattr(contract, "update_date", None)
        if isinstance(raw, datetime.date):
            return raw
        try:
            return datetime.date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    @abstractmethod
    def _probe_contract(self, resolver: Any) -> Optional[Any]:
        """
        - Description:
            取一張**本市場**的合約，用來讀合約檔的更新日期；取不到時回 None

            **由子類決定探測哪一張，不可共用**：合約檔逐市場更新，拿股票合約
            去佐證期貨的交易日，在兩個市場開休市不一致的那天會直接判錯——
            而 `BrokerContractCalendarSource` 只會回 True 或 None，
            判錯的方向是「誤判為開市」，不會有任何錯誤訊息。
        - Parameters:
            - resolver: Any
                券商的合約解析器
        - Return:
            - Optional[Any]
                合約物件
        """
        pass

    # === 資料新鮮度 ===
    def verify_data_freshness(self, today: Optional[datetime.date] = None) -> None:
        """
        - Description:
            檢查歷史資料是否更新到前一個交易日

            三種失敗各自有不同的成因，訊息要分得開：
            - **最新日 ≥ 今天**：`price` 表已經有今天的資料。盤前不該有，
              多半是有人手動灌了資料或系統日期錯了。
            - **間隔過大**：資料停住了（ETL 掛掉、磁碟滿了）。
            - **中間有明確的交易日**：那天的資料漏了。

            **第三種只擋得住「來源確定說是交易日」的情況**。官方日曆未涵蓋的年度
            平日答不出明確結果（券商合約檔只答得出今天），那些日子只剩前兩種擋得住。
        - Parameters:
            - today: Optional[datetime.date]
                今天；None 時取系統時間
        - Raise:
            - DataFreshnessError
                資料沒有更新到前一個交易日
        """

        current: datetime.date = today or self._now().date()
        latest: Optional[datetime.date] = self.get_latest_data_date()

        if latest is None:
            raise DataFreshnessError(
                "歷史資料表是空的，無法啟動；請先跑 `python -m tasks.update_db`"
            )

        if latest >= current:
            raise DataFreshnessError(
                f"歷史資料最新日 {latest} 不早於今天 {current}。"
                "盤前不該有今天的資料——請確認系統日期，以及是否有人手動灌了資料"
            )

        gap: int = (current - latest).days
        if gap > self.MAX_DATA_GAP_DAYS:
            raise DataFreshnessError(
                f"歷史資料最新日 {latest} 距今 {gap} 個曆日，超過容許的 "
                f"{self.MAX_DATA_GAP_DAYS} 天；請先跑 `python -m tasks.update_db`"
            )

        missing: List[datetime.date] = self._definite_trading_days_between(
            latest, current
        )
        if missing:
            raise DataFreshnessError(
                f"歷史資料最新日是 {latest}，但 {missing} 確定是交易日而資料缺漏；"
                "請先跑 `python -m tasks.update_db`"
            )

        logger.info(f"歷史資料新鮮度檢查通過：最新日 {latest}，今天 {current}")

    def _definite_trading_days_between(
        self, latest: datetime.date, current: datetime.date
    ) -> List[datetime.date]:
        """
        找出 `(latest, current)` 之間**確定**是交易日的日子

        判斷不出來的日子不算缺漏——這裡寧可漏報也不要誤報：
        誤報會讓人在正常的日子被擋住啟動，然後學會忽略這個檢查。
        """

        definite: List[datetime.date] = []
        cursor: datetime.date = latest + datetime.timedelta(days=1)
        while cursor < current:
            for source in self.calendar_sources:
                if source.is_trading_day(cursor) is True:
                    definite.append(cursor)
                    break
            cursor += datetime.timedelta(days=1)
        return definite

    def get_latest_data_date(self) -> Optional[datetime.date]:
        """
        - Description:
            歷史資料表的最新交易日；表為空或尚未連線時回 None

            表名由子類的 `LATEST_DATE_TABLE` 指定——與 `MAX_DATA_GAP_DAYS`
            同一個模式：子類只宣告差異的那一項，查詢本身不必各抄一份。
        - Return:
            - Optional[datetime.date]
                最新交易日
        """

        if not self.LATEST_DATE_TABLE:
            raise NotImplementedError(
                f"{type(self).__name__} 沒有宣告 LATEST_DATE_TABLE，"
                "查不到歷史資料最新日，新鮮度檢查無法進行"
            )

        if self.conn is None:
            return None

        # 表名不能用 `params=(...)` 佔位符（SQLite 的佔位符只吃值），
        # 而它來自子類的類別常數、不是外部輸入，故直接組進字串
        rows: List[Any] = self.conn.execute(
            f"SELECT MAX(date) FROM {self.LATEST_DATE_TABLE}"
        ).fetchall()
        raw: Any = rows[0][0] if rows else None
        if not raw:
            return None
        return datetime.date.fromisoformat(str(raw))

    # === 即時報價 ===
    @abstractmethod
    def get_live_quotes(
        self, timing: ExecutionTiming, symbols: Sequence[str]
    ) -> List[BaseQuote]:
        """
        - Description:
            取得某個段落的即時報價

            開盤段回傳的是 `PreOpen*Quote`（讀 OHLC 會拋出），
            尾盤段與盤中回傳一般報價。
        - Parameters:
            - timing: ExecutionTiming
                執行段落
            - symbols: Sequence[str]
                商品代號
        - Return:
            - List[BaseQuote]
                報價；查無資料的標的不會出現
        """
        pass

    def get_price_limit_basis(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得漲跌停基準價

            **實盤用交易所公告值**（券商合約檔的 `reference`），
            不是 `TwStockSpec` 的公式推算：除權息日的基準是另行公告的開盤競價基準，
            沿用前收會讓整段漲跌停區間偏移。

            預設回空 dict（沿用骨架語意）；能取得公告值的子類自行覆寫。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 基準價}`
        """

        return {}

    @staticmethod
    def _as_optional_float(contract: Any, field: str) -> Optional[float]:
        """取合約的浮點欄位；缺值回 None（不填 0，0 會被當成一個真實價格）"""

        value: Any = getattr(contract, field, None)
        return float(value) if value else None

    # === 歷史報價與連線 ===
    def get_quotes(
        self, date: datetime.date, scale: Scale, adjusted: bool = False
    ) -> List[BaseQuote]:
        """
        - Description:
            歷史報價（T−1 以前）；今天的報價請走 `get_live_quotes()`

            **今天一律拒絕**：歷史資料表要到收盤後才有今天的資料，
            這裡若靜默回空 list，策略會以為今天全市場都沒有報價。

            更早的日期也不從歷史表逐日取——實盤的策略是走 API 拿歷史資料的。
        - Parameters:
            - date: datetime.date
                交易日
            - scale: Scale
                報價級別
            - adjusted: bool
                是否附上還原價
        - Return:
            - List[BaseQuote]
                該日報價
        - Raise:
            - ValueError
                查詢今天或未來的日期
            - NotImplementedError
                查詢過去的日期
        """

        if date >= self._now().date():
            raise ValueError(
                f"{date} 不早於今天：歷史報價只到前一個交易日，"
                "今天的報價請用 get_live_quotes()"
            )

        raise NotImplementedError(
            f"實盤不從歷史表逐日取報價；策略需要歷史資料時走 API（{self.HISTORY_API_HINT}）"
        )

    def close(self) -> None:
        """關閉歷史資料連線；可重複呼叫（引擎以 `try/finally` 保證它跑到）"""

        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def plan_rolls(
        self, positions: Sequence[Any], today: datetime.date
    ) -> List[RollPlan]:
        """
        - Description:
            今天要轉倉的部位；不需要轉倉的市場（例如股票）回空清單
        - Parameters:
            - positions: Sequence[Any]
                該策略的部位
            - today: datetime.date
                交易日
        - Return:
            - List[RollPlan]
                轉倉計畫
        """

        return []
