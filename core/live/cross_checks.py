from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.live.attribution.conflict_guard import CrossStrategyConflictGuard
from core.live.attribution.position_ledger import ACCOUNT_STRATEGY
from core.live.context import StrategyContext
from core.live.risk.risk_manager import ExposureItem, PreTradeRiskManager
from core.models import BaseOrder
from core.portfolio import order_rules

"""
CrossStrategyChecks：跨策略守門與兩層批次曝險截斷

守門在最前面：被擋的單不該佔用曝險額度。策略層各自都過、加總仍可能超過
帳戶能承受的量——那正是多策略的典型破口，所以帳戶層再合計一次。
"""


class CrossStrategyChecks:
    """跨策略同標的守門 → 策略層批次曝險 → 帳戶層批次曝險"""

    # 守門擋下開倉單後重新切分的輪數上限（保險絲；正常每輪至少少一檔）
    MAX_RESIZE_ROUNDS: int = 10

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        conflict_guard: CrossStrategyConflictGuard,
        risk_manager: PreTradeRiskManager,
        account_equity: Callable[[], float],
        regenerate_entries: Optional[
            Callable[[StrategyContext, Set[str]], List[BaseOrder]]
        ] = None,
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
            - regenerate_entries: Optional[Callable[[StrategyContext, Set[str]], List[BaseOrder]]]
                `(脈絡, 要剔除的標的) → 重新切分後的開倉單`；守門擋下開倉單時把名額與資金
                還給後面的候選。None 時被擋的單仍佔名額（只擋、不重切）
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.conflict_guard: CrossStrategyConflictGuard = conflict_guard
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.account_equity: Callable[[], float] = account_equity
        self.regenerate_entries: Optional[
            Callable[[StrategyContext, Set[str]], List[BaseOrder]]
        ] = regenerate_entries

    def apply(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """
        - Description:
            跨策略守門 → 策略層批次曝險 → 帳戶層批次曝險

            守門在最前面：被擋的單不該佔用曝險額度。

            **被擋的開倉單也不該佔名額與資金**：策略在產生委託時已依可開檔數均分餘額，
            被擋的那一檔佔走了一格與一份資金，後面的候選因此沒有名額、每檔的張數也
            少算。守門擋下開倉單後，請訊號元件把那幾檔剔除、以同一份報價重新切分，
            再送回守門（重切出來的單可能又撞到別人，故反覆到沒有新的被擋為止）；
            重切後若仍回傳被剔除的標的，直接丟掉、不再寫事件。
        - Parameters:
            - candidates: List[Tuple[StrategyContext, BaseOrder]]
                各策略的委託（已各自前處理）
        - Return:
            - List[Tuple[StrategyContext, BaseOrder]]
                通過的委託
        """

        survivors: List[Tuple[StrategyContext, BaseOrder]] = self._guard_and_resize(
            list(candidates)
        )

        survivors = self._truncate_per_strategy(survivors)
        return self._truncate_account(survivors)

    def _guard_and_resize(
        self, candidates: List[Tuple[StrategyContext, BaseOrder]]
    ) -> List[Tuple[StrategyContext, BaseOrder]]:
        """守門；擋下開倉單時把該策略的開倉單重新切分後再守門一次，直到沒有新的被擋"""

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        excluded: Dict[str, Set[str]] = {}
        # 上限只是保險絲：每一輪至少多剔除一檔，輪數不會超過候選的檔數
        for _ in range(self.MAX_RESIZE_ROUNDS + 1):
            guarded = self.conflict_guard.filter(
                [(context.name, order) for context, order in candidates]
            )
            allowed: List[Tuple[StrategyContext, BaseOrder]] = [
                (by_name[name], order) for name, order in guarded
            ]
            passed: Set[int] = {id(order) for _, order in allowed}
            blocked: Dict[str, Set[str]] = {}
            for context, order in candidates:
                if id(order) not in passed and not order_rules.is_closing(order):
                    blocked.setdefault(context.name, set()).add(order.symbol)
            if not blocked or self.regenerate_entries is None:
                return allowed

            for name, symbols in blocked.items():
                excluded.setdefault(name, set()).update(symbols)
                context = by_name[name]
                resized: List[BaseOrder] = [
                    order
                    for order in self.regenerate_entries(context, excluded[name])
                    if order.symbol not in excluded[name]
                ]
                logger.info(
                    f"[Guard] {name}：{'、'.join(sorted(symbols))} 被擋，"
                    f"剔除後重新切分為 {len(resized)} 張開倉單"
                )
                candidates = [
                    (owner, order)
                    for owner, order in candidates
                    if owner is not context or order_rules.is_closing(order)
                ] + [(context, order) for order in resized]
        return allowed

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
