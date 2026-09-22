import contextlib
import signal
import threading
from types import FrameType
from typing import Any, Iterator, Optional

from loguru import logger

"""
實盤行程收到 SIGTERM 時的收尾：先撤單、寫結束原因，再結束

容器停止（`docker stop`）、排程逾時、手動 `kill` 送的都是 SIGTERM。預設處理是
**直接結束行程**：場上的委託不會被撤、`live_run` 沒有結束紀錄（下次啟動才被標成崩潰），
而那段時間掛著的委託可能在沒人看的時候成交。

這裡把 SIGTERM 換成在主流程拋出 `LiveTerminated`：`LiveTrader.run()` 本來就在
`finally` 裡撤未成交單、續收回報、寫結束紀錄，例外會沿著同一條收尾路徑走完。

**只拋一次**：收尾本身要時間（撤單要等券商回覆），期間再收到的 SIGTERM 一律忽略，
不可把撤單打斷在一半。真的要強制結束用 SIGKILL（容器的 `stop_grace_period` 到期
也會送 SIGKILL），那條路一定要在。

與 `core/pipeline/shared/graceful_stop.py` 不同：ETL 是在迴圈的安全點「問」旗標，
實盤的等待散在送單、等回報、等時窗各處，逐處輪詢容易漏，故改用例外讓控制權
直接回到 `finally`。「先寫 DB 再送單」保證了例外落在任何一行都能由恢復流程接手。
"""


class LiveTerminated(BaseException):
    """
    實盤行程收到結束訊號；沿收尾路徑撤單並寫結束紀錄後離開

    **繼承 `BaseException` 而不是 `Exception`**（與 `KeyboardInterrupt`、`SystemExit`
    同一層）：引擎到處有「一張單失敗不拖垮其他單」的 `except Exception`，
    訊號若落在送單途中，繼承 `Exception` 的版本會被當成「送單失敗」吞掉、
    接著送下一張——收到停止訊號卻繼續下單。
    """


@contextlib.contextmanager
def raise_on_sigterm() -> Iterator[None]:
    """
    - Description:
        區塊內收到第一個 SIGTERM 時拋出 `LiveTerminated`，之後的忽略；離開時還原

        非主執行緒無法註冊訊號處理器（`signal.signal()` 會拋 `ValueError`），
        此時不接手、維持預設行為，而不是讓整個段落啟動失敗。
    """

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    raised: bool = False

    def handle(signum: int, frame: Optional[FrameType]) -> None:
        nonlocal raised
        if raised:
            logger.warning("收尾中再次收到 SIGTERM，已忽略；要強制結束請用 SIGKILL")
            return
        raised = True
        logger.warning("收到 SIGTERM：撤未成交單、寫結束紀錄後結束")
        raise LiveTerminated("收到 SIGTERM")

    previous: Any = signal.signal(signal.SIGTERM, handle)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
