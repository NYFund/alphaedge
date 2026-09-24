import re
import threading
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from loguru import logger

"""
事件通知：實盤最常見的失效不是下錯單，而是**該跑的沒跑、沒人發現**

`live_risk_event` 與 log 都是 pull 型：人要先去看才知道。kill switch 也一樣——
要先察覺不對勁，才會有人去建那個檔案。程式在 09:05 因為登入失敗退出，
到收盤都不會有人知道。

三條鐵律：

1. **推播失敗絕不可影響交易主流程。** `send()` 內部吞掉所有例外。
   監控拖垮被監控的東西是典型反例。
2. **不阻塞。** 送出走背景執行緒——通知端的網路逾時不該讓尾盤那 4 分鐘卡住。
3. **內容不得包含金鑰、憑證密碼或完整帳號。** 推播會被轉發、截圖、貼進群組，
   它的傳播範圍遠大於 log。

**相依刻意壓到最低**：除了標準函式庫與 `logger`，只允許 `core.config`／`core.utils`，
不碰模型也不碰 DAO——旁路的相依越少，它壞掉能拖垮的東西就越少。
"""

# 推播等級常量
NOTIFY_LEVEL_INFO = "INFO"
NOTIFY_LEVEL_WARN = "WARN"
NOTIFY_LEVEL_CRITICAL = "CRITICAL"

# 送出的逾時（秒）。**短而明確**：通知是旁路，寧可漏一則也不要讓它拖住任何東西
SEND_TIMEOUT_SECONDS: float = 5.0

# 遮蔽用的樣式。
#
# **與券商 session 的刮除是兩件事，不是重複**：那邊是在**來源**把登入請求的
# 簽章負載刮掉（例外訊息一產生就帶著它）；這裡是在**出口**把帳號與長字串遮掉，
# 因為推播的傳播範圍遠大於 log。
_LONG_TOKEN_PATTERN: re.Pattern = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_ACCOUNT_PATTERN: re.Pattern = re.compile(r"\b\d{7,}\b")
_REDACTED: str = "<已遮蔽>"


class NotifyLevel(str, Enum):
    """
    推播等級

    | 等級 | 事件 |
    |------|------|
    | `INFO` | 段落啟動、段落正常結束、日終摘要 |
    | `WARN` | 風控拒單、批次曝險截斷、報價過期、斷線重連 |
    | `CRITICAL` | 對帳不一致、交易模式降級、kill switch、登入失敗、平倉單未成交、段落未在時限內完成 |

    等級**直接取自 `live_risk_event.severity`**，不要在推播端再寫一份判斷——
    兩份判斷必然漂移，而漂移的那一刻兩邊看起來都正確。
    """

    INFO = NOTIFY_LEVEL_INFO
    WARN = NOTIFY_LEVEL_WARN
    CRITICAL = NOTIFY_LEVEL_CRITICAL


def redact(text: str) -> str:
    """
    - Description:
        遮蔽推播內容中的敏感片段

        帳號只留末四碼，長字串（金鑰、token、憑證指紋）整段遮掉。
        **寧可遮過頭**：漏遮一次的代價是金鑰流出去，而遮過頭只是要多查一次 log。
    - Parameters:
        - text: str
            原始內容
    - Return:
        - str
            可安全送出的內容
    """

    def mask_account(match: re.Match) -> str:
        digits: str = match.group(0)
        return f"****{digits[-4:]}"

    cleaned: str = _LONG_TOKEN_PATTERN.sub(_REDACTED, text)
    return _ACCOUNT_PATTERN.sub(mask_account, cleaned)


class BaseNotifier(ABC):
    """
    - Description:
        推播管道的共用骨架

        子類別只實作 `deliver()`；例外吞掉、內容遮蔽、背景送出都由骨架處理，
        **不要讓每個管道各寫一次**——漏寫的那一個會在最需要它的時候把主流程弄死。
    """

    def __init__(self, blocking: bool = False) -> None:
        """
        - Description:
            建立推播管道
        - Parameters:
            - blocking: bool
                是否同步送出。**預設非同步**；測試需要確定性時才傳 True
        """

        self.blocking: bool = blocking
        self.sent: int = 0
        self.failed: int = 0

    def send(self, level: NotifyLevel, title: str, body: str) -> None:
        """
        - Description:
            送出一則推播；**失敗只記 log，絕不往上拋**

            監控拖垮被監控的東西是典型反例：通知端的網路逾時若讓送單路徑卡住，
            那正好發生在最需要送單的時候。
        - Parameters:
            - level: NotifyLevel
                等級
            - title: str
                標題
            - body: str
                內容（送出前會遮蔽敏感片段）
        """

        safe_title: str = redact(title)
        safe_body: str = redact(body)

        if self.blocking:
            self._guarded_deliver(level, safe_title, safe_body)
            return

        thread: threading.Thread = threading.Thread(
            target=self._guarded_deliver,
            args=(level, safe_title, safe_body),
            daemon=True,  # 主流程結束時不等它；漏一則推播好過卡住結束
        )
        thread.start()

    def _guarded_deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        """包著 try 的送出；任何例外都只記 log"""

        try:
            self.deliver(level, title, body)
            self.sent += 1
        except Exception as exc:
            self.failed += 1
            logger.opt(exception=True).warning(f"推播失敗（忽略）：{exc}")

    @abstractmethod
    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        """實際送出；例外由骨架處理，子類別不必自己包 try"""
        pass


class NullNotifier(BaseNotifier):
    """
    不推播的管道（預設）

    **它存在的意義是讓「沒設定通知」成為一個明確的狀態**，而不是讓呼叫端
    到處寫 `if notifier is not None`。啟動時仍要 log 警告並寫進 `live_run`——
    「以為有告警其實沒有」比「知道沒有告警」危險得多。
    """

    def deliver(self, level: NotifyLevel, title: str, body: str) -> None:
        """只記 log"""

        logger.info(f"[通知未設定／{level.value}] {title}：{body}")


def notify_safely(
    notifier: Optional[BaseNotifier], level: str, title: str, body: str
) -> None:
    """
    - Description:
        推播一則訊息；**任何失敗都不往外拋**

        `BaseNotifier.send()` 自己就吞例外，這裡再包一層是因為 notifier
        可能是任何注入進來的東西——監控拖垮被監控的東西是典型反例。

        放模組層而不是讓每個呼叫端各寫一份：送單段落與盤後作業都要推播，
        兩份 try/except 遲早會有一邊漏掉。
    - Parameters:
        - notifier: Optional[BaseNotifier]
            推播管道；None 表示不推播
        - level: str
            等級字串；對應 `NotifyLevel`
        - title: str
            標題
        - body: str
            內文
    """

    if notifier is None:
        return

    try:
        notifier.send(NotifyLevel(level), title, body)
    except Exception as exc:
        logger.opt(exception=True).warning(f"推播失敗（忽略）：{exc}")
