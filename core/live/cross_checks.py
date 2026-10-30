from typing import Callable, Dict, List, Sequence, Tuple

from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import ACCOUNT_STRATEGY
from core.live.context import StrategyContext
from core.live.risk.risk_manager import ExposureItem, PreTradeRiskManager
from core.models import BaseOrder

"""
CrossStrategyChecks：跨策略守門與兩層批次曝險截斷

守門在最前面：被擋的單不該佔用曝險額度。策略層各自都過、加總仍可能超過
帳戶能承受的量——那正是多策略的典型破口，所以帳戶層再合計一次。
"""


class CrossStrategyChecks:
    """跨策略同標的守門 → 策略層批次曝險 → 帳戶層批次曝險"""

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        conflict_guard: CrossStrategyConflictGuard,
        risk_manager: PreTradeRiskManager,
        account_equity: Callable[[], float],
    ) -> None:
        """
        - Description:
            建立跨策略檢查；相依一律由外部注入
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - conflict_guard: CrossStrategyConflictGuard
                跨策略同標的守門
            - risk_manager: PreTradeRiskManager
                事前風控（批次曝險試算）
            - account_equity: Callable[[], float]
                帳戶層曝險的分母：與額度檢查、單日虧損共用同一個口徑，由帳務元件提供
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.conflict_guard: CrossStrategyConflictGuard = conflict_guard
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.account_equity: Callable[[], float] = account_equity

    def apply(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        - Description:
            跨策略守門 → 策略層批次曝險 → 帳戶層批次曝險

            守門在最前面：被擋的單不該佔用曝險額度。
        - Parameters:
            - candidates: List[Tuple[StrategyContext, BaseOrder]]
                各策略的委託（已各自前處理）
        - Return:
            - List[Tuple[StrategyContext, BaseOrder]]
                通過的委託
        """

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        guarded = self.conflict_guard.filter(
            [(context.name, order) for context, order in candidates]
        )
        survivors: List[Tuple[StrategyContext, BaseOrder]] = [
            (by_name[name], order) for name, order in guarded
        ]

        survivors = self._truncate_per_strategy(survivors)
        return self._truncate_account(survivors)

    def _truncate_per_strategy(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """逐策略試算曝險；超額的截斷後面的單"""

        kept: List[Tuple[StrategyContext, BaseOrder]] = []
        for context in self.contexts:
            items: List[ExposureItem] = [
                ExposureItem(order, context.exposure(order))
                for owner, order in candidates
                if owner is context
            ]
            if not items:
                continue

            allowed: List[ExposureItem] = self.risk_manager.check_batch(
                items, context.name, context.capital_limit, context.risk_limits
            )
            kept.extend((context, item.order) for item in allowed)
        return kept

    def _truncate_account(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        帳戶層合計試算

        策略層各自都過、加總仍可能超過帳戶能承受的量——那正是多策略的典型破口。

        **依市場分組、各用該市場的預設上限**：股票與期貨的曝險計量不同（持倉金額 vs
        契約價值），合在一起比會讓期貨的契約價值吃掉股票的額度。帳戶層不讀策略宣告：
        各策略的宣告只管自己，帳戶合計用哪一支的值都說不通。
        """

        groups: Dict[type, List[Tuple[StrategyContext, BaseOrder]]] = {}
        for context, order in candidates:
            groups.setdefault(type(context.risk_limits), []).append((context, order))

        # **與額度檢查、單日虧損共用同一個口徑**：這裡自己內嵌一份
        # 「可用餘額 ＋ 持倉占用」的公式的話，改了一邊另一邊不會跟著改
        equity: float = self.account_equity() if groups else 0.0
        approved: List[BaseOrder] = []
        for config_cls, members in groups.items():
            items: List[ExposureItem] = [
                ExposureItem(order, context.exposure(order))
                for context, order in members
            ]
            allowed: List[ExposureItem] = self.risk_manager.check_batch(
                items, ACCOUNT_STRATEGY, equity, config_cls()
            )
            approved.extend(item.order for item in allowed)
        return [(context, order) for context, order in candidates if order in approved]
