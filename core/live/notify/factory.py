from typing import Optional

from loguru import logger

from core.live.notify.base import BaseNotifier, NullNotifier
from core.live.notify.telegram_notifier import TelegramNotifier

"""
推播管道的組裝

**獨立成一個模組而不是放在 `base.py`**：`telegram_notifier` 要 import `base`
拿骨架，`base` 若反過來 import 它（即使是惰性的）就形成循環——
`check_layer_deps.py` 會抓到，而惰性 import 只是把爆炸時間往後推到第一次呼叫。
"""


def build_notifier(
    channel: Optional[str],
    token: Optional[str],
    target: Optional[str],
    blocking: bool = False,
) -> BaseNotifier:
    """
    - Description:
        依設定建立推播管道；缺值時退化為不推播

        **退化要留下痕跡**：缺值時記 warning，由呼叫端一併寫進 `live_run`。
        靜默退化會讓人以為告警是通的——而「以為有告警其實沒有」
        比「知道沒有告警」危險得多。
    - Parameters:
        - channel: Optional[str]
            管道名稱；目前只有 `telegram`
        - token: Optional[str]
            管道憑證
        - target: Optional[str]
            送達對象
        - blocking: bool
            是否同步送出
    - Return:
        - BaseNotifier
            推播管道；設定不全時為 `NullNotifier`
    """

    if not channel:
        logger.warning("未設定通知管道，本次執行不會有任何推播")
        return NullNotifier(blocking)

    if channel.lower() != "telegram":
        logger.warning(f"不支援的通知管道 {channel}，本次執行不會有任何推播")
        return NullNotifier(blocking)

    if not token or not target:
        logger.warning("通知管道缺少 token 或送達對象，本次執行不會有任何推播")
        return NullNotifier(blocking)

    return TelegramNotifier(token, target, blocking)
