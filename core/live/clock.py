import datetime
import time
from typing import Callable, Optional

from core.config.settings import now_live
from core.market.segment import SegmentWindow

"""
SegmentClock：段落時窗的時間判斷

**只看時間、不寫事件、不推播**。引擎本體與送單、換月、盤中三個元件都要判斷
「現在過了時窗上的某個時點沒有」「還在不在送單時窗內」，等待迴圈也都以
「開始等待時離目標還有多久 ＋ 寬限」當作獨立於時鐘的保險絲。這些判斷只需要
時鐘與等待函式，集中在一個小物件上，元件之間才不必互相注入三個回呼。

時鐘與等待函式都是可替換的屬性（`now`、`sleep`）：測試用假時鐘推進時間，
引擎本體與各元件共用同一個物件，換掉一處就全部換掉。
"""


class SegmentClock:
    """段落時窗的時間判斷：持有時鐘與等待函式，判斷本身只讀時間"""

    # 沒有回報時的輪詢間隔（秒）。太短會空轉吃 CPU，太長會讓撤單與時限判斷變遲鈍
    POLL_INTERVAL_SECONDS: float = 0.5

    # 等待的安全上限＝開始等待時離目標時點還有多久 ＋ 這段寬限（秒）。
    # **這條是獨立於時鐘的保險絲**：時窗判斷靠 `now()`，而它若因為時鐘卡住、
    # 倒退或注入錯誤而不再前進，迴圈會永遠轉下去——段落不結束，下一個段落的行程
    # 拿不到連線與寫入鎖，而存活監控只會看到「開始了但沒有正常結束」。故另外累計
    # 實際睡過的秒數，超過就強制跳出並記 error。
    # **上限跟著時窗長度走**：寫死一個秒數的話，比它長的時窗（股票開盤段 35 分鐘）
    # 會在時鐘完全正常時撞到上限，天天記一條「請確認系統時鐘」的假警報
    WAIT_GRACE_SECONDS: float = 300.0

    def __init__(
        self,
        now_provider: Callable[[], datetime.datetime] = now_live,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        - Description:
            建立時鐘
        - Parameters:
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - sleep: Callable[[float], None]
                等待函式
        """

        self.now: Callable[[], datetime.datetime] = now_provider
        self.sleep: Callable[[float], None] = sleep

    def today(self) -> datetime.date:
        """目前的交易日（以時鐘的日期為準）"""

        return self.now().date()

    def past(self, window: Optional[SegmentWindow], attribute: str) -> bool:
        """目前時刻是否已過時窗上的某個時點；沒有時窗時永遠為否"""

        if window is None:
            return False
        return self.now().time() >= getattr(window, attribute)

    def outside_window(self, window: Optional[SegmentWindow]) -> Optional[str]:
        """目前時刻不在送單時窗內時回原因（還沒到／已過），在時窗內回 None"""

        if window is None:
            return None
        now: datetime.time = self.now().time()
        if now < window.submit_start:
            return f"尚未到送單時窗（{window.submit_start}）"
        if now >= window.submit_end:
            return "已過送單時限"
        return None

    def wait_cap_seconds(self, target: datetime.time) -> float:
        """
        - Description:
            這次等待的安全上限：開始等待時離目標時點的秒數 ＋ 寬限

            只在開始等待時讀一次時鐘。之後時鐘卡住也不影響上限，
            迴圈仍會在累計睡滿上限後跳出。
        - Parameters:
            - target: datetime.time
                要等到的時點（同一個交易日）
        - Return:
            - float
                上限秒數；目標已過時只剩寬限
        """

        now: datetime.datetime = self.now()
        target_at: datetime.datetime = datetime.datetime.combine(
            now.date(), target, tzinfo=now.tzinfo
        )
        remaining: float = (target_at - now).total_seconds()
        return max(remaining, 0.0) + self.WAIT_GRACE_SECONDS
