import datetime
import queue
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from core.config.settings import now_live
from core.models import BaseQuote, ExecutionReport

"""
盤中事件迴圈：行情、回報、心跳在同一個主執行緒依序處理

**一個 queue 不是三個**。行情回呼與回報回呼都跑在券商的執行緒上，各自一個 queue
的話，主迴圈就得輪詢——而 Python 的 `queue.Queue` 不像 socket 能 `select` 多個，
輪詢除了空轉還會讓「回報與行情的先後」變成不確定的。同一筆成交是先被記進帳戶
還是先被下一筆報價觸發新訊號，結果會不一樣，而且不會有任何錯誤。

**策略鉤子只在主執行緒呼叫**：回呼執行緒直接呼叫策略的話，策略就得自己處理併發，
而策略是使用者寫的——那是把最難的部分推給最不該處理它的人。

**心跳不另開計時器執行緒**：`queue.get(timeout=...)` 逾時本身就是心跳。
多一條執行緒就多一組要同步的狀態，而它要做的事只是「看一下時鐘」。
"""


class EventKind(str, Enum):
    """事件類型；主迴圈依它分派"""

    QUOTE = "QUOTE"  # 一筆行情
    EXECUTION = "EXECUTION"  # 一筆成交回報
    HEARTBEAT = "HEARTBEAT"  # 沒有事件時的計時脈衝


@dataclass(frozen=True)
class Event:
    """
    進入主迴圈的一件事

    **帶自己的時戳**：入列時間與被處理的時間可能差很遠（前面塞了一批行情），
    延遲要量得出來，就不能在處理當下才取時間。
    """

    kind: EventKind
    payload: Any = None
    ts: Optional[datetime.datetime] = None


@dataclass
class LoopStats:
    """一段迴圈跑完的統計；測試與盤後報表都用它"""

    quotes: int = 0
    executions: int = 0
    heartbeats: int = 0
    handler_errors: int = 0
    stale_symbols: List[str] = field(default_factory=list)


