import dataclasses
import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.live.attribution.position_ledger import (
    UNATTRIBUTED_STRATEGY,
    PositionAttributionLedger,
)
from core.live.notify.base import NotifyLevel
from core.live.risk.event_log import RiskEventLogger
from core.models import BrokerPositionSnapshot

"""
以券商部位重建歸屬帳：判定規則與寫入

對帳不一致之後（券商強制平倉、人工在券商端平倉、回報漏接），人工確認要以券商為準時
才會走到這裡。規則逐標的、逐方向比對券商與本地：

| 情況 | 處理 |
|---|---|
| 券商 > 本地 | 差額收進 `__unattributed__`（只允許平倉） |
| 券商 < 本地 | 扣減該標的唯一持有者的 lot，FIFO，扣到 0 就標平倉 |
| 方向相反 | 前兩條的組合：本地方向扣到 0、券商方向整筆收進 `__unattributed__` |
| 同一標的有兩個持有者 | 整份拒絕，要求人工 |

**不按比例分配、也不要求人逐檔指定**：同一標的只允許一支策略持有
（`__unattributed__` 也算），所以歸屬帳裡每一檔最多一個持有者，沒有「分給誰」的問題。
按比例分等於猜一個分法——各策略的已實現損益都會錯，合計卻是對的，對帳看不出來。
出現兩個持有者代表紀錄本身已經壞了，這時再猜只會讓它更難查。

判定與寫入刻意分開：`plan_resync()` 只產出計畫，`apply_resync()` 才寫入。
只列計畫不寫入的那條路徑因此不可能動到任何一筆。
"""

# 風控事件類別：每一筆異動一筆事件
RESYNC_EVENT_CATEGORY: str = "RESYNC_FROM_BROKER"

# 異動種類
RESYNC_ADOPT: str = "ADOPT"  # 券商多出來的量收進未歸屬
RESYNC_REDUCE: str = "REDUCE"  # 扣減 lot 的部分數量
RESYNC_CLOSE: str = "CLOSE"  # lot 整筆標平倉

# 交易內的 savepoint 名稱
_SAVEPOINT: str = "resync_from_broker"


class ResyncRefusedError(Exception):
    """重建被拒絕：歸屬帳已損壞或仍有未終結的委託，需要人工處理"""


@dataclass(frozen=True)
class ResyncAction:
    """一筆 lot 異動"""

    kind: str  # RESYNC_ADOPT／RESYNC_REDUCE／RESYNC_CLOSE
    strategy_name: str
    symbol: str
    direction: str
    volume: int  # 本次異動量（收進或扣掉的量，不是異動後的剩餘量）
    lot_id: Optional[str] = None  # 扣減與平倉的對象；收進時由寫入端產生
    open_price: Optional[float] = None  # 收進時以券商均價當開倉價

    def describe(self) -> str:
        """給人看的一行說明"""

        if self.kind == RESYNC_ADOPT:
            return (
                f"{self.symbol} {self.direction}：券商多出 {self.volume}，"
                f"收進 {UNATTRIBUTED_STRATEGY}（均價 {self.open_price}）"
            )
        verb: str = "整筆標平倉" if self.kind == RESYNC_CLOSE else "扣減"
        return (
            f"{self.symbol} {self.direction}：{self.strategy_name} 的 lot "
            f"{self.lot_id} {verb} {self.volume}"
        )


@dataclass
class ResyncPlan:
    """重建計畫；有衝突時 `actions` 一律為空"""

    actions: List[ResyncAction] = field(default_factory=list)
    conflicts: Dict[str, List[str]] = field(default_factory=dict)  # 標的 → 持有者

    @property
    def is_refused(self) -> bool:
        """有標的被兩支以上策略持有 → 整份拒絕"""

        return bool(self.conflicts)

    def describe(self) -> List[str]:
        """給人看的計畫，一筆異動一行"""

        if self.is_refused:
            return [
                f"{symbol} 同時被 {holders} 持有，歸屬帳已損壞，拒絕重建；請人工處理"
                for symbol, holders in sorted(self.conflicts.items())
            ]
        if not self.actions:
            return ["券商部位與歸屬帳一致，無需重建"]
        return [action.describe() for action in self.actions]


