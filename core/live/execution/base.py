from abc import ABC, abstractmethod
from typing import Any, Callable, FrozenSet, Optional, Tuple

from core.models import BaseOrder
from core.utils import Action, ExecutionStyle, ExecutionTiming, OrderType

"""
實盤執行層：把策略的執行方式換成券商接受的委託

策略只宣告「要成交」（`ExecutionStyle.MARKET`）或「照價掛單」（`LIMIT`），
本層依段落與商品決定價格類型、委託價與委託效期（ROD／IOC）：

| 執行方式 ＼ 時段 | 集合競價 | 連續交易 |
|------------------|----------|----------|
| `MARKET` | 買掛漲停、賣掛跌停，限價＋ROD | 由各市場決定（股票：保護價限價＋IOC；期貨：範圍市價＋IOC） |
| `LIMIT` | 策略給的價，限價＋ROD | 同左 |

集合競價掛漲跌停：集合競價以單一價格撮合，掛漲停只是確保排得進去，成交價仍是
競價結果。這是台股版的 market-on-close，與回測「以收盤價成交」的假設最一致。
不送真正的市價單，是因為集合競價時段不收市價單、期交所也不收「市價＋ROD」。

**與 `core/broker/` 的 execution 不同義**：`shioaji_execution_handler.py`、
`execution_dedup.py` 的 execution 指成交回報（FIX 的 ExecutionReport）；
本層的 execution 指「決定怎麼送單」（業界的 Execution Model）。

**改寫委託之前先記下決策價**（`BaseOrder.decision_price`）：送出的價可能是漲停價，
事後比對執行品質要的是策略原本要的價。
"""

# 取得某商品當日漲跌停的函式：`symbol → (漲停, 跌停)`，取不到的一側為 None
PriceLimitLookup = Callable[[str], Tuple[Optional[float], Optional[float]]]


class ExecutionUnavailableError(ValueError):
    """這張委託目前換不出合法的券商委託（例如取不到漲跌停）；呼叫端略過該檔"""


class BaseExecutionModel(ABC):
    """
    - Description:
        執行層骨架；各市場實作連續交易時段的 `MARKET` 換算與自己的價格類型列舉

        **漲跌停只取自券商合約**，取不到就拋 `ExecutionUnavailableError` 讓呼叫端
        略過該檔，不自行推算：除權息日的基準價是另行公告的，公式推出來的區間會整段偏移，
        而掛錯價的漲停單會被交易所退單。新上市前五日無漲跌幅限制的股票因此會被略過。
    """

    # 哪些段落落在集合競價時段；由各市場宣告
    AUCTION_TIMINGS: FrozenSet[ExecutionTiming] = frozenset()

    def __init__(self, price_limits: Optional[PriceLimitLookup] = None) -> None:
        """
        - Description:
            建立執行層
        - Parameters:
            - price_limits: Optional[PriceLimitLookup]
                取當日漲跌停的函式（由組裝層以資料源注入）；None 時一律視為取不到
        """

        self._price_limits: Optional[PriceLimitLookup] = price_limits

    @property
    @abstractmethod
    def limit_price_type(self) -> Any:
        """本市場的限價列舉值（`StockPriceType.LMT`／`FuturesPriceType.LMT`）"""

    def get_price_limits(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        - Description:
            取得當日漲跌停；取不到的一側為 None

            同一個值也要交給事前風控：風控檢查決策價是否超出漲跌停，
            本層用它決定集合競價掛的價。兩邊各查一次的話，中間合約檔若更新，
            會出現風控放行、掛單價卻超出漲跌停的組合。
        - Parameters:
            - symbol: str
                商品代號
        - Return:
            - Tuple[Optional[float], Optional[float]]
                （漲停, 跌停）
        """

        if self._price_limits is None:
            return (None, None)
        return self._price_limits(symbol)

    def apply(
        self,
        order: BaseOrder,
        limits: Tuple[Optional[float], Optional[float]] = (None, None),
    ) -> None:
        """
        - Description:
            依執行方式與段落填好委託的價格類型、委託價與委託效期（**就地改寫**）

            執行方式或段落未決定時拋錯，不猜：委託沒經過引擎標註代表某條送單路徑
            漏接了執行層，猜一個值等於把那個漏洞蓋掉。
        - Parameters:
            - order: BaseOrder
                待送出的委託；`execution_style` 與 `timing` 須已由引擎填入
            - limits: Tuple[Optional[float], Optional[float]]
                （漲停, 跌停），通常來自 `get_price_limits()`
        - Raise:
            - ValueError
                執行方式或段落未決定
            - ExecutionUnavailableError
                需要漲跌停卻取不到
        """

        style: Optional[ExecutionStyle] = order.execution_style
        timing: Optional[ExecutionTiming] = order.timing
        if style is None:
            raise ValueError(
                f"{order.symbol} 的委託沒有執行方式；送單路徑必須先標註"
                "（策略委託沿用 live_execution，停損與系統委託為 MARKET）"
            )
        if timing is None:
            raise ValueError(f"{order.symbol} 的委託沒有執行段落，無法決定怎麼送")

        if order.decision_price is None:
            order.decision_price = float(order.price)

        if style is ExecutionStyle.LIMIT:
            self._apply_resting_limit(order)
        elif timing in self.AUCTION_TIMINGS:
            self._apply_auction_market(order, limits)
        else:
            self._apply_continuous_market(order, limits)

    def _apply_resting_limit(self, order: BaseOrder) -> None:
        """照價掛單：策略給的價，限價＋ROD"""

        order.price_type = self.limit_price_type
        order.order_type = OrderType.ROD

    def _apply_auction_market(
        self, order: BaseOrder, limits: Tuple[Optional[float], Optional[float]]
    ) -> None:
        """
        集合競價的 `MARKET`：買掛漲停、賣掛跌停，限價＋ROD

        **不用決策價 ± 幅度**：收盤價高於決策價超過那個幅度時實盤不成交、
        回測卻會成交；動能股多半離漲停不遠，兩者的差別也不大。
        """

        limit_up, limit_down = limits
        price: Optional[float] = limit_up if order.action is Action.BUY else limit_down
        if not price:
            raise ExecutionUnavailableError(
                f"{order.symbol} 取不到{'漲停' if order.action is Action.BUY else '跌停'}"
                "價，集合競價的 MARKET 委託無從定價，本檔略過"
            )

        order.price = float(price)
        order.price_type = self.limit_price_type
        order.order_type = OrderType.ROD

    @abstractmethod
    def _apply_continuous_market(
        self, order: BaseOrder, limits: Tuple[Optional[float], Optional[float]]
    ) -> None:
        """連續交易時段的 `MARKET`；各市場的保護方式不同"""