class IntradayEventLoop:
    """
    - Description:
        盤中的單執行緒事件迴圈

        三個來源（行情回呼、回報回呼、心跳）共用一個 queue，主迴圈只從這一個
        queue 取事件依序處理。**本層不認識策略也不認識引擎**：要做什麼由外部
        注入的三個 callable 決定，否則這一層就得 import `core.live.trader`
        （分層 5），而它在元件層。
    """

    def __init__(
        self,
        on_quote: Callable[[BaseQuote], None],
        on_execution: Callable[[ExecutionReport], None],
        on_market_data_lost: Callable[[str], None],
        now_provider: Callable[[], datetime.datetime] = now_live,
        heartbeat_seconds: float = 1.0,
        silence_limit_seconds: float = 30.0,
        stale_quote_seconds: float = 30.0,
        event_queue: Optional[queue.Queue] = None,
        stuck_clock_heartbeats: int = 60,
        on_heartbeat: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        - Description:
            建立事件迴圈
        - Parameters:
            - on_quote: Callable[[BaseQuote], None]
                收到一筆行情時要做的事（逐筆觸發策略鉤子）
            - on_execution: Callable[[ExecutionReport], None]
                收到一筆成交回報時要做的事（更新帳戶與歸屬帳）
            - on_market_data_lost: Callable[[str], None]
                行情中斷時要做的事；收到原因字串，由呼叫端決定怎麼降級
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - heartbeat_seconds: float
                沒有事件時多久醒來看一次時鐘
            - silence_limit_seconds: float
                全市場靜默超過幾秒判定為行情中斷
            - stale_quote_seconds: float
                單一標的最新報價超過幾秒視為過期
            - event_queue: Optional[queue.Queue]
                事件佇列；預設自己建一個
            - stuck_clock_heartbeats: int
                連續幾次心跳都讀到同一個時刻就判定時鐘停住並跳出；
                以預設心跳 1 秒計約 60 秒
            - on_heartbeat: Optional[Callable[[], None]]
                每次心跳要做的事（日終強制動作這類「到點就要做」的工作）。
                **不可放在 `on_quote` 裡**：行情停了它就不會再被呼叫，
                而日終回補正是不能因為沒行情就不做的事
        """

        self.on_quote: Callable[[BaseQuote], None] = on_quote
        self.on_execution: Callable[[ExecutionReport], None] = on_execution
        self.on_market_data_lost: Callable[[str], None] = on_market_data_lost
        self._now: Callable[[], datetime.datetime] = now_provider

        self.heartbeat_seconds: float = heartbeat_seconds
        self.silence_limit_seconds: float = silence_limit_seconds
        self.stale_quote_seconds: float = stale_quote_seconds
        self.stuck_clock_heartbeats: int = stuck_clock_heartbeats
        self.on_heartbeat: Optional[Callable[[], None]] = on_heartbeat

        self.events: queue.Queue = (
            event_queue if event_queue is not None else queue.Queue()
        )

        # 每檔最後一次收到報價的時刻；報價過期判定看它
        self.last_quote_at: Dict[str, datetime.datetime] = {}
        # 全市場最後一次收到任何報價的時刻；心跳判定看它
        self.last_any_quote_at: Optional[datetime.datetime] = None

        # **只降級一次**：降級是單向的、恢復一律人工，重複送事件只會洗版告警，
        # 讓真正需要注意的那一則被淹掉
        self.market_data_lost: bool = False

    # === 餵事件（由券商回呼執行緒呼叫）===
    def submit_quote(self, quote: BaseQuote) -> None:
        """把一筆行情放進 queue；**跑在券商執行緒上，只做入列**"""

        self.events.put(Event(EventKind.QUOTE, quote, self._now()))

    def submit_execution(self, report: ExecutionReport) -> None:
        """把一筆成交回報放進 queue；同樣只做入列"""

        self.events.put(Event(EventKind.EXECUTION, report, self._now()))

    # === 主迴圈 ===
    def run_until(self, deadline: datetime.datetime) -> LoopStats:
        """
        - Description:
            處理事件直到時限

            **時限一到就停，不管 queue 還有沒有東西**：盤中段落有收線時間，
            積壓的行情再處理下去只會送出過期的訊號。

            **任何一個 handler 拋例外都不中斷迴圈**：一筆報價格式有問題不該讓
            後面所有的成交回報都收不到——那會讓帳戶停在錯的狀態。例外記下來，
            由 `handler_errors` 反映在統計裡。
        - Parameters:
            - deadline: datetime.datetime
                跑到什麼時候為止
        - Return:
            - LoopStats
                本段的事件統計
        """

        stats: LoopStats = LoopStats()

        # **獨立於時鐘的保險絲**：結束條件只看時鐘的話，時鐘停住時這個迴圈
        # 永遠不會結束——段落不收線、盤後作業不會開始，而且沒有任何錯誤訊息。
        # 同一個坑在 `LiveTrader` 的等待迴圈已經踩過一次
        frozen_since: Optional[datetime.datetime] = None
        frozen_beats: int = 0

        while self._now() < deadline:
            moment: datetime.datetime = self._now()
            if moment == frozen_since:
                frozen_beats += 1
            else:
                frozen_since = moment
                frozen_beats = 0

            if frozen_beats >= self.stuck_clock_heartbeats:
                logger.error(
                    f"連續 {frozen_beats} 次心跳都讀到同一個時刻 {moment}，"
                    "判定系統時鐘停住，盤中迴圈結束；請確認時鐘是否正常"
                )
                break

            event: Optional[Event] = self._next_event()
            if event is None:
                stats.heartbeats += 1
                self._check_market_data_silence()
                self._beat(stats)
                continue

            self._dispatch(event, stats)

        stats.stale_symbols = self.stale_symbols()
        return stats

    def _beat(self, stats: LoopStats) -> None:
        """跑一次心跳工作；**失敗不中斷迴圈**，但要計進統計"""

        if self.on_heartbeat is None:
            return

        try:
            self.on_heartbeat()
        except Exception as exc:
            stats.handler_errors += 1
            logger.opt(exception=True).error(f"心跳工作失敗，迴圈繼續：{exc}")

    def _next_event(self) -> Optional[Event]:
        """取下一個事件；逾時回 None（那就是一次心跳）"""

        try:
            return self.events.get(timeout=self.heartbeat_seconds)
        except queue.Empty:
            return None

    def _dispatch(self, event: Event, stats: LoopStats) -> None:
        """分派單一事件；handler 的例外吞在這裡"""

        try:
            if event.kind is EventKind.QUOTE:
                self._record_quote(event)
                stats.quotes += 1
                self.on_quote(event.payload)
            elif event.kind is EventKind.EXECUTION:
                stats.executions += 1
                self.on_execution(event.payload)
            else:
                stats.heartbeats += 1
                self._check_market_data_silence()
        except Exception as exc:
            stats.handler_errors += 1
            logger.opt(exception=True).error(
                f"盤中事件處理失敗（{event.kind.value}），迴圈繼續：{exc}"
            )

    def _record_quote(self, event: Event) -> None:
        """
        記下這一檔的最後更新時刻

        **取事件自己的時戳而不是處理當下的時間**：前面塞了一批行情時，
        兩者可能差好幾秒，用處理時間會讓過期的報價看起來很新鮮。
        """

        moment: datetime.datetime = event.ts or self._now()
        symbol: str = str(getattr(event.payload, "symbol", "") or "")
        if symbol:
            self.last_quote_at[symbol] = moment
        self.last_any_quote_at = moment

    # === 心跳與過期 ===
    def _check_market_data_silence(self) -> None:
        """
        全市場靜默太久 → 行情中斷

        **降到 `REDUCE_ONLY` 而不是 `HALTED`**：行情中斷時部位還在場上，
        `HALTED` 連平倉都不送，等於把停損一起關掉。降級的目標由呼叫端決定，
        本層只負責把「行情斷了」這件事說出去。
        """

        if self.market_data_lost:
            return

        silent: Optional[float] = self.silent_seconds()
        if silent is None or silent <= self.silence_limit_seconds:
            return

        self.market_data_lost = True
        reason: str = (
            f"行情中斷：已 {silent:.0f} 秒沒有收到任何報價"
            f"（上限 {self.silence_limit_seconds:.0f} 秒）"
        )
        logger.error(reason)
        try:
            self.on_market_data_lost(reason)
        except Exception as exc:
            logger.opt(exception=True).error(f"行情中斷的降級處理失敗：{exc}")

    def silent_seconds(self) -> Optional[float]:
        """
        距離最後一筆行情過了幾秒；**還沒收到任何報價時回 None**

        None 與 0 要分得開：開盤前本來就沒有報價，把它當成「靜默 0 秒」會讓
        心跳永遠不觸發；當成「靜默很久」則會在開盤第一秒就誤判中斷。
        """

        if self.last_any_quote_at is None:
            return None
        return (self._now() - self.last_any_quote_at).total_seconds()

    def is_quote_stale(self, symbol: str) -> bool:
        """
        - Description:
            這一檔的報價是不是過期了

            逐筆觸發沒有「片」的概念，故以**每檔各自的最後更新時間**判斷。
            **沒收過這檔的報價一律視為過期**：拿不到報價就不該對它開新倉，
            預設為新鮮等於用一個不存在的價格下單。
        - Parameters:
            - symbol: str
                商品代號
        - Return:
            - bool
                是否過期
        """

        moment: Optional[datetime.datetime] = self.last_quote_at.get(symbol)
        if moment is None:
            return True
        return (self._now() - moment).total_seconds() > self.stale_quote_seconds

    def stale_symbols(self) -> List[str]:
        """目前報價已過期的標的；只看收過報價的那些"""

        return sorted(
            symbol for symbol in self.last_quote_at if self.is_quote_stale(symbol)
        )
