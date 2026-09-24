import datetime
import json
from typing import Any, Callable, Dict, Optional

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.notify.base import NotifyLevel

"""
風控事件的**唯一**寫入口

各呼叫端自己呼叫 `dao.insert_risk_event()` 的話，`if self.dao is None: return`
的降級守衛、`run_id`／`occurred_at` 的組法與 severity 的字面值就會被抄很多份，
而**抄一份的代價不是行數，是它們會各自漂移**：severity 一旦寫成 `"WARNING"`
（`NotifyLevel` 只認得 `"WARN"`），寫進資料庫沒問題，推播卻會在
`NotifyLevel(level)` 拋例外、被 `notify_safely()` 吞掉——
**事件有紀錄、該收到通知的人收不到，而且沒有人會發現**。

故 `severity` 這裡只收 `NotifyLevel`，不收字串：拼錯在型別層就過不了。
"""


class RiskEventLogger:
    """
    - Description:
        把風控事件寫進 `live_risk_event`

        **`dao` 允許為 None**：測試常只驗判定邏輯、不想帶一個資料庫。
        降級行為（只留 log、不寫庫）集中在這裡一處，不再由每個呼叫端各寫一次。
    """

    def __init__(
        self,
        dao: Optional[LiveTradeDAO],
        run_id: str,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立寫入器
        - Parameters:
            - dao: Optional[LiveTradeDAO]
                紀錄庫；None 時只留 log
            - run_id: str
                本次執行的識別碼
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
        """

        self.dao: Optional[LiveTradeDAO] = dao
        self.run_id: str = run_id
        self._now: Callable[[], datetime.datetime] = now_provider

    def write(
        self,
        category: str,
        severity: NotifyLevel,
        message: str,
        strategy_name: Optional[str] = None,
        symbol: Optional[str] = None,
        client_order_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> None:
        """
        - Description:
            寫一筆風控事件

            **`severity` 收 `NotifyLevel` 而不是字串**：它是推播分級的唯一來源，
            拼錯的話事件照樣入庫、推播卻會靜靜失敗。收 Enum 讓拼錯在型別層就擋下。
        - Parameters:
            - category: str
                事件類別（Ex: `RECONCILE_MISMATCH`）
            - severity: NotifyLevel
                嚴重度
            - message: str
                人話說明
            - strategy_name / symbol / client_order_id: Optional[str]
                事件的歸屬；不適用時留 None
            - detail: Optional[Dict[str, Any]]
                結構化細節，序列化成 `detail_json`
            - commit: bool
                是否立即 commit；**交易區塊內的呼叫要傳 False**
        """

        if self.dao is None:
            logger.debug(f"[{category}] {message}（無紀錄庫，只留 log）")
            return

        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": strategy_name,
                "severity": severity.value,
                "category": category,
                "symbol": symbol,
                "client_order_id": client_order_id,
                "message": message,
                "detail_json": json.dumps(detail, ensure_ascii=False)
                if detail is not None
                else None,
                "occurred_at": self._now(),
            },
            commit=commit,
        )