def plan_resync(
    positions: List[BrokerPositionSnapshot], lots: List[Dict[str, object]]
) -> ResyncPlan:
    """
    - Description:
        比對券商部位與未平倉 lot，產出重建計畫

        扣減順序 FIFO（最早開倉者先扣），與平倉沖銷 `close_lots()` 一致：
        兩邊順序不同的話，同一批部位留下來的開倉價就不同，之後的已實現損益會對不上。
    - Parameters:
        - positions: List[BrokerPositionSnapshot]
            券商端部位
        - lots: List[Dict[str, object]]
            歸屬帳的未平倉 lot（`LiveTradeDAO.get_open_lots()` 的結果）
    - Return:
        - ResyncPlan
            重建計畫；有衝突時只帶衝突、不帶任何異動
    """

    holders: Dict[str, Set[str]] = {}
    for lot in lots:
        holders.setdefault(str(lot["symbol"]), set()).add(str(lot["strategy_name"]))

    conflicts: Dict[str, List[str]] = {
        symbol: sorted(names) for symbol, names in holders.items() if len(names) > 1
    }
    if conflicts:
        return ResyncPlan(conflicts=conflicts)

    # 排序不依賴呼叫端：FIFO 的正確性建立在「開倉日 → lot_id」的順序上
    ordered: List[Dict[str, object]] = sorted(
        lots, key=lambda lot: (str(lot["open_date"]), str(lot["lot_id"]))
    )
    local_lots: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
    for lot in ordered:
        key: Tuple[str, str] = (str(lot["symbol"]), str(lot["direction"]))
        local_lots.setdefault(key, []).append(lot)

    broker: Dict[Tuple[str, str], BrokerPositionSnapshot] = {
        (position.symbol, position.direction.value): position
        for position in positions
        if position.volume > 0
    }

    actions: List[ResyncAction] = []
    for key in sorted(set(broker) | set(local_lots)):
        symbol, direction = key
        key_lots: List[Dict[str, object]] = local_lots.get(key, [])
        local_volume: int = sum(int(lot["volume"]) for lot in key_lots)
        position: Optional[BrokerPositionSnapshot] = broker.get(key)
        broker_volume: int = position.volume if position is not None else 0

        if broker_volume > local_volume:
            actions.append(
                ResyncAction(
                    kind=RESYNC_ADOPT,
                    strategy_name=UNATTRIBUTED_STRATEGY,
                    symbol=symbol,
                    direction=direction,
                    volume=broker_volume - local_volume,
                    open_price=position.avg_price,
                )
            )
        elif broker_volume < local_volume:
            actions.extend(
                _reduce_fifo(key_lots, local_volume - broker_volume, symbol, direction)
            )

    return ResyncPlan(actions=actions)


def _reduce_fifo(
    lots: List[Dict[str, object]], excess: int, symbol: str, direction: str
) -> List[ResyncAction]:
    """從最早的 lot 開始扣掉 `excess`"""

    actions: List[ResyncAction] = []
    remaining: int = excess
    for lot in lots:
        if remaining <= 0:
            break

        available: int = int(lot["volume"])
        taken: int = min(available, remaining)
        actions.append(
            ResyncAction(
                kind=RESYNC_CLOSE if taken >= available else RESYNC_REDUCE,
                strategy_name=str(lot["strategy_name"]),
                symbol=symbol,
                direction=direction,
                volume=taken,
                lot_id=str(lot["lot_id"]),
            )
        )
        remaining -= taken
    return actions


def apply_resync(
    ledger: PositionAttributionLedger,
    plan: ResyncPlan,
    run_id: str,
    now_provider: Callable[[], datetime.datetime],
) -> None:
    """
    - Description:
        把重建計畫寫進歸屬帳，每筆異動一筆 `RESYNC_FROM_BROKER` 風控事件

        **全部在同一個 savepoint 內**：寫到一半失敗的話，歸屬帳會停在
        「扣了一半、還沒收進未歸屬」的狀態，而那個狀態既不是本地原本的樣子、
        也不是券商的樣子，之後連要怎麼重建都判斷不出來。

        扣減與平倉的事件是 CRITICAL：部位是在系統外消失的，那筆損益本地算不出來，
        要有人以券商對帳單補登。
    - Parameters:
        - ledger: PositionAttributionLedger
            部位歸屬帳
        - plan: ResyncPlan
            `plan_resync()` 產出的計畫
        - run_id: str
            本次執行的識別碼
        - now_provider: Callable[[], datetime.datetime]
            取得目前時間
    - Raise:
        - ResyncRefusedError
            計畫本身是被拒絕的（有衝突）
    """

    if plan.is_refused:
        raise ResyncRefusedError("；".join(plan.describe()))

    with ledger.dao.savepoint(_SAVEPOINT):
        for action in plan.actions:
            _apply_action(ledger, action, run_id, now_provider)


def _apply_action(
    ledger: PositionAttributionLedger,
    action: ResyncAction,
    run_id: str,
    now_provider: Callable[[], datetime.datetime],
) -> None:
    """寫入一筆異動與它的風控事件；不 commit"""

    moment: datetime.datetime = now_provider()
    lot_id: Optional[str] = action.lot_id

    if action.kind == RESYNC_ADOPT:
        lot_id = ledger.add_unattributed_lot(
            action.symbol, action.direction, action.volume, action.open_price or 0.0
        )
        severity: NotifyLevel = NotifyLevel.WARN
        message: str = action.describe()
    else:
        if action.kind == RESYNC_CLOSE:
            ledger.dao.close_lot(str(lot_id), moment)
        else:
            ledger.dao.reduce_lot(str(lot_id), action.volume)
        severity = NotifyLevel.CRITICAL
        message = f"{action.describe()}；這段損益本地算不出來，請以券商對帳單補登"

    detail: Dict[str, object] = dataclasses.asdict(action)
    detail["lot_id"] = lot_id
    RiskEventLogger(ledger.dao, run_id, now_provider=lambda: moment).write(
        category=RESYNC_EVENT_CATEGORY,
        severity=severity,
        message=message,
        strategy_name=action.strategy_name,
        symbol=action.symbol,
        detail=detail,
        # **交易區塊內不可自己 commit**：重建要嘛整批寫進去、要嘛整批不寫
        commit=False,
    )
