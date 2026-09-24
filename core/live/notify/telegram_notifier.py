from typing import Any

import requests

from core.live.notify.base import SEND_TIMEOUT_SECONDS, BaseNotifier, NotifyLevel

"""TelegramNotifier：以 Telegram Bot API 送出推播"""

# 等級前綴；手機上一眼就能分出輕重，不必點進去看內容
_LEVEL_PREFIX: dict = {
    NotifyLevel.INFO: "ℹ️",
    NotifyLevel.WARN: "⚠️",
    NotifyLevel.CRITICAL: "🚨",
}


class TelegramNotifier(BaseNotifier):
    """
    - Description:
        以 Telegram Bot API 送出推播

        **token 只存在實例裡，不進 log 也不進訊息**：`BaseNotifier.send()` 會先
        遮蔽內容，但 URL 本身帶著 token，所以錯誤訊息一律只印狀態碼，不印 URL。
    """

    API_TEMPLATE: str = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, blocking: bool = False) -> None:
        """
        - Description:
            建立 Telegram 推播管道
        - Parameters:
            - token: str
                Bot token
            - chat_id: str
                送達對象
            - blocking: bool
                是否同步送出
        """

        super().__init__(blocking)
        self._token: str = token
        self._chat_id: str = chat_id

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        """
        - Description:
            送出訊息；**逾時設得短**，通知是旁路
        - Parameters:
            - level: NotifyLevel
                等級
            - title: str
                標題
            - body: str
                內容
        - Raise:
            - RuntimeError
                Telegram 回非 200（由骨架吞掉並記 log）
        """

        prefix: str = _LEVEL_PREFIX.get(level, "")
        response: Any = requests.post(
            self.API_TEMPLATE.format(token=self._token),
            json={"chat_id": self._chat_id, "text": f"{prefix} {title}\n{body}"},
            timeout=SEND_TIMEOUT_SECONDS,
        )

        if response.status_code != 200:
            # **不印 URL**：它帶著 bot token，而錯誤訊息會進 log
            raise RuntimeError(f"Telegram 回應 {response.status_code}")
