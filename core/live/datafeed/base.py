import datetime
from abc import abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from core.backtest.datafeed.base import BaseDataFeed
from core.config.settings import now_live
from core.live.datafeed.calendar import (
    TradingCalendarSource,
    resolve_trading_day,
)
from core.models import BaseQuote
from core.utils import ExecutionTiming

"""
BaseLiveDataFeed：歷史資料到 T−1，今天的報價由券商提供

**繼承 `BaseDataFeed`**，因為策略的 `setup_apis(feed)` 型別就是它——策略不必改。
（把 `BaseDataFeed` 移到中立位置不在本次範圍，這條同層邊已在分層規則裡登記。）

兩道啟動檢查，少任何一道都會讓策略拿到錯的輸入而不報錯：

1. **資料新鮮度**：`price` 表要更新到前一個交易日。沒有這道檢查，
   策略會拿前天的資料當昨天用，訊號錯了也不會有任何徵兆。
2. **交易日判定**：休市日不該啟動。判斷不出來就拒絕，**不預設為開市**。
"""


class DataFreshnessError(RuntimeError):
    """歷史資料沒有更新到前一個交易日"""


class BaseLiveDataFeed(BaseDataFeed):
    """
    - Description:
        實盤資料源的共用骨架

        歷史資料一律**唯讀**開啟：實盤行程不該寫研究庫，而唯讀也避免它與
        背景 ETL 搶寫入鎖。
    """

    # `price` 表最新日與今天的最大容許間隔（曆日）。
    # 取 5 是為了容納「週五收盤 → 下週一開盤」再加一天國定假日；
    # 更長的間隔代表資料真的停了，而不是連假
    MAX_DATA_GAP_DAYS: int = 5

    def __init__(
        self,
        broker: Any,
        calendar_sources: Optional[Sequence[TradingCalendarSource]] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
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
        """

        self.broker: Any = broker
        self.calendar_sources: List[TradingCalendarSource] = list(
            calendar_sources or []
        )
        self._now: Callable[[], datetime.datetime] = now_provider

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

            **第三種只擋得住「來源確定說是交易日」的情況**。目前平日多半判不出來，
            所以主要靠前兩種；那是缺官方日曆表的直接後果，已列為已知限制。
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

    @abstractmethod
    def get_latest_data_date(self) -> Optional[datetime.date]:
        """歷史資料表的最新交易日；表為空時回 None"""
        pass

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
