import datetime
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from core.models.base.order import BaseOrder
from core.utils import Action, LiveOrderStatus, PositionType

"""
實盤特有的領域模型：委託單、成交回報、券商帳務快照

**為什麼不直接傳 Shioaji 物件、也不傳 dict**：
- 傳券商物件等於讓券商型別一路滲進 OMS、風控與 DAO，換券商時要改的地方是整條路徑。
- 傳 dict 則是把欄位名變成字串，打錯不會報錯，只會在某個分支安靜地取到 None。

回測沒有這一層：它的訂單一送出就成交，不存在「已送出但還沒成交」的狀態。
實盤的每一張單都要能回答「現在走到哪了、券商那邊叫什麼編號、成交了多少」。
"""


def _as_date(raw: Any) -> datetime.date:
    """TEXT／`date`／`datetime` 一律收成 `date`"""

    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    return datetime.date.fromisoformat(str(raw))


def _as_datetime(raw: Any) -> Optional[datetime.datetime]:
    """TEXT 收成 `datetime`；空值回 None（不補一個「現在」當預設）"""

    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime.datetime):
        return raw
    return datetime.datetime.fromisoformat(str(raw))


class OrderTicket:
    """
    一張已（或即將）提交給券商的委託

    **`order` 與 `status` 是兩件事**：前者是策略要做什麼（不再變動），
    後者是這張單在本地狀態機的位置（會一路變動）。混成一個物件的話，
    「策略原本想送什麼」會被成交結果覆蓋掉，parity 比對就失去比對對象。
    """

    def __init__(
        self,
        client_order_id: str = "",
        strategy_name: str = "",
        order: Optional[BaseOrder] = None,
        status: LiveOrderStatus = LiveOrderStatus.PENDING_SUBMIT,
        broker_order_id: Optional[str] = None,
        broker_seqno: Optional[str] = None,
        filled_volume: int = 0,
        avg_fill_price: float = 0.0,
        created_at: Optional[datetime.datetime] = None,
        updated_at: Optional[datetime.datetime] = None,
        reject_reason: Optional[str] = None,
        dry_run: bool = False,
        custom_field: Optional[str] = None,
    ) -> None:
        # Identity
        self.client_order_id: str = client_order_id  # 本地識別碼（FIX 的 ClOrdID）

        # 隨委託往返券商的 6 字元壓縮碼。**只由 OMS 產生一次**，券商閘道原樣送出、
        # 刷新時原樣帶回：兩邊各算一份的話，重啟接管的精確比對永遠比不到，
        # 接管後的單也撤不掉。券商端刷新回來、還沒對上本地委託的 ticket，
        # `client_order_id` 為空、只有這個欄位——不可把它塞進 `client_order_id`
        self.custom_field: Optional[str] = custom_field

        # 歸屬到哪一支策略。**不可為空**：一個帳戶跑多支策略時，券商端只有一本
        # 合併帳，這個欄位是「這張單、這筆成交、這口部位屬於誰」整條歸屬鏈的起點
        self.strategy_name: str = strategy_name

        # Order Info
        self.order: Optional[BaseOrder] = order  # 策略送出的原始訂單（不隨回報變動）
        self.status: LiveOrderStatus = status  # 本地狀態機的狀態

        # Broker Info
        #
        # 兩個編號都可能是 None：在 `place_order()` 回傳前崩潰的委託沒有 seqno，
        # 那正是重啟接管時最棘手的一類——要靠 `custom_field` 回券商精確比對
        self.broker_order_id: Optional[str] = broker_order_id  # Shioaji 的 ordno
        self.broker_seqno: Optional[str] = broker_seqno  # Shioaji 的 seqno

        # Fill Info
        self.filled_volume: int = filled_volume  # 已成交數量（台股為張、期貨為口）
        self.avg_fill_price: float = avg_fill_price  # 成交均價

        # Timestamps（一律為 Asia/Taipei 的 aware datetime）
        self.created_at: Optional[datetime.datetime] = created_at
        self.updated_at: Optional[datetime.datetime] = updated_at

        # 拒單原因（券商訊息原文）；`status` 為 REJECTED／FAILED 時才有值
        self.reject_reason: Optional[str] = reject_reason

        # 是否為 `--dry-run` 的委託：走完整流程但不真的送出。
        # **要落地而不是只存在記憶體**：否則盤後分不出「今天沒下單」與「今天是演練」
        self.dry_run: bool = dry_run

    @property
    def remaining_volume(self) -> int:
        """未成交的殘量；沒有原始訂單時為 0"""

        if self.order is None:
            return 0
        return max(self.order.volume - self.filled_volume, 0)

    @property
    def is_terminal(self) -> bool:
        """
        這張單是否已經走到終態

        終態的定義是「不會再有回報進來」。段落結束時只撤非終態的單，
        對終態的單送撤單會拿到一個看起來很像真問題的券商錯誤。
        """

        return self.status in (
            LiveOrderStatus.FILLED,
            LiveOrderStatus.CANCELLED,
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.FAILED,
        )


