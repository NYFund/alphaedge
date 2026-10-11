import datetime
from dataclasses import dataclass
from typing import Dict, List

from core.models import BaseOrder
from core.utils import ExecutionStyle, OrderType

"""
逐筆回測的在途委託：送出之後、成交或作廢之前的委託

委託送出後要等**同一檔的下一筆 tick** 才嘗試撮合，所以必須有地方暫放；
沒有在途委託的話，只能在下單當下那一筆就成交，那就是前視。
"""

# 在途委託的階段：開倉、停損、平倉（與 `submitted_orders` 的標記相同）
STAGE_OPEN: str = "open"
STAGE_STOP_LOSS: str = "stop_loss"
STAGE_CLOSE: str = "close"


@dataclass
class PendingOrder:
    """
    - Description:
        一張在途委託

        - `time_in_force`：市價是 IOC（下一筆沒成交就作廢，與實盤「決策價加保護價的限價＋IOC」一致，不追價），
          限價是 ROD（留到收盤未成交作廢）。
        - `opening_auction`：開盤段送出的委託只在該檔當天第一筆（開盤集合競價）撮合；
          沒成交時市價作廢，限價轉成盤中的 ROD 繼續等。
    """

    order: BaseOrder
    stage: str
    style: ExecutionStyle
    time_in_force: OrderType
    submitted_at: datetime.datetime
    opening_auction: bool = False

    @property
    def is_exit(self) -> bool:
        """是否為出場（停損或平倉）"""

        return self.stage != STAGE_OPEN


class PendingOrderManager:
    """
    - Description:
        在途委託簿：依股票查詢、成交或作廢時移除、收盤清空

        同一檔的委託依送出順序保存，撮合時照這個順序，結果才可重現。
    """

    def __init__(self) -> None:
        self.orders: Dict[str, List[PendingOrder]] = {}

    def add(self, pending: PendingOrder) -> None:
        """加入一張在途委託"""

        self.orders.setdefault(pending.order.symbol, []).append(pending)

    def get_orders(self, symbol: str) -> List[PendingOrder]:
        """某檔的在途委託（送出順序）；回傳副本，呼叫端在迴圈中移除不受影響"""

        return list(self.orders.get(symbol, []))

    def remove(self, pending: PendingOrder) -> None:
        """成交或作廢後移除"""

        remaining: List[PendingOrder] = [
            p for p in self.orders.get(pending.order.symbol, []) if p is not pending
        ]
        if remaining:
            self.orders[pending.order.symbol] = remaining
        else:
            self.orders.pop(pending.order.symbol, None)

    def has_open(self, symbol: str) -> bool:
        """某檔是否有在途的開倉委託"""

        return any(not p.is_exit for p in self.orders.get(symbol, []))

    def has_exit(self, symbol: str) -> bool:
        """某檔是否有在途的出場委託"""

        return any(p.is_exit for p in self.orders.get(symbol, []))

    def open_symbols(self) -> List[str]:
        """有在途開倉委託的標的（持倉檔數上限要把它們算進去）"""

        return sorted(symbol for symbol in self.orders if self.has_open(symbol))

    def cancel_all(self) -> List[PendingOrder]:
        """收盤：清空並回傳所有未成交的委託（依代號、送出順序）"""

        cancelled: List[PendingOrder] = [
            p for symbol in sorted(self.orders) for p in self.orders[symbol]
        ]
        self.orders = {}
        return cancelled
