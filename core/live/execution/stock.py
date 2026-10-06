from typing import FrozenSet, Optional, Tuple

from core.models import BaseOrder
from core.utils import Action, ExecutionTiming, OrderType, StockPriceType
from core.utils.instrument import StockUtils

from .base import BaseExecutionModel, ExecutionUnavailableError

"""台股執行層：開盤段與尾盤段都在集合競價，盤中逐筆才是連續交易"""


class StockExecutionModel(BaseExecutionModel):
    """
    台股的執行方式換算

    連續交易時段的 `MARKET` 送**保護價限價＋IOC**，不送市價單：決策價 ± 幅度、
    對齊檔位、夾在漲跌停內。這是交易所價格穩定帶的常見定義方式；固定檔數在
    不同價位的實際寬度差很多，對手價＋N 檔又需要即時五檔報價。
    """

    # 開盤段 08:30~08:59 送單進開盤集合競價；尾盤段 13:25 之後送單進收盤集合競價
    AUCTION_TIMINGS: FrozenSet[ExecutionTiming] = frozenset(
        {ExecutionTiming.AT_OPEN, ExecutionTiming.AT_CLOSE}
    )

    PROTECTION_RATIO: float = 0.02  # 連續交易時段保護價幅度（決策價 ± 2%）

    @property
    def limit_price_type(self) -> StockPriceType:
        """台股的限價列舉值"""

        return StockPriceType.LMT

    def _apply_continuous_market(
        self, order: BaseOrder, limits: Tuple[Optional[float], Optional[float]]
    ) -> None:
        """
        保護價限價＋IOC

        **對齊檔位往不利於成交的方向**（買往下、賣往上），與送單前的轉換同一個方向：
        往有利於成交的方向取整，轉換那一層會再往回對齊一次，兩邊就對不上了。

        **取不到漲跌停也略過**：夾不進漲跌停的保護價會被交易所退單，
        與其送一張注定被退的單，不如在本地就記下原因。
        """

        limit_up, limit_down = limits
        if not limit_up or not limit_down:
            raise ExecutionUnavailableError(
                f"{order.symbol} 取不到漲跌停價，保護價無從夾限，本檔略過"
            )

        decision: float = float(order.decision_price or order.price)
        if order.action is Action.BUY:
            raw: float = decision * (1 + self.PROTECTION_RATIO)
            price: float = min(
                StockUtils.round_to_tick(raw, "down", order.symbol), limit_up
            )
        else:
            raw = decision * (1 - self.PROTECTION_RATIO)
            price = max(StockUtils.round_to_tick(raw, "up", order.symbol), limit_down)

        order.price = float(price)
        order.price_type = StockPriceType.LMT
        order.order_type = OrderType.IOC
