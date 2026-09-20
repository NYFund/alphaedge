import datetime
from dataclasses import dataclass
from typing import Dict, Optional

from core.utils import ExecutionTiming

"""
段落時窗：每個執行段落的「何時開始送單、何時停止送單、何時可以收線」

**送單時限與連線關閉時點必須分開。** 尾盤段送出的委託，成交回報要等收盤集合競價
撮合完才會進來；送完單就關連線的話，當日的成交明細會出現一段空窗，
而對帳會在錯的時點判定不一致。

時點本身是**市場特性**（台股 13:25 才送、期貨依 TAIFEX 公告），故一律由外部注入，
引擎本體不持有任何時刻常數。
"""


@dataclass(frozen=True)
class SegmentWindow:
    """
    - Description:
        單一段落的三個時點

        `submit_start` 之前不送單是有理由的：台股尾盤段若在 13:25 之前送出限價單，
        它會在逐筆交易時段就成交，成交價不是收盤價——與回測「以收盤價成交」的假設
        對不上，而且看起來完全正常。
    """

    submit_start: datetime.time  # 可以開始送單的時刻
    submit_end: datetime.time  # 必須停止送單的時刻
    drain_end: datetime.time  # 停止等待回報、可以收線的時刻

    def __post_init__(self) -> None:
        """時點順序寫反會讓整段行為顛倒，建立時就擋下"""

        if not self.submit_start <= self.submit_end <= self.drain_end:
            raise ValueError(
                f"段落時點順序錯誤：送單起 {self.submit_start} → 送單迄 "
                f"{self.submit_end} → 收線 {self.drain_end}"
            )


SegmentSchedule = Dict[ExecutionTiming, SegmentWindow]


def resolve_window(
    schedule: SegmentSchedule, timing: ExecutionTiming
) -> Optional[SegmentWindow]:
    """
    - Description:
        取得某個段落的時窗；未定義時回 None（由呼叫端決定是否放行）
    - Parameters:
        - schedule: SegmentSchedule
            段落時窗表
        - timing: ExecutionTiming
            執行段落
    - Return:
        - Optional[SegmentWindow]
            時窗
    """

    return schedule.get(timing)
