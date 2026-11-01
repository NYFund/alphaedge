from dataclasses import dataclass, field
from typing import List, Optional, Set

from core.market.cost_model import BaseCostModel
from core.models import BaseOrder
from core.portfolio import order_rules
from core.utils import PositionType

"""
委託前處理：方向驗證 → 市場欄位補值 → 決定性排序 → 持倉檔數上限，回測與實盤只有這一份

四步的純函式本來就在 `order_rules` 與成本模型裡，但串接順序以前寫了三份
（實盤的訊號元件、日 K simulator、逐筆 simulator），其中一份少補值、另一份
平倉不補值，分岔不會報錯，只會讓實盤與回測對同一張單做出不同的事。

- Features:
    1. `prepare_orders()`：固定順序跑完前四步；被剔除的單由回傳值帶出，
       回測拿去計數、實盤拿去寫風控事件
    2. `admit()`：單張開倉單的持倉檔數上限判定，給要逐單判的呼叫端（日 K 回測在
       每張單成交後才知道名額有沒有被佔走）
- 使用場景:
    `Backtester` 建一份給兩支 simulator；實盤的 `SignalCollector` 逐策略建一份。
    佔用集合由呼叫端傳入：日 K 回測傳部位、實盤與逐筆回測傳「部位 ∪ 在途委託」。
"""

STAGE_OPEN: str = "open"
STAGE_CLOSE: str = "close"


@dataclass
class PreprocessResult:
    """一次前處理的結果：通過的委託，以及各步剔除的委託"""

    orders: List[BaseOrder] = field(default_factory=list)
    rejected_direction: List[BaseOrder] = field(default_factory=list)
    rejected_max_holdings: List[BaseOrder] = field(default_factory=list)


class OrderPreprocessor:
    """委託前處理的唯一實作"""

    def __init__(
        self,
        allowed: Set[PositionType],
        cost_model: Optional[BaseCostModel],
        max_holdings: Optional[int],
    ) -> None:
        """
        - Description:
            建立前處理器
        - Parameters:
            - allowed: Set[PositionType]
                允許的方向白名單（由策略的 `direction` 推得）
            - cost_model: Optional[BaseCostModel]
                補市場欄位的成本模型；None 時不補值（純記憶體測試的脈絡）
            - max_holdings: Optional[int]
                持倉檔數上限；None 為不限制
        """

        self.allowed: Set[PositionType] = allowed
        self.cost_model: Optional[BaseCostModel] = cost_model
        self.max_holdings: Optional[int] = max_holdings

    def prepare_orders(
        self,
        orders: List[BaseOrder],
        stage: str,
        occupied: Optional[Set[str]] = None,
    ) -> PreprocessResult:
        """
        - Description:
            方向驗證 → 補值 → 排序 →（開倉階段且給了佔用集合時）持倉檔數上限

            **三個階段都補值**：平倉單的放空管道與當沖旗標由策略基底從部位帶入時，
            補值是 no-op；策略自己組的回補單漏填時，這裡補上才不會到券商轉換層被拒。

            **排序之後才套上限**：先排序才知道超額時該留下哪幾張。通過的開倉單會加進
            `occupied`（同一批後面的單要看得到它），呼叫端傳進來的集合會被改動。
            沒給佔用集合時不套上限，由呼叫端逐單以 `admit()` 判——日 K 回測每張單
            成交後名額才被佔走，一批之前就判會讓沒成交的單白佔名額。
        - Parameters:
            - orders: List[BaseOrder]
                策略鉤子回傳的委託
            - stage: str
                `"open"` 或 `"close"`
            - occupied: Optional[Set[str]]
                目前佔住名額的標的；None 時不套持倉檔數上限
        - Return:
            - PreprocessResult
                通過的委託與各步剔除的委託
        """

        valid: List[BaseOrder] = order_rules.validate_orders(
            orders, stage, self.allowed
        )
        passed: Set[int] = {id(order) for order in valid}
        result: PreprocessResult = PreprocessResult(
            rejected_direction=[order for order in orders if id(order) not in passed]
        )
        if self.cost_model is not None:
            valid = self.cost_model.enrich_orders(valid)
        ordered: List[BaseOrder] = order_rules.sort_orders(valid)

        if stage != STAGE_OPEN or occupied is None:
            result.orders = ordered
            return result

        for order in ordered:
            if self.admit(order, occupied):
                occupied.add(order.symbol)
                result.orders.append(order)
            else:
                result.rejected_max_holdings.append(order)
        return result

    def admit(self, order: BaseOrder, occupied: Set[str]) -> bool:
        """單張開倉單是否還有名額；已佔名額的標的（加碼）一律放行"""

        return order_rules.check_max_holdings(order, self.max_holdings, occupied)
