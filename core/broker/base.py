import queue
from abc import ABC, abstractmethod
from typing import Any, Callable, List

from loguru import logger

from core.models import (
    BaseOrder,
    BaseQuote,
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    ExecutionReport,
    OrderTicket,
)

"""BaseBroker: 市場與券商皆無關的下單、回報、帳務與行情介面"""


class CallbackQueue:
    """
    - Description:
        長得像 `queue.Queue` 但只把東西轉交出去的接收端

        **為了讓「同一個事件 queue」成立而存在**：行情與回報的回呼各自對著自己的
        `queue.Queue` 呼叫 `put()`，要把兩者匯進一條，最省事的做法就是換掉那兩個
        queue 物件，而不是改每個回呼。

        `get_nowait()` 一律拋 `Empty`：盤中模式下回報已經改走事件迴圈，
        `drain_execution_queue()` 再撈一次就會撈到重複的（而且順序也亂了）。
    """

    def __init__(self, sink: Callable[[Any], None]) -> None:
        """
        - Description:
            建立轉交端
        - Parameters:
            - sink: Callable[[Any], None]
                收到東西時要呼叫的函式
        """

        self.sink: Callable[[Any], None] = sink

    def put(self, item: Any) -> None:
        """轉交；**例外吞在這裡**——這是跑在券商執行緒上的回呼路徑"""

        try:
            self.sink(item)
        except Exception as exc:
            logger.opt(exception=True).error(f"事件轉交失敗：{exc}")

    def get_nowait(self) -> Any:
        """一律視為空：盤中模式下不從這裡撈，避免與事件迴圈重複消化"""

        raise queue.Empty


