from typing import Any, Set, Tuple, Union

from core.models import ExecutionReport, OrderStatusEvent

"""
回報去重：**去重規則只寫這一份**

券商閘道與 OMS 都要用它。兩處各寫一份的話，其中一處漏掉某個欄位就會讓重複的成交
被記兩次——那等於帳上多了一筆不存在的部位，而它要到盤後對帳才會被發現。

放在 `core/broker/` 而不是某家券商的子目錄：去重是回報這種資料**本身**的性質
（不保證順序、不保證不重複），不是 Shioaji 的特性。
"""


class ExecutionEventDeduplicator:
    """
    - Description:
        回報去重：同一筆事件重複推送時只留第一次

        **去重規則只寫在這裡一份**，OMS 直接用它。兩處各寫一份的話，
        其中一處漏掉某個欄位就會讓重複的成交被記兩次——而那等於帳上多了一筆
        不存在的部位，直到盤後對帳才發現。

        去重鍵：
        - 成交：`(委託序號, 成交序號)`。
        - 委託事件：`(委託序號, 操作別, 交易所時戳)`。同一張單的同一種操作可能被重推，
          但不同時點的兩次改價是兩個事件。
    """

    def __init__(self) -> None:
        self._seen: Set[Tuple[Any, ...]] = set()

    def is_new(self, event: Union[ExecutionReport, OrderStatusEvent]) -> bool:
        """
        - Description:
            這筆事件是不是第一次看到；是的話就地記錄
        - Parameters:
            - event: Union[ExecutionReport, OrderStatusEvent]
                回報事件
        - Return:
            - bool
                True 代表第一次看到，應該處理
        """

        key: Tuple[Any, ...] = (type(event).__name__,) + tuple(event.dedup_key)
        if key in self._seen:
            return False

        self._seen.add(key)
        return True

    def reset(self) -> None:
        """清空；換日時使用"""

        self._seen.clear()
