from typing import Optional

from loguru import logger

from core.live.notify.base import BaseNotifier, NullNotifier
from core.live.notify.discord_notifier import DiscordNotifier
from core.live.notify.telegram_notifier import TelegramNotifier

"""
推播管道的組裝

**獨立成一個模組而不是放在 `base.py`**：各管道要 import `base`
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
            管道名稱：`discord` 或 `telegram`
        - token: Optional[str]
            管道憑證：Discord 為 webhook URL；Telegram 為 bot token
        - target: Optional[str]
            送達對象：Telegram 的 chat id；Discord 的 webhook 已綁定頻道，不需要
        - blocking: bool
            是否同步送出
    - Return:
        - BaseNotifier
            推播管道；設定不全時為 `NullNotifier`
    """

    if not channel:
        logger.warning("未設定通知管道，本次執行不會有任何推播")
        return NullNotifier(blocking)

    name: str = channel.lower()

    if name == "discord":
        if not token:
            logger.warning("Discord 缺少 webhook URL，本次執行不會有任何推播")
            return NullNotifier(blocking)
        # 只貼了 webhook token 而非整串 URL 是常見誤設，送出時才會 404；
        # 在組裝時擋下，warning 跟著寫進 `live_run`，不會被當成告警已接通
        if not (token.startswith("https://") and "/api/webhooks/" in token):
            logger.warning(
                "Discord 的 token 必須是完整的 webhook URL，本次執行不會有任何推播"
            )
            return NullNotifier(blocking)
        return DiscordNotifier(token, blocking)

    if name == "telegram":
        if not token or not target:
            logger.warning("Telegram 缺少 token 或 chat id，本次執行不會有任何推播")
            return NullNotifier(blocking)
        return TelegramNotifier(token, target, blocking)

    logger.warning(f"不支援的通知管道 {channel}，本次執行不會有任何推播")
    return NullNotifier(blocking)
