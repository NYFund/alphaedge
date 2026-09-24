import signal
import threading
import time
from types import FrameType
from typing import Any, Dict, Optional, Tuple

from loguru import logger

"""
把中止訊號變成「可以問的旗標」，讓長時間爬取收在安全點

權益變動表的整段回補以**數十小時**計（逐檔查詢、十萬次以上請求），
「跑到一半被中斷」不是例外狀況而是常態：要關機、網路要重連、要讓位給別的工作。
而預設的 `KeyboardInterrupt` 是從**當下那一行**炸出去的，於是已經爬好、
還在記憶體裡等湊滿一批的資料全部作廢（`EQUITY_CHANGE_LOAD_BATCH_SIZE` 是
100 檔，約兩分鐘的請求），而那些公司在資料庫裡不留任何列，
下次重跑會被當成「還沒爬」再打一次。

改法是不讓訊號直接打斷流程，而是先記下來：

| 第幾次訊號 | 行為 |
|---|---|
| 第一次（Ctrl+C／SIGTERM） | 只把 `requested` 立起來；呼叫端在每一檔之間問一次，於是能先把手上這批寫進資料庫、存好進度、印完統計行才離開 |
| 第二次 | 還原預設處理器並立刻拋 `KeyboardInterrupt` |

第二次那一列是刻意留的：「收工也要等一下」不能變成「按了沒反應」，
強制退出的路一定要在。

`sleep()` 也收在這裡。節流的 `time.sleep()` 被訊號打斷後會**自動續睡**
（PEP 475），若沿用它，按下 Ctrl+C 還要等完整整 15 秒才看得到反應；
本方法改成切成小段輪詢，旗標一立起來就回。
"""


class GracefulStop:
    """
    - Description:
        可輪詢的中止旗標；以 context manager 註冊與還原訊號處理器

        用法：

            with GracefulStop(label="equity_change") as stop:
                for stock_id in stock_ids:
                    ...
                    if stop.requested:
                        break          # 先收尾再離開
                    stop.sleep(delay)  # 可被中止打斷的節流

        **不接手訊號時一樣能用**：非主執行緒無法註冊處理器（`signal.signal()`
        會拋 `ValueError`），此時 `requested` 永遠是 False，行為退化成預設的
        直接中斷，而不是讓呼叫端整段爬取失敗。
    """

    # 收到第幾次訊號就立刻中止
    FORCE_QUIT_SIGNAL_COUNT: int = 2
    # `sleep()` 的輪詢間隔：越小反應越快，代價是醒來次數
    SLEEP_SLICE_SECONDS: float = 0.2
    # 預設接手的訊號：Ctrl+C 與 `kill`（含容器停止、排程逾時）
    HANDLED_SIGNALS: Tuple[int, ...] = (signal.SIGINT, signal.SIGTERM)

    def __init__(
        self,
        label: str = "crawl",
        signals: Optional[Tuple[int, ...]] = None,
    ) -> None:
        """
        - Description:
            建立中止旗標；此時尚未接手訊號，`install()`（或 `with`）才會註冊
        - Parameters:
            - label: str
                來源名稱，只用於訊息
            - signals: Optional[Tuple[int, ...]]
                要接手的訊號；None 取 `HANDLED_SIGNALS`
        """

        self.label: str = label
        self.signals: Tuple[int, ...] = (
            tuple(signals) if signals is not None else self.HANDLED_SIGNALS
        )
        self.requested: bool = False
        self.signal_count: int = 0
        self.reason: str = ""

        # 先前的處理器，離開 context 時還原；只放實際註冊成功的訊號
        self._previous_handlers: Dict[int, Any] = {}

    def __enter__(self) -> "GracefulStop":
        """註冊訊號處理器"""

        self.install()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """還原訊號處理器（不吞例外）"""

        self.restore()

    def install(self) -> None:
        """
        - Description:
            接手訊號；非主執行緒則不接手

            `signal.signal()` 只能在主執行緒呼叫，在其他執行緒會拋 `ValueError`。
            那種情況下讓整段爬取因為「裝不上處理器」而失敗完全不合理，
            故只記一行 debug 後退化成預設行為。
        """

        if threading.current_thread() is not threading.main_thread():
            logger.debug(
                f"[{self.label}] 非主執行緒，不接手中止訊號（沿用預設的立即中斷）"
            )
            return

        for signum in self.signals:
            try:
                self._previous_handlers[signum] = signal.signal(signum, self._handle)
            except (ValueError, OSError) as error:
                logger.debug(
                    f"[{self.label}] 無法接手訊號 {signum}"
                    f"（{type(error).__name__}: {error}）"
                )

    def restore(self) -> None:
        """還原先前的訊號處理器"""

        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                # 還原失敗不值得讓呼叫端失敗：行程即將結束，或處理器已被別人接手
                pass

        self._previous_handlers.clear()

    def request(self, reason: str = "") -> None:
        """
        - Description:
            主動要求收工（測試與非訊號來源用；例如上游偵測到 IP 被擋）
        - Parameters:
            - reason: str
                原因，只用於訊息
        """

        self.requested = True
        self.reason = reason

    def sleep(self, seconds: float) -> bool:
        """
        - Description:
            可被中止打斷的 sleep

            **不能直接用 `time.sleep()`**：PEP 475 之後它被訊號打斷會自動續睡，
            於是「每 50 檔睡 15 秒」那一段按下 Ctrl+C 要等 15 秒才有反應。
        - Parameters:
            - seconds: float
                要睡多久
        - Return:
            - bool
                睡完為 True；中途被要求收工而提早返回為 False
        """

        deadline: float = time.monotonic() + seconds

        while True:
            if self.requested:
                return False

            remaining: float = deadline - time.monotonic()
            if remaining <= 0:
                return True

            time.sleep(min(remaining, self.SLEEP_SLICE_SECONDS))

    def _handle(self, signum: int, frame: Optional[FrameType]) -> None:
        """訊號處理器：第一次記旗標，第二次立即中止"""

        self.signal_count += 1
        name: str = signal.Signals(signum).name

        if self.signal_count >= self.FORCE_QUIT_SIGNAL_COUNT:
            logger.warning(
                f"[{self.label}] 再次收到 {name}，立即中止"
                f"（手上尚未入庫的那批會遺失，下次重跑會重新爬）"
            )
            self.restore()
            raise KeyboardInterrupt(f"{name} received {self.signal_count} times")

        self.requested = True
        self.reason = name
        logger.warning(
            f"[{self.label}] 收到 {name}，本檔結束後收工："
            f"手上這批先入庫、進度存檔、印完統計行才離開；"
            f"要立刻中止請再按一次（未入庫的那批會遺失）"
        )