class ExecutionReport:
    """
    一筆成交回報

    **回報是實盤唯一可信的成交來源**，而且它不保證順序、不保證不重複：
    成交回報可能比委託確認先到，斷線重連後同一筆可能再推一次。
    故本類別帶一個去重鍵，由 OMS 以它判斷「這筆處理過了沒」。
    """

    def __init__(
        self,
        broker_seqno: str = "",
        broker_trade_id: str = "",
        symbol: str = "",
        action: Action = Action.BUY,
        price: float = 0.0,
        volume: int = 0,
        ts: Optional[datetime.datetime] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Broker Info
        self.broker_seqno: str = broker_seqno  # 委託序號
        self.broker_trade_id: str = broker_trade_id  # 成交序號（同一張單可有多筆）

        # Deal Info
        self.symbol: str = symbol
        self.action: Action = action
        self.price: float = price  # 實際成交價
        self.volume: int = volume  # 本筆成交數量

        # 交易所回報的時戳（Asia/Taipei aware）。**不要拿本機時間代替**：
        # 亂序重放時要靠它決定哪一筆才是最新狀態
        self.ts: Optional[datetime.datetime] = ts

        # 原始訊息全文。**一定要留**：欄位對照猜錯時，這是唯一能事後重建真相的東西，
        # 而實盤的回報不可能重來一次
        self.raw: Dict[str, Any] = raw if raw is not None else {}

    @property
    def dedup_key(self) -> Tuple[str, str]:
        """去重鍵：同一筆成交重複推送時，`(委託序號, 成交序號)` 相同"""

        return (self.broker_seqno, self.broker_trade_id)


class OrderStatusEvent:
    """
    一筆委託狀態事件（新單成功、被拒、改價、刪單）

    **與 `ExecutionReport` 分開**：成交是「部位真的動了」，狀態事件是「券商對這張單
    做了什麼」。合成一種的話，`quantity` 這個欄位會一下子代表成交量、一下子代表
    剩餘量，而兩者的差別正是殘量處理要用的。

    `op_code` 為 `"00"` 以外的值代表這次操作失敗，`op_msg` 是券商的原文訊息。
    """

    def __init__(
        self,
        broker_seqno: str = "",
        broker_order_id: Optional[str] = None,
        op_type: str = "",
        op_code: str = "",
        op_msg: str = "",
        symbol: str = "",
        custom_field: str = "",
        exchange_ts: Optional[datetime.datetime] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.broker_seqno: str = broker_seqno
        self.broker_order_id: Optional[str] = broker_order_id

        # 操作別（New／Cancel／UpdatePrice／UpdateQty…）與結果
        self.op_type: str = op_type
        self.op_code: str = op_code
        self.op_msg: str = op_msg

        self.symbol: str = symbol
        self.custom_field: str = custom_field  # 隨委託往返的識別碼，用來反查本地委託

        # 交易所時戳。**去重鍵的一部分**：同一張單的同一種操作可能被重推，
        # 但不同時點的兩次改價是兩個事件
        self.exchange_ts: Optional[datetime.datetime] = exchange_ts
        self.raw: Dict[str, Any] = raw if raw is not None else {}

    @property
    def dedup_key(self) -> Tuple[str, str, Optional[datetime.datetime]]:
        """去重鍵：`(委託序號, 操作別, 交易所時戳)`"""

        return (self.broker_seqno, self.op_type, self.exchange_ts)

    @property
    def is_failure(self) -> bool:
        """這次操作是否失敗；`op_code` 為 `"00"` 才是成功"""

        return bool(self.op_code) and self.op_code != "00"


class BrokerPositionSnapshot:
    """
    券商端某一檔部位的正規化快照

    **它是對帳的右手邊**：左手邊是本地由回報推導出來的部位。兩者不一致時
    一律停止開新倉，**不自動以券商值覆蓋本地**——自動修正會把真正的 bug
    （例如回報漏接）蓋掉，而那個 bug 明天還會再發生一次。
    """

    def __init__(
        self,
        symbol: str = "",
        direction: PositionType = PositionType.LONG,
        volume: int = 0,
        avg_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.symbol: str = symbol
        self.direction: PositionType = direction  # 多空方向
        self.volume: int = volume  # 部位數量（台股為張、期貨為口）
        self.avg_price: float = avg_price  # 平均成本
        self.unrealized_pnl: float = unrealized_pnl  # 未實現損益
        self.raw: Dict[str, Any] = raw if raw is not None else {}


class RealizedTradeSnapshot:
    """
    券商端一筆已平倉交易的正規化快照（盤後校正成本用）

    **以「交易」為單位，不是逐筆成交**：券商的已實現損益查詢一筆對應一組開平倉，
    帶不出是哪兩筆成交（期貨沒有委託序號，股票只帶開倉那張委託的序號）。
    各欄位在不同市場給得出來的不一樣，給不出來的留 None，不猜：

    | 欄位 | 股票 | 期貨 |
    |------|------|------|
    | `pnl` | 淨損益（已扣費用與稅） | 淨損益 |
    | `fee`／`tax` | 無 | 有 |
    | `entry_price` | 無（以 `open_seqno` 回查本地成交） | 有 |
    | `open_seqno` | 開倉委託序號 | 無 |
    """

    def __init__(
        self,
        symbol: str = "",
        quantity: int = 0,
        pnl: float = 0.0,
        cover_price: float = 0.0,
        entry_price: Optional[float] = None,
        fee: Optional[float] = None,
        tax: Optional[float] = None,
        open_seqno: Optional[str] = None,
        is_futures: bool = False,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.symbol: str = symbol
        self.quantity: int = quantity  # 股票為張、期貨為口
        self.pnl: float = pnl  # 券商算的淨損益
        self.cover_price: float = cover_price  # 平倉價
        self.entry_price: Optional[float] = entry_price  # 開倉價；股票給不出
        self.fee: Optional[float] = fee  # 手續費（整筆交易）；股票給不出
        self.tax: Optional[float] = tax  # 交易稅（整筆交易）；股票給不出
        self.open_seqno: Optional[str] = open_seqno  # 開倉委託序號；期貨給不出
        self.is_futures: bool = is_futures
        self.raw: Dict[str, Any] = raw if raw is not None else {}


@dataclass
class PendingAction:
    """
    一筆跨日待辦（平倉單未成交、次日開盤段補平）

    **存在的理由是它要跨出 `core/live/`**：`LiveTrader` 會把它整個交給
    **策略作者實作**的 `build_cover_order(action)`。改傳資料列 dict 就等於把
    `live_pending_action` 的 schema 變成策略層的公開契約，而 dict 沒有型別定義——
    改一個欄位名會無聲地弄壞每一支策略，因為取不到的鍵只會安靜地變成 None。

    **`action` 與 `position_type` 收 Enum**：兩者都是 `str` 子類，
    當字典鍵與字串比較都照舊，但拼錯的值在建立時就過不了。

    `status` 維持 `str`：它的值域由 DAO 的 `ACTION_*` 常數定義，
    那是紀錄庫的狀態機，不是領域概念。
    """

    action_id: str
    strategy_name: str
    symbol: str
    action: Action
    volume: int
    due_date: datetime.date
    status: str
    position_type: Optional[PositionType] = None
    reason: Optional[str] = None
    source_client_order_id: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    resolved_at: Optional[datetime.datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "PendingAction":
        """
        - Description:
            由紀錄庫的資料列建立

            SQLite 把日期與時間都存成 TEXT，**轉型在這裡做一次**：
            散在呼叫端的話，有人會忘記轉而直接拿字串去比日期——
            `"2026-09-24" <= "2026-9-3"` 這種比較不會報錯，只會給錯的答案。
        - Parameters:
            - row: Dict[str, Any]
                `live_pending_action` 的一列
        - Return:
            - PendingAction
                待辦
        """

        direction: Any = row.get("position_type")
        return cls(
            action_id=str(row["action_id"]),
            strategy_name=str(row["strategy_name"]),
            symbol=str(row["symbol"]),
            action=Action(str(row["action"])),
            volume=int(row["volume"]),
            due_date=_as_date(row.get("due_date")),
            status=str(row["status"]),
            position_type=PositionType(str(direction)) if direction else None,
            reason=str(row["reason"]) if row.get("reason") else None,
            source_client_order_id=(
                str(row["source_client_order_id"])
                if row.get("source_client_order_id")
                else None
            ),
            created_at=_as_datetime(row.get("created_at")),
            resolved_at=_as_datetime(row.get("resolved_at")),
        )


class BrokerAccountSnapshot:
    """
    券商端帳務的正規化快照

    `available_balance` 是資金額度分配的天花板：每支策略的可用資金取
    「本策略額度 − 本策略已用」與「帳戶可用餘額 − 其他策略已保留」的較小者，
    否則兩支策略會同時看到同一筆錢而各自下滿。
    """

    def __init__(
        self,
        ts: Optional[datetime.datetime] = None,
        available_balance: float = 0.0,
        total_equity: float = 0.0,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.ts: Optional[datetime.datetime] = ts  # 查詢時點（Asia/Taipei aware）
        self.available_balance: float = available_balance  # 可動用餘額
        # 總權益（可用餘額 ＋ 持倉市值）。**額度檢查的分母是它，不是可用餘額**：
        # 拿可用餘額當分母，只要隔日還有部位在場上就必然誤判成額度超標而拒絕啟動，
        # 而那是完全正常的續跑狀態
        self.total_equity: float = total_equity
        self.realized_pnl: float = realized_pnl  # 當日已實現損益
        self.unrealized_pnl: float = unrealized_pnl  # 未實現損益
        self.raw: Dict[str, Any] = raw if raw is not None else {}
