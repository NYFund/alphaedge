from typing import Any

import requests

from core.live.notify.base import SEND_TIMEOUT_SECONDS, BaseNotifier, NotifyLevel

"""DiscordNotifier：以 Discord webhook 送出推播"""

# 等級前綴；手機上一眼就能分出輕重，不必點進去看內容
_LEVEL_PREFIX: dict = {
    NotifyLevel.INFO: "ℹ️",
    NotifyLevel.WARN: "⚠️",
    NotifyLevel.CRITICAL: "🚨",
}

# Discord 單則訊息的 `content` 上限（字元）；超過整則會被拒收（400），
# 截斷後至少標題與開頭送得到，好過整則消失
_MAX_CONTENT_LENGTH: int = 2000
_TRUNCATED_SUFFIX: str = "\n…（已截斷，完整內容見 log）"


class DiscordNotifier(BaseNotifier):
    """
    - Description:
        以 Discord webhook 送出推播

        **webhook URL 本身就是憑證**：任何拿到它的人都能往頻道發訊息，
        所以和 Telegram 的 bot token 一樣只存在實例裡，錯誤訊息只印狀態碼、不印 URL。
    """

    def __init__(self, webhook_url: str, blocking: bool = False) -> None:
        """
        - Description:
            建立 Discord 推播管道
        - Parameters:
            - webhook_url: str
                頻道的 webhook URL（`https://discord.com/api/webhooks/{id}/{token}`）
            - blocking: bool
                是否同步送出
        """

        super().__init__(blocking)
        self._webhook_url: str = webhook_url

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
                Discord 回非 2xx（由骨架吞掉並記 log）
        """

        prefix: str = _LEVEL_PREFIX.get(level, "")
        content: str = f"{prefix} **{title}**\n{body}"
        if len(content) > _MAX_CONTENT_LENGTH:
            keep: int = _MAX_CONTENT_LENGTH - len(_TRUNCATED_SUFFIX)
            content = content[:keep] + _TRUNCATED_SUFFIX

        try:
            response: Any = requests.post(
                self._webhook_url,
                # 內容含錯誤訊息等外部字串，關掉 mention 解析，
                # 避免出現 `@everyone` 時對整個伺服器發通知
                json={"content": content, "allowed_mentions": {"parse": []}},
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            # 連線失敗與逾時的例外訊息帶著完整 URL（含 webhook token），骨架又會連同
            # traceback 記進 log；`from None` 切斷原例外，這一行也刻意不引用例外物件，
            # 否則 loguru 的 diagnose 會把它的值印出來
            raise RuntimeError("Discord 連線失敗（逾時或無法連線）") from None

        # 成功時回 204（未帶 `?wait=true`）
        if not 200 <= response.status_code < 300:
            # **不印 URL**：它帶著 webhook token，而錯誤訊息會進 log
            raise RuntimeError(f"Discord 回應 {response.status_code}")
