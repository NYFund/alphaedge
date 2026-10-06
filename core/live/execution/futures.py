from typing import FrozenSet, Optional, Tuple

from core.models import BaseOrder
from core.utils import ExecutionTiming, FuturesPriceType, OrderType

from .base import BaseExecutionModel

"""台期貨執行層：兩個段落都在連續交易時段，`MARKET` 送範圍市價＋IOC"""


class FuturesExecutionModel(BaseExecutionModel):
    """
    台期貨的執行方式換算

    **兩個段落都不在集合競價**：開盤段 08:45 起送單時日盤已經開盤，
    尾盤段 13:30~13:44 也還在連續交易（期貨的段落時窗見 `core/live/factory.py`）。
    故 `MARKET` 一律走連續交易的換算；段落時窗日後若移進 08:30~08:45 的
    開盤前集合競價，要同步把該段落加進 `AUCTION_TIMINGS`，否則會送出集合競價不收的 IOC。

    連續交易的 `MARKET` 送**範圍市價（`MKP`）＋IOC**：`MKP` 本身就是期交所
    附保護範圍的市價單，不必像股票那樣自己算保護價。期交所不收「市價＋ROD」，
    故委託效期一律 IOC，沒成交的部分當場取消、不留在場上。
    """

    AUCTION_TIMINGS: FrozenSet[ExecutionTiming] = frozenset()

    @property
    def limit_price_type(self) -> FuturesPriceType:
        """台期貨的限價列舉值"""

        return FuturesPriceType.LMT

    def _apply_continuous_market(
        self, order: BaseOrder, limits: Tuple[Optional[float], Optional[float]]
    ) -> None:
        """範圍市價＋IOC；委託價保留決策價，送出時由轉換層換成 0"""

        order.price_type = FuturesPriceType.MKP
        order.order_type = OrderType.IOC