class BaseBroker(ABC):
    """
    - Description:
        券商閘道的共用介面

        引擎只認這個介面，Shioaji 只是其中一種實作。這樣做有兩個具體好處：
        測試不必連網（`FakeBroker` 可以腳本化延遲回報、部分成交、拒單、斷線），
        而換券商或加第二個市場時，要改的是一個實作，不是整條送單路徑。

        **回報走 queue 而不是 callback**：券商的回呼跑在它自己的執行緒上，
        在裡面查 DB、下單或做重運算會拖住回報接收，而回報是實盤唯一可信的
        成交來源。故回呼只負責「轉成 `ExecutionReport` 然後 put」，
        真正的處理一律回到主執行緒。
    """

    def __init__(self) -> None:
        # 回報佇列：委託狀態與成交事件都往這裡塞，由引擎主執行緒消化。
        #
        # **刻意只有一個 queue**：Python 的 `queue.Queue` 不能像 socket 一樣
        # `select` 多個，分開幾個就得輪詢，還會讓回報與行情的處理順序變得不確定
        self.execution_queue: queue.Queue = queue.Queue()

    # === 連線 ===
    @abstractmethod
    def connect(self) -> None:
        """建立連線（含登入與憑證啟用）；失敗時拋出，不回傳成功與否的旗標"""
        pass

    @abstractmethod
    def close(self) -> None:
        """關閉連線；必須可重複呼叫（引擎以 `try/finally` 保證它一定會跑到）"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """目前是否連線中"""
        pass

    # === 下單 ===
    @abstractmethod
    def place_order(self, ticket: OrderTicket) -> OrderTicket:
        """
        - Description:
            送出委託

            **收的是 `OrderTicket` 而不是 `BaseOrder`**：`client_order_id` 要在
            送出前就存在（先寫 DB 再送單），而它由 OMS 產生。只收訂單的話，
            券商層就得自己生識別碼，恢復流程要比對的東西會變成兩份。
        - Parameters:
            - ticket: OrderTicket
                已寫入本地、狀態為 `PENDING_SUBMIT` 的委託
        - Return:
            - OrderTicket
                補上券商編號與最新狀態的同一張委託
        """
        pass

    @abstractmethod
    def cancel_order(self, ticket: OrderTicket) -> OrderTicket:
        """
        - Description:
            撤銷未成交的委託；已是終態的委託應直接回傳，不送出撤單請求
        - Parameters:
            - ticket: OrderTicket
                要撤的委託
        - Return:
            - OrderTicket
                更新後的委託
        """
        pass

    @abstractmethod
    def update_order_price(self, ticket: OrderTicket, price: float) -> OrderTicket:
        """
        - Description:
            改價（平倉與停損單未成交時的唯一追價手段）
        - Parameters:
            - ticket: OrderTicket
                要改價的委託
            - price: float
                新的委託價
        - Return:
            - OrderTicket
                更新後的委託
        """
        pass

    @abstractmethod
    def refresh_order_status(self) -> List[OrderTicket]:
        """
        - Description:
            向券商查詢當日所有委託的最新狀態

            **這是恢復流程用的，不是拿來輪詢的**：它算在下單類的限流額度內
            （與 `place_order` 共用同一個桶），拿它當心跳會把送單額度吃光。
            平時的狀態更新一律靠 `execution_queue`。
        - Return:
            - List[OrderTicket]
                券商端當日的委託清單
        """
        pass

    # === 帳務 ===
    @abstractmethod
    def get_positions(self) -> List[BrokerPositionSnapshot]:
        """取得券商端的部位；這是對帳的右手邊"""
        pass

    @abstractmethod
    def get_account(self) -> BrokerAccountSnapshot:
        """取得券商端的帳務快照（餘額、權益、損益）"""
        pass

    # === 行情 ===
    @abstractmethod
    def get_snapshots(self, symbols: List[str]) -> List[BaseQuote]:
        """
        - Description:
            取得即時快照，轉成與回測同款的報價物件

            回傳的順序與筆數**不保證**與 `symbols` 相同：查無資料的標的會被略過
            （停牌、代號錯誤）。呼叫端一律以 symbol 對應，不要用位置索引。
        - Parameters:
            - symbols: List[str]
                商品代號清單
        - Return:
            - List[BaseQuote]
                報價清單
        """
        pass

    @abstractmethod
    def subscribe_quotes(self, symbols: List[str]) -> None:
        """訂閱即時行情；超過券商單一連線的訂閱上限時要在訂閱前拋出"""
        pass

    @abstractmethod
    def unsubscribe_quotes(self, symbols: List[str]) -> None:
        """取消訂閱"""
        pass

    # === 共用工具 ===
    def route_events(
        self,
        on_quote: Callable[[Any], None],
        on_execution: Callable[[Any], None],
    ) -> None:
        """
        - Description:
            把行情與回報導向單一事件接收端（盤中模式）

            骨架只導回報——它是所有閘道都有的。行情訂閱是市場特性，
            有串流的閘道自己覆寫。

            **導向之後 `drain_execution_queue()` 會一直是空的**：那是刻意的，
            回報已經改由事件迴圈逐筆消化，兩邊都撈會重複且順序錯亂。
        - Parameters:
            - on_quote: Callable[[Any], None]
                收到一筆行情時呼叫
            - on_execution: Callable[[Any], None]
                收到一筆回報時呼叫
        """

        self.execution_queue = CallbackQueue(on_execution)

    def reconnect(self) -> bool:
        """
        - Description:
            重新建立連線

            **這是有預設實作的 hook，不是抽象方法**：多數閘道「關掉再連一次」
            就夠了，真正需要退避與每日上限的（Shioaji 的登入額度）才覆寫。
            宣告成抽象等於對每個新閘道說「重連這件事你得自己想」。

            **本身不做恢復**：重新訂閱、接管委託與對帳要按順序做完才可以恢復
            送單，那是引擎的責任——閘道層不知道有哪些策略、訂了哪些標的。
        - Return:
            - bool
                是否重新連上
        """

        self.close()
        try:
            self.connect()
        except Exception as exc:
            logger.opt(exception=True).error(f"重連失敗：{exc}")
            return False
        return self.is_connected()

    def drain_execution_queue(self) -> List[ExecutionReport]:
        """
        - Description:
            一次取出目前佇列中的所有回報

            **非阻塞**：段落迴圈要能在沒有回報時繼續做別的事（檢查時限、
            檢查 kill switch）。阻塞式取用會讓「沒有回報」和「程式卡住」
            在外部看起來一模一樣。
        - Return:
            - List[ExecutionReport]
                本次取出的回報，順序即入列順序
        """

        reports: List[ExecutionReport] = []
        while True:
            try:
                reports.append(self.execution_queue.get_nowait())
            except queue.Empty:
                break
        return reports

    @staticmethod
    def describe_order(order: BaseOrder) -> str:
        """委託的單行摘要，供 log 與拒單訊息使用（不含任何帳號資訊）"""

        return (
            f"{order.symbol} {order.action.value} {order.volume}@{order.price} "
            f"({order.position_type.value})"
        )
