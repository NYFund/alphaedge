import datetime
from typing import Any, Dict

from loguru import logger

from core.utils import TimeUtils

"""
TPEX（櫃買中心）區間查詢端點的共用防呆

TPEX 的公告端點（除權息、減資恢復買賣）收 `startDate`／`endDate`，**日期格式
送錯時不會報錯**，而是回傳站方預設的區間（或不帶區間）。不擋下來，拿到的
就是錯的那一段，照樣入庫、而且不會有任何錯誤訊息。回應 JSON 的 `date` 欄位
會回報實際套用的區間，比對它是唯一抓得到的方法。
"""


def check_tpex_date_range(
    payload: Dict[str, Any],
    start_date: datetime.date,
    end_date: datetime.date,
    label: str,
) -> bool:
    """
    - Description:
        確認 TPEX 回傳的區間與送出的區間一致

        回應的 `date` 格式為 `YYYYMMDD~YYYYMMDD`（2026-09-21 以減資端點實測；
        送出的日期不是斜線格式時該欄位為 None）。
    - Parameters:
        - payload: Dict[str, Any]
            端點回傳的 JSON
        - start_date / end_date: datetime.date
            送出的查詢區間
        - label: str
            log 用的來源名稱
    - Return:
        - bool
            區間相符為 True；不符時記 warning 並回 False
    """

    expected: str = (
        f"{TimeUtils.format_date(start_date)}~{TimeUtils.format_date(end_date)}"
    )
    actual: str = str(payload.get("date", ""))

    if actual != expected:
        logger.warning(
            f"{label} date range mismatch: requested {expected}, got {actual}. "
            f"Aborting to avoid ingesting the wrong period"
        )
        return False

    return True
